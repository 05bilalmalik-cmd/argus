# Controlled preparation bridge — experimental

The bridge connects an MCP client to a restricted stdio facade, then to an authenticated loopback ARGUS endpoint. ARGUS retains application facts, document selection, approval authority and browser ownership. **It does not grant autonomous application submission.**

## Included surfaces

- Five MCP operations: readiness, approved preparation request, run readback, handoff history and pause.
- Single-use operator grants, persisted idempotency keys, expiry, revocation and mutation guards.
- A companion SQLite authority store, separate from the main application transaction.
- A local operator CLI, deliberately absent from the MCP surface.
- Guard-bearing PREFILL runners cannot upgrade into submit mode or advance pages.
- Strict classification of preparation audit manifests versus submission/integrity evidence.
- Synthetic acceptance harness and focused regression tests.
- A portable [Hermes operating skill](../skills/argus-preparation-bridge/SKILL.md).

## Installation contract

Use a dedicated environment and inspect the deployment before changing it. Installing source does not authorize replacing a running service, approving applications or submitting anything.

Install the optional dependency in that environment:

```text
python -m pip install ".[bridge]"
```

For a separately authorized preparation-only service, configure `ARGUS_ENABLE_PREPARATION_BRIDGE=true`, an explicit `ARGUS_DATA_DIR`, `ARGUS_AUTOMATION_MODE=REVIEW_ONLY`, `ARGUS_ENABLE_LIVE_SUBMIT=false`, `ARGUS_ENABLE_TRACKR_LIVE=false`, `ARGUS_ENABLE_APPLY_CLICK=false` and `ARGUS_SWEEP_INTERVAL_HOURS=0`. Bind only to `127.0.0.1`. Existing instances and human handoffs must not be disturbed without authorization.

Use `python -m app.bridge.operator --help` to inspect local authority commands. Provisioning requires an existing ARGUS data directory and encryption key; it exclusively creates a new scoped credential file. Its TTL must be explicit and cannot exceed 86400 seconds. Credentials belong outside source control, with restricted filesystem permissions. Provisioning does not approve an application.

The MCP executable is:

```text
python -I -m app.bridge.mcp_server --credential-file <ABSOLUTE_CREDENTIAL_PATH>
```

Use the dedicated installed interpreter; `-I` will not discover an uninstalled checkout. Configure this command in the client's supported custom-MCP workflow. For Hermes, inspect `hermes mcp add --help`; `--args` must be last. Confirm the tool-selection prompt: EOF can cancel despite exit 0. Read the named configuration back with `hermes mcp test argus_preparation` and call native readiness. The desktop can enable an already configured custom server with its `setup_mcp` tool.

The credential value never goes into tool arguments or client configuration; only its filesystem path does. Loopback authentication is scoped credential authentication, **not Windows process attestation**.

## Use and verify

Read readiness first. `pending=0` means no approved work is waiting, not permission for the assistant to approve work itself. A concrete, verified, already `PACKAGE_PREPARED` application needs separate explicit local operator approval. Before requesting preparation, persist an idempotency key and reuse it after timeout. Read and correlate the resulting run/handoff; do not equate history entries with current browser liveness.

An unauthenticated POST to `/api/preparation-bridge/call` must return HTTP 401. A valid native readiness call should return a closed protocol reply; decode the reply status rather than treating transport success as a pass. Do not request preparation, pause the service or create grants just to test connectivity.

## Evidence and unresolved acceptance

A prior authorized operator installation demonstrated five-tool discovery, actual Hermes native readiness/handoff calls, authenticated loopback service access and preservation of existing application rows. This is historical installation evidence, not a fresh clean-machine test of a GitHub checkout.

**Full application acceptance has not passed.** Earlier real synthetic attempts found normal PREFILL audit manifests were incorrectly treated as submission evidence; the classifier and regressions address that mechanism. Later attempts still encountered the generic adapter's correct `unknown_ats/HUMAN_REQUIRED` boundary and could not establish independent headed-browser command-line proof. Successful supported-form handoff correlation and duplicate replay remain unverified end-to-end.

Prior review also identified approval/fingerprint race exposure, integrity-priority concerns, historical rather than live handoff listings, SDK raw-stdio duplicate-key/frame-bound limitations and a per-I/O rather than absolute HTTP deadline. Focused tests and activation do not constitute a final independent review of those limitations. Do not deploy for unattended real-employer applications on the strength of source publication.

See [bridge publication checks](PREPARATION_BRIDGE_VERIFICATION.md) for fresh scoped results. The acceptance harness is `scripts/verify_preparation_bridge.py`; its full run creates owned synthetic runtime/browser instances and requires separate execution scope. Oracle unit tests are not substitutes for passing that harness.
