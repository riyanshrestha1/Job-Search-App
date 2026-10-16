from __future__ import annotations
import io, re, time, zipfile
from pathlib import Path
import pandas as pd
import requests

from .names import normalize

DATA_HUB_PAGES = [
    "https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub/h-1b-employer-data-hub-files",
    "https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub",
]
# Direct-URL patterns tried if page scraping finds nothing (override in the UI if USCIS changes them).
GUESS_PATTERNS = ["https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{fy}.csv"]

UA = {"User-Agent": "Mozilla/5.0 (h1b-job-finder; personal use)"}

# canonical column -> accepted header spellings (compared after lowercasing + stripping non-alphanumerics)
ALIASES = {
    "fy": ["fiscalyear", "fy", "year"],
    "name": ["employerpetitionername", "employer", "employername", "petitionername", "petitioner", "employerpetitioner"],
    "tax_id": ["taxid"],
    "naics": ["industrynaicscode", "naicscode", "naics", "industry"],
    "city": ["petitionercity", "city", "employercity"],
    "state": ["petitionerstate", "state", "employerstate"],
    "zip": ["petitionerzipcode", "zipcode", "zip"],
    "new_app": ["newemploymentapproval", "initialapproval", "initialapprovals"],
    "new_den": ["newemploymentdenial", "initialdenial", "initialdenials"],
    "cont_app": ["continuationapproval", "continuingapproval", "continuingapprovals"],
    "cont_den": ["continuationdenial", "continuingdenial", "continuingdenials"],
}
OTHER_APP = ["changewithsameemployerapproval", "newconcurrentapproval", "changeofemployerapproval", "amendedapproval"]
OTHER_DEN = ["changewithsameemployerdenial", "newconcurrentdenial", "changeofemployerdenial", "amendeddenial"]
INT_COLS = ["new_app", "new_den", "cont_app", "cont_den", "other_app", "other_den"]


def _key(c) -> str:
    return re.sub(r"[^a-z0-9]", "", str(c).lower())


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce").fillna(0).astype(int)


def standardise(df: pd.DataFrame, fallback_fy: int | None = None) -> pd.DataFrame:
    """Map an arbitrary USCIS-style frame onto the canonical columns. Raises ValueError if no employer column."""
    keys = {_key(c): c for c in df.columns}
    # some exports have a few junk rows above the header
    if not any(a in keys for a in ALIASES["name"]):
        for i in range(min(15, len(df))):
            row_keys = [_key(v) for v in df.iloc[i].tolist()]
            if any(a in row_keys for a in ALIASES["name"]):
                df = df.iloc[i + 1:].copy()
                df.columns = row_keys
                keys = {c: c for c in row_keys}
                break
    out = pd.DataFrame(index=df.index)
    for canon, names in ALIASES.items():
        col = next((keys[n] for n in names if n in keys), None)
        out[canon] = df[col] if col is not None else None
    if out["name"].isna().all():
        raise ValueError("No employer-name column found. Columns seen: " + ", ".join(map(str, df.columns))[:300])
    for target, group in (("other_app", OTHER_APP), ("other_den", OTHER_DEN)):
        cols = [keys[g] for g in group if g in keys]
        out[target] = sum((_num(df[c]) for c in cols), pd.Series(0, index=df.index)) if cols else 0
    for c in ["new_app", "new_den", "cont_app", "cont_den"]:
        out[c] = _num(out[c]) if out[c].notna().any() else 0
    out["fy"] = pd.to_numeric(out["fy"].astype(str).str.replace(",", "").str.extract(r"(\d{4})")[0], errors="coerce")
    if fallback_fy:
        out["fy"] = out["fy"].fillna(fallback_fy)
    out = out[out["name"].notna() & (out["name"].astype(str).str.strip() != "")].copy()
    out["norm"] = out["name"].map(normalize)
    out = out[out["norm"] != ""]
    for c in ["tax_id", "naics", "city", "state", "zip"]:
        out[c] = out[c].where(out[c].notna(), None)
    out["name"] = out["name"].astype(str).str.strip()
    out["fy"] = out["fy"].fillna(0).astype(int)
    return out.reset_index(drop=True)


