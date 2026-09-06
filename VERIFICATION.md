# Public source verification

## Preparation bridge publication

Fresh scoped checks and unresolved acceptance limits for the preparation bridge
are recorded in [PREPARATION_BRIDGE_VERIFICATION.md](docs/PREPARATION_BRIDGE_VERIFICATION.md).
The baseline numbers below are historical and do not certify the bridge revision.

## Baseline source publication — 2026-09-06

This is a source publication, not a signed release, installed upgrade, or
certification of real-employer autonomous applications.

## Fresh checks of the public working tree

- Python 3.13.13, Windows; existing development dependencies and Chromium.
- Full suite collection: **2349 tests collected**.
- Focused regression run: **691 passed**, no failures or skips. Covers sanitised unit/integration fixtures and launcher, sandbox, folder resolution, document, dropdown, radio and PREFILL boundaries.
- Browser rechecks: **5 passed**, no failures or skips.
- Distinct passing public-tree tests across those completed runs: **696**.
- The first browser recheck also passed independently on the untouched original source; this does not establish the cause of the earlier failure.
- Python source compiled successfully without execution.
- Production privacy scanner passed. Full-tree redacted Gitleaks scan passed after three reviewed non-secret fixture annotations (answer identifiers and a DOM-root identifier).
- Independent publication-scope review reported no blocking privacy/export/semantic issues. This was not an application-wide security audit.

## Failures and limits retained

The initial export omitted `requeue_blocked.py`, a dependency of the state-release
tests. Collection failed; the dependency was restored and the focused run includes
its importing test module.

A broad browser-inclusive run was **interrupted, not passed**. Its saved progress
record contains **5 failures**. All of those tests passed in subsequent
focused public-tree reruns, but their original failure mechanisms remain
**UNVERIFIED**. Do not describe the full suite as green or claim these failures
were fixed. The smaller reruns do not replace a clean complete-suite result.

Observed failures in that interrupted run:

- `tests/e2e/test_application_navigator_journeys.py::test_typed_journey_command_runs_on_navigator_owner_thread`
- `tests/e2e/test_application_navigator_journeys.py::test_low_level_submit_mode_journey_stops_before_activation[workable-journey-workable]`
- `tests/e2e/test_application_navigator_journeys.py::test_captcha_preflight_hands_off_without_consuming_authority_or_post[after_preflight]`
- `tests/e2e/test_application_navigator_journeys.py::test_http_confirm_rejects_same_origin_target_mutation_after_display[action]`
- `tests/e2e/test_lab_adapter_variants.py::test_ats_variants_detect_fill_and_submit_once[greenhouse-greenhouse]`

One dependency deprecation warning was present in the completed pytest runs.
The staged whitespace check reports inherited formatting warnings; every warning
line was checked against and matches the original source. No new full-suite,
clean-machine installation, packaged executable build, or real-employer submission
was performed for this publication. GitHub CI has not been certified.

## Reproduce locally

Use a disposable environment, synthetic data and loopback destinations only.
Do not run tests against an existing applicant database or browser handoff.

```text
python -m pytest tests --collect-only -q -p no:cacheprovider
python scripts/privacy_scan.py --root .
gitleaks dir . --redact --no-banner
```

For runtime tests use `scripts.verify.safe_environment(Path.cwd())` as the child
environment, `PYTHONDONTWRITEBYTECODE=1`, and a fresh external pytest `--basetemp`.
The complete verification entry points are `scripts/verify.ps1` and
`scripts/verify.sh`; running them is separate from the checks reported here.

Raw local logs and XML reports are retained outside this public repository because
they contain workstation paths. Public source fixture names are synthetic. Private
Git history, candidate documents, profiles, databases, keys, browser captures and
backups are not part of this publication. Historical documents elsewhere in this
repository are not fresh evidence for this revision.
