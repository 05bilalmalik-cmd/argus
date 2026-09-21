# ARGUS Typed Decisions over Hermes

## Overview

This document describes the design and implementation of a **Typed Decision Loop** over the existing Hermes notification channel. The goal is to allow a human to resolve escalations from their phone instead of the dashboard, by sending structured decision requests (not free text) and receiving validated responses.

---

## The Problem

- **97 applications in NEEDS_USER** and **145 in BLOCKED** sit waiting for human input.
- The review digest (`review_digest.py`) can *tell* the user something needs attention.
- The Hermes backend (`_HermesBackend.send` in `notifications.py:297-327`) shells out `hermes send -t <target> <message>` with a **plain string** — one-way, untyped, fire-and-forget.
- There is **no way to answer** from the phone.

---

## The Solution: Typed Decision Envelope

Instead of free text, ARGUS now emits a **`DecisionRequest`** carrying:

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Stable UUID for the decision |
| `application_id` | `str` | ARGUS application identifier |
| `employer` | `str` | Employer name (e.g., "Goldman Sachs") |
| `role` | `str` | Role title (e.g., "2027 Placement") |
| `question_text` | `str` | Human-readable question |
| `canonical_key` | `CanonicalKey` | Reuses `app.domain.questions.CanonicalKey` |
| `sensitivity` | `Sensitivity` | Reuses `app.domain.questions.Sensitivity` (STANDARD/SENSITIVE/LEGAL/ASSESSMENT) |
| `permitted_options` | `tuple[str, ...]` | Typed choices the human may select |
| `confidence` | `float \| None` | ARGUS's best guess (0..1), **always `None` for LEGAL/SENSITIVE tiers** |
| `prompt` | `str` | Short human-readable prompt including employer/role |

The response is a **`DecisionResponse`**:

| Field | Type | Description |
|-------|------|-------------|
| `decision_id` | `str` | Must match the request `id` |
| `chosen_option` | `str` | Must be one of `permitted_options` |
| `decided_by` | `str` | Human identifier (e.g., "phone", "telegram-user-123") |
| `decided_at` | `datetime` | Timezone-aware timestamp |

Both types are **frozen dataclasses with `__slots__`**, serializable to/from compact dicts via `to_dict()` / `from_dict()`.

---

## Reuse of Existing Vocabulary

**No new enums invented.** The decision layer reuses:

- `CanonicalKey` (`app/domain/questions.py:7-46`) — 46 canonical field identities
- `Sensitivity` (`app/domain/questions.py:49-53`) — four risk tiers
- `QuestionMapping` (`app/domain/questions.py:68-76`) — confidence + sensitivity + reason
- Notification reason codes (`notifications.py:49-70`) — closed set, no free text
- Application states (`states.py:71-89`) — `NEEDS_USER`, `NEEDS_OA`, `BLOCKED`, etc.

Mapping from notification reason → `CanonicalKey` is in `decisions.py:_canonical_key_for_reason()`.

---

## Safety Rules (Non-Negotiable)

### 1. Invalid Option Rejection
A `DecisionResponse` whose `chosen_option` is **not in** the request's `permitted_options` is rejected.
- Enforced by `DecisionRequest.validate_response(response)` → raises `ValueError`.
- Tested: `TestSafetyRules.test_invalid_option_rejected`

### 2. Sensitive Tiers Carry No Default / No Guess
For `Sensitivity.LEGAL` and `Sensitivity.SENSITIVE` (covering `WORK_AUTHORISATION`, `SPONSORSHIP`, `CRIMINAL_RECORD`, `LEGAL_ATTESTATION`, `DEMOGRAPHIC`):
- `confidence` **must be `None`** — ARGUS never suggests an answer.
- `permitted_options` **must not contain** a "default" option — the human must choose explicitly.
- Enforced in `DecisionRequest.__post_init__`.
- Tested: `TestSafetyRules.test_sensitive_tiers_carry_no_default_or_guess`

### 3. No Secrets / PII in Payload
The envelope contains **only**: employer, role, application_id, question_text, canonical_key, sensitivity, permitted_options, confidence, prompt.
- **Never** includes: candidate name, email, phone, address, university, degree, LinkedIn, GitHub, work authorisation text, sponsorship text, passwords, tokens, absolute paths.
- Tested: `TestPrivacyNoSecretsOrPII.test_payload_contains_no_pii_or_secrets`, `test_decision_payload_excludes_candidate_profile_data`

### 4. Read-Only Decision Request Generation
`build_decision_requests(session)` performs **zero database writes**.
- Only reads `Application` + `Opportunity` joined on human-blocking states.
- Tested: `TestNoDatabaseWrites.test_build_decision_requests_performs_no_writes`, `TestBuildDecisionRequests.test_build_requests_readonly_no_writes`

---

## Outbound Delivery (Implemented)

### Minimal Edit to `notifications.py`

Added one method to `NotificationService`:

```python
def send_decision_payload(self, payload: dict[str, object]) -> bool:
    """Send a structured decision payload through the existing backend fanout."""
```

- Reuses the **existing fanout** (stdout, ntfy, webhook, Hermes).
- Fire-and-forget, fail-soft (logs warning, continues).
- **No change** to existing `HumanAttentionEvent` / `_payload` / `_item` / digest behavior.
- Regression tested: `TestExistingNotificationBehaviourUnchanged.test_plain_notification_still_works`, `test_decision_payload_delivery_uses_same_backend`

