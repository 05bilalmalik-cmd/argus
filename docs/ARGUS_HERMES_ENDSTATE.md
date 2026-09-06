# ARGUS end-state: unattended operation on Hermes

Status: **design note, not yet built.** Captured 2026-08-27. The dispatch prompt at the
bottom is a draft to be finalised once Phases 8/10/11/12 have landed.

## The goal, in the candidate's words

> "could i get this to a level where this could run without me saying anything and
> automatically search and apply and when it reaches human intervention it can somehow
> notify me"

and

> "see if somehow at the end goal you can have this as a hermes automation thing where they
> have the bots and what not"

## Why Hermes is the right host

Verified against the installed CLI, not assumed:

| Need | Hermes capability | Notes |
|---|---|---|
| Run the sweep on a schedule | `hermes cron` | replaces/augments ARGUS's in-process scheduler |
| Push "a human is needed" | `hermes send -t telegram\|slack\|signal\|discord` | **no LLM, no agent loop, no running gateway** for bot-token platforms; reuses `~/.hermes/.env` credentials |
| Fill unknown-ATS forms | `hermes computer-use` (`cua-driver`, MCP `computer_use` toolset) | Windows supported; real CUA |
| Queue of work awaiting the human | `hermes kanban` | multi-profile board with tasks/comments |
| Emergency stop | `hermes pause` | "pause cron/kanban dispatch and new gateway turns" — the kill switch |
| Inbound triggers | `hermes webhook` | dynamic webhook subscriptions |
| Health/observability | `hermes monitoring`, `hermes status` | |

`hermes send` in particular collapses the whole notification problem to one line:

```bash
hermes send -t telegram "ARGUS: Goldman Sachs — 2027 Placement. Captcha. -> http://127.0.0.1:8787/needs-you/<id>"
```

## Target architecture

```
hermes cron  ──▶  argus sweep            scrape Trackr (region=UK) ─▶ ingest ─▶ classify
                       │
                       ▼
                  argus resolve-targets  priority order: YII/spring ─▶ summer,
                       │                 earliest deadline first
                       ▼
                  argus prepare          programme ─▶ (degree length, grad year, CV variant)
                       │                 coupled in ONE frozen dataclass
                       ▼
              ┌────────┴────────┐
     known ATS│                 │unknown ATS (773 rows)
        adapter│                 │hermes computer-use
              └────────┬────────┘
                       ▼
                  FILLED, NOT SUBMITTED
                       │
                       ▼
                  hermes send  ──▶  phone     "Citadel — captcha, 3 fields need you"
                       │
                       ▼
                  human: captcha + review + submit
```

## Non-negotiable invariants (these are why it is safe to run unattended)

1. **The submit gate stays in code, never in a prompt.** A CUA decides what to click; the
   harness decides what may be clicked. `CONFIRM_SUBMISSION` requires a `before_click`
   callable and a `submission_binding` mapping; a source-resolution executor has neither, so
   a forged command hits "Submission authority gate is not installed". Verified by tracing,
   not by naming.
2. **The model never sees candidate PII.** For unknown-ATS filling, the model maps form
   controls to a *closed enum of field identities*; ARGUS supplies the values from the stored
   profile. A model that never sees the data cannot leak it.
3. **Legal declarations are never model-decided.** Work authorisation, sponsorship, criminal
   record, diversity, any "I certify that…" — answered only from the stored profile by exact
   match. No stored answer ⇒ stop and ask the human. A wrong answer here is a false
   declaration to a real employer.
4. **Programme type drives graduation year AND CV together**, from one frozen dataclass, so
   no code path can produce a mixed pair. Unknown programme ⇒ fail closed to NEEDS_USER.
5. **Notifications carry no PII** — employer, role, reason, and a `127.0.0.1` deep link only.
6. **`hermes pause` is the kill switch** and must be tested as part of the runbook.

## Staged rollout — earn autonomy, don't assume it

| Stage | Autonomous | Human |
|---|---|---|
| 1 | discover, resolve, fill | **every** submit, after review |
| 2 | + submit where no captcha and the run is risk-zero | captcha + spot checks |
| 3 | + unattended overnight | notified only on block |

Do not start at stage 3. On the night this was written, two live defects were found that
would each have gone to real employers: **51 applications carrying a CV with the wrong
graduation year**, and a stored work-authorisation answer reading *"No — I am eligible to
work in the UK"*, which would have answered "are you authorised to work in the UK?" with
**No**. Both were caught only because nothing had ever been submitted. Unattended, a
systematic error reaches a hundred employers before anyone notices, and a job application
has no undo.

## Prerequisites before this can be built

- [ ] Phase 10 — priority ordering + JOB_DETAIL→form conversion (resolution is at ~10%)
- [ ] Phase 8 — unknown-ATS filling
- [ ] Phase 11 — scope control (no graduate schemes; archive non-UK)
- [ ] Phase 12 — notification interface (add a `hermes send` backend to it)
- [ ] Phase 9 — hardening (capability revocation, selection-cache perf)
- [ ] A run of ≥20 applications completed correctly under human review

## Draft dispatch prompt (finalise once the above are done)

> Build the unattended Hermes operating layer for ARGUS at
> `<ARGUS_REPOSITORY_ROOT>`.
>
> Deliver: (1) a `hermes cron` job invoking the ARGUS sweep on a schedule, with a lockfile so
> two runs never overlap; (2) a `hermes send` notification backend behind ARGUS's existing
> notifier interface, carrying employer/role/reason/local-link and no PII; (3) a runbook
> covering start, stop (`hermes pause`), inspect, and recover; (4) an operator dashboard view
> of what ran, what blocked, and why.
>
> Hard constraints: nothing may submit without the existing code-level submission-authority
> gate; the model may never receive candidate PII; legal-declaration fields are answered only
> from the stored profile by exact match and otherwise stop for the human; unknown programme
> type fails closed; notifications are deduplicated, digested and rate-limited so an overnight
> run cannot produce hundreds of pushes.
>
> Ship it OFF by default behind an explicit opt-in, and at rollout stage 1 (human confirms
> every submit). Write failing tests first. Do not weaken any fail-closed check to make a
> number look better.
