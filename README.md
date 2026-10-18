# Job-Search-App

Local web app: reads USCIS H-1B employer data, finds open roles at those employers, and ranks them against your resume.

## Run
```
pip install -r requirements.txt
python run.py            # first start imports data/Employer_Information.xlsx (~15s), then open http://localhost:5000
```
1. **Create an account** – choose a username and a password of at least 12 characters. Each user has a private resume library.
2. **Resume & scan** – upload resume (PDF/DOCX/TXT), pick how many top-sponsoring employers to scan, Start. A successful upload shows a confirmation for six seconds; refreshing does not show it again.
3. **My resumes** – view extracted text, download the original, choose a resume for matching, or delete a saved resume. New uploads become active automatically. Deleting the active resume requires choosing another one before matching.
4. **Recommendations** – ranked openings with match score, matched/missing skills, and each employer's H-1B history.
5. **Employers** – browse/search the USCIS data, per-year history, scanned openings.
6. **USCIS data (administrator controls)** – "Find files on uscis.gov" pulls other fiscal years of the H-1B Employer Data Hub; or paste an official USCIS download URL / upload a
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
* Accounts, password hashes, sessions and resumes are stored in `data/app.db`, which is ignored by Git. Keep the database private and backed up. Uploaded documents are stored on the app server, including when running in Codespaces.
* After upgrading from the single-user version, create an account and upload your resume again. The old unowned resume is preserved in the database but is never assigned to a new account or exposed by the API.
* Employer data, scans and public job listings are shared across signed-in users. Resumes and the selected matching profile are private to each account. Only administrators can import or remove datasets. Scan controls still affect the shared workspace, so use this version with trusted users.
* Upload limit: 10 MB per request, 20 resumes per account. Accounts currently use local usernames/passwords and do not offer password recovery.
* Sessions expire after seven days; signing out revokes the session. A random session-signing key is persisted in the database, or can be supplied through `H1B_SECRET_KEY`. Use HTTPS and set `H1B_COOKIE_SECURE=1` on a hosted instance (automatic in Codespaces). `H1B_DB_PATH` optionally selects a different database.
* Account tests: `python -m unittest discover -s tests -p test_accounts.py`. Upload feedback tests: `node tests/test_resume_feedback.cjs`.
* USCIS publishes only the last 4 digits of Tax ID, so employer identity = normalised name (suffixes like INC/LLC and "DBA …" stripped).
* Data counts are petitions approved, not job openings; an employer with approvals may not currently be hiring.
* `python tests/test_app.py` runs offline tests (HTTP mocked).

## Administrator setup

All new and existing accounts start without administrator access. There is no
first-user takeover or administrator option on public registration.

1. Create your account in the app (or use your existing account).
2. In the repository's server/Codespace terminal, run:
   ```sh
   python -m h1bfinder.admin grant YOUR_APP_USERNAME
   ```
3. Refresh the app. The USCIS data tab now exposes import, upload, discovery and
   removal controls for that account. Other users can view datasets, scan employers,
   browse jobs and manage their own resumes, but cannot change shared datasets.

To revoke access, run `python -m h1bfinder.admin revoke YOUR_APP_USERNAME`.
Use the same `H1B_DB_PATH` environment variable as the app if you configured a
custom database. Only someone with server-terminal access can grant privileges.
Existing datasets, resumes and accounts are preserved during the upgrade.

URL imports and automatic discovery only use HTTPS on `www.uscis.gov` or
`uscis.gov`. Imports require a direct CSV/XLSX/XLS/ZIP URL below
`/sites/default/files/`. Private-address DNS results, credentials in URLs,
nonstandard ports and redirects are rejected. Downloads are limited to 25 MB.
If USCIS redirects a link or the source is elsewhere, an administrator can download
the file separately and use Upload a dataset (the app's 10 MB upload limit applies).

## Automated checks

GitHub Actions runs Python tests, the spreadsheet-based app smoke test, and both
JavaScript checks on pushes and pull requests. Run the same checks locally:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/test_app.py
node tests/test_resume_feedback.cjs
node tests/test_job_links.cjs
```


