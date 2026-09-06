# ARGUS — Internship Application Operating System

ARGUS is a local-first, experimental control centre for high-volume internship applications. It imports opportunities, checks eligibility and employer conflicts, selects approved documents, maps application fields to verified candidate data, runs guarded browser automation, captures submission evidence, and hands control back to the candidate for legal declarations, sensitive questions, CAPTCHAs, online assessments, and interviews.

ARGUS is deliberately **not** a blind clicking bot. The browser may submit only when the completed form is assessed as **Risk 0**, the destination is permitted, every required value has an approved source, and no human-only step is present.

## What is included

- Responsive local dashboard and JSON API.
- Encrypted candidate profile and answer bank.
- Approved CV and cover-letter library with SHA-256 verification.
- CSV, manual URL, and user-triggered browser-extension capture.
- Eligibility, duplicate, graduation-year, sponsorship, and employer-conflict checks.
- Greenhouse, Lever, Workday, and semantic generic ATS inspection.
- Dry-run, review, and guarded submission modes.
- Playwright trace, screenshot, receipt, and hash-linked audit evidence.
- Read-only `.eml` and optional IMAP ingestion for confirmations, OAs, HireVue, interviews, and rejections.
- A synthetic ATS laboratory for standard, sensitive, essay, assessment, and destination-mismatch scenarios.
- Windows PowerShell and Linux/macOS launchers.

## Safety posture

The default configuration binds to `127.0.0.1`, uses `OFF`, leaves live submission disabled, and permits submission only to the built-in loopback ATS laboratory. Scheduled sweeps are disabled by the default `ARGUS_SWEEP_INTERVAL_HOURS=0`; `REVIEW_ONLY` must be selected explicitly. Live Trackr discovery is also disabled. ARGUS never attempts assessments, generates legal attestations, infers work-authorisation facts, bypasses CAPTCHAs, or performs a blind Trackr crawl. Live domains require an explicit automation mode, the live-submit guard, and an exact hostname allowlist.

## Public source status

This repository contains a privacy-sanitised source snapshot, including the
browser/form repairs and explicit disposable launcher sandbox. It is not an
installed upgrade or a signed binary release.

**Current limitation:** autonomous end-to-end applications to real employers
have not been demonstrated. Local/synthetic test success is not evidence of
unattended reliability across public employer portals. Supported form handling
and human handoffs exist. An experimental [controlled preparation MCP bridge](docs/PREPARATION_BRIDGE.md)
is included, together with a [portable operating skill](skills/argus-preparation-bridge/SKILL.md).
Its full application acceptance is incomplete; it is not a general CUA or autonomous
submission bridge. See [the intended end state](docs/ARGUS_HERMES_ENDSTATE.md),
which remains a design document rather than a delivered end-to-end capability.

The public export excludes candidate profiles, CVs, local databases, browser
captures, private operational records, caches, backups, and private Git history.
Personal fixture names and workstation examples are replaced with synthetic
values. The existing MIT license and upstream history are retained. Legacy
CV migration examples are illustrative, not the original candidate's corpus.

[VERIFICATION.md](VERIFICATION.md) records checks for this publication.
Historical documents under `docs/` describe earlier work and are not proof of
this revision's release status. No real applications were submitted as part of
publishing this source.

## Automation modes and trust boundaries

`ARGUS_AUTOMATION_MODE` is the explicit state for scheduled automation:

- `OFF` disables scheduled discovery and automation. Manual inspection and explicitly requested read-only work remain available.
- `REVIEW_ONLY` (explicit opt-in) permits discovery, eligibility checks, preparation, and review, but does not arm live submission.
- `ARMED` permits a scheduled or manual run to request submission after every risk, destination, field, and receipt gate passes.
- `RUNNING` records that an armed run is actively executing. Do not set it merely to make a batch run; return to `REVIEW_ONLY` or `OFF` when the run is complete.

The mode never overrides the browser safety gates. `argus apply ID --mode dry-run` inspects without submission; `--mode review` may navigate and fill only values and destinations trusted by the current policy, but never submits; `--mode submit` is only a request to attempt the guarded path. A real submission is accepted only with Risk 0, approved values, exact employer/role and destination checks, the required live opt-ins, and confirmation/receipt evidence. A button click alone is not proof of an application.

Per-application action-time confirmation is mandatory for submit: the API requires `confirm_application_id` to exactly equal the application id, and `argus apply --mode submit` requires `--confirm-submit ID`. The Applications-page Submit button sends that confirmation automatically after a dialog that names the employer and role — confirm they match before accepting.

Set the mode before starting the service. The legacy `ARGUS_ENABLE_LIVE_SUBMIT` variable is retained for deployments that do not set an explicit mode; when an explicit mode is present, the mode is authoritative. `ARGUS_ENABLE_TRACKR_LIVE` is a separate, default-off opt-in for live Trackr discovery and never authorises submission.

The scheduler interval defaults to `0` hours, which is disabled. Zero, negative, blank, and sub-second values fail closed; a positive interval must be at least one real second (`1/3600` hour) to be schedulable.

