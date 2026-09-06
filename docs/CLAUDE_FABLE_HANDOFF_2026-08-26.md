# ARGUS handover for Claude Fable

Date: 2026-08-26 (Europe/London)  
Project: `%USERPROFILE%\Downloads\argus-0.1.0\ARGUS-0.1.0`  
Private runtime: `%LOCALAPPDATA%\ARGUS`  
Dashboard: `http://127.0.0.1:8787`

## Stop point

Codex was asked to pause and hand the work to Claude Fable. Do not assume the
last audit-CLI change is verified: its test was first run red, the implementation
was then written, and the user paused the task before the green rerun.

Do not submit a real application during takeover. The running server is deliberately
`REVIEW_ONLY`, `live_submit=false`, `trackr_live=false`, headed, with no active
Navigator session. At the handover check it was healthy on port 8787 under PID
39448, and drive C had about 165.37 GB free.

## User's actual objective

ARGUS must open the exact application—not a listing or wrong role—prefill only
approved evidence, attach the approved CV, and hand the same visible employer
browser to the user for CAPTCHAs, legal/sensitive questions, ambiguous fields, or
anything not safely resolved. It must never claim submission without an exact
provider receipt, and it must not retry an unknown submission.

## Live Point72 application used for verification

- Application ID: `06b89385-194d-41d8-b348-eaa38b572878`
- Opportunity ID: `c9101e9e-fcba-448b-91ce-e82076920408`
- Role: `2026 Warsaw MI Data – Web Scraping Internship`
- Source URL: `https://boards.greenhouse.io/point72/jobs/8423978002?gh_jid=8423978002`
- Verified application URL: `https://job-boards.greenhouse.io/point72/jobs/8423978002?gh_jid=8423978002`
- Provider/requisition: `greenhouse` / `8423978002`

The target is now persisted as an exact, identity-verified Greenhouse application
form. The old `Source-resolution application/session binding was inconsistent`
loop no longer occurred in the live retest.

## Proven live behavior

The final live prefill run before the pause used run ID
`a43d617b-3b8a-44a6-a267-daf0a4a4729d` and Navigator session
`4f7baa0b383f462c` (subsequently cancelled and cleaned up).

Evidence:

- Screenshot: `%LOCALAPPDATA%\ARGUS\artifacts\screenshots\a43d617b-3b8a-44a6-a267-daf0a4a4729d.png`
- Trace: `%LOCALAPPDATA%\ARGUS\artifacts\traces\a43d617b-3b8a-44a6-a267-daf0a4a4729d.zip`
- Greenhouse presigned-field bootstrap returned HTTP 200.
- The approved DOCX upload POST to the exact provider S3 bucket returned HTTP
  201 Created.
- The employer form visibly showed the approved CV filename attached.
- First name, last name, email, phone, country code, sponsorship answer, and
  graduation year were populated.
- `submission` remained `not_clicked`; no application was submitted.
- The session ended in a truthful user handoff, not a success claim.

## Important unresolved facts on this application

Do not submit this Point72 role. Its advert says expected completion in 2026,
while the saved candidate profile currently says graduation year 2029. ARGUS's
stored eligibility result is currently `eligible=true`, so this mismatch needs
an eligibility/review fix before this record could ever be considered safe to
submit.

The live form still handed off these fields:

- Candidate location: Greenhouse calls
  `https://api-geocode-earth-proxy.greenhouse.io/v1/autocomplete`; ARGUS currently
  blocks it. A future exception must be exact-host/path/method, PREFILL-only, and
  bound to the active approved city value.
- Degree: Greenhouse calls
  `https://boards.greenhouse.io/v1/boards/point72/education/degrees`; ARGUS
  currently blocks it. Any exception must be exact and bound to the active degree
  field/search value.
- Work authorization: the client-side dropdown selection still failed and needs
  a focused adapter reproduction.
- Employer-specific required questions remain deliberately unresolved for human
  review.

These are not grounds to weaken the egress guard. A manual same-browser handoff is
the correct fallback until exact bounded behavior is implemented and tested.

## Main fixes already made

1. Greenhouse `gh_jid` is no longer falsely classified as candidate PII.
2. Current Greenhouse React/Remix loader data is used to bind employer, role, and
   requisition identity.
3. Passive reCAPTCHA configuration/badges/hidden response fields are no longer
   treated as active human challenges; real visible challenges still hand off.
4. `job-boards.eu.greenhouse.io` support was added to target, egress, and scout
   handling.
5. Inline-network detection no longer mistakes ordinary Remix asset URLs for
   scripted exfiltration.
6. Greenhouse application-root binding tolerates its embedded search widget and
   ID-only fields.
7. React comboboxes are inspected/filled/verified explicitly, with conservative
   degree aliases.
8. PREFILL now safely fills resolved fields before handing off unresolved ones;
   it does not click Next or Submit on that branch.
