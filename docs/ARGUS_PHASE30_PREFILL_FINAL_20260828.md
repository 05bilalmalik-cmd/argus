# ARGUS Phase 30 Prefill Final Report

## Outcome

Phase 30 implementation and the guarded live PREFILL workflow are complete. The automation remains fail-closed: no application was submitted, no Submit control was clicked, and no final-submit mode was invoked.

Aquatic reached the intended headed human handoff. The real employer form remains open in the headed browser with CAPTCHA visible and unsolved. The single permitted second-role request was made for DRW and failed closed at target resolution before creating another browser.

## Implementation delivered

- Added semantic verification for native option values/labels, provider-rendered custom-combobox labels, explicit date representations, checkboxes, and provider-visible file attachments.
- Kept native HTML validity checks. Custom comboboxes use selected-label readback because their hidden required search input can be empty after a valid provider selection.
- Added deterministic mappings for explicit education fields, current location, work-location preference, desired office, study level, availability, and education dates using only existing profile or approved-answer keys.
- Preserved blank-and-flag behavior for absent evidence, unsupported questions, implausible values, legal declarations, and programme/graduation conflicts.
- Required unique enumerated provider options for typeaheads; the three unresolved education selectors were restored blank for human selection.
- Added a CSP-compatible same-origin Greenhouse lab readback fixture. The adapter now keeps a stable upload-wrapper handle while the provider widget may unmount the file input after successful upload.
- Added a headed-review wait fallback so a slow live provider remains a `NEEDS_USER` handoff rather than being closed as a synthetic failure.
- Did not modify the Phase 28 protected files: `app/services/navigator.py`, `app/services/resolution_apply_click.py`, or `app/automation/targets.py`.

## Verification ledger

All pytest commands used `-p no:cacheprovider`.

| Check | Result |
|---|---:|
| Initial RED semantic-verifier run | Native-select/date assertions failed as designed; genuine mismatch remained rejected |
| Final Phase30 verifier/readback tests | `13 passed` in `35.07s` |
| Mapping, plausibility, profile, and candidate-service tests | `131 passed` |
| Existing adapter/planning tests | `77 passed` |
| Greenhouse lab variants and submit-guard cases | `4 passed, 4 deselected` in `47.27s` |
| Handoff/egress/candidate-service/lifecycle subset | `104 passed, 1 warning` in `23.37s` |
| Python compileall | Passed |
| Privacy scan | `15 passed` (earlier final scan; rerun after this report update is recorded in the worklog) |

Exact full-suite command:

```powershell
.venv\Scripts\python.exe -m pytest tests\unit tests\integration tests\e2e -p no:cacheprovider
```

The latest exact full-suite rerun after the runner/file-readback hardening collected `2079` tests and finished with `2076 passed, 1 failed, 2 skipped, 2 warnings` in `1030.20s` (exit code `1`). The one failure was `tests/integration/test_navigator_lifecycle.py::test_dynamic_captcha_boundaries_require_continue_and_keep_context`; its isolated rerun passed (`1 passed` in `4.59s`). The failure occurred during the long run while the intentionally retained headed session was live and is timing/resource-sensitive; the affected lifecycle subset passed afterward. An earlier exact run before the final runtime hardening was green at `2077 passed, 2 skipped, 2 warnings`. The final Greenhouse fixture change was separately covered by the green lab gate above, and the full suite was not repeated afterward so this report does not claim an all-green full gate.

## Aquatic live handoff

- Application ID: `75db5b22-02c7-4cbf-967f-ec63a4b8655a`
- Run ID: `2aada9e6-d4a6-4098-8585-40509103e7e1`
- Handoff session ID: `5951f40330bb494c`
- Provider: Greenhouse
- Final state: `NEEDS_USER`; lifecycle status `HUMAN_REQUIRED`
- Boundary: `captcha_detected`; CAPTCHA remains visible and unsolved
- Headed worker: alive; visibility `headed`; cleanup not complete; `no_auto_submit=true`
- Submission marker: `not_clicked`
- Exact stored employer form URL: `https://job-boards.greenhouse.io/aquaticcapitalmanagement/jobs/8489186002&gh_src=Trackr`
- Status/resume reference: `http://127.0.0.1:8787/api/handoff/sessions/5951f40330bb494c/status`
- Resume endpoint: `http://127.0.0.1:8787/api/handoff/sessions/5951f40330bb494c/resume` (POST, service-owned)
- Session expiry: `2026-08-29T15:24:24.415309+00:00`
- Preserved handoff window: `2026-09-05T15:24:24.415309+00:00`
- Screenshot: `%LOCALAPPDATA%\ARGUS\artifacts\screenshots\2aada9e6-d4a6-4098-8585-40509103e7e1.png`
- Trace destination is registered at `%LOCALAPPDATA%\ARGUS\artifacts\traces\2aada9e6-d4a6-4098-8585-40509103e7e1.zip`, but the ZIP is not yet materialized while `cleanup_complete=false`; the service-owned navigator writes it during cleanup. Cleanup was intentionally not forced because it would close the retained headed handoff.

