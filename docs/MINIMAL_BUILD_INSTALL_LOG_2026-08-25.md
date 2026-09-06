# ARGUS minimal working build and install log — 2026-08-25

## User-directed finish line

The final pass was narrowed to the minimum usable application workflow:

- resolve a saved listing to one browser-observed application form;
- run the supported Greenhouse, Lever, and Workday application paths;
- keep CAPTCHA, MFA, legal, demographic, assessment, and other human-only
  boundaries visible and resumable in the same owned browser session;
- build and install a fresh Windows executable without enabling automation.

No real employer application or live Trackr request was used for verification.

## Last blocking fixes

- Candidate-profile and first-application creation now use exact SQLite
  conflict targets, so concurrent Resolve/Apply requests converge without an
  HTTP 500.
- Persisted target loading validates the application-root token separately
  from the bound form/requisition identity.
- URL path text is not accepted as form identity evidence.
- Resolve-to-Apply promotion requires the source browser worker to be stopped
  and cleanup to be complete; escalated/incomplete cleanup fails closed.
- The browser UI rejects stale/cross-application session responses, uses a
  single source-resolution poller, stops on terminal states, re-enables retry
  controls, and respects server-provided human-boundary capabilities.

## Verification

The decisive offline suite completed with `97 passed`, `1 deselected`, and
one existing Starlette deprecation warning. The deselected test was a
reload-only browser case that hung in the test harness; the active Apply and
human-handoff journey passed.

The fresh PyInstaller executable has:

- product version `0.2.0` / file version `0.2.0.0`;
- size `68,202,916` bytes;
- SHA-256 `F7C935391A16D1AEE4A00EA4830CAC1DE27A6DEA76168B87023589ECA7FD6DA9`;
- Authenticode state `NotSigned`.

## Installation evidence

The prior executable and runtime essentials were preserved in the dated
pre-install recovery copy. The new executable was copied only after the two
verified old ARGUS processes were stopped.

Post-install health:

```json
{"status":"ok","service":"ARGUS","version":"0.2.0","automation_mode":"OFF","live_submit":false,"trackr_live":false}
```

The service listens only on `127.0.0.1:8787`. The desktop shortcut still
launches the installed safe wrapper. Post-install database checks returned
`integrity_check=ok`, zero foreign-key violations, schema user version `3`,
and unchanged entity counts: 362 opportunities, 359 applications, 5,929 audit
events, and one candidate profile.

## Honest scope boundary

This build proves the supported provider paths and human handoff against local
loopback fixtures. It does not certify every external employer portal or
future ATS redesign. Unsupported/generic portals remain review/manual paths,
and the installed launcher deliberately remains OFF until the user explicitly
chooses an operating mode.
