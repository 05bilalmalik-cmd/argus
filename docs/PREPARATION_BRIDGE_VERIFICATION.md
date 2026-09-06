# Preparation bridge source publication checks

This publishes experimental source and a portable operating skill, not a certified application agent or an installed upgrade.

Fresh checks ran on an isolated Git-normalized export with Python 3.13.13 on Windows; temporary data and subprocess environments were separate from live ARGUS.

- Full collection: **2528 tests collected**. Collection is not execution.
- Focused bridge, transport, API, operator, manifest, guard, PREFILL, combobox, Workday and acceptance-oracle regressions: **203 passed**, zero failures/errors/skips.
- The real production/browser observer test was deliberately deselected; no browser acceptance run or real application was attempted.
- Exported Python compiled without execution. Staged whitespace checks passed.
- Source privacy scanner and redacted Gitleaks 8.30.1 full-export scan passed. Credentials, databases, CVs, runtime logs, backups and private implementation history are excluded.

Initial privacy inspection of the worktree found retained local backups and synthetic fixture names outside the approved vocabulary. Backups were excluded from the exact Git export; fixture names were changed to Demo/Candidate while preserving positive-versus-corrupted value distinctions, then the focused tests were rerun. One workstation-specific Markdown example was made portable. The privacy policy was not weakened. Gitleaks also flagged one synthetic idempotency identifier; that reviewed non-credential line received a narrow gitleaks:allow annotation before rescanning.

## Reproduce the focused checks

Run inside a disposable environment with the optional bridge/dev dependencies. Use `scripts.verify.safe_environment` and a fresh external data root and pytest basetemp.

```text
python -m pytest tests/unit/test_preparation_bridge_core.py tests/unit/test_preparation_bridge_manifest.py tests/unit/test_preparation_bridge_operator.py tests/unit/test_preparation_bridge_review_regressions.py tests/unit/test_preparation_bridge_transport.py tests/unit/test_preparation_prefill_entry.py tests/unit/test_preparation_runner_guard.py tests/integration/test_preparation_bridge_api.py tests/unit/test_prefill_advance_guard.py tests/unit/test_combobox_no_submit_regressions.py tests/unit/test_workday_state_machine.py tests/integration/test_phase33_prepare_application.py tests/e2e/test_preparation_bridge_demo.py -q -k "not test_real_production_prefill_observer"
```

## Limits retained

Full supported-form handoff/replay and independent headed-browser acceptance remain incomplete. Generic unknown-ATS safety is not bypassed. Prior source-review concerns are listed in [PREPARATION_BRIDGE.md](PREPARATION_BRIDGE.md). The original broad-suite failures recorded in the baseline verification are not resolved by this focused run. No full-suite pass, GitHub CI pass, release signing, deployment or autonomous real-employer submission is claimed.