### Hermes Backend Adaptation

`_HermesBackend.send` now accepts payloads **with or without a `message` key**:
- If `message` present → sends that string (backward compatible).
- If absent → serializes entire payload as compact JSON.

This allows decision envelopes to flow through Hermes unchanged.

---

## Inbound Path: What Actually Exists

**Honest assessment:** **No inbound path exists today.**

| Mechanism | Status |
|-----------|--------|
| `hermes webhook` | Documented in `ARGUS_HERMES_ENDSTATE.md:28` as a Hermes capability, but **no HTTP endpoint exists in ARGUS** to receive it. |
| `hermes kanban` | Documented as a multi-profile board, but **no integration** in ARGUS routers. |
| `/api/handoff` | Exists for Navigator browser sessions (`continue_after_human`, `final_manifest`, `confirm`), but **not wired to Hermes**. |
| `/api/review-queue/confirm` | Accepts batch confirmation of reviewed apps, but **not decision responses**. |
| `/api/mail/ingest` | Ingests `.eml` files, not decision responses. |

**No webhook, no callback, no polling endpoint** for decision responses exists in `app/routers/`.

---

## Closing the Loop: Concrete Proposal

To complete the typed decision loop end-to-end, the following is needed:

### 1. Inbound HTTP Endpoint (New Router)
```python
# app/routers/decisions.py (new)
@router.post("/api/decisions/{decision_id}/respond")
def respond_decision(
    decision_id: str,
    payload: DecisionResponsePayload,  # Pydantic model mirroring DecisionResponse
    session: SessionDep,
) -> dict[str, object]:
    # 1. Look up pending DecisionRequest by decision_id (store in Redis/SQLite/DB)
    # 2. Validate: chosen_option in permitted_options
    # 3. Apply decision: write AnswerEntry, advance Application state
    # 4. Return { "status": "accepted" }
```

### 2. Pending Decision Store
- Lightweight table: `decision_requests (id PK, application_id, payload_json, created_at, expires_at)`
- TTL: 24h (configurable)
- Cleanup on response or expiry

### 3. Hermes Webhook Registration
- On startup, register a webhook with Hermes: `hermes webhook add --url http://127.0.0.1:8787/api/decisions/webhook --events message`
- Webhook handler verifies HMAC/signature, parses payload, routes to `respond_decision`.

### 4. Phone-Side UX (Hermes Bot)
- Telegram/Slack/Signal/Discord bot receives decision payload as structured message (buttons/quick replies).
- User taps "Yes" / "No" / "Upload CV" / etc.
- Bot POSTs to webhook with `decision_id`, `chosen_option`, `decided_by`, `decided_at`.

### 5. State Transition on Decision
| CanonicalKey | Current State | Decision → New State |
|--------------|---------------|----------------------|
| `WORK_AUTHORISATION` | `NEEDS_USER` | Answer stored → `PACKAGE_PREPARED` or `BLOCKED` |
| `SPONSORSHIP` | `NEEDS_USER` | Answer stored → `PACKAGE_PREPARED` or `BLOCKED` |
| `CRIMINAL_RECORD` | `NEEDS_USER` | Answer stored → `PACKAGE_PREPARED` or `BLOCKED` |
| `LEGAL_ATTESTATION` | `NEEDS_USER` | "I confirm" → `READY_TO_SUBMIT` |
| `DEMOGRAPHIC` | `NEEDS_USER` | Answer stored → `PACKAGE_PREPARED` |
| `CAPTCHA` | `NEEDS_USER` | "Completed" → Navigator `continue_after_human` |
| `ASSESSMENT` | `NEEDS_OA` | "Start now" → Navigator handoff |
| `CV` / `COVER_LETTER` | `NEEDS_USER` | "Upload" → Navigator handoff |

---

## File Map

| File | Purpose |
|------|---------|
| `app/services/decisions.py` | `DecisionRequest`, `DecisionResponse`, `build_decision_requests()`, serialization |
| `app/services/notifications.py` | `NotificationService.send_decision_payload()`, `_HermesBackend` JSON fallback |
| `tests/unit/test_decisions.py` | 22 tests covering envelope round-trip, safety rules, privacy, no-DB-writes, regression |
| `docs/TYPED_DECISIONS.md` | This document |

---

## Test Results (Real Output)

```
.\.venv\Scripts\python.exe -m pytest tests/unit/test_decisions.py -q
22 passed in 2.26s

.\.venv\Scripts\python.exe -m pytest tests/unit -q -k "notification or digest or decision or questions" --ignore=tests/unit/test_vendor_behind_custom_domain.py
110 passed, 1733 deselected, 1 warning in 12.50s
```

---

## Summary

| Aspect | Status |
|--------|--------|
| Typed envelope (`DecisionRequest`/`DecisionResponse`) | ✅ Implemented |
| Reuses `CanonicalKey` / `Sensitivity` / reason codes | ✅ |
| Safety rule: invalid option rejected | ✅ Tested |
| Safety rule: sensitive tiers no default/guess | ✅ Tested |
| Safety rule: no PII/secrets in payload | ✅ Tested |
| Safety rule: read-only request generation | ✅ Tested |
| Outbound via existing Hermes fanout | ✅ Implemented |
| Regression: existing notifications unchanged | ✅ Tested |
| Inbound path | ❌ **Does not exist** — documented above |
| Proposal to close loop | ✅ Documented above |