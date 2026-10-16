from __future__ import annotations
import hashlib, html, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
 
import requests
 
from . import db
from .names import normalize, strip_generic_tail
 
HEADERS = {"User-Agent": "h1b-job-finder/1.0 (personal job search)"}
NEG_TTL = 14 * 86400      # retry failed slug lookups after 2 weeks
JOB_TTL = 12 * 3600       # refresh openings after 12h
GENERIC_FIRST = {"GLOBAL", "AMERICAN", "NATIONAL", "FIRST", "UNITED", "ADVANCED", "DIGITAL", "SMART", "INFO",
                 "CLOUD", "DATA", "TECH", "NEW", "BLUE", "GREEN", "RED", "UNIVERSAL", "PRIME", "ALPHA"}
 
US_STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA",
             "MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
             "TX","UT","VT","VA","WA","WV","WI","WY","DC"}
NON_US = re.compile(r"\b(canada|india|united kingdom|uk|london|germany|berlin|france|paris|ireland|dublin|spain|"
                    r"netherlands|amsterdam|singapore|australia|sydney|japan|tokyo|brazil|mexico|israel|poland|"
                    r"toronto|vancouver|bangalore|bengaluru|hyderabad|emea|apac|latam)\b", re.I)
 
 
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}


def is_us_location(loc: str, country: str | None = None) -> bool:
    # Structured country data takes precedence over ambiguous free text.
    if country and country.strip():
        return country.strip().lower() in {"us", "usa", "united states", "united states of america"}
    if not loc:
        return True
    if re.search(r"united states|\busa\b|\bu\.s\.(?:a\.)?|(?:^|[,;\s])US(?:$|[,;\s])", loc, re.I):
        return True
    if NON_US.search(loc):
        return False
    parts = [p.strip().lower() for p in re.split(r"[,;|]", loc)]
    if any(p in US_STATE_NAMES for p in parts):
        return True
    if re.search(r",\s*(%s)\b" % "|".join(US_STATES), loc):
        return True
    return bool(re.search(r"remote|new york|san francisco|seattle|austin|boston|chicago", loc, re.I))


class BoardRequestError(RuntimeError):
    """An incomplete lookup must not overwrite cached jobs."""


def _get_json(sess, url, *, allow_missing=False, timeout=20):
    try:
        response = sess.get(url, timeout=timeout)
        if allow_missing and response.status_code in {404, 410}:
            return None
        if response.status_code != 200:
            raise BoardRequestError(f"HTTP {response.status_code}: {url}")
        return response.json()
    except BoardRequestError:
        raise
    except Exception as exc:
        raise BoardRequestError(f"Could not fetch {url}: {exc}") from exc

