# ARGUS target/provider review — fix round 2 (2026-08-25)

## Scope

This lane covered the isolated target/provider safety contracts only.  No live
employer site, Trackr, production database, build, or installation was used.

Backups created before edits:

- `C:\Users\demo\Downloads\argus-0.1.0-backups\20260825-011122962-AarADeA-provi25er-9ix2-pre`
- `C:\Users\demo\Downloads\argus-0.1.0-backups\20260825-011642485-provider-variant-tests-pre`
- `C:\Users\demo\Downloads\argus-0.1.0-backups\20260825-011715604-target-provider-tests-pre`
- `C:\Users\demo\Downloads\argus-0.1.0-backups\20260825-011843101-navigator-direct-validator-pre`

## Changes

1. `app/automation/host_policy.py` no longer classifies a semantic path word
   such as `/assessment` as candidate data.  Query pairs and fragments still
   inspect recursive percent-encoding/base64, while bodies and headers retain
   their existing fail-closed checks.  This lets the guarded assessment flow
   reach its intentional `NEEDS_OA` boundary.
2. `app/automation/targets.py` compares `FormHandle.frame_url` and
   `bound_target_url` using the target-contract canonical URL.  Default ports,
   trailing slashes, and equivalent query `%20`/`+` encodings therefore bind
   consistently without weakening scheme/host/path identity.
3. `validate_target_resolution_contract()` is a shared nested-evidence
   contradiction gate.  Persisted and direct resolver paths use it before
   worker construction; it checks provider/ATS aliases, employer/role/form
   identity aliases, explicit paths, frame/bound-target URLs, origins, and
   nested form bindings.
4. Provider-variant assertions use neutral `Demo`, `Candidate`, and
   `demo@example.test` fixtures.

## Verification

Focused tests after the final edits:

- `122 passed` — target/provider, egress, target-resolution, handoff, direct
  resolution, assessment-flow, and provider-variant suites.
- `18 passed, 1 warning` — navigator journey E2E suite.
- Assessment guarded E2E: `4 passed`; the assessment scenario reaches
  `NEEDS_OA` and records no submission.
- Direct-resolution adversarial tests: `7 passed`; contradictory nested proof
  raises before any worker is created (`created == []`, no session registered).
- Provider-variant E2E: all Greenhouse, Lever, and Workday happy paths pass;
  each fixture submits exactly once with the neutral candidate values.

The warning is the existing Starlette/httpx TestClient deprecation warning.

## Remaining limitations

- This validates the synthetic loopback laboratory and deterministic provider
  contracts; it does not prove every live employer/custom ATS flow.
- CAPTCHA, MFA, assessments, and unsupported provider journeys remain explicit
  human handoffs.  No CAPTCHA was solved or bypassed.
- Release build/package/install and final whole-suite verification remain
  parent-orchestrator gates.
