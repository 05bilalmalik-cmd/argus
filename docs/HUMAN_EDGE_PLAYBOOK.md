# ARGUS Human-Edge Playbook

> Parallel track to the autopilot. The pipeline maximises volume; this
> maximises *conversion*. Statistically, referred candidates are ~5–10× more
> likely to land interviews than cold portal applications. Run this alongside,
> not instead of, ARGUS.

## 1. Weekly cadence (30 min, Sunday evening)

| Step | Action | Time |
|------|--------|------|
| 1 | Open the funnel dashboard (`/api/dashboard/funnel`) — sort by `queued` desc | 2 min |
| 2 | Pick the top 5 firms where ARGUS has apps in flight but zero human contact | 3 min |
| 3 | For each: run the referral-mining queries below, send 2–3 outreach messages | 20 min |
| 4 | Log every contact in the tracker (§4) | 5 min |

## 2. Referral mining — search templates per firm

Replace `<FIRM>` and `<DIVISION>`. Run on LinkedIn; alumni search works even
with a free account via Google site: queries.

**LinkedIn people search:**
```
site:linkedin.com/in "<FIRM>" ("<graduating 2028>" OR "spring week" OR "year in industry") "LSE" OR "UCL" OR "Warwick"
```
**Alumni angle (strongest):**
```
site:linkedin.com/in "<FIRM>" "<your university>" ("analyst" OR "intern")
```
**Recruiter hunt:**
```
site:linkedin.com/in ("<FIRM>") ("campus recruitment" OR "early careers" OR "graduate recruitment") "London"
```

**Priority order for outreach:** (1) alumni at your university in the target
division, (2) anyone who did the same programme you're applying to within the
last 2 years, (3) campus recruiters. Never message MDs first.

## 3. Message templates

### 3a. Referral request (alumni, warm-ish)
> Subject: Fellow <UNIVERSITY> student — quick question about <FIRM>
>
> Hi <NAME>, I'm a <YEAR> studying <DEGREE> at <UNIVERSITY> and have just
> applied to <FIRM>'s <PROGRAMME>. I saw you've been through <DIVISION> since
> <UNIVERSITY> — would you be open to a 15-minute chat about what surprised
> you most? Not asking for a referral straight away — just keen to learn
> before any interviews.

*Why it works:* asks for time, not a favour; specific programme; no template
smell. If they reply positively, the referral usually follows unasked.

### 3b. Follow-up after chat (the actual ask)
> Thanks again for the chat, <NAME> — the point about <SPECIFIC DETAIL> was
> really useful and I've reworked how I talk about my PwC Risk Analytics
> placement because of it. If you think my profile would resonate with the
> <DIVISION> team, I'd of course be grateful for a referral to
> <PROGRAMME> — happy to send my CV as-is or trimmed.

### 3c. Recruiter cold outreach
> Subject: <PROGRAMME> application — <NAME>, <UNIVERSITY> (PwC Risk Analytics)
>
> Hi <NAME>, I applied to <FIRM>'s <PROGRAMME> on <DATE> (ref
> <SUBMISSION_REFERENCE> if available). Quick context beyond the form: I'm a
> <YEAR> at <UNIVERSITY> reading <DEGREE>, completed PwC's Risk Analytics
> placement, and build systematic trading strategies in Python in my own time.
> If there's anything useful I can add to my file — a transcript, project
> write-up, or a word from someone on the team — I'd welcome the pointer.

*Rules:* one follow-up max after 7 calendar days; never during offer-season
crunch weeks; always name the specific programme.

### 3d. Event/webinar sign-up blurb (use in registration "questions" box)
> Looking forward to the session on <TOPIC>. Currently applying to
> <PROGRAMME>; particularly interested in <DIVISION>.

Then: connect with every panellist within 24h referencing their answer to an
audience question — highest reply-rate channel measured across spring-week
cohorts.

## 4. Contact tracker (keep in this folder)

| Date | Firm | Name | Role | Channel | Programme | Status | Next action | Due |
|------|------|------|------|---------|-----------|--------|-------------|-----|
| | | | | | | sent / replied / call booked / referral given | | |

## 5. Events calendar sweep (weekly, 5 min)

Check, in order:
1. Target firm careers pages → "Events" section (BlackRock, JPM, MS, Citi run
   constant EMEA insight sessions).
2. University careers portal → employer presentations (attendance lists get
   shared with recruiters).
3. BrightNetwork events tab — free, firm-sponsored, attendance tracked.
4. WSO/FinanceFi UK spring-week threads for pop-up events.

Sign up with your real email; these feed the direct-contact layer above.

## 6. What NOT to do

- No mass InMail spam (>3 unanswered messages to one firm = stop).
- Never ask a stranger for a referral in the first message.
- Don't reference ARGUS/automation anywhere, ever. Applications are yours;
  the tooling is private infrastructure.
