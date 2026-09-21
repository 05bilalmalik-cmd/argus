# ARGUS Tracker-first Design and Implementation Plan

Approved direction: tracker-first, retaining optional preparation (user decision in this session).

**Goal:** An automatically refreshed UK internship/placement/spring-week tracker that remains useful without AI, candidate credentials, or employer-form automation.

**Architecture:** New `app/tracker/` standalone FastAPI service inside ARGUS, reusing existing pure programme/ATS parsers where correct. A dedicated SQLite database and single-flight refresh worker keep discovery completely separate from the legacy application database/browser runner. Existing ARGUS remains unchanged and available as an optional preparation workspace; CSV export provides a deliberate transfer, never implicit automation authority.

**Stack:** Existing Python 3.13 venv, FastAPI, sqlite3, httpx, BeautifulSoup, plain local HTML/CSS/JS. No new third-party dependencies. Model workers: GPT-5.6 Luna, Codex provider, maximum reasoning. No commits or live installation.

## Product contract
- Dense Explore surface inspired by the references, not a marketing dashboard: searchable table, programme/UK/new/saved filters, clear external Apply link, saved flag, manual application stages, notes and next-action due date.
- Automatic startup refresh and configurable recurring refresh; manual refresh must use the identical single-flight path. No overlap between processes using the same data root.
- Dedicated source results: ok/empty/error/partial, raw result count, elapsed time, last attempt/success. HTTP 200 on changed HTML is not proof of zero jobs.
- Public employer APIs first: current Greenhouse watchlist plus DV Trading; Recruitee for Silverpeak; Pinpoint for Menzies if its public endpoint is verified. SimplyTK listing HTML as a supplementary observation source, with incomplete capture disclosed. Do not scrape authenticated Jorb data or bypass access blocks.
- Preserve first_seen and last_seen separately from source-published/updated dates. Never call first_seen 'posted'. Failed or partial scrapes never mark roles closed. Deadline expiry and observation staleness are computed at read time.
- Strict URL validation, no arbitrary-URL fetching endpoint, local-only origin/host checks on mutations, bounded source concurrency/timeouts. Untrusted content rendered as text, safe exports, no credentials stored.
- Deduplicate repeat captures and known ATS identities; preserve source provenance and user edits. Do not conflate multiple roles sharing a generic listing URL.
- CSV transfer and link to the existing ARGUS preparation workspace. No new submission pathway; legal, assessment and CAPTCHA boundaries retained.

## Files and task ownership
1. Parent: `app/tracker/contracts.py` shared data contract, `__init__.py`, programme regression fixes/tests, plan/docs and UI assets.
2. Luna source worker: `app/tracker/sources.py`, optional `source_parsers.py`, `tests/tracker/test_sources.py`. Public source fetching and truthful per-source outcomes.
3. Luna storage worker: `app/tracker/store.py`, `tests/tracker/test_store.py`. Persistent role/provenance/health/user state and filter/export support.
4. Parent: `app/tracker/server.py`, `__main__.py`, `tests/tracker/test_server.py`, static UI; scheduling, safety and presentation.
5. Parent + Luna read-only review: integration verification and adversarial checks. No Python writes during final certification; shared source-freeze fixture detects mixed revisions.

## Shared interfaces
`Listing`: dataclass with employer, title, url, location='', programme='', source_id='', posted_at=None, deadline=None. Date fields ISO strings (posted_at timezone-aware timestamp when known; deadline YYYY-MM-DD). `SourceResult`: name, url, status ('ok','empty','partial','error'), listings=list[Listing], error='', checked_at (UTC ISO default), elapsed_seconds=0.0. Both in contracts.py. Fetch function `collect_sources() -> list[SourceResult]`; individual failures must be represented, not raised across the whole sweep.

`TrackerStore(path)` initializes standalone SQLite. `ingest(results: list[SourceResult]) -> dict` (new, updated, observed, sources, errors integer totals). `list_jobs() -> list[dict]` rows with id, employer, title, url, location, programme, posted_at, deadline, first_seen, last_seen, saved(bool), stage, notes, due_date, sources(list[str]); source content must not overwrite user fields. `update_job(id, *, saved=None, stage=None, notes=None, due_date=None) -> dict` (unknown id KeyError, invalid input ValueError; empty due_date clears). Stages: not_applied, applied, assessment, interview, offer, rejected, withdrawn. `list_sources() -> list[dict]` with name,url,status,error,checked_at,last_success_at,row_count,elapsed_seconds. `summary() -> dict` total,saved,new_24h,source_errors. No global persistent connections; per-operation transactions safe across worker/request threads. Store may add internal helpers but not change contracts without coordinating.

## Verification sequence
- Baseline already executed: `.venv/Scripts/python.exe -m pytest tests/unit/test_sources.py tests/unit/test_scouting.py tests/unit/test_sweep_lock.py tests/unit/test_prefill_advance_guard.py tests/unit/test_shared_navigator_routes.py -q -p no:cacheprovider` with JUnit evidence.
- RED/GREEN programme regression for Placement Student and Summer Placement; preserve explicit industrial placement and negative unrelated placements.
- RED/GREEN source shape/filter/error reporting; fixture tests cannot silently label an error empty.
- RED/GREEN store persistence/repeated sweep/user edits/multi-source duplicates/error retention.
- API integration for refresh/status/filters/persisted state/CSV and hostile-origin/malformed inputs.
- Real read-only refresh against public sources into a NEW isolated data root; read back counts from SQLite/API. Repeat and verify identity stability.
- Run candidate on unused loopback port 8791; verify health, desktop and mobile UI, search/filter/save/stage persistence and browser console; leave existing 8787 instance untouched.
- Run focused old+new regressions under frozen source and parse JUnit. Report exact coverage, failed sources and unverified runtime longevity. No claim of universal auto-apply or 24/7 uptime from a short smoke.
