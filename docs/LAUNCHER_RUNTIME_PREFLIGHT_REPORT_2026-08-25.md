# ARGUS launcher/runtime preflight — 2026-08-25

## Scope

This bounded lane covered only the launcher, frozen-build Playwright driver
inclusions, launcher/version metadata checks, and OFF-mode scheduler coverage.
It did not build or install ARGUS, stop the installed process, access the live
database, use Trackr, or make external network requests.

## Changes

- `launcher.py` now supplies explicit safe defaults for `OFF`, live submit
  disabled, Trackr disabled, headless browser mode, and a zero-hour sweep.
- Dashboard browser opening is opt-in through `ARGUS_BROWSER_OPEN=true` (also
  accepts `1`, `yes`, and `on`). Missing, false, or unknown values fail closed;
  no opener thread is created when the flag is disabled.
- The launcher’s version fallback is `0.2.0`, matching the checked-in runtime
  metadata.
- `launcher.spec` now explicitly bundles Playwright’s platform driver
  executable (`node.exe` on Windows) alongside the collected Playwright data.
  The browser cache remains external and is not packaged.
- Release coverage asserts that stale generated `argus_applications.egg-info`
  metadata is excluded from the source archive rather than rewritten.

## Round 2 hardening

- Launcher-created child environments now overwrite every automation control
  (`ARGUS_AUTOMATION_MODE`, legacy automation aliases, live-submit, Trackr,
  sweep interval, live-domain allowlist, and host) instead of using
  `setdefault`.
- Inherited data directories, credentials, provider/profile overrides, proxy
  variables, and browser-profile paths are removed. `Settings.load` therefore
  selects the standard user-local ARGUS data directory, while explicit browser
  visibility remains opt-in through `ARGUS_BROWSER_OPEN`.
- The frozen in-process path receives the same sanitized environment as the
  source-mode child process.
- Browser opening now requires a successful HTTP 200 response containing valid
  JSON with `status=ok`, `service=ARGUS`, a non-empty version,
  `automation_mode=OFF`, `live_submit=false`, and `trackr_live=false`.
  Malformed, incomplete, unsafe, or non-200 responses fail closed.

## Verification

- Launcher-focused integration tests: **16 passed**.
- Round-2 launcher safety/health tests: **25 passed** (the original 16 plus
  nine regression cases).
- Full state/release integration module: **35 passed**.
- Config/scheduler/environment focused tests: **39 passed**.
- Packaging-focused integration tests: **4 passed**.
- The existing scheduler tests already verify that `OFF` and non-positive
  intervals create no scheduler thread and perform no sweep.
- Launcher source syntax compilation passed.
- The stale-egg-info archive test now uses the approved in-repository output
  root and passed. No release script was changed in this lane.

## Build-time carryovers

1. Run the release lane’s corrected source-binding tests after its output-root
   handling is finalized.
2. Build the frozen executable and run packaged loopback smoke with
   `ARGUS_BROWSER_OPEN=false`; verify no browser process/window is created and
   `/healthz` remains `OFF`, `live_submit=false`, and `trackr_live=false`.
3. Run the equivalent explicit opt-in smoke with `ARGUS_BROWSER_OPEN=true` in a
   disposable loopback test context only.

## Round 3 hostile-review fixes — 2026-08-25

The independent hostile review found that the previous health probe accepted
any non-empty version, followed HTTP redirects, and allowed inherited
`LOCALAPPDATA`/`APPDATA` or unknown `ARGUS_*` path overrides to influence the
runtime.  This round addressed those findings without touching the installed
runtime or live database.

- `_healthy` now requires the exact six-key JSON contract, exact
  `app.version.__version__`, `OFF`, `false`, and an exact loopback health URL.
- Health probes use a no-redirect opener with proxies disabled, so a 3xx or
  external destination cannot become a readiness signal.
- The launcher resolves Windows LocalAppData through the shell known-folder
  API and writes an explicit `ARGUS_DATA_DIR`; inherited OS path variables are
  retained for Windows/Playwright compatibility but cannot select ARGUS data.
- Inherited `ARGUS_*` variables now use an allowlist limited to browser display
  flags and the loopback port.  Unknown provider/watchlist/profile/credential
  overrides are discarded; automation, live submit, Trackr, interval,
  allowlist, and host values are always overwritten with safe values.
- An explicit absolute non-root `data_root` is supported for disposable test
  and source/frozen regression runs.
- Source and frozen launcher regressions verify that both paths receive the
  same sanitized environment.  Playwright cache discovery is tied to the
  trusted known folder rather than inherited path text.

## Round 3 verification

- Launcher/state integration module: **44 passed**.
- Combined state/config/scheduler/packaging gate:
  `tests/unit/test_states.py tests/unit/test_config.py
  tests/unit/test_sweep_lock.py tests/integration/test_state_release.py
  tests/integration/test_packaging.py`: **109 passed in 64.41s**.
- `python -m py_compile launcher.py`, `compileall`, and `git diff --check`
  passed.
- Redirect, stale-version, extra-health-key, inherited-data-root,
  allowlist, in-process, source-child, frozen, and Playwright-cache probes
  all passed.

No build, install, Trackr access, external network, or live database access was
performed in this round.  The frozen executable still requires the separate
release/build gate before installation.
