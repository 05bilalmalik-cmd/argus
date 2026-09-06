# ARGUS Security Model

## Security objective

ARGUS is designed to reduce repetitive application work without allowing an automation error, hallucinated fact, compromised browser session, or accidental click to create an unauthorised submission. Its primary security boundary is **local execution plus fail-closed decision gates**.

## Protected assets

- Candidate identity, contact, education, work-authorisation, and sponsorship data.
- Approved written answers and supporting evidence.
- CVs and cover letters.
- Application sessions, receipts, traces, and screenshots.
- Capture API token and encryption key.
- Employer-specific application choices and conflict rules.

## Trust boundaries

1. **Dashboard and API:** bound to `127.0.0.1` by default. Do not expose the port through router forwarding, tunnels, public reverse proxies, or permissive container bindings.
2. **Capture extension:** can read the active tab’s URL/title when invoked and can contact only `localhost` or `127.0.0.1`. It cannot crawl arbitrary pages or execute scripts in them.
3. **Browser automation:** receives resolved values from approved profile/document/answer services. Question classification does not grant authority to invent an answer.
4. **Optional Ollama:** receives question wording and allowed canonical keys for classification. It is not authorised to supply candidate facts or final application answers.
5. **External ATS:** untrusted and mutable. Destination identity, detected form fields, risk, submission permission, and confirmation evidence are checked independently.

## Data protection

Sensitive profile and answer values are encrypted using a locally generated Fernet key. Files are stored under the private ARGUS data directory with sanitised names and recorded SHA-256 hashes. Browser question records store redacted previews rather than sensitive answers. Audit events are hash-linked so later mutation is detectable.

Filesystem encryption such as BitLocker, FileVault, or LUKS remains strongly recommended. Application-level encryption cannot protect data while ARGUS is running under a compromised user account.

## Automation modes and trust

`ARGUS_AUTOMATION_MODE` is an explicit operational control for scheduled automation:

- `OFF` disables scheduled discovery and automation.
- `REVIEW_ONLY` (explicit opt-in) permits discovery, eligibility, preparation, and review without arming live submission.
- `ARMED` permits a caller to request the guarded submission path after the independent controls below pass.
- `RUNNING` denotes an armed run currently executing; it is not a substitute for the individual submission gates.

The scheduler interval defaults to `0` hours and is disabled at that value. Zero,
negative, blank, and sub-second values fail closed; a positive interval must be
at least one real second (`1/3600` hour) before a scheduler thread may start.

The CLI run mode is a second, per-run intent. `dry-run` inspects and fills without submission; `review` may inspect and fill only approved values on destinations trusted by host policy, but never submits; `submit` requests a guarded submission. Neither the global mode nor the CLI mode can invent candidate facts, approve a sensitive/legal response, bypass a CAPTCHA or assessment, or treat a clicked button as proof.

## Submission controls

Automatic submit requires all of the following:

- risk level exactly 0;
- an explicit `ARMED` or `RUNNING` automation mode for scheduled/live operation (the legacy live-submit flag remains the compatibility opt-in when no explicit mode is configured);
- an application in the correct prepared state;
- a recognised or explicitly reviewed destination;
- exact employer/role destination consistency;
- all required values resolved from approved sources;
- no legal attestation, sensitive demographic policy gap, CAPTCHA, OA, or video task;
- a loopback destination in the built-in laboratory, or a live submission destination explicitly enabled with an exact hostname allowlisted;
- a recognised confirmation state after the click.

A button click alone is never treated as proof of submission. Real submissions are confirmation/receipt-gated: ARGUS expects confirmation text and/or a reference and stores the receipt URL, trace, screenshot, and audit event. A missing receipt is a handoff for review, not a successful application.

## Human-only actions

ARGUS must stop for:

- CAPTCHA or anti-bot challenge;
- MFA and identity verification;
- online, coding, psychometric, numerical, or situational-judgement assessments;
- HireVue or other recorded video;
- legal attestations and electronic signatures;
- unfamiliar work-authorisation or sponsorship wording;
- criminal, regulatory, conflicts-of-interest, disability, or demographic questions without an explicit user policy;
- salary or location decisions that have not been approved;
- new required fields with no verified mapping.

Do not add CAPTCHA solving, stealth plugins, fingerprint evasion, or bulk crawling. Those features undermine the project’s security model and may violate site rules.

## Secrets

- `secret.key` decrypts encrypted database values.
- `api_token.txt` authorises browser-extension capture into the local opportunity inbox.
- IMAP credentials are read from environment variables and are not stored by ARGUS.
- Browser cookies and ATS sessions may exist in Playwright browser storage during a run.

Keep the data directory private. Never commit `.env`, local databases, keys, tokens, traces, screenshots, or uploaded documents. The release packaging script excludes these classes of file.

### Token rotation

1. Stop ARGUS and the extension.
2. Delete `api_token.txt`, or set a new `ARGUS_API_TOKEN` environment value.
3. Run `argus init` and restart ARGUS.
4. Update the extension’s Connection settings.

### Encryption-key recovery

There is no backdoor. Restore `secret.key` from the same backup as the database. A database restored without its matching key retains records but encrypted values cannot be decrypted.

## Live-mode policy

Live submission is disabled by default. `ARGUS_ENABLE_TRACKR_LIVE` is also false by default and is an independent opt-in; turning it on does not authorise submission or blind crawling. Enable one portal at a time, perform a headed dry run, inspect all mappings, and retain the trace. Disable live mode and Trackr live discovery again after the batch.

`ARGUS_LIVE_DOMAIN_ALLOWLIST` accepts hostnames only. A bare entry such as `jobs.example.com` matches that hostname exactly (case and a trailing dot are normalised); it does not match `uk.jobs.example.com` or another sibling host. An explicit entry such as `*.example.com` matches subdomains such as `uk.example.com`, but never the apex `example.com`. Wildcards must be exactly one leading `*.` over a real DNS suffix; values such as `*.com`, embedded `*`, URLs, paths, and ports are refused rather than broadened. Do not rely on a provider root entry to trust all of its subdomains—write each exact host or an intentional explicit wildcard.

The candidate remains responsible for truthful declarations, employer application limits, portal terms, and duplicate applications. ARGUS’s conflict rules are an additional control, not a legal or contractual authority.

## Email ingestion

`.eml` and IMAP content are untrusted input. HTML is converted to text without executing scripts. Attachments are ignored. IMAP access is read-only, and polling uses `BODY.PEEK[]`. Message classification and application matching can be wrong; ambiguous or unmatched mail should be reviewed before acting on a deadline.

## Audit verification

Run:

```bash
python -m app.cli audit
```

Exit code `0` means the chain verifies. Exit code `2` means at least one event or link is inconsistent. A broken chain should trigger backup preservation and investigation before further applications.

## Vulnerability reporting

Do not include real candidate data, credentials, cookies, or employer application content in a report. Provide the affected version, reproduction against the synthetic ATS laboratory, observed result, expected result, and relevant redacted trace.