9. University-policy questions are no longer misclassified as the university
   name.
10. Exact Greenhouse runtime CDN assets and locale JSON are allowed read-only.
11. The exact `presigned_fields` bootstrap is allowed only for resume/cover-letter
    descriptors from the verified Greenhouse origin.
12. Approved document upload is bound to an active approved document manifest,
    exact filename/hash/type/origin, exact S3 bucket, PREFILL mode, and multipart
    POST.
13. A delayed browser event can no longer erase a target-path mutation after the
    exact manifest was displayed. The production Navigator seals the displayed
    manifest; path/action/control mutation tests pass fail-closed.
14. Backup artifacts were moved under `backups\2026-08-26` (excluded from release
    privacy scanning) instead of leaving PII-bearing `.bak` files in production
    source paths.

## Test status before the final paused CLI change

These were all rerun against the Greenhouse and manifest-sealing code and passed:

- Unit: `755 passed, 2 skipped` (expected Windows symlink privilege and opt-in
  live-network skips).
- Integration: `204 passed`.
- E2E browser: `83 passed`.
- Focused manifest mutation group: `3 passed` for path/action/control mutation.
- Focused manifest/authority UI group: `7 passed`.

Warnings were non-fatal: Starlette's `httpx` deprecation warning and an intentional
duplicate ZIP-member warning in the archive-limit test.

## Gemini report assessment

The supplied Gemini report was partly stale:

- SmartRecruiters and Workable adapters already exist.
- Workday's Apply Manually bridge and tests already exist.
- The old process/PID and test counts were stale.
- EU Greenhouse was a real gap and was fixed.
- `grnh.se` cannot be trusted as Greenhouse wholesale; observed redirects can go
  to Greenhouse or custom employer sites and must be resolved individually.
- The audit-history observation was accurate: epoch 0 remains historically broken
  at event 4819, while the recovered current epoch is valid.

## Paused, not-yet-verified audit CLI change

The last code change, made immediately before this handover, modifies `app/cli.py`
and `tests/integration/test_cli.py` so that:

- `argus audit` defaults to the durable current epoch and states that historical
  epochs were not included.
- `argus audit --epoch N` verifies one explicit epoch.
- `argus audit --all-epochs` preserves the honest historical failure.
- Negative or nonexistent epochs are refused.

The new test
`test_audit_cli_defaults_to_current_epoch_without_hiding_broken_history` was first
run before implementation and correctly failed red. The implementation was then
written. No green run has happened yet. Treat this as unverified code.

## Exact next actions for Claude Fable

1. Work only in the existing project; do not reset, clean, or overwrite the dirty
   worktree. Many changes are intentional and uncommitted.
2. First run the paused focused test:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q tests\integration\test_cli.py::test_audit_cli_defaults_to_current_epoch_without_hiding_broken_history -p no:cacheprovider
   ```

3. If green, run:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q tests\integration\test_cli.py tests\integration\test_audit_epochs.py tests\integration\test_audit_review_fixes.py -p no:cacheprovider
   .\.venv\Scripts\python.exe -m app.cli audit
   .\.venv\Scripts\python.exe -m app.cli audit --epoch 1
   .\.venv\Scripts\python.exe -m app.cli audit --all-epochs
   ```

   Expected production behavior: the first two validate current epoch 1; the last
   exits nonzero and reports the preserved historical break at event 4819.

4. If any audit code changes, rerun all unit, integration, and E2E suites before
   claiming completion. The last fully green totals above predate only the paused
   audit-CLI patch.
5. Recheck `http://127.0.0.1:8787/healthz`. Keep the server REVIEW_ONLY with live
   submit and Trackr disabled. Do not arm or submit anything without a new explicit
   user instruction and a role-eligibility review.
6. If continuing application work, fix the 2026-vs-2029 eligibility mismatch before
   expanding optional location/degree lookups. Write tests first and keep the
   same-browser human handoff as the fail-closed fallback.
7. Update this handover or create a new dated worklog before further handoff.

## Files central to this round

- `app/services/navigator.py`
- `app/automation/runner.py`
- `app/automation/adapters/generic.py`
- `app/automation/adapters/greenhouse.py`
- `app/automation/targets.py`
- `app/automation/host_policy.py`
- `app/automation/classifier.py`
- `app/scouting/service.py`
- `app/routers/handoff.py`
- `app/cli.py` (latest change unverified)
- `tests/unit/test_navigator_source_resolution_guards.py`
- `tests/unit/test_application_root_scoping.py`
- `tests/unit/test_runner_review_target_revalidation.py`
- `tests/unit/test_classifier_education_fields.py`
- `tests/e2e/test_application_navigator_journeys.py`
- `tests/integration/test_cli.py` (latest test not yet rerun green)

Backups are under `backups\2026-08-26`. Preserve unrelated/user edits and do not
use destructive Git commands.
