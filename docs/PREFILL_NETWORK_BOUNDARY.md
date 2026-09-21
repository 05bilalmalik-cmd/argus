# PREFILL network boundary

Status: enforced in `app/services/navigator.py` (`HeadedSessionWorker._route`);
acceptance campaign: `tests/unit/test_prefill_network_campaign.py`.

## Contract

PREFILL is fill-only. It must not emit `POST`/`PUT`/`PATCH`/`DELETE` from
field filling, page JavaScript/autosave, or any upload path. Trust in an
origin is not a mutation allowance: a same-origin request is still refused
when the method is mutating. No delivery exception survives PREFILL; the
provider flag is classification-only and does not authorize delivery.

- Allowed in PREFILL: read-only `GET`/`HEAD`/`OPTIONS` navigation and passive
  assets required to render and fill the form.
- Refused in PREFILL with exact fatal reason `prefill_mutating_request_blocked`:
  any `POST`/`PUT`/`PATCH`/`DELETE` that is still marked allowed when the
  guard runs (typically the old `approved_origin` same-origin hole).
- Untouched: `REVIEW`/`inspect`/`dry_run` keep `read_only_data_bearing_request`;
  foreign-origin mutating requests keep `data_bearing_unapproved_origin`;
  the action-time authorized `SUBMIT` path keeps `approved_origin` delivery.
  There is no new global deny for `SUBMIT`.

## Root cause it closes

`classify_egress` (host policy, intentionally mode-unaware) approves
same-origin mutating requests with reason `approved_origin`, and the old
`_route` read-only block covered only `inspect`/`review`/`dry_run` — not
`prefill`. A page input listener calling `fetch('/submit', {method: 'POST'})`
on fill therefore delivered one real POST with no Next/Submit click, no
authority, and no intent (Agent12 adjudication, section 4; reproduced RED by
the browser campaign in this change).

## The guard

Location: `app/services/navigator.py`, inside `HeadedSessionWorker._route`,
immediately after the existing read-only (`inspect`/`review`/`dry_run`)
block and before egress recording, so the refusal is recorded with the exact
reason and surfaced as a fatal `REQUEST` event:

- condition: `decision.allowed` is still true, worker mode casefolds to
  `prefill`, request method casefolds to `post`/`put`/`patch`/`delete`;
- effect: `allowed=False, fatal=True, reason=prefill_mutating_request_blocked`,
  route aborted with `blockedbyclient`.

Placing it after the existing blocks is deliberate: every previously refused
request keeps its precise historical reason; only the previously allowed hole
receives the new reason.

## No PREFILL upload exception (not a silent bypass)

The exact Greenhouse approved-document S3 upload branch
(`greenhouse_bound_approved_document_upload`) is evaluated *before* this
guard and sets `provider_document_upload_allowed` for classification purposes
only. The guard **does not exempt** that flag — no host, origin, or path
prefix receives a delivery exception. The binding stays exact for
classification: provider `greenhouse`, non-run-scoped journey, exact bucket
host `grnhse-prod-jben-us-east-1.s3.amazonaws.com`, `POST`/`xhr`,
`data_bearing` classification, one active approved `document.cv` /
`document.cover_letter` with a 64-hex sha256, multipart filename equal to the
approved basename (must end in `.pdf`, `.doc`, `.docx`, `.txt`, or `.rtf`),
`multipart/form-data` content type, and request origin equal to the verified
form origin.

**Critical**: Production PREFILL is always run-scoped (`allowlist_is_run_scoped=True`).
The Greenhouse classification branch explicitly requires `not run_scoped`, so it
**cannot fire in production**. An upload POST under run-scoped PREFILL is
refused with `data_bearing_unapproved_origin` (cross-origin) or
`prefill_mutating_request_blocked` (same-origin) — it is never claimed as
uploaded, and unauthorized writes are never retagged as uploads. The
classification branch exists only for non-production verification fixtures and
does not widen the PREFILL contract.

## Preserved read-only and other-mode behaviour

**PREFILL read-only positives**: `GET`/`HEAD`/`OPTIONS` navigation and passive
assets required to render and fill the form remain allowed. The browser
campaign test `test_prefill_readonly_get_fill_navigation_allowed_browser`
confirms input fills complete, allowed GET records are present, and zero POSTs
are delivered. Additional parameterized tests cover `HEAD` and `OPTIONS`.

**REVIEW/inspect/dry_run preservation**: These modes keep the existing
`read_only_data_bearing_request` refusal for same-origin candidate-bearing
requests. They are not widened or narrowed by this change. Parameterized
tests confirm refusal preservation for each mode.

**SUBMIT direct route positive — route-only, not action-time authority proof**:
The `SUBMIT` mode same-origin `POST` to `/apply/submit` still receives
`approved_origin` and is delivered. This is a *route-level* allowance only; it
does not prove action-time authority, user intent, or final submission
confirmation. Authority is established only by the separate preflight/submit
confirmation flow, not by the egress guard.

## Known consequence

Page JavaScript that requires autosave, beacon, or upload POSTs to function
will see those requests refused under PREFILL. The refusal is recorded in the
egress log and forces the existing human-handoff path; fill of read-only
fields is unaffected. This document is not a full-provider certification:
per-provider upload/autosave flows still need individual binding review
before any further exception is considered.

## Tests

`tests/unit/test_prefill_network_campaign.py` (10+ cases, real headless
Chromium + two loopback counting servers + the real `_route`):

- PREFILL same-origin POST: 0 delivered, exact `prefill_mutating_request_blocked`
  (fails pre-fix with 1 delivery — the demonstrated RED);
- REVIEW same-origin POST: 0 delivered, `read_only_data_bearing_request`;
- PREFILL foreign-origin POST: 0 delivered, `data_bearing_unapproved_origin`;
- PREFILL read-only GET/HEAD/OPTIONS fill: input fills, allowed records present, 0 POSTs;
- synthetic `POST`/`PUT`/`PATCH`/`DELETE` same-origin PREFILL: all refused exactly;
- run-scoped PREFILL upload POST: refused, reason keeps its origin form and is
  never upload-tagged;
- SUBMIT same-origin POST positive: still `approved_origin`, still delivered (route-only);
- exact approved-S3 branch blocked with `prefill_mutating_request_blocked`;
- exact approved-S3 near-misses: absent filename, wrong filename, missing hash, malformed hash;
- REVIEW/inspect/dry_run refusal preservation parameterized.

## Classification parity and remaining caller coverage

`tests/unit/test_phase17_egress_impact.py` now expects the same-origin
PREFILL upload POST to be aborted in both classification modes. Its original
classification-parity purpose is preserved. Exact approved-S3 fixtures reach
`prefill_mutating_request_blocked`; malformed upload metadata is rejected
earlier with `data_bearing_unapproved_origin`.

The route tests prove request refusal, not upload-caller, handoff or manifest
status. Those downstream claims require integration coverage; route-only
success must not be described as a verified handoff or a successful upload.