def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = re.sub(r"<(script|style).*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()
 
 
def similar(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return False
    if a == b or a.startswith(b + " ") or b.startswith(a + " "):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.85
 
 
def slug_candidates(norm: str) -> list[tuple[str, int]]:
    """[(slug, tier)] - tier 1 is most specific. Lever/Ashby (no name check) only accept tier<=2."""
    toks = norm.lower().split()
    if not toks:
        return []
    out: list[tuple[str, int]] = []
 
    def add(s, t):
        s = re.sub(r"[^a-z0-9-]", "", s)
        if len(s) >= 3 and s not in {c for c, _ in out}:
            out.append((s, t))
 
    add("".join(toks), 1)
    if len(toks) > 1:
        add("-".join(toks), 1)
    core = strip_generic_tail(norm).lower().split()
    if core != toks:
        add("".join(core), 1)
        if len(core) > 1:
            add("-".join(core), 1)
    if len(core) > 2:
        add("".join(core[:2]), 2)
    if len(core) > 1 and len(core[0]) >= 5 and core[0].upper() not in GENERIC_FIRST:
        add(core[0], 3)
    return out
 
 
# ---- providers: each returns (found, company_name|None, [job dicts]) -------------------------------------------
 
def _job(provider, slug, id_, title, location, url, desc, dept, posted):
    return {"uid": hashlib.sha1(f"{provider}:{slug}:{id_}".encode()).hexdigest(), "provider": provider, "slug": slug,
            "title": (title or "").strip(), "location": (location or "").strip(), "url": url,
            "description": desc or "", "department": dept or "", "posted": posted or ""}
 
 
def greenhouse(sess, slug):
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    meta = _get_json(sess, base, allow_missing=True, timeout=10)
    if meta is None:
        return False, None, []
    if not isinstance(meta, dict):
        raise BoardRequestError("Malformed Greenhouse board")
    name = meta.get("name")
    body = _get_json(sess, base + "/jobs?content=true", timeout=30)
    if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
        raise BoardRequestError("Malformed Greenhouse jobs")
    jobs = [_job("greenhouse", slug, j.get("id"), j.get("title"), (j.get("location") or {}).get("name"),
                 j.get("absolute_url"), strip_html(j.get("content")),
                 ", ".join(d.get("name", "") for d in j.get("departments") or []), j.get("updated_at"))
            for j in body["jobs"] if isinstance(j, dict)]
    return True, name, jobs

def lever(sess, slug):
    data = _get_json(sess, f"https://api.lever.co/v0/postings/{slug}?mode=json", allow_missing=True)
    if data is None:
        return False, None, []
    if not isinstance(data, list):
        raise BoardRequestError("Malformed Lever jobs")
    jobs = []
    for j in data:
        if not isinstance(j, dict):
            continue
        c = j.get("categories") or {}
        posted = time.strftime("%Y-%m-%d", time.gmtime(j["createdAt"] / 1000)) if isinstance(j.get("createdAt"), (int, float)) else ""
        jobs.append(_job("lever", slug, j.get("id"), j.get("text"), c.get("location"), j.get("hostedUrl"),
                         j.get("descriptionPlain") or strip_html(j.get("description")), c.get("team"), posted))
    return True, None, jobs
 
 
def ashby(sess, slug):
    data = _get_json(sess, f"https://api.ashbyhq.com/posting-api/job-board/{slug}", allow_missing=True)
    if data is None:
        return False, None, []
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise BoardRequestError("Malformed Ashby jobs")
    jobs = [_job("ashby", slug, j.get("id"), j.get("title"), j.get("location"), j.get("jobUrl"),
                 j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")), j.get("department"),
                 (j.get("publishedAt") or "")[:10]) for j in data["jobs"] if isinstance(j, dict) and j.get("isListed", True)]
    return True, None, jobs
 
 
def smartrecruiters(sess, slug):
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    jobs, seen = [], set()
    name, offset = None, 0
    while True:
        data = _get_json(sess, f"{base}?limit=100&offset={offset}", allow_missing=(offset == 0))
        if data is None:
            return False, None, []
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise BoardRequestError("Malformed SmartRecruiters page")
        content = data["content"]
        try:
            total = int(data["totalFound"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BoardRequestError("Missing SmartRecruiters totalFound") from exc
        if not content:
            if offset < total:
                raise BoardRequestError("SmartRecruiters pagination ended early")
            # A valid empty board is not a confirmed missing board.
            return True, name, jobs
        added = 0
        for posting in content:
            if not isinstance(posting, dict) or not posting.get("id"):
                raise BoardRequestError("Malformed SmartRecruiters posting")
            ident = str(posting["id"])
            if ident in seen:
                continue
            seen.add(ident)
            added += 1
            detail = _get_json(sess, f"{base}/{ident}")
            if not isinstance(detail, dict):
                raise BoardRequestError("Malformed SmartRecruiters details")
            name = name or (posting.get("company") or {}).get("name")
            loc = detail.get("location") or posting.get("location") or {}
            loc_s = ", ".join(str(x) for x in
                              [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x)
            sections = (detail.get("jobAd") or {}).get("sections") or {}
            description = " ".join(strip_html((sections.get(key) or {}).get("text"))
                                   for key in ("companyDescription", "jobDescription",
                                               "qualifications", "additionalInformation"))
            job = _job("smartrecruiters", slug, ident, detail.get("name") or posting.get("name"), loc_s,
                       detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{ident}",
                       description, (detail.get("department") or posting.get("department") or {}).get("label"),
                       (detail.get("releasedDate") or posting.get("releasedDate") or "")[:10])
            job["is_us"] = is_us_location(loc_s, loc.get("country"))
            jobs.append(job)
        offset += len(content)
        if offset >= total:
            return True, name, jobs
        if not added:
            raise BoardRequestError("SmartRecruiters pagination made no progress")

PROVIDERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}
NEEDS_NAME_CHECK = {"greenhouse", "smartrecruiters"}
 
 
def resolve(sess, employer_name: str, norm: str, known: dict | None = None):
    """Return a complete board, a confirmed miss, or raise to preserve cache."""
    errors, checked = [], set()
    # Only a successful lookup or explicit 404/410 resolves a cached board.
    for prov, slug in (known or {}).items():
        checked.add((prov, slug))
        try:
            ok, _, jobs = PROVIDERS[prov](sess, slug)
        except Exception as exc:
            raise BoardRequestError(f"Cached {prov}/{slug}: {exc}") from exc
        if ok:
            return prov, slug, jobs

    for slug, tier in slug_candidates(norm):
        for prov, fn in PROVIDERS.items():
            if (prov, slug) in checked or (prov not in NEEDS_NAME_CHECK and tier > 2):
                continue
            try:
                ok, board_name, jobs = fn(sess, slug)
            except Exception as exc:
                errors.append(f"{prov}/{slug}: {exc}")
                continue
            if not ok:
                continue
            if prov in NEEDS_NAME_CHECK and not (
                similar(board_name or "", employer_name) or similar(board_name or "", norm)
            ):
                continue
            return prov, slug, jobs
    if errors:
        raise BoardRequestError("; ".join(errors[:3]))
    return None

# ---- scanning -------------------------------------------------------------------------------------------------
 
class Scanner:
    """Background scan: resolve boards + fetch openings for a list of employers, persisting to SQLite."""
 
    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.state = {"running": False, "total": 0, "done": 0, "boards": 0, "jobs": 0, "cancel": False, "error": None, "failed": 0}
 
    def start(self, employers: list[tuple[str, str]], refresh=False, workers=12):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, total=len(employers), done=0, boards=0, jobs=0, cancel=False, error=None, failed=0)
        threading.Thread(target=self._run, args=(employers, refresh, workers), daemon=True).start()
        return True
 
    def cancel(self):
        self.state["cancel"] = True
 
    def _cached(self, norm):
        rows = db.thread_conn(self.conn).execute("SELECT provider,slug,found,checked_at FROM boards WHERE norm=?", (norm,)).fetchall()
        return rows
 
    def _one(self, name, norm, refresh):
        now = time.time()
        rows = self._cached(norm)
        known = {r["provider"]: r["slug"] for r in rows if r["found"]}
        if not known and rows and not refresh and now - max(r["checked_at"] for r in rows) < NEG_TTL:
            return norm, None, "cached-miss"
        if known and not refresh:
            fresh = db.thread_conn(self.conn).execute("SELECT MAX(fetched_at) m FROM jobs WHERE norm=?", (norm,)).fetchone()["m"]
            if fresh and now - fresh < JOB_TTL:
                return norm, None, "fresh"
        with requests.Session() as sess:
            sess.headers.update(HEADERS)
            return norm, resolve(sess, name, norm, known), "scanned"
 
    def _run(self, employers, refresh, workers):
        try:
            with ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(self._one, name, norm, refresh): (name, norm)
                        for name, norm in employers}
                for fut in as_completed(futs):
                    if self.state["cancel"]:
                        for pending in futs:
                            pending.cancel()
                        break
                    name, employer_norm = futs[fut]
                    try:
                        norm, res, how = fut.result()
                        if norm and how == "scanned":
                            self._store(norm, res)
                        if res:
                            self.state["boards"] += 1
                            self.state["jobs"] += len(res[2])
                    except Exception as exc:
                        self.state["failed"] += 1
                        self.state["error"] = (
                            f"{name} ({employer_norm}): {type(exc).__name__}: {exc}"
                        )
                        import traceback
                        traceback.print_exc()
                    finally:
                        # A failed employer is processed too; continue the remaining scan.
                        self.state["done"] += 1
        except Exception as exc:
            self.state["error"] = f"{type(exc).__name__}: {exc}"
            import traceback
            traceback.print_exc()
        finally:
            self.state["running"] = False

    def _store(self, norm, res):
        now = time.time()
        c = db.thread_conn(self.conn)
        # Commit replacement atomically; failures roll back the deletion.
        with c:
            c.execute("DELETE FROM boards WHERE norm=?", (norm,))
            c.execute("DELETE FROM jobs WHERE norm=?", (norm,))
            if res is None:
                c.execute("INSERT INTO boards VALUES(?,?,?,?,?)", (norm, "none", "", 0, now))
            else:
                prov, slug, jobs = res
                c.execute("INSERT INTO boards VALUES(?,?,?,?,?)", (norm, prov, slug, 1, now))
                c.executemany("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              [(j["uid"], norm, prov, slug, j["title"], j["location"],
                                int(j["is_us"] if "is_us" in j else is_us_location(j["location"])),
                                j["url"], j["description"], j["department"], j["posted"], now)
                               for j in jobs])

