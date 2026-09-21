# Integrated source verification — 2026-09-21

This records three separate scopes: the frozen integrated candidate, its
privacy-sanitised public source export, and the authorised local installation.
It is not certification of autonomous real-employer applications or a signed
binary release. Earlier publication evidence, including its unresolved failure
mechanisms, is preserved in [the previous report](docs/VERIFICATION_PUBLIC_20260906.md).

## Fresh public-source checks

Environment: Windows, Python 3.13.13, existing development dependencies and
Chromium. Test runs used fresh external data/cache/temp roots before importing
ARGUS; the private applicant database and browser profiles were not test inputs.

- Full collection: **3,096 tests collected**, exit 0.
- Non-E2E source regressions: **2,989 passed, 2 skipped, 0 failures/errors** across
  **2,991 unique test identities**, exit 0. Application and test source hashes
  were unchanged during the run.
- Final trailing-whitespace cleanup touched one application module and five
  test files. The affected/dependent regression replay then returned **151
  passed**, exit 0, with unchanged source hashes. Removing a final empty line
  from the tracker stylesheet was followed by **175 passing tracker tests**.
  These are repeated tests already in the non-E2E run, not additional distinct
  coverage.
- Production privacy scanner: exit 0.
- Full-tree redacted Gitleaks: no findings after narrowly annotating reviewed
  synthetic fixture identifiers and deliberately fake redaction-test secrets.
- Candidate-name scan: no remaining candidate-name findings. Workstation and
  applicant examples are synthetic; private operational artifacts are excluded.
- Upstream license and public history are retained; no private local Git history
  was imported.

The two skips are explicit:

1. Windows symlink creation requires a privilege unavailable in this environment.
2. The live Greenhouse smoke is opt-in and `ARGUS_NET_TESTS=1` was not enabled.

A browser-inclusive public-export replay was started and deliberately stopped
while repeating the long browser E2Es. **It was not passed.** The completed
non-E2E run above and the installed UI check below do not certify all public-
export browser E2Es. The required E2E inventory still includes the local-portal
and static-legal suites and retains strict missing/unexpected-file rejection.
No test was removed from that inventory to make publication pass.

## Frozen integrated candidate evidence

Before public-source sanitisation, the integrated candidate's complete raw JUnit
contained **3,093 passed, 2 skipped and 1 expected failure**, with no failures or
errors, across **3,096 unique identities**. These counts were recomputed from
that retained JUnit for this publication; they are not a new execution against
the public export. The expected failure is the direct month-field case where a
year alone is not valid `YYYY-MM` evidence.

PyInstaller produced the executable successfully. Its separate fresh-sandbox
smoke verified OFF mode, both live flags false, HTTP 200 for the dashboard and
static assets, ownership of the listener, and teardown of the owned runtime.
The candidate source stayed frozen. Public sanitisation and documented
whitespace cleanup are separate from that binary's build provenance.

## Authorised local installation

The maintainer separately authorised installation. The previous executable and
launcher wrapper were retained with verified hashes and the live SQLite database
was backed up through SQLite's online-backup API. No keys or human browser
profiles were copied. Empty handoffs and no unfinished automation runs were
verified before the exact old ARGUS process tree was stopped.

The effective Start Menu/Startup wrapper now launches the tested executable,
not the stale source launcher. Installed and tested executable hashes match.
The installed process owns the intended loopback listener; health is
`REVIEW_ONLY`, `live_submit=false`, `trackr_live=false`. Periodic sweeps are set
to zero for this installation posture. Dashboard/CSS/JavaScript return HTTP 200,
and an isolated headless browser rendered the installed dashboard with no
JavaScript page errors.

Database integrity, schema version and every table's row count were unchanged.
The current audit epoch remained valid. A retained historical audit-chain break
was present before maintenance and is still reported; it was not rewritten or
misrepresented as a new installation failure. Companion bridge processes were
not replaced by this executable update.

The browser-use transport timed out; the successful installed UI evidence came
from the separate isolated Playwright check, not from that failed transport.
A post-stop HTTP timeout also occurred in the maintenance helper; native process
and listener checks proved shutdown before the staged replacement resumed.

## Reproduction and remaining limits

Use a disposable environment and synthetic loopback destinations only. Do not
run tests against an existing applicant database or human browser handoff.

```text
python -m pytest tests --collect-only -q -p no:cacheprovider
python -m pytest tests --ignore=tests/e2e -q -p no:cacheprovider
python scripts/privacy_scan.py --root .
gitleaks dir . --redact --no-banner
git diff --cached --check
```

Set the safe child environment before importing ARGUS; use
`scripts.verify.safe_environment(Path.cwd())` or an equivalently isolated
launcher with fresh external data, temp and bytecode-cache roots. The full
release entry points remain `scripts/verify.ps1` and `scripts/verify.sh`.

Real-employer ARGUS + Hermes operation has not been demonstrated by these
checks. Interactive AI assistance is an intended operating model, but a
notification backend is not proof of a working CUA form-filling connection.
Upload-caller/handoff/manifest qualification, consolidated query-bearing target
binding qualification, and a valid hard-process-crash receipt test remain
separate outstanding evidence. No real application was submitted during this
publication or installation.

This repository has no configured GitHub Actions workflow; the reported checks
are local execution, not a claim of passing GitHub CI. Raw local logs, manifests,
JUnit, screenshots and installation backups remain outside the public tree to
avoid publishing applicant data and workstation paths.
