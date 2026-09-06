# Controlled preparation bridge implementation plan

> **For Hermes:** Use subagent-driven-development, TDD and independent spec then quality review.

**Goal:** Execute operator-approved PREFILL through a real Hermes MCP client, without exposing applicant data or browser/submission authority.
**Architecture:** Persist single-use grants in additive bridge tables; the existing ARGUS process owns dispatch. A strict stdio facade calls a dedicated authenticated loopback endpoint. Reuse the public PREFILL implementation and inject a fail-closed mutation guard.
**Tech stack:** Existing Python/FastAPI/SQLAlchemy/Playwright, Pydantic strict wire schema, MCP SDK optional dependency, pytest; Windows source-runtime only for acceptance.

Root: <USER_HOME>/argus-github-publication. Approved spec: docs/superpowers/specs/2026-09-06-argus-hermes-preparation-bridge-design.md. No deployment, submissions, external push or user profile changes.

## Ownership and frozen interface

Independent implementers use separate local clones and disjoint paths. Parent owns app/main.py, app/routers/api.py, app/automation/runner.py, app/routers/preparation_bridge.py, pyproject.toml, sample config/runbook and executable verification. Core implementer owns app/services/preparation_bridge.py, app/bridge/storage.py, app/bridge/operator.py and tests/unit/test_preparation_bridge_core.py. Transport implementer owns app/bridge/protocol.py, app/bridge/client.py, app/bridge/mcp_server.py and tests/unit/test_preparation_bridge_transport.py. Do not copy another worker's changes or run tests in another worker's tree.

Transport: POST /api/preparation-bridge/call with Authorization Bearer. JSON {operation: tool_name, body: object}. Exactly five tool names from approved spec. Unknown/duplicate fields and duplicate JSON keys fail. Request <=16KiB. Response always contains protocol='argus.preparation.v1', status, reason, run_id (canonical UUID or null), pending (bounded int), handoffs (bounded list of {run_id,status,reason}), next_cursor (canonical UUID or null). No other fields.

Statuses: READY, DISABLED, PAUSED, BUSY, RUNNING, PREFILLED_HANDOFF, HUMAN_REQUIRED, BLOCKED, FAILED, INTERRUPTED, REFUSED. Reasons: NONE, DISABLED, UNAUTHENTICATED, INVALID_REQUEST, NO_APPROVAL, EXPIRED, REVOKED, PAUSED, BUSY, DRIFT, NOT_READY, HANDOFF, INTEGRITY, INTERRUPTED, EXECUTION_FAILED, NOT_FOUND, TRANSPORT_ERROR. Bounds: 0<=pending<=100000; at most 100 handoffs. Idempotency key is ASCII [A-Za-z0-9_-]{8,64}. list requires after_cursor=null/canonical UUID and limit strict int 1..100. pause requires reason_code in OPERATOR_REQUESTED, INTEGRITY, TRANSPORT_ERROR. No-argument operations require an empty body. get run requires run_id. No arbitrary identifier reflection in error responses.

Facade credentials: operator-created JSON file {endpoint,token,expires_at}; endpoint exactly http://127.0.0.1:PORT (no path/query/userinfo/fragment), token random ASCII hex, expires_at Unix seconds. It is read privately; not an MCP argument. Expiry validated by both ends. HTTP trust_env=False, follow_redirects=False, finite timeout, bounded response, no error-body/log reflection. Facade launch: python -m app.bridge.mcp_server --credential-file PATH. The optional SDK is imported only for the MCP executable. No app.main/database/browser imports in facade.

Core API: PreparationBridge(database,settings,crypto,navigator,execute_prefill, *, enabled=False, clock=time.time). execute_prefill(application_id, mutation_guard) returns existing raw PREFILL result; parent wires it to the production-shared entry point. Methods: start() (recover orphans); authenticate(token)->bool; provision(credential_file,endpoint,ttl_seconds) local-only; approve(application_id,confirm_application_id,ttl_seconds)->opaque grant id; revoke(grant_id); clear_pause(); dispatch(operation,body)->wire dict. operator CLI uses explicit existing --data-dir, never default production. Local-only methods never routed through machine HTTP/MCP. Disabled runtime does not silently enable on credentials alone.

## Task 1 — Frozen approval binding (core worker)
1. Write a failing test proving no approval means no dispatch.
2. Run focused test in a disposable safe_environment and retain RED log.
3. Implement minimal additive tables and closed refusal response.
4. Add a valid synthetic approval test, then binding fingerprints derived from application/opportunity, profile, answer bank and selected approved document bytes/metadata. No caller-supplied fingerprints. Fail on unknown programme or unverified application target.
5. Run RED/GREEN for drift/revocation/expiry and exact confirmation. Required selected documents must remain approved and byte-identical; use existing ApplicationService preparation/eligibility semantics.

