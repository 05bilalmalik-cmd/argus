# Greenhouse Source-Resolution Repair — 2026-08-26

## User-visible failure

The Point72 recovery record reported:

`Source-resolution application/session binding was inconsistent; retry was refused.`

When a session was eventually created, Playwright reported:

`Page.goto: net::ERR_BLOCKED_BY_CLIENT`

No application was submitted during these failures.

## Root causes

1. The packaged `ARGUS.exe` launcher deliberately overwrites inherited automation mode and live-domain settings with `OFF` and an empty allowlist. The operator walkthrough incorrectly attempted to start `REVIEW_ONLY` through that OFF-only launcher. The supported operator path is `.\scripts\start.ps1` / `.venv\Scripts\python.exe -m app.cli serve --open`.
2. Greenhouse redirects legacy `boards.greenhouse.io` URLs to `job-boards.greenhouse.io`; both exact hosts are required for this reviewed journey.
3. The egress classifier treated the numeric Greenhouse `gh_jid` requisition identifier as a phone-like candidate value. The approved read-only document GET was consequently classified as data-bearing and aborted.

## Repair

- Added a narrowly bound Greenhouse job-identity exception in `app/automation/host_policy.py`.
- `gh_jid` is non-candidate requisition metadata only when:
  - the host is exactly `boards.greenhouse.io` or `job-boards.greenhouse.io`;
  - the URL path contains `/jobs/<numeric id>`; and
  - the `gh_jid` value exactly matches that path id.
- Host, path, or value mismatches remain data-bearing and fail closed.
- Corrected `ARGUS_RUN_AND_APPLY_WALKTHROUGH_2026-08-26.md` to use the supported source launcher for `REVIEW_ONLY` and `ARMED` modes.

## Evidence

- RED observed: both official Greenhouse URLs classified as `data_bearing`; 2 failing and 3 passing counterexample cases.
- GREEN targeted regression: 5/5 passed.
- Complete egress-policy module: 29/29 passed.
- Relevant source-resolution suite: 52/52 passed (one pre-existing Starlette deprecation warning).
- Live local retest:
  - automation mode `REVIEW_ONLY`;
  - live submission disabled;
  - exact allowlist `boards.greenhouse.io,job-boards.greenhouse.io`;
  - HTTP 202 with matching application/session handoff;
  - Navigator reached `HUMAN_REQUIRED` with a live headed worker and the reason `Visible source inspection is ready; verify the exact application destination in this session`.

## Recovery copies

- `app/automation/host_policy.py.pre-greenhouse-job-id-fix-20260826.bak`
- `tests/unit/test_mode_and_egress.py.pre-greenhouse-job-id-fix-20260826.bak`
- `docs/ARGUS_RUN_AND_APPLY_WALKTHROUGH_2026-08-26.pre-source-launcher-correction.bak`
