# ARGUS Phase 25 User Status Final Report

## Outcome

Phase 25 is implemented in source and applied to the live database. ARGUS now
has a candidate-owned application status that is independent of the internal
application state, audited, editable in the v2 UI/API, and enforced as a
reversible pipeline gate.

The exact status vocabulary is:

- `NOT_APPLIED` (default)
- `INTERESTED`
- `NOT_INTERESTED`
- `APPLICATION_SUBMITTED`
- `ONLINE_ASSESSMENT`
- `HIREVUE`
- `ONLINE_TEST`
- `FIRST_ROUND`
- `OFFER`
- `REJECTED`

Each opportunity stores the status, update time, and actor (`default`, `user`,
or `import`). The machine reads this state but does not overwrite a user-owned
value.

## Pipeline enforcement

- `NOT_APPLIED` and `INTERESTED` are eligible for work.
- `INTERESTED` is ranked ahead of `NOT_APPLIED`.
- Every other status is excluded from target resolution, queueing, package
  preparation, batch selection, autopilot, API/direct runs, handoff, and final
  submission authority consumption.
- Exclusion is a filter only. It does not archive or delete a row, change the
  machine state, or remove the row from ordinary inventory.
- Reverting a status to an eligible value re-enables the same row.
- Final authority consumption includes an atomic database status check, so a
  status change between selection and action fails closed.
- Phase 21-owned runner and Navigator files were not edited. Their entry paths
  are protected by caller/service guards, the automation URL projection, and
  the atomic final-authority check.

## UI and API

- Pipeline rows and role/application detail pages show a one-click status
  selector with no page reload.
- Pipeline and Calendar have persistent status filters.
- Today shows the complete status breakdown and a prominent active-process
  lane for post-application stages.
- Excluded rows remain visible and explain that status is the reason
  automation is unavailable.
- Status changes use a same-origin-guarded REST resource and write an audit
  event containing old value, new value, and actor.
- The v2 implementation uses only local assets and updates text with safe DOM
  operations.

## Live import result

The supplied 71 rows were parsed exactly: 60 summer, 6 spring-week, and 5
year-in-industry rows. Matching used only an exact normalized employer,
programme, and programme-group triple. Normalization covers case,
punctuation, dots, whitespace, and `&`/`and`; no fuzzy match, alias merge, or
guess was used.

- Unique exact matches: 47.
- Applied changes: 47.
- Ambiguous database matches left untouched: 23.
- Unmatched rows left untouched: 1.
- User-owned statuses overwritten: 0.
- Second import pass changes: 0.
- Fresh post-apply dry-run changes: 0.
- Rows now excluded from automation by status: 7.

The unmatched row is:

- Line 17: `NOT_APPLIED | Wells Fargo | EMEA Summer Analyst [summer]`.

The 23 ambiguous rows left untouched are:

1. Wincent — Quant Research/Trading Internship - Summer 2027
2. D.E. Shaw — Trader/Analyst Intern (London) - Summer 2027
3. UBS — 2027 Summer Internship
4. Bank of America — Summer 2027 Analyst Programme
5. Rothschild & Co. — 2027 Summer Analyst Programme
6. Evercore — Summer Internship (2027)
7. LionTree — 2027 Summer Internship Programme
8. Standard Chartered — Internship Programme 2027
9. Blackstone — 2027 Summer Analyst
10. Citadel — 2027 Associate Intern
11. Hines — 2026 Summer Analyst
12. Optiver — Quantitative Trading Internship (2027 Start)
13. Xantium — Quantitative Researcher Internship
14. iSAM — Quantitative Research Internship
15. DV Trading — Trading Intern - Summer 2027
16. Jane Street — Quantitative Trading Intern Summer 2027
17. Squarepoint Capital — Intern Quant Researcher
18. G-Research — Quant Research Internship
19. Chicago Trading Company — Quant Trading Internship - Summer 2027
20. Castleton Commodities International — Summer Analyst Internship Programme
    (Summer 2027)
21. Virtu Financial — 2027 Internship - Quantitative Trading
22. Citadel Securities — Trading & Quant Internship 2027
23. Aquatic Capital Management — Quantitative Researcher, Intern (Summer 2027)

## Required discrepancy

The supplied data places Evercore's year-in-industry role at `HIREVUE`, which
is consistent with an application having been made. Both supplied Goldman
Sachs rows are `NOT_APPLIED`, despite the separate recollection that Goldman
was among the applications already made. The importer preserved the supplied
values verbatim and made no silent correction.

## Database evidence

- Pre-change online backup:
  `<local-data>/backups/argus.phase25-prechange-20260828T000336764.db`.
- Apply-mode online backup:
  `<local-data>/backups/argus.phase25-status-preapply-20260827T234927652099Z-f739c6d88e2c4ff5994c2b716a259f5f.db`.
- Apply backup SHA-256:
  `2a85641dc554ce273bb9d5aa813cc10affc53c22a58c7380c9a1c47377e033ad`.
- SQLite integrity: `ok`.
- Schema version: 9.
- Opportunities before/after: 1,175 / 1,175.
- Applications before/after: 1,172 / 1,172.
- Import-owned opportunity rows: 47.
- Status audit events: 47.
- Live distribution: 1,164 `NOT_APPLIED`, 4 `INTERESTED`, 3
  `NOT_INTERESTED`, 2 `APPLICATION_SUBMITTED`, and 2 `HIREVUE`.

## Verification evidence

RED-first tests were used for the domain/schema, importer, pipeline gates,
REST API, and v2 UI. Focused and affected-regression results include:

- Phase 25 v2/API/pipeline regression: 22 passed.
- Affected service regression: 118 passed.
- Affected target/Navigator/UI/API regression after compatibility fixes: 66
  passed.
- Phase 25, Phase 20 v2, and local-asset set: 49 passed.
- Latest post-change notification and Phase 21 measurement rerun: 60 passed,
  1 warning, with zero files changed during the run.
- Python compile check for the application and importer: passed.
- Ruff was unavailable in the environment (`No module named ruff`).

The required full command was run twice without piping away its exit code or
summary:

1. `1 failed, 1806 passed, 2 skipped, 2 warnings in 967.86s`. The only failure
   was the Phase 21-owned privacy scan. Four Phase 21-owned files changed
   during the run.
2. `2 failed, 1867 passed, 2 skipped, 2 warnings in 966.07s`. The failures
   were the Phase 21 privacy scan and a notification latency threshold under
   concurrent load. Two Phase 21 measurement files changed during the run.

The notification test passed on a stable isolated rerun. The privacy scan's
findings were confined to Phase 21 code, tests, its worklog, and adjacent
Phase 21 backups. Final source scans after writing the reports found zero
Phase 25 findings; the remaining total continued changing as the concurrent
Phase 21 owner created new adjacent backups. Because that owner changed files
during both aggregate windows, neither aggregate is represented as a
stable-tree all-green run. No Phase 21-owned file was altered to conceal or
repair that failure.

## Runtime and invariants

The pre-existing packaged executable was stopped after automation OFF,
live-submit false, Trackr live false, and zero active Navigator sessions were
verified, because it held the maintenance lock. It predates schema 9 and was
not restarted against the migrated database. No detached server was started.
The final combined tree needs a release-owned package rebuild before that
packaged runtime is used again.

Final safety totals:

- Rows deleted: 0.
- Submissions: 0.
- Forms filled: 0.
- Statuses guessed: 0.
- Destructive Git operations: 0.
- Commits: 0.
