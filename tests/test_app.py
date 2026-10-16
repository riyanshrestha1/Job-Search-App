import io, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from unittest import mock
import pandas as pd
from h1bfinder import db, uscis, ats, matcher, resume
from h1bfinder.employers import EmployerIndex
from h1bfinder.names import normalize


class R:
    def __init__(self, code=200, js=None, text=""):
        self.status_code, self._js, self.text = code, js, text
    def json(self): return self._js
    def raise_for_status(self): pass


def fake_get(self, url, **kw):
    if url == "https://boards-api.greenhouse.io/v1/boards/stripe": return R(js={"name": "Stripe"})
    if url.startswith("https://boards-api.greenhouse.io/v1/boards/stripe/jobs"):
        return R(js={"jobs": [{"id": 1, "title": "Data Engineer", "location": {"name": "San Francisco, CA"},
            "absolute_url": "http://x/1", "updated_at": "2026-10-01",
            "content": "&lt;p&gt;Python SQL Spark AWS pipelines. We do not provide visa sponsorship.&lt;/p&gt;", "departments": []},
            {"id": 2, "title": "Machine Learning Engineer", "location": {"name": "New York, NY"}, "absolute_url": "http://x/2",
            "updated_at": "2026-10-02", "content": "Python machine learning pytorch AWS. H-1B transfer supported.", "departments": []},
            {"id": 3, "title": "Account Executive", "location": {"name": "London, UK"}, "absolute_url": "http://x/3",
            "updated_at": "", "content": "Sell things. Salesforce.", "departments": []}]})
    if url.startswith("https://api.lever.co/v0/postings/acme"): return R(js=[{"id": "a", "text": "Backend Engineer", "categories": {"location": "Austin, TX", "team": "Eng"},
            "hostedUrl": "http://l/a", "descriptionPlain": "Python Docker Kubernetes SQL", "createdAt": 1760000000000}])
    return R(404, {})


def check(c, msg):
    assert c, msg
    print("ok -", msg)


def main():
    tmp = Path(tempfile.mkdtemp())
    conn = db.connect(tmp / "t.db")
    real = Path(__file__).resolve().parent.parent / "data" / "Employer_Information.xlsx"

    # normalisation
    check(normalize("1HCS FRANCHISING LLC D B A 1HEART CAREGIVER SERVICES") == "1HCS FRANCHISING", "dba stripped")
    check(normalize("Stripe, Inc.") == "STRIPE", "suffix stripped")

    # ingest primary file + an 'older format' file for another FY
    r = uscis.ingest_path(conn, real, "primary:Employer_Information.xlsx")
    check(r["fiscal_years"] == [2026] and r["rows"] > 49000, f"primary ingest {r['rows']} rows")
    old = pd.DataFrame({"Fiscal Year": ["2024", "2024"], "Employer": ["STRIPE, INC.", "ACME CORP"], "Initial Approvals": ["10", "1"],
                        "Initial Denials": ["1", "0"], "Continuing Approvals": ["1,200", "3"], "Continuing Denials": ["0", "0"],
                        "NAICS": ["54", "54"], "Tax ID": ["1", "2"], "State": ["CA", "TX"], "City": ["SF", "AUSTIN"], "ZIP": ["94", "73"]})
    buf = io.BytesIO(); old.to_csv(buf, index=False)
    r2 = uscis.ingest_bytes(conn, buf.getvalue(), "h1b_datahubexport-2024.csv")
    check(r2["fiscal_years"] == [2024], "old-format file ingested with FY2024")
    ix = EmployerIndex(conn)
    h = ix.history("STRIPE")
    check({x["fy"] for x in h} == {2024, 2026}, "multi-year history merged by employer name")
    check(any(x["cont_app"] == 1200 for x in h), "comma-formatted numbers parsed")

    # board resolution (mocked HTTP)
    with mock.patch("requests.Session.get", fake_get):
        sc = ats.Scanner(conn)
        sc.start([("STRIPE LLC", "STRIPE"), ("ACME CORP", "ACME"), ("NOBODY INC", "NOBODY")], workers=3)
        for _ in range(100):
            if not sc.state["running"]: break
            time.sleep(0.1)
    check(sc.state["boards"] == 2 and sc.state["jobs"] == 4, f"scan found 2 boards / 4 jobs: {sc.state}")
    check(conn.execute("SELECT found FROM boards WHERE norm='NOBODY'").fetchone()[0] == 0, "miss cached")
    check(conn.execute("SELECT is_us FROM jobs WHERE title='Account Executive'").fetchone()[0] == 0, "London job flagged non-US")

    # recommendations
    text = "Data engineer with 6 years experience. Python, SQL, Spark, AWS, machine learning, pytorch, Kubernetes."
    prof = resume.build_profile(text)
    jobs = [dict(x) for x in conn.execute("SELECT * FROM jobs")]
    sponsor = dict(zip(ix.df.norm, ix.df.sponsor_score))
    res = matcher.rank(jobs, text, prof["skills"], prof["years"], sponsor)
    titles = [x["title"] for x in res]
    check("Data Engineer" not in titles, "'no sponsorship' job hidden by default")
    check(titles[0] == "Machine Learning Engineer" and "Account Executive" not in titles, f"ranking sane: {titles}")
    res2 = matcher.rank(jobs, text, prof["skills"], prof["years"], sponsor, hide_no_sponsor=False)
    check(any("no sponsorship" in f for x in res2 for f in x["flags"]), "no-sponsor flag shown when not hidden")

    # Flask smoke test
    import h1bfinder.server as srv
    srv._main, srv.index, srv.scanner = conn, ix, sc
    c = srv.app.test_client()
    check(c.get("/").status_code == 200, "index page")
    check(c.get("/api/status").get_json()["employers"] > 40000, "status")
    check(c.post("/api/resume", json={"text": text}).status_code == 200, "resume upload")
    j = c.get("/api/recommendations").get_json()
    check(j["results"] and j["results"][0]["employer"], "recommendations endpoint")
    check(len(c.get("/api/employers?q=stripe").get_json()) >= 1, "employer search")
    up = c.post("/api/datasets/upload", data={"file": (io.BytesIO(buf.getvalue()), "x_2023.csv")}, content_type="multipart/form-data")
    check(up.status_code == 200 and up.get_json()["fiscal_years"] == [2024], "dataset upload endpoint")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
