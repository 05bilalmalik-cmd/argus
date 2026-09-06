# ARGUS Phase 27 Final Report

Date: 2026-08-28 (Europe/London)

## Outcome

Phase 27 fixed the optional-resource abort defect, added a separate exact-host CAPTCHA authorization, raised handoff TTL to 24 hours, made expiry preserve the service-owned page for explicit resumption, and added a pytest source-stability guard for concurrent edits.

The one permitted live Aquatic PREFILL improved from zero to six populated fields and reached a visible, unsolved reCAPTCHA handoff without touching Submit. It did not fully fill the form: an exact reCAPTCHA release request reported by Chromium as resource type `other` remained unknown and fatal, so the run stopped without widening policy. The service later exited during verification and the visible window closed; the no-retry rule was honored. The final-window-open success condition was therefore not met at reporting time.

## Egress consequence table

| Resource | Observed count | Consequence |
|---|---:|---|
| `www.recaptcha.net` `/recaptcha/enterprise.js` GET script | 1 | essential |
| Dropbox drop-in script | 1 | optional |
| Google API script | 1 | optional |
| Google account script | 1 | optional |
| Greenhouse Snowplow POST fetch | 2 | optional; still blocked |
| Exact Aquatic banner image | 1 | optional |

The later live measurement added the exact Roboto `fonts.gstatic.com` path as optional. Arbitrary font paths and every other unclassified request remain unknown/fatal.

## CAPTCHA boundary

Closed exact hosts:

- `www.recaptcha.net`
- `www.google.com`
- `www.gstatic.com`

The category is separate from the Phase 21 vendor manifest; that manifest's registrable-domain rule was not changed. CAPTCHA authorization applies only during PREFILL on a recognized exact ATS form page for a resolved vendor, excludes main-frame navigation, and requires an explicit `carries_candidate_data: false`. Lookalike/suffix hosts and candidate-bearing requests are blocked.

Google documents `www.google.com`/`www.gstatic.com` as reCAPTCHA CSP/load dependencies and documents `www.recaptcha.net` as the alternative endpoint: <https://developers.google.com/recaptcha/docs/faq> and <https://developers.google.com/recaptcha/docs/loading>.

## Handoff lifetime

- TTL: 24 hours (`86,400` seconds), long enough for sleep or a workday while still finite.
- Expiry: revokes ordinary commands and stale authority but preserves a service-owned page for a bounded seven-day resume window.
- Resume: explicit application-bound API action issues a fresh 24-hour deadline on the same owner-thread page.
- Batch/source-resolution contexts remain destructive and non-resumable.
- Process boundary: the page outlives the requesting HTTP/agent process only when the persistent ARGUS service owns it. It cannot survive service/OS termination; Chromium was not detached because that would bypass owner-thread egress and cleanup controls.

## Phase 25

The requested Phase 25 file currently passes `29/29`. The historic three failures cannot be named truthfully because Phase 26 retained only the aggregate, not node IDs or tracebacks. Evidence shows concurrent in-place source/test mutation. The new session-level guard fingerprints all Python files under `app`, `scripts`, and `tests` and invalidates pytest if any path is added, deleted, or rewritten during the run. Phase 25 plus guard passed `31/31`.

## Live Aquatic result

- Run: `3851b4af-697b-4b95-bbb6-497185c188e2`
- Session: `80c0c3b56b2644c3`
- Ending application/lifecycle state: `NEEDS_USER` / `HUMAN_REQUIRED`
- Handoff: `captcha_detected`; visible and unsolved at handoff
- Populated: six approved fields
- Failed fields: zero
- Plausibility rejects: zero
- Optional blocked resources: six records across banner, Dropbox, telemetry, and social-sign-in dependencies
- Unknown fatal wall: exact gstatic reCAPTCHA release request reported as resource type `other`; the live font was also unknown at that time
- Submission: not clicked; no receipt; no confirmation
- Process allowlist: one exact row-derived hostname, process-only, never persisted
- Exclusions honored: Xantium, iSAM, and DV Trading were not selected

Legal declarations and other unanswered controls remained blank unless an exact stored-profile value existed. No model judgement was used for legal declarations.

## Verification

- Focused egress/CAPTCHA disclosure set: `142 passed`
- Handoff manager/API/lifecycle/e2e: `46`, `17`, `6`, and `25` passed respectively
- Phase 25 plus stability guard: `31 passed`
- Expanded target/egress regression set after final integration fixes: `162 passed`
- Full source-guarded suite: `2044 passed, 2 skipped, 2 warnings in 971.30s (0:16:11)`. It launched alone; another workspace suite began midway, but the source fingerprint remained stable and the two-browser ceiling was retained.

## Safety totals

- Submissions: `0`
- Submit clicks: `0`
- Rows deleted: `0`
- Persisted allowlist entries: `0`
- Trust-registry changes: `0`
- Vendor-manifest domain rule relaxations: `0`
- `BLOCKED` states cleared: `0`
- Legal declarations answered by model judgement: `0`

Database backup: `%LOCALAPPDATA%\ARGUS\backups\argus-phase27-prechange-20260828T053800312926Z.db`, SHA-256 `f3361aee29da4a675cfd507d1a0fc6601d0ad9e619e1f96b63484b4ed5e2f64d`.

Detailed evidence is in `docs/ARGUS_PHASE27_EGRESS_CAPTCHA_HANDOFF_WORKLOG_20260828.md`.