The request returned `NEEDS_USER` after approximately 16 seconds with an initial verification-risk reason. After the owner completed its bounded provider work, the authoritative handoff status settled at the CAPTCHA boundary above. The retained manifest is:

| Outcome | Count | Reason/accounting |
|---|---:|---|
| Recorded prefilled | `8` | Profile/document-backed controls, including the CV attachment |
| Provider selectors handed to human | `3` | School, degree, and discipline had no unique enumerated option |
| Blank fields | `20` | All unsupported, missing, implausible, legal, or conflicted controls |
| Plausibility-rejected blanks | `3` | Existing plausibility guard rejected the stored value |
| Programme-tier conflict blanks | `1` | Stored graduation evidence conflicted with the programme tier |
| No approved stored answer | `1` | Education start year was not present as an approved answer |
| No approved mapping | `15` | Unsupported employer-specific, legal, competition, compensation, referral, and link questions |

The three education typeaheads remained blank because the provider did not expose a unique enumerated match. No education value was guessed. Legal declarations remained blank because no exact stored-profile match was available. The asynchronous Greenhouse file widget showed provider-visible attachment readback before the final handoff.

## Second-role guard

- Exactly one second-role request was made after the Aquatic handoff: DRW application `94a2189c-75df-43b7-9dff-d02f28f51eb5`.
- Result: HTTP `409`, `target_evidence_invalid`.
- No browser was created for DRW. The Aquatic headed browser is the sole remaining live session.
- The persisted target was not repaired because contradictory tracking/source evidence is a fail-closed condition.

## Safety counters

| Counter | Result |
|---|---:|
| Submissions | `0` |
| Submit clicks | `0` |
| Rows deleted | `0` |
| Persisted allowlist changes | `0` |
| Trust-registry changes | `0` |
| `BLOCKED` rows cleared | `0` |
| Legal answers supplied by model judgment | `0` |
| Invented values | `0` |

The current SQLite database passes integrity (`ok`) and foreign-key (`0`) checks. The selected Aquatic and DRW rows remain `NEEDS_USER`, `APPLICATION_FORM`, `OPEN`, and `NOT_APPLIED`, with no submission reference. Read-only comparison with the pre-change snapshot found zero added or removed identities in the tracked application, opportunity, approved-answer, document, conflict-rule, and archive records. The phase audit delta contains no allowlist-persistence, trust-registry, submit-click, submission-intent, row-delete, or `BLOCKED`-clear events.

The service intentionally remains alive in `REVIEW_ONLY` with live submit and Trackr live disabled. One headed Chrome window remains open on the Aquatic form. No commit or push was performed.

## Backup record

The live database was backed up before implementation to `%LOCALAPPDATA%\ARGUS\backups\argus-phase30-prechange-20260828T093853568776Z.db`.

- Size: `19,398,656` bytes at backup time
- SHA-256: `783f2000eb142274e8f30dbe9072afa22e5911a743f7b11e856313fb3877e6ea`
- Backup SQLite integrity: `ok`
- Backup foreign-key violations: `0`

Existing rewritten files were backed up immediately before their rewrites. The complete timestamped backup set and SHA-256 index is in `docs\ARGUS_PHASE30_PREFILL_WORKLOG_20260828.md`; backups are retained in the environment-neutral sibling directory `..\ARGUS-0.1.0-prechange-backups\phase30` and alongside the initial source/test backups. The service remains intentionally uncommitted in the existing INLINE worktree.