The launcher owns one configured loopback port and one data-directory process lock. If either is already in use, it stops with an error; it never silently moves to `8788`, `8789`, or `8790`. Stop the existing instance or choose a deliberate `ARGUS_PORT` before restarting.

## Windows installation

1. Install Python 3.11 or newer and Git.
2. Open PowerShell in the extracted ARGUS directory.
3. Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\start.ps1
```

The dashboard opens at `http://127.0.0.1:8787`.

To load the synthetic demonstration profile and five ATS scenarios:

```powershell
.\.venv\Scripts\python.exe -m app.cli seed
```

## Linux or macOS installation

```bash
./scripts/setup.sh
./scripts/start.sh
```

For a demonstration fixture:

```bash
./.venv/bin/python -m app.cli seed
```

## First real setup

1. Open **Profile** and enter factual candidate information. Work-authorisation wording is encrypted and must be explicitly approved before automation can use it.
2. Open **Documents**, upload the relevant CV variants, tag them, and mark only reviewed versions as approved.
3. Open **Answer Bank** and add evidence-backed reusable answers. Unapproved answers are never supplied to the browser.
4. Import `samples/trackr_import_template.csv`, add a URL manually, or install the capture extension. Live Trackr discovery is a separate explicit opt-in; saved CSV/manual/extension capture does not require it.
5. Evaluate an opportunity, review eligibility and conflict findings, queue it, and prepare its document package.
6. Run **Dry run** first. Use guarded submit only after inspecting the mapped fields and risk result.

## Browser capture extension

The extension captures only the page currently selected by the user. It does not crawl, enumerate, or scrape job boards.

1. Start ARGUS.
2. Print the local capture token:

```powershell
.\.venv\Scripts\python.exe -m app.cli token
```

3. Open Chrome or Edge extension management, enable Developer mode, choose **Load unpacked**, and select the `extension` directory.
4. Open the extension’s **Connection settings** and enter:
   - Base URL: `http://127.0.0.1:8787`
   - Capture token: the value printed above
   - Default cycle: for example `2027`
5. On an employer application page, open ARGUS Capture, verify the employer and role, then send it to the inbox.

The manifest grants access only to the active tab, local extension storage, `localhost`, and `127.0.0.1`.

## Opportunity CSV import

Use the included template or run:

```bash
python -m app.cli import-csv path/to/opportunities.csv --source personal_tracker
```

Required columns are employer, role title, cycle, and URL. Common alternative headings such as `Company`, `Programme`, `Year`, and `Application Link` are normalised. Tracking parameters are removed before duplicate checks.

## CLI reference

```text
argus init                         Create the database, encryption key, and capture token
argus seed                         Create an idempotent synthetic laboratory fixture
argus serve --open                 Start the local command centre
argus token                        Print the local extension capture token
argus audit                        Verify the complete hash-linked audit chain
argus import-csv FILE              Import opportunities from a UTF-8 CSV
argus apply ID --mode dry-run      Inspect and fill without submission
argus apply ID --mode review       Run for human review
argus apply ID --mode submit       Request guarded submission
argus apply ID --mode submit --headed  Keep the browser visible
```

A refused or non-zero-risk run returns a non-zero CLI status. This is intentional and makes ARGUS safe to call from scripts.

## Email and assessment tracking

The **Mail & Assessments** page accepts local `.eml` files up to 5 MiB. ARGUS extracts plain text, classifies the recruitment event, attempts to match it by submission reference, employer, and role, then updates the next action and deadline.

Optional read-only IMAP polling is available through `scripts/poll_imap.py`:

```bash
export ARGUS_IMAP_HOST=imap.example.com
export ARGUS_IMAP_USERNAME=you@example.com
export ARGUS_IMAP_PASSWORD='app-password'
export ARGUS_IMAP_FOLDER=INBOX
python scripts/poll_imap.py
```

The poller opens the selected folder read-only and fetches messages with `BODY.PEEK[]`; it never sends, deletes, archives, labels, or marks messages as read intentionally.

## Live submission configuration

Live mode is intentionally unavailable as a dashboard toggle. Set the mode and exact values before starting ARGUS. Use the real provider hostnames for the portal you have reviewed; the examples below are illustrative:

```powershell
$env:ARGUS_AUTOMATION_MODE = "ARMED"
$env:ARGUS_LIVE_DOMAIN_ALLOWLIST = "jobs.example.com,*.careers.example.com"
$env:ARGUS_ENABLE_TRACKR_LIVE = "false"
.\scripts\start.ps1
```

If no explicit automation mode is set, `ARGUS_ENABLE_LIVE_SUBMIT=true` is the compatibility opt-in for live submission. With an explicit mode, `ARMED` or `RUNNING` is the submission arm; neither path bypasses the other gates. The application must still be prepared, destination-matched, free of human-only questions, and Risk 0. Start with one employer domain and a headed dry run. Portal terms, application declarations, and employer rules remain the candidate’s responsibility.