def _read_any(data: bytes, filename: str) -> pd.DataFrame:
    fn = filename.lower()
    if fn.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(n for n in z.namelist() if n.lower().endswith((".csv", ".xlsx", ".xls")))
            return _read_any(z.read(member), member)
    if fn.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data), dtype=str)
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            sep = "\t" if enc == "utf-16" else None
            return pd.read_csv(io.BytesIO(data), dtype=str, encoding=enc, sep=sep, engine="python")
        except Exception:
            continue
    raise ValueError(f"Could not parse {filename}")


def ingest_bytes(conn, data: bytes, filename: str, source: str | None = None, url: str | None = None) -> dict:
    m = re.search(r"(20\d{2})", filename)
    df = standardise(_read_any(data, filename), fallback_fy=int(m.group(1)) if m else None)
    source = source or f"file:{Path(filename).name}"
    conn.execute("DELETE FROM filings WHERE source=?", (source,))
    cols = ["fy", "norm", "name", "tax_id", "naics", "city", "state", "zip"] + INT_COLS
    rows = [tuple(r) + (source,) for r in df[cols].itertuples(index=False, name=None)]
    conn.executemany(f"INSERT INTO filings({','.join(cols)},source) VALUES({','.join('?' * (len(cols) + 1))})", rows)
    fys = sorted({int(x) for x in df["fy"].unique() if x})
    conn.execute("INSERT OR REPLACE INTO datasets(source,label,url,rows,fys,added_at) VALUES(?,?,?,?,?,?)",
                 (source, filename, url, len(df), ",".join(map(str, fys)), time.time()))
    conn.commit()
    return {"source": source, "rows": len(df), "fiscal_years": fys, "employers": int(df["norm"].nunique())}


def ingest_path(conn, path: str | Path, source: str | None = None) -> dict:
    p = Path(path)
    return ingest_bytes(conn, p.read_bytes(), p.name, source=source or f"file:{p.name}")


def ingest_url(conn, url: str) -> dict:
    r = requests.get(url, headers=UA, timeout=120)
    r.raise_for_status()
    fname = url.split("?")[0].rsplit("/", 1)[-1] or "dataset.csv"
    return ingest_bytes(conn, r.content, fname, source=f"url:{fname}", url=url)


def discover_links() -> list[dict]:
    """Scrape the USCIS Data Hub pages for downloadable data files; fall back to guessed URLs."""
    from bs4 import BeautifulSoup
    found: dict[str, dict] = {}
    for page in DATA_HUB_PAGES:
        try:
            r = requests.get(page, headers=UA, timeout=30)
            r.raise_for_status()
        except Exception:
            continue
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.(csv|xlsx|xls|zip)(\?|$)", href, re.I):
                url = requests.compat.urljoin(page, href)
                m = re.search(r"(20\d{2})", a.get_text(" ") + " " + href)
                found[url] = {"url": url, "label": a.get_text(" ", strip=True) or url.rsplit("/", 1)[-1],
                              "fy": int(m.group(1)) if m else None}
    if not found:
        for fy in range(2026, 2018, -1):
            for pat in GUESS_PATTERNS:
                url = pat.format(fy=fy)
                try:
                    if requests.head(url, headers=UA, timeout=15, allow_redirects=True).status_code == 200:
                        found[url] = {"url": url, "label": f"H-1B Employer Data Hub FY{fy}", "fy": fy}
                except Exception:
                    pass
    return sorted(found.values(), key=lambda d: -(d["fy"] or 0))


def list_datasets(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM datasets ORDER BY added_at DESC")]


def remove_dataset(conn, source: str):
    conn.execute("DELETE FROM filings WHERE source=?", (source,))
    conn.execute("DELETE FROM datasets WHERE source=?", (source,))
    conn.commit()