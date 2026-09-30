# ARGUS Operations Manual

## Normal operating sequence

1. Start ARGUS with `scripts/start.ps1` on Windows or `scripts/start.sh` on Linux/macOS.
2. Check **Settings**: local bind, `OFF`/`REVIEW_ONLY`/`ARMED`/`RUNNING` mode, dry-run default, expected data directory, and no unintended live domains or Trackr opt-in.
3. Check **Audit Trail** for a valid chain.
4. Import or capture opportunities.
5. Evaluate eligibility and employer conflicts before queueing.
6. Prepare documents and inspect the selected CV/cover letter.
7. Run a dry run, review mapped fields and blockers, then use guarded submission only where appropriate.
8. Verify the confirmation reference and evidence paths.
9. Ingest confirmation/OA email and work only the **Needs You** queue.

## Start and stop

### Windows

```powershell
.\scripts\start.ps1
```

Stop with `Ctrl+C` in the PowerShell window. Avoid killing Python during a submission click; wait for the run to complete or for a human handoff state.

### Linux/macOS

```bash
./scripts/start.sh
```

The default bind is `127.0.0.1:8787`. To change the port:

```bash
ARGUS_PORT=8790 ./scripts/start.sh
```

ARGUS owns exactly one configured loopback port and one process lock per data directory. If the configured port or runtime lock is already in use, startup fails and reports the owner; the launcher never silently falls back to `8788`, `8789`, or `8790`. Stop the existing process cleanly, or set an intentional `ARGUS_PORT` and use the matching health URL. A stale lock file is diagnostic metadata—the operating-system lock, not file existence, is authoritative.

## Health check

```bash
curl http://127.0.0.1:8787/healthz
```

Expected response:

```json
{"status":"ok","service":"ARGUS","live_submit":false}
```

## Needs You notifications

Notifications are disabled by default. To send human-blocking transitions through an
existing local Hermes channel, enable notifications and select exactly one supported
target (`telegram`, `slack`, `signal`, or `discord`) before starting ARGUS:

```powershell
$env:ARGUS_ENABLE_NOTIFICATIONS = "true"
$env:ARGUS_HERMES_NOTIFY_TARGET = "telegram"
.\scripts\start.ps1
```

`ARGUS_HERMES_BIN` is optional and defaults to `hermes`; set it only when the executable
is not available on `PATH`. Leave `ARGUS_HERMES_NOTIFY_TARGET` unset to disable this
backend. An unsupported or explicitly empty target is rejected with a warning and does
not invoke Hermes.

Delivery uses the argument-list form `hermes send -t <target> <message>` with a five-second
timeout and no shell. The message is one argv element. Hermes output is discarded, and a
missing binary, timeout, or non-zero exit is warning-only: the application transition and
run continue. Hermes composes with `ARGUS_NTFY_TOPIC` and `ARGUS_NOTIFY_WEBHOOK_URL`; every
configured backend is attempted independently.

## Backups

Two supported paths. The hot path needs no shutdown; the manual path captures
everything including evidence files.

**Hot backup (no shutdown):** press **Back up now** on Control, or
`POST /api/backup`. It snapshots the database through the SQLite online
backup API plus `secret.key`, `api_token.txt`, and `documents/` into
`<data-dir>/backups/argus-backup-YYYYMMDDTHHMMSSZ/`, with a `manifest.json`
(sha256 per file, named exclusions). Caches, logs, traces, screenshots, locks,
and notification state are excluded. The newest 5 snapshots are retained.
`GET /api/backup` reports the latest snapshot for freshness checks.

**Manual full copy (includes evidence):** stop ARGUS, then copy the complete
data directory as one unit. A consistent backup contains:

- `argus.db`
- `secret.key`
- `api_token.txt`
- `documents/`
- `artifacts/`

Recommended naming: `argus-backup-YYYY-MM-DD-HHMM`. Store the backup on encrypted media. Test restoration periodically by setting `ARGUS_DATA_DIR` to a temporary restored copy and running `argus audit`.

## Restoration

1. Stop ARGUS.
2. Move the damaged data directory aside; do not overwrite it.
3. Restore the complete backup to a new directory.
4. Set `ARGUS_DATA_DIR` to the restored path.
5. Run `python -m app.cli audit`.
6. Start ARGUS and verify profile, documents, opportunity counts, and recent receipts.
7. Rotate the capture token if the original location may have been exposed.

## Database integrity

ARGUS uses SQLite. Never edit application states, audit hashes, or encrypted fields directly. For a read-only diagnostic copy:

```bash
sqlite3 /path/to/argus.db ".backup /tmp/argus-diagnostic.db"
```

A valid SQLite file does not guarantee an intact audit chain; run `argus audit` separately.

## Accidental requeue repair

`requeue_blocked.py` repairs only the evidence-backed batch emitted around `2026-08-22T23:34:31Z`. Planning is the default and is read-only:

