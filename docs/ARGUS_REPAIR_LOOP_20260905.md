# ARGUS repair and end-to-end verification — 2026-09-05

## Approved scope
Founder requested repair, repeated verification, and an end-to-end candidate build. Installation approval could not be recovered from the preserved conversation; the running instance must remain untouched. The subsequent prompt for an explicit executable test mode and form-scoped combobox recovery timed out without approval. Never submit real applications. Preserve open human handoffs; never terminate a browser or process not owned by this run. No secrets, live database edits, gate rebaselines, trust-registry widening, or document deletion.

## Baseline evidence
- Git HEAD observed: 17b889e; tracked working tree initially clean, many untracked historical backups.
- GET http://127.0.0.1:8787/healthz: status ok, version 0.2.0, REVIEW_ONLY, live_submit false, trackr_live false.
- Unit baseline in progress; output %TEMP%/argus-baseline-unit-20260905.log.

## Plan and decisive tests
1. Finish baseline unit run without modifying Python sources. Rerun each failure in isolation; distinguish environment/test drift from product bugs.
2. Reproduce generic Attach upload confusion using real headless Chromium and the production adapter/classifier. Expected: CV only receives CV, cover letter gets its own type, transcript/ambiguous upload remains unknown. Attempt falsification with unrelated/sibling headings and conflicting labels.
3. Back up each changed existing file to a timestamped byte-identical sibling, record hashes; use smallest coherent repair, with red/green evidence.
4. Run regression and full suite without concurrent edits. Run isolated loopback E2E repeatedly, including refusal/duplicate/handoff cases; require exact state/readback evidence, not browser titles.
5. Build candidate into a separate directory. Exercise candidate safely; do not assume ARGUS_DATA_DIR survives launcher sanitization. Verify source/runtime identity and no-submit posture.
6. Inspect existing runtime ownership and live handoffs. Install/restart only if safely replaceable, preserving recovery artifacts and exact process ownership. Otherwise report the specific deployment blocker.
7. Record tests, build output, changed files, measured limitations, and live vs synthetic evidence separately.

## Non-claims
A loopback fixture is synthetic evidence only. Real-employer end-to-end coverage remains UNVERIFIED until measured in this session. Existing memory/test counts are not fresh measurements.
