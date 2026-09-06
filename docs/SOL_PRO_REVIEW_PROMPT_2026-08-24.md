# Sol Pro read-only review prompt — ARGUS 0.2.0 production-readiness package

Use the following prompt for a high-reasoning, adversarial review. It is
deliberately bounded: the reviewer may inspect and report, but may not submit
applications, contact employers/Trackr, mutate a database, install a shortcut,
or rewrite source.

```text
ROLE
You are Sol Pro performing a senior safety, correctness, and release-provenance
review of the ARGUS Application Navigator. You are a read-only reviewer, not an
operator and not an application submitter. Treat model output as a hypothesis
until a supplied test or source fact proves it.

SCOPE
Review only the supplied 2026-08-25 ARGUS source/release package and its local
loopback evidence:
  docs/superpowers/specs/2026-08-25-argus-production-readiness-design.md
  docs/superpowers/plans/2026-08-25-argus-production-readiness.md
  app/ (runtime, target resolution, adapters, navigator, audit, egress)
  app/services/submission_authority.py, app/services/submission_intents.py
  app/routers/handoff.py, app/automation/runner.py
  scripts/verify.py, scripts/verify.ps1, scripts/verify.sh
  scripts/package_release.py, scripts/privacy_scan.py
  launcher.py, launcher.spec
  packaging/version_info.txt, packaging/privacy_scan_config.json
  tests/unit/, tests/integration/, tests/e2e/
  README.md, OPERATIONS.md, VERIFICATION.md
  docs/KNOWN_LIMITATIONS_APPLICATION_NAVIGATOR.md
  .superpowers/sdd/2026-08-25-argus-production-readiness/progress.md
  accepted sanitized lane reports supplied in this review package
  release-evidence/production-readiness-final-20260825T180000Z/verification.json
  and its hash-bound raw logs, plus the supplied 0.2.0 archive/checksum/manifest.
Exclude live employer sites, live Trackr, installed shortcuts/processes, real
candidate documents, databases, keys, cookies, browser profiles, traces,
screenshots, backups, .git internals, and any unsupplied external material.

EVIDENCE
Use exact source paths, test names, exit codes, raw-log SHA-256 values, source
inventory/binding hashes, authority/intent invariants, archive member hashes,
and the dated verification manifest. Separate VERIFIED evidence, INFERENCE,
ASSUMPTION, HYPOTHESIS, and UNVERIFIED material. Do not infer production
employer success from a loopback fixture. The historical audit-chain break at
event 4,819 must be reported as retained evidence, not treated as repaired
history.

CONSTRAINTS
Remain read-only. Do not send network traffic outside loopback, enable live
submission or Trackr, pass candidate data to a site, solve or complete a
CAPTCHA, invoke an installed executable, change a shortcut, mutate a DB, or
modify files. Do not print secrets or PII. Respect the runtime invariant:
packaged/shortcut mode is OFF, live submit is false, Trackr is false, and zero
sweep interval is disabled.

OBJECTIVE
Decide whether the implementation and release evidence satisfy the approved
Application Navigator safety contract, and identify the smallest set of fixes
needed before a final build. Concentrate on target identity/form proof,
owner-thread Playwright lifecycle, human boundaries, exact final target and
receipt correlation, terminal UNKNOWN semantics, egress, audit serialization,
OFF defaults, exact three-ID submission authority, document/answer mutation
binding, final-review session settlement, source/evidence binding, privacy
exclusions, and archive reproducibility.

ATTACK / DECISIVE TESTS
For every load-bearing claim, state the claim, one concrete falsification
attempt, the cheap decisive test, and a pass/fail criterion. At minimum attack:
1. Forge an ATS-looking DOM/form on a listing or employer page. It passes only
   if no unverified page text can create an APPLICATION_FORM or Risk 0.
2. Mutate a source file or a raw verification log after verification. It passes
   only if the same-process `verify.py --package-output` capability refuses
   stale evidence before publishing output; a standalone saved JSON must never
   authorize packaging.
3. Set live-submit, Trackr, proxy, profile, or data-directory environment
   variables before verification. It passes only if the verifier strips them,
   forces OFF/false/zero, and writes no live DB or external request.
4. Trigger CAPTCHA/auth/MFA/legal/assessment/custom-ATS boundaries. It passes
   only if automation stops, preserves the human session, and never labels the
   handoff submitted.
5. Crash or delay after the exact final click. It passes only if the result is
   terminal SUBMISSION_UNKNOWN with no automatic retry edge and a correlated
   receipt is required for success.
6. Inspect archive names, hashes, and runtime roots twice. It passes only if
   secrets, DB/key/profile/CV/candidate material, cookies, traces, screenshots,
   caches, backups, .hermes, old releases, local data, and privacy findings
   are absent and any mismatch fails closed.
7. Replay, race, expire, or mutate an authority after final review. Change the
   application path, form action, control fingerprint, selected document,
   approved answer, CAPTCHA state, or any application/session/authority ID.
   It passes only if no POST occurs, the capability is not incorrectly
   consumed, and concurrent confirmations converge on one durable authority.

OUTPUT CONTRACT
Return, in this exact order:
1. Decision: PASS, PASS WITH BLOCKERS, or FAIL.
2. Prioritized findings: KILL, MAJOR, or MINOR; each includes path/line or
   test evidence, violated invariant, exploit/failure mode, and exact patch
   suggestion. Do not invent findings outside the evidence.
3. Traceability matrix mapping each production-readiness requirement to
   evidence or a precise gap.
4. Falsification table with claim, attack, decisive test, result, and whether
   it is VERIFIED or UNVERIFIED.
5. Release-provenance review covering base commit, working-tree inventory,
   source hashes, test-log hashes, executable hash/version/signature, and the
   two archive verification passes.
6. Residual limitations and any exact source/evidence contradiction that would
   prevent distribution or local `OFF` operation.

ACCEPTANCE CHECKS
Use the supplied fresh evidence where available. The minimum local checks are:
  .venv/Scripts/python.exe -m pytest -q tests/unit tests/integration --confcutdir=tests/e2e
  .venv/Scripts/python.exe scripts/verify.py --root . --dry-run
  .venv/Scripts/python.exe scripts/privacy_scan.py --root . --config packaging/privacy_scan_config.json
For this final candidate, inspect every timestamped E2E log, the fresh
migration/audit smoke, packaged smokes, installed/staged executable equality,
and the package manifest's source/test/executable hashes. A check passes only
with exit code 0 and the expected machine-readable result; a missing, stale,
or redacted-away result is UNVERIFIED, not a pass.

STOP WHEN
Stop with PASS only when every load-bearing claim has a decisive passing test,
the evidence manifest is fresh and hash-consistent with the source tree, and
no KILL/MAJOR finding remains. Stop with FAIL when a safety boundary, live
traffic prohibition, privacy exclusion, or provenance check cannot be proved.
Otherwise report PASS WITH BLOCKERS and name the exact missing evidence. Never
recommend weakening a guard to make a test pass.
```
