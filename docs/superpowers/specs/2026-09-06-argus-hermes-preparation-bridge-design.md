# ARGUS–Hermes controlled preparation bridge

Status: implementation intent approved in chat; written contract awaiting review.
Baseline: public source commit `6de3573cc06ef285d84ce8de7c1838e6bf84876d`.
Workspace: the clean GitHub publishing checkout, not the installed ARGUS runtime.

## 1. Deliverable and scope

Build a default-off, source-runnable bridge that lets Hermes request an already
operator-approved application preparation job, inspect its sanitised outcome,
and find human handoffs. The actual fill uses ARGUS's production PREFILL engine,
existing adapters and owner-thread Navigator. The bridge is not an alternate
browser driver, form-mapping model, autonomy lease or submission path.

The first decisive demonstration is a real Hermes MCP client connection to the
bridge, triggering a granted synthetic application through ARGUS and obtaining
its independently verified handoff state. A mocked tool response or direct manual
browser operation does not satisfy this deliverable.

This is a staged addition to, not a claim of completing, the larger autonomy
architecture. The earlier raw-CUA vision is not the implementation contract.
The existing full autonomy sidecar's lease operations and five-tool interface
remain separate future work; this bridge has a distinctly versioned preparation
protocol and cannot be presented as that sidecar.

## 2. Ownership and data flow

1. The local operator explicitly approves one application using an ARGUS command
   that requires an explicit data root and an exact confirmation of the chosen
   application. No inference from the user's old approvals is allowed.
2. ARGUS checks preparation eligibility and freezes the grant's target, programme,
   approved document identities/hashes, profile revision and answer revision.
3. Hermes requests the next approved grant; it cannot select an application,
   supply a URL, change a value, choose a file, or create/extend an approval.
4. A transaction claims one eligible grant. ARGUS revalidates the binding and
   invokes the shared production PREFILL entry point, not a second implementation
   of the route's guards.
5. The result is read back from ARGUS's run and Navigator state. The bridge emits
   only a closed sanitised projection. Browser state belongs to ARGUS throughout.
6. Any human handoff suspends further bridge dispatch until local operator action.
   No agent shares, focuses, closes or mutates another application's browser.

## 3. Components

- `app/services/preparation_bridge.py`: grants, atomic claims, pause/revoke,
  binding checks, dispatch lifecycle and closed outcome projection.
- A small shared preparation service extracted from the existing public PREFILL
  route only as needed so the UI and bridge exercise identical preflight/locking
  and runner logic. Preserve the public API response contract.
- Additive persisted grant/request records in the existing database/model layer.
  Do not rewrite applicant records or rebaseline submission authority.
- A dedicated default-off router for the bridge's constrained authenticated
  protocol. It must not expose the application's general API credential.
- `app/bridge/` or equivalent small package: stdio MCP facade and strict schemas.
  The facade never imports `app.main`, opens the candidate database, or holds
  profile/document/browser values. Importing `app.main` creates an application;
  that side effect must not select production data accidentally.
- ARGUS operator CLI commands for enabling/provisioning a source-runtime binding,
  granting/revoking a preparation and clearing a pause. Exact option names are
  frozen in the implementation plan after inspecting the CLI conventions.
- A sample Hermes configuration, connection/runbook and isolated executable
  verification harness. No modification to the user's running Hermes profile.

No packaged release, live installation, scheduling or automatic GitHub push is
included in this implementation approval.

## 4. Narrow MCP surface

The preparation protocol exposes exactly these capabilities:

- `get_preparation_readiness()`
- `request_approved_preparation(idempotency_key)`
- `get_preparation_run(run_id)`
- `list_preparation_handoffs(after_cursor, limit)`
- `pause_preparation(reason_code)`

All request and response models are strict, bounded and extra-forbid. Unknown
operations and enum values fail closed. No generic HTTP, shell, browser, click,
keyboard, upload, raw DOM, screenshot, candidate lookup, approval, resume or
submission tool is exposed. MCP resources and prompts must not become an
alternate data channel.

Responses contain opaque bridge identifiers, enums, booleans and bounded counts.
They contain no candidate values, application URLs/query strings, document paths,
raw exceptions, cookies, credentials, authority handles or free-form page text.
Human-facing detail remains inside ARGUS. Bridge identifiers never become
submission authority or local operator authority.

## 5. Transport and threat model

The source implementation uses an authenticated, exact-loopback HTTP channel
between the stdio facade and the already running ARGUS service. The bridge
credential is separate from the general ARGUS API token, scoped to the bridge
operations, revocable and expiring. The facade loads it privately; MCP arguments,
responses, logs and sample configuration never contain its value.

Endpoint configuration is operator-owned. Reject non-loopback hosts, userinfo,
fragments, unexpected paths and redirects; disable inherited proxy routing.
Bridge requests authenticate before accessing job state or causing effects.
The existing Host/Origin protections remain intact. Operator approval/provisioning
is not available through this machine-facing route.