Allowlist entries are hostnames only—do not include a scheme, path, or port. `jobs.example.com` is an exact match and does not trust sibling or subdomain hosts; a bare `example.com` is also exact, never an implicit suffix. `*.careers.example.com` matches subdomains such as `uk.careers.example.com` but deliberately does not match the apex `careers.example.com`; malformed or broad values such as `*.com`, URLs, and arbitrary `*` entries do not create wildcard trust. Hostname case and a trailing dot are normalised.

To enable live Trackr discovery intentionally, set `$env:ARGUS_ENABLE_TRACKR_LIVE = "true"` for that process and leave it off again when finished. This does not turn on blind crawling, and it does not make any submission safe by itself; mode, destination, risk, and human-handoff controls still apply.

## Local data

Default locations:

- Windows: `%LOCALAPPDATA%\ARGUS`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/argus`
- macOS: `~/.local/share/argus`, unless `ARGUS_DATA_DIR` is set

The directory contains the SQLite database, encryption key, capture token, approved documents, browser traces, and screenshots. Back up the whole directory together. Losing `secret.key` makes encrypted fields unrecoverable; exposing it together with the database exposes those fields.

## Development and verification

```bash
./scripts/verify.sh
```

or on Windows:

```powershell
.\scripts\verify.ps1
```

The verification command disables unrelated globally installed pytest plugins, runs the unit/integration/browser suite, compiles all Python modules, and checks the CLI entry point. It writes fresh raw logs and a machine-readable manifest below `release-evidence/`. A missing, stale, or manually edited manifest is not release evidence.

## Versioned builds, manifests, and signing

Keep the release version aligned in `pyproject.toml`, `app/version.py`, `packaging/version_info.txt`, and `launcher.spec`. Build the Windows executable first, then package only through the same verifier process. The in-memory capability issued by `verify.py` is deliberately never serialized; a later standalone `package_release.py --test-evidence ...` invocation cannot authorize a Git working-tree release.

For a 0.2.0 release, after a fresh executable build, run:

```powershell
.\.venv\Scripts\python.exe scripts\verify.py `
  --root . `
  --evidence-dir release-evidence `
  --package-output release-artifacts\argus-0.2.0 `
  --package-version 0.2.0 `
  --package-executable dist\ARGUS.exe `
  --package-executable-version 0.2.0.0 `
  --package-allow-unsigned-executable
```

This runs the required checks and, only after they pass in that same process, writes a deterministic, secret-free archive, checksum, and manifest under the approved `release-artifacts/` root. The unsigned-executable flag is an explicit acknowledgement, not a trust claim. For an already signed executable, supply its verified signature inputs instead of acknowledging unsigned output.

Then run the packaged smoke against the exact archive produced above and retain that fresh evidence alongside the package:

```powershell
.\.venv\Scripts\python.exe scripts\verify.py `
  --root . `
  --packaged-smoke release-artifacts\argus-0.2.0\argus-0.2.0.zip `
  --evidence-dir release-evidence
```

The archive manifest binds the source inventory, raw verification logs, executable metadata, and privacy policy. Detached manifest signing, if required by the distribution process, is a separate cryptographic artifact and must not be described as present until its signature and verification have succeeded. Never pass private-key material or candidate data on a command line.

Build the Windows executable with the version resource, then use the separate Authenticode hook. Install the release certificate in the Windows `My` certificate store through your approved certificate-management process and pass only its thumbprint:

```powershell
.\.venv\Scripts\pyinstaller.exe launcher.spec --noconfirm --clean
.\.venv\Scripts\python.exe scripts\sign_release.py `
  --exe dist\ARGUS.exe `
  --thumbprint HEX `
  --store My `
  --timestamp-url https://<approved-timestamp-authority>/
```

The signing hook intentionally refuses `--pfx`: SignTool’s PFX password option is a command-line argument, and this workflow does not assume an environment-variable or stdin password channel. It fails closed before a signing process starts. The thumbprint path keeps the private password out of the command line; success still requires SignTool and `signtool verify /pa`. A detached manifest signature and an executable Authenticode signature are different artifacts; neither should be described as present until its verification command succeeds. See `docs/UPGRADE_0.2.0.md` for the release/upgrade checklist.

## Project structure

```text
app/domain/        Pure state, eligibility, conflict, question, and risk decisions
app/services/      Candidate data, documents, opportunities, orchestration, mail, dashboard
app/automation/    Question mapping, ATS adapters, Playwright runner, receipt verification
app/routers/       Local API, pages, mail, and synthetic ATS laboratory
app/templates/     Server-rendered command-centre interface
extension/         Manifest V3 user-triggered capture extension
scripts/           Setup, launch, IMAP, verification, and packaging tools
tests/             Unit, integration, browser, and adversarial verification
```

## Boundaries and limitations

Real employer portals change without notice. ARGUS includes dedicated adapters and a conservative semantic fallback, but a new or changed portal can require a new mapping. Synthetic ATS testing proves the execution and safety contracts; it does not certify every external site. Authentication, MFA, CAPTCHA, legal declarations, OAs, HireVue, and interviews remain human-controlled. Never enable broad live-domain access merely to make a failing portal proceed.

See `SECURITY.md` for the threat model and `OPERATIONS.md` for backup, recovery, token rotation, browser troubleshooting, and release procedures.
