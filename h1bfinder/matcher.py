from __future__ import annotations
import re
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from .resume import find_skills

NO_SPONSOR = re.compile(
    r"(?:no|not|unable to|cannot|can't|won't|will not|do not|does not|don't|doesn't|without)\s+(?:\w+\s+){0,4}?"
    r"(?:visa\s+)?(?:sponsor|sponsorship)|(?:u\.?s\.?\s+citizens?\s+(?:only|required)|must be a u\.?s\.? citizen|"
    r"(?:ts/sci|top secret|secret)\s+(?:security\s+)?clearance|permanent resident (?:or citizen )?only)", re.I)
YES_SPONSOR = re.compile(r"(?:will|can|able to|may|do|does|offer|provide)\s+(?:\w+\s+){0,3}?(?:visa\s+)?sponsor|"
                         r"h-?1b\s+(?:transfer|sponsorship)|sponsorship\s+(?:is\s+)?available", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|director|head of|vp|vice president|manager)\b", re.I)
JUNIOR = re.compile(r"\b(intern|internship|junior|jr\.?|entry[- ]level|new grad|graduate)\b", re.I)


def rank(jobs: list[dict], resume_text: str, resume_skills: list[str], years: int, sponsor: dict[str, float],
         keywords: list[str] | None = None, us_only=True, hide_no_sponsor=True, limit=100) -> list[dict]:
    keywords = [k.lower() for k in (keywords or []) if k.strip()]
    if us_only:
        jobs = [j for j in jobs if j["is_us"]]
    if not jobs or not resume_text.strip():
        return []
    docs = [(j["title"] + " ") * 3 + j["description"][:6000] for j in jobs]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1, max_features=60000, sublinear_tf=True)
    X = vec.fit_transform(docs + [resume_text])
    sims = linear_kernel(X[-1], X[:-1]).ravel()
    # rescale cosine so the best match in this pool ~ 1 (raw cosine of resume vs JD is small in absolute terms)
    top = np.percentile(sims, 99) or 1.0
    sims = np.clip(sims / top, 0, 1)
    rs = set(resume_skills)
    out = []
    for j, sim in zip(jobs, sims):
        text = j["title"] + " " + j["description"]
        jskills = set(find_skills(text))
        matched = sorted(jskills & rs)
        cover = len(matched) / min(len(jskills), 8) if jskills else 0.0
        cover = min(cover, 1.0)
        title_l = j["title"].lower()
        kw = (sum(k in title_l for k in keywords) / len(keywords)) if keywords else 0.0
        sp = sponsor.get(j["norm"], 0.0)
        score = 0.55 * sim + 0.25 * cover + 0.10 * kw + 0.10 * sp
        flags = []
        no_sp = bool(NO_SPONSOR.search(j["description"]))
        if no_sp:
            flags.append("says no sponsorship / citizen-only")
            score *= 0.35
        elif YES_SPONSOR.search(j["description"]):
            flags.append("mentions sponsorship available")
            score *= 1.05
        if SENIOR.search(j["title"]) and years and years < 4:
            flags.append("senior title vs your experience")
            score *= 0.8
        if JUNIOR.search(j["title"]) and years >= 5:
            flags.append("junior/intern title")
            score *= 0.7
        if hide_no_sponsor and no_sp:
            continue
        out.append({**{k: j[k] for k in ("uid", "norm", "title", "location", "url", "department", "posted", "provider")},
                    "score": round(min(score, 1.0) * 100, 1), "text_sim": round(float(sim) * 100),
                    "skills_matched": matched, "skills_missing": sorted(jskills - rs)[:6], "flags": flags})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]