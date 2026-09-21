# Typed decision lifecycle (Agent07)

Acknowledgement ledger for human decisions. It records **which action
label a human chose** (for example `Upload CV`, `Completed`) and nothing
else. It is not, and can never become, proof that the action happened.

## What an answer is — and is not

- An answer is a human acknowledgement: `{decision_id, chosen_option,
  decided_by, decided_at}` plus the request context it was given under.
- `chosen_option` is an **action label**, never a field value, document
  path, CAPTCHA solution, or declaration proof. `Upload CV` does not
  contain a CV. `Completed` does not complete a CAPTCHA.
- Answering never fills a field, never completes a challenge, never
  submits an application, never arms automation, and never writes to the
  main database. There is no code path from this ledger to the runner,
  the answer bank, or submission authority.

## API (all routes require `X-Decision-Token`)

Configure with the `ARGUS_DECISION_TOKEN` environment variable. When it
is unset or empty, every decisions route returns `404` (inactive). A
wrong or missing token returns `401`. Error bodies never echo the token.

| Method | Path | Meaning |
| --- | --- | --- |
| `GET` | `/api/decisions` | Currently pending questions. Answered items stay listed while their application still needs a human. |
| `POST` | `/api/decisions/{id}/answer` | Record `{chosen_option, decided_by}` for a pending question. `200` first write, `409` duplicate, `400` bad option, `404` unknown or stale, `503` store unavailable. |
| `GET` | `/api/decisions/{id}` | Readback: `{decision_id, status, manual_action_needed, request, answer, note}`. `status` is `answered`, `unresolved`, or `stale_or_resolved`. `manual_action_needed` is always `true`. |

Sensitive-tier questions (work authorisation, sponsorship, criminal
record, legal attestation, demographic — single source:
`LEGAL_SENSITIVE_KEYS` in `app/services/decisions.py`) reject any
`default`-shaped choice with `400` and carry no confidence guess and no
default option. They remain human-only: this ledger offers no path that
would auto-fill them.

## Identity and staleness

Decision IDs are deterministic per **application + canonical key +
request context**:

- context revision = SHA-256 over canonical key, question wording,
  offered action labels, and sensitivity tier (16 hex chars);
- decision ID = SHA-256 over `application_id:key:revision` (32 hex chars).

The same context reproduces the same ID (safe retry). A later,
different question yields a different ID, so an old acknowledgement can
never block it: answering the old ID returns `404 Decision not found or
stale`, while the readback still returns the recorded receipt as
`stale_or_resolved`. Answers are bound to their `application_id`; an
unrelated application cannot consume them (unknown ID → `404`).

## Storage and operational boundaries

- Store root: `<settings.data_dir>/decisions/` — never the global
  `%LOCALAPPDATA%` tree. Two data roots are fully isolated even when
  they share a machine.
- Uniqueness authority: sidecar SQLite
  `decision_answers.sqlite3` (`PRIMARY KEY (decision_id)`), so two
  processes racing the same answer produce exactly one `200` and one
  `409`. Audit mirror: append-only `decisions_answers.jsonl` in the
  same directory (restrictive permissions).
- The legacy file `%LOCALAPPDATA%/ARGUS/decisions_answers.jsonl` is a
  **read-only compatibility source only for that same ARGUS data root**.
  Other configured data roots never import it. It is never written,
  deleted or migrated by this reader. Pre-revision records are accepted
  with an empty revision and resolve only as stale receipts.
- SQLite commit is the authority for new-format answers. The local JSONL
  mirror is validated but cannot supply an answer absent from SQLite:
  an append may survive a failed fsync or a rolled-back transaction.
- Any malformed line, schema violation, or database error fails closed:
  the API returns `503` and records/returns nothing. If a deployment
  hits `503`, inspect `<data_dir>/decisions/` (and the legacy file, if
  present) for truncated or hand-edited lines; restore from backup
  rather than editing in place. A corrupt legacy file poisons reads the
  same way — quarantine or repair that file; the primary store is
  untouched by the legacy reader.
- No outbound calls. No notifier fanout. No ports or tunnels. Loopback
  only, like the rest of the local API.

## Ownership

`app/routers/decisions.py` (HTTP boundary, auth, ID binding),
`app/services/decisions.py` (question generation, options, sensitivity,
single sensitive-key source), `app/services/decision_store.py`
(durable sidecar store). No other component may read or write the
store; in particular the automation runner, the answer bank, the main
database layer, and submission authority are out of scope and unedited.