```powershell
.\.venv\Scripts\python.exe requeue_blocked.py --data-dir "$env:ARGUS_DATA_DIR"
```

```bash
./.venv/bin/python requeue_blocked.py --data-dir "$ARGUS_DATA_DIR"
```

The dry run reports the immutable `application.requeued` audit rows selected by the batch timestamp, actor, entity type, and reason. It does not write the source database. Apply only after reviewing that report and preserving a complete data-directory backup:

```powershell
.\.venv\Scripts\python.exe requeue_blocked.py --data-dir "$env:ARGUS_DATA_DIR" --apply
```

An apply creates a consistent SQLite backup under `<data-dir>\backups\argus-requeue-repair-<timestamp>-<id>.db`, checks SQLite integrity and the audit chain before and after, appends repair evidence, and performs only `ELIGIBILITY_CHECKED -> BLOCKED`. Rows already `BLOCKED`, rows that advanced (including unresolved R4 states), missing rows, and already repaired events are skipped; the command is idempotent. It never downgrades an advanced or unresolved R4 application.

To restore after an applied repair, stop ARGUS and work on a copy. The generated file is a database-only recovery point; for a complete recovery use the matching whole-directory backup containing `secret.key`, `api_token.txt`, documents, and artifacts. Copy the generated `argus-requeue-repair-*.db` to `argus.db` inside a new copied data directory (preserve the original data directory), set `ARGUS_DATA_DIR` to that copy, run `python -m app.cli audit`, and inspect the applications before restarting. Never overwrite the live database in place or restore a database without its matching encryption key.

## Browser failures

### Browser executable missing

Run:

```bash
python -m playwright install chromium
```

On Linux, system libraries may also be required. Use the Playwright installation guidance for the host distribution rather than weakening sandbox controls globally.

### Portal changed

Symptoms include unknown required fields, generic-adapter risk, missing submit control, or no recognised receipt. Keep the run in dry-run/headed mode, preserve the trace, and update the relevant adapter. Do not broaden the live allowlist or lower the risk score to force progress.

### CAPTCHA, MFA, or OA encountered

This is an expected handoff, not a failure. Complete the human step in the visible browser where appropriate, or record the OA deadline in the queue. ARGUS must not automate the assessment itself.

### Stale browser process

Stop ARGUS cleanly. Confirm no application run is active, then close the orphaned Chromium process through the operating system. Restart ARGUS and repeat a dry run; never reuse an uncertain partially submitted state without checking the employer portal and mail.

## Traces and screenshots

Evidence is stored under `artifacts/traces` and `artifacts/screenshots` inside the data directory. These files can contain personal information visible on application pages. Treat them as sensitive, retain only as long as operationally necessary, and redact before sharing.

## Email operations

For `.eml`, export a single message and upload it on **Mail & Assessments**. For IMAP, use a dedicated app password where supported and pass credentials through the environment. The poller inspects the newest 200 message identifiers in the selected folder.

After ingestion, verify the matched employer/application and deadline. An ambiguous employer name or missing reference causes no match; open **Activity → Mail & Assessments** and use the per-message **Bind** form to attach it to the exact scored candidate after reviewing the content. Binding is confirmed, audited (`email.match_overridden`), and never rewrites a past application state — a transition the message already caused elsewhere is left standing.

## Live submission runbook

1. Confirm a current backup and valid audit chain.
2. Set `ARGUS_AUTOMATION_MODE=ARMED` for the deliberate batch. If no explicit mode is configured, set the compatibility opt-in `ARGUS_ENABLE_LIVE_SUBMIT=true`; an explicit mode is authoritative.
3. Set `ARGUS_LIVE_DOMAIN_ALLOWLIST` to one exact ATS hostname, or an intentional `*.example.com` subdomain pattern. Entries are hostnames only: a bare host is exact, and `*.example.com` does not match the apex `example.com`.
4. Leave `ARGUS_ENABLE_TRACKR_LIVE=false` unless live Trackr discovery is separately approved; setting it to true does not arm submission.
5. Restart ARGUS and confirm the red live-mode indicator.
6. Open one prepared application and run headed dry-run/review.
7. Verify employer, role, location, selected files, every mapped field, and risk 0. Review/fill is not submit permission.
8. Submit one application only through the guarded submit mode.
9. Confirm a receipt/reference and confirmation email. A click without confirmation is not a successful application.
10. Review the trace, screenshot, audit event, and portal status before processing another role.
11. Remove live-mode and Trackr variables and restart when the batch is complete.

## Audit-chain incident runbook

1. Stop writes by stopping ARGUS.
2. Copy the data directory and preserve timestamps.
3. Run `argus audit` against the copy and record the broken event ID.
4. Compare with the most recent known-good backup.
5. Do not delete the broken chain or regenerate hashes; that destroys evidence.
6. Restore a known-good complete backup if operational continuity is required.
7. Reconcile any applications submitted after that backup using employer portals and confirmation emails.

## Release build and signing