A same-user attacker with arbitrary filesystem/process access is outside this
source bridge's containment claim. A restricted Hermes configuration is not an
OS security sandbox. This transport does not claim the Windows named-pipe process
attestation, DPAPI provisioning or full autonomy-sidecar guarantees in the larger
design. Those require separate implementation and certification.

## 6. Approval and state machine

An approval is short-lived, revocable, single-use and specific to one application
and one frozen preparation binding. Its fingerprints are server-derived; caller
assertions cannot approve a target or document. Missing eligibility, programme,
approval or target evidence produces a refusal before browser creation.

Grant lifecycle: APPROVED -> CLAIMED -> terminal outcome; APPROVED may instead
become EXPIRED or REVOKED. Atomic compare-and-set prevents two clients from
claiming the same grant. Identical idempotency keys return the existing request;
conflicting reuse is rejected. Keys and run IDs are independently validated.

Job outcomes distinguish running, verified prefilled handoff, human-required
before completion, blocked, failed and interrupted. An HTTP success or a queued
worker is not a successful fill. Source-target drift, candidate revision drift,
expired/revoked approval and unavailable Navigator state deny dispatch.

On restart, an orphaned claimed/running job becomes INTERRUPTED, never silently
requeued. The operator must reconcile and explicitly approve a new attempt.
No blind retry after partial field mutation or an ambiguous transport timeout.
Pause blocks new claims, revokes pending dispatch and stops further bridge-owned
mutation at a checked boundary. It never kills unrelated processes or closes a
retained human handoff. Only the local operator can clear pause.

## 7. Browser and submission invariants

- PREFILL only: no Next, Apply, submit, confirmation or implicit Enter activation.
- No source-resolution URL guessing. A grant requires an already verified
  application target; unsupported discovery remains a visible prerequisite.
- Reuse existing approved profile/answer/document selection and target/egress gates.
- Unknown or sensitive questions, CAPTCHA, authentication and assessments hand off.
- A live handoff prevents starting another bridge preparation.
- No broad process cleanup, existing-profile attachment or coordinate fallback.
- Browser/session identifiers are private to ARGUS, not model capabilities.
- Reject a result that unexpectedly reports submission or a crossed click boundary;
  retain an integrity incident and pause, rather than sanitising it into success.
- No submission authority, intent creation or receipt-success assertion is added.

## 8. Verification and falsification

Run in new external temporary data/profile directories, loopback-only synthetic
sites and owned Chromium processes. Keep the existing live service, data and
human browser windows untouched. Use the project's safe test environment.

Required checks:

1. Default-off startup: no new bridge capability or listener, and unchanged
   existing health/API contracts. Explicit enablement without required binding
   fails closed, not a permissive fallback.
2. Real MCP protocol/client discovery: exactly the five tools above. Call through
   Hermes's installed MCP client, not merely an independent JSON-RPC mock.
3. Positive end-to-end: locally approve a synthetic application; Hermes requests
   it; production ARGUS fills the current form; independent DOM readback verifies
   the approved values and document; MCP reports the correlated handoff. No final
   submission and no page-advance or submission network mutation occur.
4. Negative authorization: no grant, expired/revoked grant, wrong token, pause,
   target/profile/document drift and an unrelated existing handoff create no new
   browser or application mutation.
5. Concurrency/replay: simultaneous requests and repeated keys cause at most one
   claim and one fill. Lost responses and restarts cannot silently retry work.
6. Privacy: plant unique synthetic markers in profile values, documents, URLs,
   page labels and exceptions; none may appear in MCP schemas, results, logs or
   resources. Extra data, malformed JSON and unknown fields are refused.
7. Adversarial browser cases: changed employer/root, stale session, disappeared
   form, missing value, CAPTCHA, legal field and worker failure produce explicit
   non-success outcomes without bypassing the existing guards.
8. Existing PREFILL/no-advance, authority, intent, receipt, target, privacy and
   launcher regression tests remain intact. New failing tests precede new code.
9. Independent review attacks both false success and needless refusal. Preserve
   every failure; report actual completed test counts from JUnit, not old records.

A real model conversation is an additional demonstration only if the narrowly
configured Hermes process can use its existing authentication without copying or
exposing credentials. Lack of model-provider access must be reported separately
from the actual Hermes MCP transport and ARGUS browser execution proof.

## 9. Completion and explicit limits

Done means the working source, operator commands, MCP facade, documented setup,
real Hermes-client execution evidence and adversarial tests exist and have been
exercised. Installation remains a separate permission. A stub sidecar that only
returns readiness is not done.

This bridge does not discover suitable jobs, solve unknown forms with AI, move
through application pages autonomously, grant standing submission consent, submit
to real employers or prove unattended public-provider reliability. Those remain
separate capabilities, not claims inherited from this milestone.
