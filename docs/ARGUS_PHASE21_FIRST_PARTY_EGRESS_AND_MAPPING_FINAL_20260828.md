# ARGUS Phase 21 final report

- Date: 2026-08-28
- Branch: `codex/argus-safety-upgrade`
- Result: implementation complete and reviewed; live remeasurement stopped safely at a new initial-navigation blocker.

## Executive result

Phase 21 now has a closed, exact Greenhouse first-party consequence manifest, PII-safe request/audit/UI disclosure, and a fail-closed field-value plausibility boundary. The implementation survived repeated independent hostile review, including a final provider-path privacy exploit and non-PREFILL parity correction. The final parent safety bundle passed `310` tests.

The first approved live measurement did not reach a browser. The effective live-domain allowlist was empty, so existing Navigator initial-navigation policy rejected the already-verified Greenhouse application page before session or worker construction. The brief forbids changing the general allowlist/reachability decision and directs new abort reasons to be reported. The cohort was therefore stopped after one attempt, with no retry or widening. The requested human-handoff/CAPTCHA end state is not live-proven.

## Closed manifest

The manifest is Greenhouse-only and applies only when all canonical checks agree: PREFILL mode, resolved Greenhouse vendor, exact recognised Greenhouse job-board current-page host, HTTPS/default transport, non-navigation `GET` or `HEAD`, and exact service host/path/resource/data-kind metadata.

| Exact host | Exact measured path | Resource | Candidate data | Kind |
|---|---|---|---:|---|
| `my.greenhouse.io` | `/users` + `/self` | fetch | No | none |
| `email-address-validator.us.greenhouse.io` | `/address/validate` | fetch | Yes | email |
| `api-geocode-earth-proxy.greenhouse.io` | `/v1/autocomplete` | fetch | Yes | location |
| `job-boards.cdn.greenhouse.io` | `/assets/flags-a2kmUSbF.webp` | image | No | none |

Workday, Lever, iCIMS, and SmartRecruiters manifests remain empty. Exact registrable-domain equality is enforced at manifest load. No wildcard/suffix matching exists. The decision remains `allowed=false`, and the browser route remains locally aborted; an exact manifest match changes only the PREFILL consequence from fatal to nonfatal. Provider-controlled or encoded noncanonical paths are fatal and blanked before all PREFILL evidence/audit boundaries. Candidate values are never recorded.

## Field mapping correction

The root defect was ordered label matching followed by unconditional acceptance of the resolved profile value. A GPA label containing a degree-related token could select `education.degree`, and `build_fill_plan` had no independent control/value semantic gate.

The new guard independently infers the control kind and validates grade/GPA, date/year/month, study level, name, email, phone, URL, university, degree, discipline, and option controls. Implausible mappings retain no value, use `source=plausibility_guard`, become blocked/omitted, and are surfaced for human review. Adapter fill loops act only on resolved, non-null values.

Currently recorded mappings this guard would reject:

| Recorded mismatch | Count |
|---|---:|
| GPA/grade → degree | 7 |
| GPA scale → university | 3 |
| Finishing year → university | 1 |
| Completion year → degree | 7 |
| Discipline/subject → degree | 189 |
| Referral/source → motivation | 59 |
| Start date → graduation year | 47 |

These are repeated historical plan/action records, not distinct applications.

## Application-form population and data quality

- Verified `APPLICATION_FORM` rows: `23`.
- Distinct requisition/role identities: `12`.
- Automation-eligible `NEEDS_USER / APPLICATION_FORM` rows: `10`; the other 13 were not forced through state gates.
- Employer-name presentation duplicates: `LionTree`/`liontree` and `Point72`/`point72`; no row was merged or deleted.
- Non-UK row: `point72 — 2026 Warsaw MI Data / Web Scraping Intern`; reported only. The reversible Phase 11 archive mechanism remains the appropriate separate action.
- `CTC Campus - External, Not Advertised` is the observed Greenhouse board/employer label for the Chicago Trading Company quantitative internship record. The evidence does not independently establish a legal entity name; no identity merge or rewrite was made.

Malformed-looking stored tracking paths such as `&gh_src=Trackr` were reported, not repaired in this phase.

## Live remeasurement

