# ARGUS Phase 3a work log — work-authorisation combobox adapters

Date: 2026-08-26  
Branch: `codex/argus-safety-upgrade`  
Scope: ATS adapters and `tests/unit/test_combobox_options.py` only

## Constraints observed

- The worktree was intentionally dirty before this task.
- Concurrent ownership exclusions were not opened, edited, or reverted.
- No destructive Git operations, commit, navigation control, egress change, or submission action is authorised.
- Production files will be backed up inside the owned adapter directory before modification.
- Tests use loopback/browser-local fixtures and never submit a form.

## Investigation

- Confirmed the generic inspection script enumerates native `<select>` options only and returns an empty option tuple for ARIA comboboxes.
- Confirmed Greenhouse separately scans all visible `[role="option"]` nodes during fill, so inspection loses the constrained choices and fill is not listbox-associated.
- Confirmed the existing prefix matcher handles simple leading `Yes`/`No` text but has no explicit priority API or work-authorisation/sponsorship alias table.

## TDD record

- RED: `python -m pytest -q tests/unit/test_combobox_options.py -p no:cacheprovider`
  produced `10 failed, 3 passed`; failures were the empty ARIA option tuple and
  missing conservative matcher, as expected.
- GREEN (owned file): `13 passed in 3.41s`.
- Compatibility repair: the first unit run found one pre-existing role-less
  React option-container fixture and privacy-scan findings in synthetic legal
  examples (`818 passed, 2 failed, 2 skipped`). The option fallback was limited
  to the nearest unique role-option container and fixture vocabulary was marked
  synthetic. Targeted recheck: `15 passed in 5.76s`.
- GREEN (unit suite): `820 passed, 2 skipped in 87.78s`.
- Final verification: `python -m pytest -q tests/unit tests/integration
  -p no:cacheprovider` produced `1088 passed, 2 skipped, 2 warnings in
  709.71s`. The total is above the brief's 1060-test baseline because other
  phases added tests concurrently. No test failed.

## Implementation delivered

- Native selects retain their original text enumeration and fill path.
- ARIA combobox inspection resolves `aria-controls`, `aria-owns`, and
  `aria-activedescendant`, then uses labelled or nearest unique listbox/option
  container associations as conservative fallbacks.
- Empty dynamic listboxes receive at most one bounded 200 ms read-only probe
  per control, capped at eight probes per inspection; collapsed state and prior
  focus are restored where practical.
- Greenhouse fill reads and clicks only the combobox-associated visible
  options. It never clicks Next or Submit. A searchable combobox may be typed
  only with an exact option label already validated during inspection.
- Matching precedence is exact case/whitespace-insensitive match, then stored
  answer as a whole-word prefix, then the explicit work-authorisation or
  sponsorship alias table. More than one match at a priority fails closed.
- Work-authorisation and sponsorship polarity is checked before any match is
  accepted; contradictory or inverse declarations do not match.

## Files

- Modified: `generic.py`, `greenhouse.py`.
- Added tests: `tests/unit/test_combobox_options.py` (9 test functions, 13
  collected cases).
- Backups: `generic.py.phase3a-prechange-20260826.bak` and
  `greenhouse.py.phase3a-prechange-20260826.bak`; SHA-256 equality with each
  pre-edit source was verified when created.
- Work log: this file.

## Tooling note

- `git diff --check` passed for the scoped files.
- Ruff is not installed in the project virtual environment, so no Ruff result
  is claimed.