Release files are deterministic and must carry an explicit version. Keep `pyproject.toml`, `app/version.py`, `packaging/version_info.txt`, and `launcher.spec` aligned. Build the Windows executable first. Then run the verifier and package from the same process; do not use a saved JSON file as authority for a later standalone `scripts/package_release.py --test-evidence ...` call. The verifier's in-memory capability is intentionally not serialised and prevents stale or forged evidence from authorising a release.

For 0.2.0, use an approved output root such as `release-artifacts/`:

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

The command must pass every source/privacy/migration check before it creates `argus-0.2.0.zip`, its `.sha256` checksum, and `argus-0.2.0.manifest.json` under the package output directory. The unsigned-executable option is an explicit acknowledgement and is not a signature claim. For a signed executable, provide its verified signature inputs instead. The allowlisted packager excludes runtime databases, keys, tokens, traces, screenshots, and other secrets.

Run the packaged smoke as a separate fresh verification against the exact archive produced by that invocation:

```powershell
.\.venv\Scripts\python.exe scripts\verify.py `
  --root . `
  --packaged-smoke release-artifacts\argus-0.2.0\argus-0.2.0.zip `
  --evidence-dir release-evidence
```

If a detached manifest signature is required, retain it as a separate cryptographic artifact and record it only after the signing and verification commands succeed. Never put private-key material or candidate data in a command line. Authenticode and detached manifest signatures are distinct claims; neither is present merely because a file exists.

Build and Authenticode-sign the Windows executable separately. Install the release certificate into the Windows `My` certificate store through the approved certificate-management process, then pass only its thumbprint:

```powershell
.\.venv\Scripts\pyinstaller.exe launcher.spec --noconfirm --clean
.\.venv\Scripts\python.exe scripts\sign_release.py `
  --exe dist\ARGUS.exe `
  --thumbprint HEX `
  --store My `
  --timestamp-url https://<approved-timestamp-authority>/
```

The hook refuses `--pfx` because SignTool would place the PFX password on the command line; it fails closed before invoking SignTool. Use the certificate-store thumbprint path instead. It refuses to claim success unless SignTool completes and `signtool verify /pa` succeeds. Detached manifest signing is not Authenticode; record and verify both artifacts separately. See `docs/UPGRADE_0.2.0.md` for the release handoff checklist.

## Verified 2026-08-25 local installation

The installed candidate is bound to the staged build by SHA-256
`9AF8687C4502D8DA7A26D4ACD7F16BCA69A8CB7EB3FA0FA6CD81115B6D6E7494`.
Its size is 68,244,681 bytes, FileVersion is `0.2.0.0`, ProductVersion is
`0.2.0`, and Authenticode state is `NotSigned`. The unsigned state is an
explicitly acknowledged limitation, not a signature claim.

Before replacement, an online SQLite backup and exact copies of the old
executable, safe wrapper, desktop shortcut, API token, and encryption key were
stored in a private recovery directory outside all public release inputs. The
old and recovery executable hashes were proved equal before stopping only the
two resolved ARGUS PIDs. The safe wrapper and shortcut were preserved.

After the final HTTP transaction-visibility correction, a second complete
recovery set and schema-v6 online backup were created before the corrected
candidate replaced the first safe build. Both recovery generations remain
private and excluded from public artifacts.

Post-install checks proved:

- listener `127.0.0.1:8787` only;
- health `status=ok`, `service=ARGUS`, version `0.2.0`, automation `OFF`, live
  submission false, and Trackr false;
- installed/staged SHA-256 equality;
- SQLite integrity `ok`, zero foreign-key violations, and schema v6;
- preserved counts of 362 opportunities, 359 applications, 602 automation
  runs, and 5,939 historical audit events;
- empty new submission-intent, authority, and review-binding tables; and
- the pre-existing historical audit-chain fork remains visible at event 4,819
  after 4,818 valid preceding events.

Never “repair” that historical fork by rewriting hashes. Keep the private
online backup as the rollback point. If installed health, hash, or database
integrity later fails, stop only the exact resolved ARGUS PIDs, restore the
paired old executable and data recovery set, and restart with the same
`OFF`/false/false wrapper values.

## Upgrade procedure

1. Back up the data directory.
2. Extract the new ARGUS release into a new code directory; do not place it inside the data directory.
3. Recreate or update the virtual environment with the setup script.
4. Point the new release at a copied data directory first.
5. Run the full verification script and `argus audit`.
6. Start the dashboard and run the synthetic standard/sensitive/assessment scenarios.
7. Switch to the production data path only after verification.

The 0.2.0 upgrade adds explicit automation modes, default-off live Trackr discovery, exact-host policy, and single-instance startup behavior. Follow `docs/UPGRADE_0.2.0.md`; do not treat a version upgrade as permission to enable live submission.

## Uninstall

Remove the code directory and browser extension. The data directory is deliberately not deleted automatically. Securely retain or delete it according to your data-retention requirements.
