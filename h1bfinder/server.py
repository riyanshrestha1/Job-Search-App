from __future__ import annotations
import argparse, threading
from pathlib import Path
from flask import Flask, jsonify, render_template, request

from . import db, uscis, resume as rs, matcher
from .ats import Scanner
from .employers import EmployerIndex
from .names import normalize

app = Flask(__name__)
conn = db.connect()
index = EmployerIndex(conn)
scanner = Scanner(conn)
_lock = threading.Lock()


class _Conn:
    """Proxy so every Flask worker thread uses its own SQLite connection."""
    def __getattr__(self, k):
        return getattr(db.thread_conn(_main), k)


_main = conn
conn = _Conn()


def _resume():
    text = db.get_meta(conn, "resume_text", "") or ""
    return text, (rs.build_profile(text) if text else {"skills": [], "years": 0, "chars": 0})


def _clean(df, cols):
    d = df[cols].copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].round(3)
    return d.where(d.notna(), None).to_dict("records")


@app.errorhandler(Exception)
def on_error(e):
    import traceback
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    traceback.print_exc()
    return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/status")
def status():
    q = lambda s: conn.execute(s).fetchone()[0]
    text, prof = _resume()
    return jsonify(employers=len(index.df), filings=q("SELECT COUNT(*) FROM filings"),
                   boards=q("SELECT COUNT(*) FROM boards WHERE found=1"), jobs=q("SELECT COUNT(*) FROM jobs"),
                   resume=prof if text else None, scan=scanner.state, facets=index.facets(),
                   latest_fy=getattr(index, "latest_fy", None))


@app.post("/api/resume")
def upload_resume():
    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        text = rs.extract_text(f.read(), f.filename)
    else:
        text = (request.get_json(silent=True) or {}).get("text", "")
    if len(text.strip()) < 50:
        return jsonify(error="Could not read any text from the resume (scanned image PDF?). Paste the text instead."), 400
    db.set_meta(conn, "resume_text", text)
    return jsonify(rs.build_profile(text))


@app.get("/api/employers")
def employers():
    a = request.args
    with_jobs = {r[0] for r in conn.execute("SELECT DISTINCT norm FROM jobs")}
    d = index.search(a.get("q", ""), a.get("state", ""), a.get("industry", ""), int(a.get("min_approvals", 0) or 0),
                     int(a.get("limit", 100)), a.get("has_jobs") == "1", with_jobs)
    d = d.assign(has_jobs=d.norm.isin(with_jobs))
    return jsonify(_clean(d, ["norm", "name", "city", "state", "naics", "years", "app_total", "approval_rate",
                              "new_app_latest", "sponsor_score", "has_jobs"]))


@app.get("/api/employer/<norm>")
def employer(norm):
    jobs = [dict(r) for r in conn.execute(
        "SELECT title,location,url,department,posted,provider FROM jobs WHERE norm=? ORDER BY title", (norm,))]
    return jsonify(history=index.history(norm), jobs=jobs)


@app.post("/api/scan")
def scan():
    b = request.get_json(silent=True) or {}
    d = index.search(b.get("q", ""), b.get("state", ""), b.get("industry", ""), int(b.get("min_approvals", 1)),
                     int(b.get("top_n", 1000)))
    ok = scanner.start(list(zip(d.name, d.norm)), refresh=bool(b.get("refresh")), workers=int(b.get("workers", 12)))
    return jsonify(started=ok, total=len(d))


@app.post("/api/scan/cancel")
def scan_cancel():
    scanner.cancel()
    return jsonify(ok=True)


@app.get("/api/recommendations")
def recommendations():
    a = request.args
    text, prof = _resume()
    if not text:
        return jsonify(error="Upload your resume first."), 400
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    jobs = [dict(r) for r in rows]
    if a.get("state"):
        st = a["state"].upper()
        jobs = [j for j in jobs if f", {st}" in j["location"] or "remote" in j["location"].lower()]
    sponsor = dict(zip(index.df.norm, index.df.sponsor_score)) if len(index.df) else {}
    names = dict(zip(index.df.norm, index.df.name)) if len(index.df) else {}
    kws = [k for k in a.get("keywords", "").split(",") if k.strip()]
    res = matcher.rank(jobs, text, prof["skills"], prof["years"], sponsor, kws,
                       us_only=a.get("us_only", "1") == "1", hide_no_sponsor=a.get("hide_no_sponsor", "1") == "1",
                       limit=int(a.get("limit", 100)))
    em = index.df.set_index("norm")
    for r in res:
        r["employer"] = names.get(r["norm"], r["norm"])
        if r["norm"] in em.index:
            e = em.loc[r["norm"]]
            r["h1b_approvals"] = int(e.app_total)
            r["h1b_new_latest"] = int(e.new_app_latest)
            r["h1b_years"] = int(e.years)
    return jsonify(results=res, profile=prof, pool=len(jobs))


# ---- extra USCIS datasets ----------------------------------------------------------------------------------------
@app.get("/api/datasets")
def datasets():
    return jsonify(datasets=uscis.list_datasets(conn))


@app.post("/api/datasets/discover")
def ds_discover():
    try:
        return jsonify(links=uscis.discover_links())
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.post("/api/datasets/ingest")
def ds_ingest():
    url = (request.get_json(silent=True) or {}).get("url", "")
    try:
        with _lock:
            r = uscis.ingest_url(conn, url)
            index.refresh()
        return jsonify(r)
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.post("/api/datasets/upload")
def ds_upload():
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file"), 400
    try:
        with _lock:
            r = uscis.ingest_bytes(conn, f.read(), f.filename)
            index.refresh()
        return jsonify(r)
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.post("/api/datasets/remove")
def ds_remove():
    uscis.remove_dataset(conn, (request.get_json(silent=True) or {}).get("source", ""))
    index.refresh()
    return jsonify(ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import-file", help="USCIS xlsx/csv to import before starting")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    default = db.DATA_DIR / "Employer_Information.xlsx"
    path = Path(args.import_file) if args.import_file else (default if default.exists() else None)
    if path and not conn.execute("SELECT 1 FROM datasets WHERE source=?", (f"primary:{path.name}",)).fetchone():
        print(f"Importing {path} ...")
        uscis.ingest_path(conn, path, source=f"primary:{path.name}")
        index.refresh()
    print(f"Loaded {len(index.df):,} employers. Open http://localhost:{args.port}")
    app.run(port=args.port, threaded=True)


if __name__ == "__main__":
    main()
