---
name: argus-preparation-bridge
description: Use when operating the ARGUS preparation bridge.
---

# ARGUS preparation bridge

A restricted preparation connection, not autonomous submission authority. Read the repository's `docs/PREPARATION_BRIDGE.md` for installation and current acceptance limits. Resolve the actual installed Python, ARGUS data directory and MCP server configuration from the operator's deployment record; never guess paths or PIDs.

## Tools

Discover `argus_preparation` using the client's tool search and load its schemas. Hermes desktop currently exposes names prefixed `mcp__argus_preparation__`; other clients may name them differently.

| Tool | Arguments | Purpose |
|---|---|---|
| `get_preparation_readiness` | `{}` | Check readiness before any action. |
| `list_preparation_handoffs` | `{"after_cursor":null,"limit":10}` | Read bounded handoff history. |
| `get_preparation_run` | `{"run_id":"<returned UUID>"}` | Read correlated preparation state. |
| `request_approved_preparation` | `{"idempotency_key":"<persisted key>"}` | Execute an already operator-approved preparation. |
| `pause_preparation` | `{"reason_code":"OPERATOR_REQUESTED"}` | Pause; verify by reading readiness back. |

Decode the JSON result inside any client wrapper. Successful transport does not mean successful preparation.

## Operating contract

1. Read readiness. With `pending=0`, report that no approved application is waiting. Do not self-approve, manufacture work, bulk approve or invoke preparation merely to test connectivity.
2. Obtain explicit operator authorization for a concrete, suitable application. Verify truthful applicant facts, target and approved documents. Local grant creation requires an existing `PACKAGE_PREPARED` application; it is not an MCP operation. Resolve any pre-existing pending grants before assuming which application will run next.
3. Persist a request key matching `[A-Za-z0-9_-]{8,64}` before execution. Retry with the same key after timeout; never generate another key to force execution.
4. Read the returned run and correlate the handoff. Stop for human review. Never advance pages, submit, invent declarations or bypass unknown-ATS, expiry, revocation or integrity blocks.
5. Handoff history is not live-session proof. Check the current ARGUS handoff API before maintenance. Never terminate an unverified PID or close unrelated browser windows.

## Credentials and startup

Never read or print credential JSON, encryption keys or token hashes. After authorization, inspect only expiry metadata with read-only SQLite against the deployment's `preparation_bridge.sqlite3`:

```sql
SELECT expires_at FROM bridge_credential WHERE id=1;
```

Compute its time using a tool. Credentials expire within 24 hours. Reconnecting does not renew them; obtain explicit authorization before replacement. Never delete an existing credential to defeat exclusive-create safeguards.

Installation, startup, upgrades and rollback require separately scoped authorization. Preserve review-only mode, disabled submission and no automatic sweeps. Check the exact listener and current handoffs first; an occupied port is not permission to kill its owner.

## Evidence limits

Health, tool discovery and authenticated readiness prove connection, not employer-form success. Preserve `unknown_ats/HUMAN_REQUIRED`. Full supported-form handoff/replay and headed-browser acceptance remain incomplete in this source publication. Report tested behavior separately from those unverified capabilities.
