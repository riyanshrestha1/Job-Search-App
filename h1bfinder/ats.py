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


def is_us_location(loc: str) -> bool:
    if not loc:
        return True  # unknown -> don't hide
    if re.search(r"united states|\busa\b|\bu\.s\.", loc, re.I):
        return True
    if re.search(r",\s*(%s)\b" % "|".join(US_STATES), loc) or re.search(r"\b(%s)\b$" % "|".join(US_STATES), loc.strip()):
        return True
    if NON_US.search(loc):
        return False
    return bool(re.search(r"remote|new york|san francisco|seattle|austin|boston|chicago", loc, re.I))


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
    r = sess.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=10)
    if r.status_code != 200:
        return False, None, []
    name = r.json().get("name")
    r = sess.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=30)
    if r.status_code != 200:
        return False, name, []
    jobs = [_job("greenhouse", slug, j["id"], j.get("title"), (j.get("location") or {}).get("name"),
                 j.get("absolute_url"), strip_html(j.get("content")),
                 ", ".join(d.get("name", "") for d in j.get("departments") or []), j.get("updated_at"))
            for j in r.json().get("jobs", [])]
    return True, name, jobs


def lever(sess, slug):
    r = sess.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=20)
    if r.status_code != 200 or not isinstance(r.json(), list):
        return False, None, []
    jobs = []
    for j in r.json():
        c = j.get("categories") or {}
        posted = time.strftime("%Y-%m-%d", time.gmtime(j["createdAt"] / 1000)) if j.get("createdAt") else ""
        jobs.append(_job("lever", slug, j.get("id"), j.get("text"), c.get("location"), j.get("hostedUrl"),
                         j.get("descriptionPlain") or strip_html(j.get("description")), c.get("team"), posted))
    return True, None, jobs


def ashby(sess, slug):
    r = sess.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=20)
    if r.status_code != 200:
        return False, None, []
    data = r.json()
    if "jobs" not in data:
        return False, None, []
    jobs = [_job("ashby", slug, j.get("id"), j.get("title"), j.get("location"), j.get("jobUrl"),
                 j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")), j.get("department"),
                 (j.get("publishedAt") or "")[:10]) for j in data["jobs"] if j.get("isListed", True)]
    return True, None, jobs


def smartrecruiters(sess, slug):
    r = sess.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100", timeout=20)
    if r.status_code != 200:
        return False, None, []
    data = r.json()
    if not data.get("totalFound"):
        return False, None, []
    name = (data["content"][0].get("company") or {}).get("name")
    jobs = []
    for j in data["content"]:
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in [loc.get("city"), (loc.get("region") or "").upper(), loc.get("country", "").upper()] if x)
        jobs.append(_job("smartrecruiters", slug, j.get("id"), j.get("name"), loc_s,
                         f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}", "",
                         (j.get("department") or {}).get("label"), (j.get("releasedDate") or "")[:10]))
    return True, name, jobs


PROVIDERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}
NEEDS_NAME_CHECK = {"greenhouse", "smartrecruiters"}


def resolve(sess, employer_name: str, norm: str, known: dict | None = None):
    """Return (provider, slug, jobs) or None. `known` = {provider: slug} from the cache (skips probing)."""
    if known:
        for prov, slug in known.items():
            try:
                ok, _, jobs = PROVIDERS[prov](sess, slug)
                if ok:
                    return prov, slug, jobs
            except requests.RequestException:
                pass
    for slug, tier in slug_candidates(norm):
        for prov, fn in PROVIDERS.items():
            if prov not in NEEDS_NAME_CHECK and tier > 2:
                continue
            try:
                ok, board_name, jobs = fn(sess, slug)
            except (requests.RequestException, ValueError):
                continue
            if not ok:
                continue
            if prov in NEEDS_NAME_CHECK:
                if not similar(board_name or "", employer_name) and not similar(board_name or "", norm):
                    continue
            return prov, slug, jobs
    return None


# ---- scanning -------------------------------------------------------------------------------------------------

class Scanner:
    """Background scan: resolve boards + fetch openings for a list of employers, persisting to SQLite."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.state = {"running": False, "total": 0, "done": 0, "boards": 0, "jobs": 0, "cancel": False, "error": None}

    def start(self, employers: list[tuple[str, str]], refresh=False, workers=12):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, total=len(employers), done=0, boards=0, jobs=0, cancel=False, error=None)
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
        sess = requests.Session()
        sess.headers.update(HEADERS)
        return norm, resolve(sess, name, norm, known), "scanned"

    def _run(self, employers, refresh, workers):
        try:
            with ThreadPoolExecutor(workers) as ex:
                futs = [ex.submit(self._one, n, nm, refresh) for n, nm in employers]
                for fut in as_completed(futs):
                    if self.state["cancel"]:
                        for f in futs:
                            f.cancel()
                        break
                    try:
                        norm, res, how = fut.result()
                    except Exception as e:
                        norm, res, how = None, None, "error"
                        self.state["error"] = f"{type(e).__name__}: {e}"
                    if norm and how == "scanned":
                        self._store(norm, res)
                    self.state["done"] += 1
                    if res:
                        self.state["boards"] += 1
                        self.state["jobs"] += len(res[2])
        except Exception as e:  # pragma: no cover
            self.state["error"] = str(e)
        finally:
            self.state["running"] = False

    def _store(self, norm, res):
        now = time.time()
        c = db.thread_conn(self.conn)
        if res is None:
            c.execute("INSERT OR REPLACE INTO boards VALUES(?,?,?,?,?)", (norm, "none", "", 0, now))
        else:
            prov, slug, jobs = res
            c.execute("DELETE FROM boards WHERE norm=? AND provider='none'", (norm,))
            c.execute("INSERT OR REPLACE INTO boards VALUES(?,?,?,?,?)", (norm, prov, slug, 1, now))
            c.execute("DELETE FROM jobs WHERE norm=?", (norm,))
            c.executemany("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          [(j["uid"], norm, prov, slug, j["title"], j["location"], int(is_us_location(j["location"])),
                            j["url"], j["description"], j["department"], j["posted"], now) for j in jobs])
        c.commit()