## Task 2 — Single dispatch and recovery (core worker)
1. RED concurrent/replayed request -> at most one execution. Add SQL transaction/CAS and persisted idempotency record; no browser work inside a held write transaction.
2. RED existing live handoff -> no dispatch. Check all Navigator sessions, not only selected application.
3. RED callback guard -> pause/revoke/expiry/drift interrupts further mutations. Guard never leaks raw errors.
4. RED orphaned running record -> INTERRUPTED and pause, not requeued; clear pause/operator approval is explicit.
5. RED fabricated successful outcome/receipt/click flag -> integrity pause. Verify real correlated active handoff and run before success; handle errors/partial work distinctly.
6. Implement source CLI provision/approve/revoke/pause-clear and test explicit root/confirmation/credential privacy. Backups before edits, no real credentials/data.

## Task 3 — Strict client and MCP (transport worker)
1. RED invalid request/response shapes, PII extra fields and noncanonical IDs.
2. Implement protocol.py with strict closed enums/models and no resources/prompts.
3. RED credential endpoint/expiry, redirect, inherited proxy, oversized body and malformed response cases; implement client.py. Never return raw service/network exceptions.
4. RED MCP tools/list exact surface and tool calls against a loopback fixture; implement SDK stdio server. Only fixed tool handlers dispatch, not arbitrary attributes.
5. Test actual MCP subprocess using SDK client. Do not edit pyproject (parent adds optional mcp dependency).

## Task 4 — Shared production PREFILL and server (parent)
1. Freeze public PREFILL response/runner-call tests. Refactor its body to a shared internal function while keeping the decorated wrapper unchanged.
2. RED optional mutation guard calls before Navigator creation and every field write/file upload. Thread callable explicitly through runner to owner-thread journey. No guard is not a bridge grant; bridge dispatch always supplies it. Existing nonbridge callsites remain behavior-compatible.
3. RED default-off / auth-before-dispatch / malformed JSON / no general credential routes. Add dedicated router; enabled service lifecycle in create_app. Keep healthz shape unchanged. Runtime configuration is explicit, off by default.
4. Integrate both reviewed worker commits; run collected existing PREFILL/authority/target/launcher/privacy tests and all bridge tests in one stable tree.

## Task 5 — Real Hermes client and browser proof (parent)
1. Create isolated source server from safe_environment, ephemeral port and synthetic data. Provision separate bridge credential and approve a prepared verified lab application using local operator path.
2. Configure installed Hermes MCP client in a disposable home/config. No real user config/auth copying; no live model required for transport proof.
3. Discover exact tools and call request_approved_preparation through Hermes's real MCP client. Observe production Navigator and inspect live form on its owner thread independently. Assert correct approved field/file values and zero Next/Submit or lab submissions.
4. Verify get run and handoff listing agree with underlying records, replay does not mutate, pause blocks new work, wrong token/no grant/drift/sensitive boundary tests remain negative.
5. Preserve logs/XML/evidence outside repo; cleanup only exact owned processes. Redact all outward data. Distinguish genuine Hermes MCP use from an LLM conversation, which is optional.

## Task 6 — Review and delivery
1. Independent spec review; fix all load-bearing gaps with RED/GREEN tests.
2. Independent security/quality review after spec PASS; address blockers without loosening gates.
3. Write runbook with exact tested commands, optional dependency and configuration. Update spec approval status and README implementation limits; do not overwrite historical failed-suite evidence.
4. Compile, collect, targeted regression, privacy/secret scans and actual executable bridge proof; full-suite attempt separately reported if feasible. Parse JUnit counts, never transplant previous results.
5. Inspect diff, verify unchanged original/live systems, commit local feature branch with evidence summary. Do not push/install automatically.

## Execution discipline

Use <USER_HOME>/Downloads/argus-0.1.0/ARGUS-0.1.0/.venv/Scripts/python.exe for core tests until isolated bridge venv is ready. Each test process imports scripts.verify.safe_environment(ROOT), sets PYTHONDONTWRITEBYTECODE=1, and uses fresh external --basetemp / --junitxml paths. Retain RED logs, then GREEN logs separately. Source stability guard prohibits editing a tree during its tests. Timeouts and failures are evidence, not permission to invent outputs or extend scope. Acceptance is actual exercised implementation, not this plan.