Before the attempt, four exact local ARGUS servers were stopped after confirming zero handoff sessions. Their eight exact Python processes and four listeners were proven gone. Database integrity was `ok`, foreign-key violations were zero, all submission tables were empty, and all ten approved applications met the required state/target/user-status gates.

| Employer | Phase 17 populated | Phase 21 populated | Abort/result | Manifest hosts observed | Unfilled result |
|---|---:|---:|---|---|---|
| Aquatic Capital Management | 8 | Not measurable | Local initial-navigation `ValueError` before Navigator session/worker/browser | None | Not measurable |
| DRW | 10 | Not measured | Not attempted after global fail-closed stop | None | Not measured |
| Xantium | 7 | Not measured | Not attempted after global fail-closed stop | None | Not measured |
| Virtu Financial | 6 | Not measured | Not attempted after global fail-closed stop | None | Not measured |

The Aquatic harness process first created and verified:

- backup: `%LOCALAPPDATA%\ARGUS\backups\argus-phase21-pre-prefill-20260828T032308532817Z-75db5b22.db`
- bytes: `19,148,800`
- SHA-256: `9671a4694d6887d6d82fa19459af399f3a04dbb472ed7472038111fe65d98176`
- source/destination integrity: `ok`
- source/destination foreign-key violations: `0`

The local TestClient POST began, but `ApplicationNavigator.start()` called `_validate_navigation_url()` with an empty effective `ARGUS_LIVE_DOMAIN_ALLOWLIST`. The raw `ValueError` occurred before `_ManagedSession` creation, worker construction, `worker.start()`, Playwright, provider navigation, provider POST, submit, or click. The runner transaction rolled back. An isolated immutable-backup reproduction with a forbidden fake worker proved the worker boundary was not reached; an independent reviewer found the same deterministic condition for all ten approved targets.

No retry was made. No second application was attempted. No allowlist entry, in-memory capability, trust entry, or speculative manifest host was added.

## Verification evidence

- Final Phase 21/17 safety bundle after the last privacy/parity correction: `310 passed, 1 warning in 28.28s`.
- Independent final whole-scope review: `289 passed, 1 warning`; no Critical, Important, or Minor finding; live harness verdict READY before the environment blocker was measured.
- Exact mandated combined command over unit/integration/E2E was run without a tail pipe, exceeded the process-hygiene ceiling, and was stopped at 614 seconds with exit `124` and no summary. It is not claimed as a green run.
- Partitioned coverage: unit `1351 passed, 2 skipped`; integration `488 passed, 1` timing failure, followed by that exact test passing and its full six-test file passing; E2E `86 passed`. Unique collected coverage with a passing execution: `1926 passed, 2 skipped`, delta `+269 passed, +0 skipped` from the addendum baseline `1657/2`.
- `py_compile`, privacy scan, scoped `git diff --check`, and trailing-whitespace checks passed. Ruff was unavailable and is not claimed.

## Final safety accounting

Independent post-attempt truth:

- integrity: `ok`
- foreign-key violations: `0`
- opportunities/applications: `1175 / 1172` (unchanged)
- automation runs/audit events: `610 / 13103` (unchanged)
- submission intents/authorities/review bindings/lab submissions: `0 / 0 / 0 / 0`
- rows deleted: `0`
- deletion audit events: `0`
- Aquatic: still `NEEDS_USER / APPLICATION_FORM / NOT_APPLIED`, with no applied/submission marker

Historical baseline, not Phase 21 activity: `36` SUBMIT-mode automation runs and `1` application with a non-empty submission marker already existed before the attempt.

Phase 21 deltas and confirmations:

- submissions: **0**
- submit clicks: **0**
- `RunMode.SUBMIT` executions: **0**
- rows deleted: **0**
- allowlist entries added: **0**
- trust-registry changes: **0**
- general reachability widening: **0**
- commits: **0**

## Material limitation

The code change is implemented, reviewed, and thoroughly tested, but the live provider outcome is unresolved. No claim is made that the four forms now reach human handoff or CAPTCHA, nor that any after-population count improved. Doing so would require new authority to change or supply the initial-target navigation policy/configuration, followed by a fresh backed-up one-ID measurement. That action is outside this brief's explicit no-widening boundary.
