# ARGUS Internship Tracker

## Open the candidate

Double-click `Start Internship Tracker.cmd` in the ARGUS repository, or run:

```text
.venv\Scripts\python.exe -m app.tracker --data-dir "%LOCALAPPDATA%\ARGUS-Tracker" --port 8791 --refresh-minutes 30 --open
```

The tracker is at **http://127.0.0.1:8791**. Existing ARGUS preparation stays on **http://127.0.0.1:8787**. This is an independently runnable source candidate, not a replacement of the installed ARGUS executable.

The startup command collects immediately and again every 30 minutes while this process is running. Closing its terminal or sleeping/powering off the computer interrupts collection. No Windows scheduled task or server deployment is installed by this change. Start only one tracker for the same directory/port; refresh requests share a file-backed single-flight lock.

Data is stored in `%LOCALAPPDATA%\ARGUS-Tracker\tracker.sqlite3`, separate from legacy application data and candidate credentials. Reopening the same directory preserves saved roles, stages, notes, source health and observations.

## Use it

- **All openings:** UK-location filter on by default. Search company, title, location or inferred division. Select programme/stage, new-to-tracker and deadline filters.
- **Save:** star a role. Open **Saved roles** to see the shortlist. The same filters apply in both views; clear filters if a saved role is missing.
- **Progress:** set a manual stage. Click the role title for notes and a next-action due date.
- **Apply:** the arrow opens the public source/employer URL in a new tab. It does not fill or submit a form.
- **Source health:** explicit `ok`, `empty`, `partial`, or `error` for each source. A zero or failed scraper does not delete listings. Old observations are labelled stale after 24 hours.
- **Export CSV:** downloads the current filtered result set, not just the displayed page. Employer, role, source URL, programme, cycle, deadlines and your notes are included, with spreadsheet-formula escaping.

## Optional application preparation

Use **Application preparation** in the sidebar to open existing ARGUS. Export the relevant filtered roles as CSV and import the file using ARGUS's existing opportunity importer. A year is taken from the role title when present; a yearless role exports `UNSPECIFIED` and needs review. The export is discovery evidence, not approved candidate data or verified application-target authority.

The old engine, CV library, application history, final-review controls, CAPTCHA handling and assessment boundaries remain unchanged. This tracker does not need an LLM, a CV, login details or a paid API to collect openings. Automated universal employer submission is **not** claimed.

## What the clocks mean

- **First detected:** first observation in this tracker, not the posting date. A fresh installation initially marks its collected inventory as new.
- **Last observed:** most recent source observation. Not a fresh visit to the individual application form.
- **Posted by source:** shown only when the source explicitly supplies a usable creation/publication timestamp. Greenhouse update time is not substituted.
- **Deadline:** only a parseable source deadline. `Not listed` does not mean rolling or unlimited.
- **Expired:** source deadline has passed. A role disappearing from a scrape is not automatically called closed.

Eligibility, degree requirements, visa sponsorship, application caps and exact opening/closing status must still be checked on the employer site. UK filtering is location-text matching, not a right-to-work assessment. Broad EMEA/remote/unknown-location rows remain available by turning off the UK filter. Programme categories reuse ARGUS's convention that generic/off-cycle internships fall under summer; inspect the role title for the actual programme.

## Command-line collection

```text
.venv\Scripts\python.exe -m app.tracker --data-dir PATH --once
.venv\Scripts\python.exe -m app.tracker --data-dir PATH --no-auto
```

`--once` prints the real refresh summary/source statuses and persists observations. A nonzero exit flags refresh problems; partial results remain usable and must be inspected. `--no-auto` serves existing inventory without automatic fetching. Minimum refresh interval is one minute. Built-in sources use public read-only requests; no authenticated Jorb data is copied and no access blocks are bypassed.

## Source coverage and limits

The collector runs 14 public Greenhouse boards, Silverpeak's Recruitee feed, Menzies' Pinpoint feed, and SimplyTK's public programme-filtered HTML pages. SimplyTK pagination is capped at 20 pages and a 60-second traversal budget, with per-request timeouts. Count drift, repeated pages, malformed rows or a mismatch with the advertised opening count leave the source partial. An HTTP 200 alone never proves complete coverage.

The tracker does not scrape authenticated Jorb data. It does not include email/push alerts, verified employer closure monitoring, or candidate-specific eligibility scoring. Known ATS identities and repeated observations are deduplicated conservatively; different unrecognised URL aliases can still represent the same role. Source status `empty` refers to that configured feed, not proof that the employer has no internships elsewhere.

The interface follows the references' useful principles—dense listings, filters, shortlist and progress—without copying their branding or claiming their coverage.

## Developer verification

```text
.venv\Scripts\python.exe -m pytest tests/tracker tests/unit/test_sources.py tests/unit/test_scouting.py tests/unit/test_sweep_lock.py tests/unit/test_prefill_advance_guard.py tests/unit/test_shared_navigator_routes.py -q -p no:cacheprovider
node --check app/tracker/static/tracker.js
```

Run final certification only while all Python source is stable. Global test containment blocks accidental external connections; public-source live probes are separate operations. Evidence from this review/build is under `.hermes/argus-review/`. The full legacy browser suite is a separate certification scope.
