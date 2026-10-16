# Job-Search-App

Local web app: reads USCIS H-1B employer data, finds open roles at those employers, and ranks them against your resume.

## Run
```
pip install -r requirements.txt
python run.py            # first start imports data/Employer_Information.xlsx (~15s), then open http://localhost:5000
```
1. **Resume & scan** – upload resume (PDF/DOCX/TXT), pick how many top-sponsoring employers to scan, Start.
2. **Recommendations** – ranked openings with match score, matched/missing skills, and each employer's H-1B history.
3. **Employers** – browse/search the USCIS data, per-year history, scanned openings.
4. **USCIS data** – "Find files on uscis.gov" pulls other fiscal years of the H-1B Employer Data Hub; or paste a URL / upload any
   csv/xlsx/zip with an employer-name column. Old and new USCIS column layouts are both understood. Files merge by normalised employer name.

## How job search works
Employers are matched to public job boards (Greenhouse, Lever, Ashby, SmartRecruiters – free JSON APIs, no keys) by trying slugs derived
from the legal name; Greenhouse/SmartRecruiters matches are verified against the board's company name. Misses are cached 14 days.
**Coverage limit:** companies on Workday, Taleo, iCIMS or their own careers site (Amazon, Microsoft, Google, big IT consultancies…) are not
reachable this way. Add a provider in `h1bfinder/ats.py` (`PROVIDERS` dict: function returning `(found, company_name, jobs)`).

## Scoring (`h1bfinder/matcher.py`)
55% TF-IDF similarity resume↔posting, 25% skill coverage, 10% your title keywords, 10% employer sponsorship strength
(recency-weighted approvals, new-hire weighted, × approval rate). Postings that say "no sponsorship / US citizens only" are hidden
(toggle) or ×0.35; senior-vs-experience mismatch is penalised.

## Notes
* USCIS publishes only the last 4 digits of Tax ID, so employer identity = normalised name (suffixes like INC/LLC and "DBA …" stripped).
* Data counts are petitions approved, not job openings; an employer with approvals may not currently be hiring.
* `python tests/test_app.py` runs offline tests (HTTP mocked).
