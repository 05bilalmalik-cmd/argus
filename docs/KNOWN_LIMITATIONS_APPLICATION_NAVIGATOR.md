# ARGUS Application Navigator — Known Limitations

Date: 2026-08-25  
Scope: verified local 0.2.0 candidate, loopback release evidence, and public review package

## What is supported

ARGUS has provider-scoped adapters for Greenhouse, Lever, and Workday. Those
supported journeys are proven **only** against synthetic loopback ATS fixtures,
with source/application URL separation, employer/role/form evidence, popup and
iframe adoption where applicable, step-wise Workday navigation, explicit
human-boundary handoff, exact final-target binding, and receipt/unknown
outcomes. The verification suite never contacts an employer, Trackr, or any
other external endpoint, so fixture success is not production-tenant proof.

The packaged and shortcut defaults are `OFF`; a zero sweep interval is
disabled. Live submission, live Trackr, and a live-domain allowlist are not
enabled by the release verification environment.

## Human-only boundaries

CAPTCHA, anti-bot challenges, MFA, authentication, legal declarations,
demographic questions, assessments, unsupported custom or generic ATS flows,
and any provider whose identity/form evidence cannot be proved remain
human-only. These boundaries require the Navigator/human-handoff path; they
must not be converted into direct automatic submission. ARGUS may keep a
headed loopback/browser handoff open and report
`HUMAN_REQUIRED`, `NEEDS_USER`, `BLOCKED`, or `SUBMISSION_UNKNOWN`; it must not
pretend that a blocked page or a human handoff is a successful submission.

## What this release does not claim

- Loopback fixture success is not proof that every production employer site
  is compatible. Employer markup, redirects, authentication, anti-bot policy,
  and provider configuration can differ.
- A verified application entry URL is not proof that an employer accepted an
  application. A final receipt is bound to the tested loopback contract.
- This source release makes no claim that ARGUS submitted an application to a
  real employer, changed a real Trackr account, or completed a real CAPTCHA.
- The final executable is unsigned. Its exact hash/version and explicit
  unsigned acknowledgement are release evidence, but they provide no publisher
  identity or Authenticode trust.
- No unattended retry is safe after a post-click uncertainty; the terminal
  `SUBMISSION_UNKNOWN` state requires reconciliation rather than retry.

## Historical audit boundary

The pre-existing historical audit-chain break at event 4,819 is retained as
evidence and is not silently rewritten. New audit appends use the repaired
concurrency and epoch safeguards; verification reports historical corruption
truthfully. Release migration/audit smokes use fresh temporary databases. The
live database was separately backed up online, migrated additively to schema
v6, and checked for preserved counts, integrity, and foreign keys; that private
backup is excluded from every public archive.

## Release boundary

The staged and installed executable are hash-identical, carry FileVersion
`0.2.0.0` / ProductVersion `0.2.0`, and passed two fresh-profile loopback
smokes plus exact installed-health validation. The existing desktop shortcut
and its safe `OFF` wrapper were preserved and revalidated rather than replaced.
The canonical public archive is produced only through the same-process
`scripts/verify.py --package-output` flow, with source/archive privacy scans,
two archive verification passes, raw logs, and a machine-readable manifest.
Recovery data, local profiles, private Hermes state, databases, keys, tokens,
documents, cookies, traces, and screenshots are excluded.
