# ARGUS Phase 19 - application-window and Trackr identity final evidence

Date: 2026-08-27
Branch: `codex/argus-safety-upgrade` (intentionally dirty; no commit)

## Outcome

Phase 19 is complete. ARGUS now stores and enforces a first-class application
window: `OPEN`, `NOT_YET_OPEN`, `CLOSED`, or fail-closed `UNKNOWN`.

- Only `OPEN` opportunities can expose an automation target or enter the
  target-resolution/application work paths.
- `NOT_YET_OPEN` rows remain visible and are re-evaluated on each complete live
  sweep.
- `CLOSED` rows use the existing reversible archive sidecar with the distinct
  reason `closed_application_window`.
- `UNKNOWN` is never treated as open.
- Trackr listing pages and employer application targets are separate; a
  Trackr-owned host is never persisted as an application target.
- Trackr records now have stable provider identity. The prior name-only
  collapse has been repaired without merges, guesses, or deletions.

No opportunity or application was deleted. Phase 19 ran no target-resolution
sweep, form interaction, submission, submission intent/authority creation, or
`RunMode.SUBMIT`.

## Literal live payload investigation

A read-only browser capture covered all four canonical listing sources and 968
raw programme rows. The response shape was `{"groups": ..., "programmes":
[...]}`.

The relevant literal top-level fields were:

- programme link: `url`;
- explicit state: `status` (null on all 968 rows);
- recruitment stage: `currentStage` (non-null on 23 summer rows, with values
  such as `Online Test` and `First Round`; not an application-window state);
- dates: `openingDate`, `closingDate`, `lastYearOpening`, and `eventDate`
  (`eventDate` null on all rows; `lastYearOpening` is historical evidence);
- rolling/cycle: `rolling` and `season` (`"2027"` on all rows).

No top-level `deadline`, `state`, `open`, `closed`, `opensSoon`, `tbc`, or
`cycle` field existed. Summer contained one nested group whose literal
`groups[].status` was also null.

| Trackr slug | Raw rows | `url` present | `openingDate` | `closingDate` | `lastYearOpening` | Literal `rolling` true / false / null |
|---|---:|---:|---:|---:|---:|---:|
| `industrial-placements` | 175 | 6 | 6 | 2 | 175 | 161 / 7 / 7 |
| `spring-weeks` | 112 | 7 | 7 | 4 | 109 | 96 / 16 / 0 |
| `summer-internships` | 440 | 69 | 68 | 23 | 428 | 408 / 29 / 3 |
| `off-cycle-internships` | 241 | 241 | 241 | 185 | 0 | 0 / 0 / 241 |
| **Total** | **968** | **323** | **322** | **214** | **712** | **665 / 52 / 251** |

Representative literal values included an ExxonMobil industrial opening of
`2026-08-24T00:00:00.000Z`, a Patrizia spring opening/closing pair of
`2026-08-10T00:00:00.000Z` / `2026-08-13T00:00:00.000Z`, a LionTree summer
closing date of `2026-09-30T00:00:00.000Z`, and a Natixis off-cycle opening /
closing pair of `2026-05-05T00:00:00.000Z` /
`2026-06-04T00:00:00.000Z`.

The candidate's missing-link heuristic is useful only after dates: 186 linked
rows already had a past closing date at capture time, so link presence alone
cannot prove openness. One supplied URL was Trackr-owned; three supplied values
were invalid application targets in total. The classifier therefore uses
recognized explicit status, then valid dates, then validated link evidence,
and otherwise fails closed.

Company-level `careersSite`, `ukJtpLink`, `usJtpLink`, and `ukHousingLink`
fields were present, but the API did not label them as programme application
links. No separate detail endpoint was observed, so Phase 19 did not promote
those alternate resources.

## Payload identity defect found and fixed

The inherited parser globally deduplicated on normalized `(company, title)`.
The live evidence proved that unsafe:

| Identity measure | Count |
|---|---:|
| Raw rows / prior name-key rows | 968 / 930 |
| Collapsed rows | 38 |
| Collision groups | 35 |
| Distinct IDs in collision groups | 73 |
| Groups whose IDs were all distinct | 35 |
| Groups differing in URL / opening / closing | 13 / 12 / 11 |
| Existing matching DB rows for those 73 IDs | 40 |
| Cardinality shortfall | 33 |

Ten collision groups existed solely within one slug, so adding the slug to the
old key would not have solved the problem. Phase 19 now:

- normalizes and persists `trackr_programme_id`;
- enforces an exact partial unique index on non-null IDs;
- deduplicates the payload by provider ID and rejects conflicting duplicates;
- plans the whole batch ID-first;
- directly refreshes a matching ID, safely claims only unambiguous legacy
  rows, and otherwise inserts the missing identified row;
- never uses a differently bound row as a broad fallback; and
- retains ambiguous legacy evidence through the reversible reason
  `ambiguous_trackr_identity` rather than guessing or merging.

The source schema constant is now 8. The live database contains the identity
column and an independently validated exact partial unique index with 968
unique values and zero duplicates.

## Implementation map

- `app/scouting/application_window.py`: state enum, date/status/link rules,
  Trackr-host rejection, and fail-closed coercion.
- `app/scouting/trackr_live.py`: literal payload parsing, stable record IDs,
  source/application URL separation, date/cycle/rolling evidence, strict URL
  validation, and ID-only deduplication.
- `app/scouting/trackr_identity.py`: deterministic whole-batch identity plan.
- `app/scouting/service.py`: ID-first refresh/claim/insert, ambiguity handling,
  state/date/rolling transitions, audited reversible archives, target
  preservation, statistics, and legacy-window cleanup.
- `app/models.py` / `app/db.py`: application-window/date columns, nullable
  provider identity, exact partial unique index, and additive migrations.
- `scripts/backfill_application_windows.py`: default dry-run, explicit apply,
  verified online backup, evidence snapshots, invariant checks, and a
  main-only exact four-source capture guard.
- `scripts/repair_trackr_identities.py`: default dry-run identity projection;
  RuntimeLock plus SQLite writer reservation; verified backup-before-live-
  schema ordering; drift, relationship, audit, deletion, immutable-evidence,
  redaction, and full idempotency gates.

The inherited OPEN-only guards in the work selectors, target-resolution
service, automation URL, and application preparation were verified. The
resumed work did not edit Phase 17's `app/services/navigator.py`, Phase 18's
document/application services, or Phase 20's templates, static assets, or page
routers.

## First live application-window backfill

The interrupted run had already completed this write. It was reconstructed
from both backups, the live database, and the exact 1,299-event audit delta.

Database: `%LOCALAPPDATA%\ARGUS\argus.db`

First pre-write backup:

`%LOCALAPPDATA%\ARGUS\backups\argus.db.pre-phase19-application-window-20260827T161529907057Z-e92f432ccb694928ad177fce2904b900.bak`

SHA-256:
`A01272B5D46CEE244C803A4B925D672E1DC30B1B2DF7E51272379F50E6C05F94`

Second/idempotency pre-write backup:

`%LOCALAPPDATA%\ARGUS\backups\argus.db.pre-phase19-application-window-20260827T161603113567Z-d1066e198a0c471993d157671bbc19d1.bak`

SHA-256:
`51926A3AB24F9A5C2A80C7AC1062F8637C38FE855DC116833B3585457030B031`

Both SQLite online backups passed `integrity_check` before their transaction.

| Metric | Before | After first window backfill |
|---|---:|---:|
| Opportunities / applications | 1,127 / 1,124 | 1,127 / 1,124 |
| OPEN / NOT_YET_OPEN / CLOSED / UNKNOWN | 0 / 0 / 0 / 1,127 | 329 / 626 / 172 / 0 |
| Captured employer application URLs | 9 | 307 |
| Real links by application-target-or-source evidence | 497 | 498 |
| Deadlines | 0 | 200 |
| Rolling true / false | 1,122 / 5 | 645 / 482 |
| Active archives | 59 | 231 |

The first live ingest saw the then-name-deduplicated 930 rows, updated 930,
imported 0, preserved one verified target, backfilled 298 application URLs,
migrated 304 legacy source URLs, and created 172 closed-window archives. The
residual pass classified 197 legacy rows and cleared 192 unsupported historical
rolling defaults. Its exact audit delta was 930 refresh events, 197 residual
window events, and 172 archive events. The separately backed-up second pass was
a complete no-op and deleted zero rows.

## Guarded live Trackr identity repair

The default dry-run recaptured all 968 rows without changing the source file's
bytes or modification time. Capture evidence:

- captured IDs: 968 unique;
- capture time: `2026-08-27T19:22:38.160374Z`;
- IDs SHA-256:
  `557d60aff547b8a22e1dbde7f316587767ac5728ba125c083887fdcf5da1adf2`;
- material payload SHA-256:
  `259aadf1bba15f99338d32c00dacb79d23156cb8a75fa8a4f65a08bd7e1fb3d8`.

The exact plan was 920 safe legacy claims and 48 inserts. Ten ambiguity
cohorts contained 20 raw IDs; four unarchived ambiguous legacy rows were
reversibly archived, six already-archived legacy rows were preserved, and 113
other unbound legacy Trackr rows were retained. The projection allowed only
the exact +48 opportunities / +48 applications and passed every relationship,
archive, audit, immutable-evidence, deletion, and identity-index gate.

The packaged ARGUS runtime holding the lock was stopped only after its exact
process path was verified. The live apply then held the application RuntimeLock
from projection through backup, used `BEGIN IMMEDIATE`, and checked the full
pre-write guard after the backup.

First identity-repair backup:

`%LOCALAPPDATA%\ARGUS\backups\argus.db.pre-phase19-trackr-identity-20260827T192626488844Z-a8209bcfdfe3442c83e1db7d02edb7b0.bak`

- SHA-256:
  `118bade67f8f25e946d83c1ac055395941443a9703f31a10408d9f382de2a746`
- size: 14,036,992 bytes;
- integrity: `ok`; foreign-key errors: 0.

| Metric | Before identity repair | After identity repair |
|---|---:|---:|
| Opportunities | 1,127 | 1,175 |
| Applications | 1,124 | 1,172 |
| Active archives | 231 | 254 |
| Audit events / outbox | 10,452 / 4,523 | 12,459 / 6,530 |
| Stable Trackr IDs | 0 | 968 |

Applied ingest totals were exactly 920 claims, 48 inserts, 920 existing-row
updates, 48 new applications, 31 URL backfills, and zero deletions. The 968
captured source rows classified as OPEN 137, NOT_YET_OPEN 645, CLOSED 185, and
UNKNOWN 1; 320 supplied application URLs were accepted, 3 rejected, and 648
rows had no validated employer link. The invariant gate proved prior verified
targets, prior archives, automation/form/submission/document evidence, and all
pre-existing application bindings unchanged.

A second separate live `--apply` created its own verified backup:

`%LOCALAPPDATA%\ARGUS\backups\argus.db.pre-phase19-trackr-identity-20260827T192828243446Z-aba200ff3dd34f6999341929da1d01b8.bak`

- SHA-256:
  `2e84554dcaa75c774ac00236371f720e6803103fd86035ed548f7ab192d27a9b`
- size: 17,440,768 bytes;
- integrity: `ok`; foreign-key errors: 0.

That second command matched 968 IDs directly and performed zero claims,
inserts, updates, applications, archives, or audit appends. Counts and complete
pre-write evidence were unchanged.

## Complete-capture fail-closed guard

Two post-repair dry-run attempts exposed a production boundary the original
brief did not anticipate: a browser timeout could return a non-empty but
partial capture. Both production CLI entry points now require at least one
canonical row from every supported slug, exact `trackr_live:<slug>` provenance,
the canonical listing URL, and exact slug/programme mapping before Settings,
database discovery, backup, or mutation-capable dispatch. Missing, unexpected,
or malformed source lists exit 2.

After this fix, a complete 968-row application-window dry-run reported all 968
as direct identity matches, with zero updates/imports/applications/archives/
audits and no identity mutations. Its before/projected state, dates, rolling
counts, and row counts were identical. Independent review approved the guard
with 0 Critical, 0 Important, and 0 Minor findings.

## Final live read-only state

| Programme | OPEN | NOT_YET_OPEN | CLOSED | UNKNOWN | Total |
|---|---:|---:|---:|---:|---:|
| `spring_week` | 6 | 105 | 1 | 0 | 112 |
| `year_in_industry` | 6 | 169 | 1 | 0 | 176 |
| `summer` | 326 | 371 | 189 | 1 | 887 |
| **Total** | **338** | **645** | **191** | **1** | **1,175** |

Additional final facts:

- applications: 1,172;
- stable Trackr IDs: 968 unique; duplicate bindings: 0;
- retained unbound legacy Trackr rows: 113;
- captured employer application URLs: 335;
- real employer links by application-target-or-source evidence: 526;
- deadlines: 221;
- rolling true / false: 665 / 510;
- active archives: `closed_application_window` 191,
  `ambiguous_trackr_identity` 4, `non_uk_location` 59;
- `PRAGMA integrity_check`: `ok`;
- `PRAGMA foreign_key_check`: 0 rows;
- submission intents / authorities / review bindings / lab submissions: all 0;
- email messages: 0.

The single `UNKNOWN` row is intentional fail-closed behavior, not unfinished
backfill: KH Holdings / `Investment Banking - Origination Internship`, Trackr
ID `64zgvovjp1`, has an opening date of 2026-06-29 after its closing date of
2026-06-28. Contradictory dates must not be inferred open.

There was concurrent target-resolution activity from another phase between the
first window backup and identity repair: 32 application URLs were cleared and
62 rows gained target-resolution evidence/attempt timestamps. Identity repair
backfilled 31 of those URLs from current Trackr evidence while its invariant
preserved every already-verified target. Phase 19 itself did not invoke target
resolution.

The restarted packaged `dist\ARGUS.exe` is healthy at `/healthz` with HTTP 200,
automation OFF, live submission false, and Trackr live disabled. It is an older
package and resets `PRAGMA user_version` to 6 on startup; this was observed
after the source repair had applied the current migration. The additive v8
identity column and exact unique index remain present and independently valid.
This marker/package mismatch is recorded plainly; no Phase 20-owned packaging
or UI artifact was rebuilt as part of Phase 19.

## `LIKE '%trackr%'` anomaly

The corrected read-only predicate returned 36 rows across 22 employer values,
not the earlier reported eight. Nine were saved-HTML provenance rows; 27 were
historical `trackr_live*` rows. Their external ATS/search URLs contain Trackr
attribution parameters such as `gh_src=Trackr`, `trid=Trackr`, or `iis=Trackr`.
They are not the current API's missing-link population and were not conflated
with it.

## Verification

Every pytest command used `-p no:cacheprovider`.

- Stable identity core final review: APPROVED, 0 Critical / 0 Important / 0
  Minor.
- Identity repair final review after three adversarial fix rounds: APPROVED, 0
  Critical / 0 Important / 0 Minor.
- Complete-capture guard review: APPROVED, 0 Critical / 0 Important / 0 Minor.
- Fresh focused Phase 19 suite: **136 passed in 40.33 s**.
- Exact required full suite
  `tests\unit tests\integration tests\e2e -p no:cacheprovider`:
  **1,627 passed, 2 skipped, 2 warnings in 1,129.30 s**.
- Phase 19 production/tests `compileall`: passed.
- Scoped tracked and untracked whitespace checks: passed.
- Privacy regressions after final documentation: **15 passed in 21.17 s**;
  the three final Phase 19 docs contain no literal local-user path.

The two skips were the Windows symlink-privilege test and opt-in live-network
smoke test. The warnings were a third-party Starlette/httpx deprecation and an
intentional duplicate archive-member fixture. Neither is a Phase 19 failure.

The first full-suite command reached its 20-minute wrapper ceiling and left an
orphaned Windows pytest tree; it supplied no verdict. That exact orphan tree
was identified by command line and creation time and stopped. The clean rerun
above was the sole surviving suite and exited 0.

## Final safety totals

- Opportunity rows deleted: **0**.
- Application rows deleted: **0**.
- Submissions made: **0**.
- Forms filled: **0**.
- `RunMode.SUBMIT` entered: **no**.
- Target resolution run by Phase 19: **no**.
- Protected Phase 17/18/20 files changed by the resumed Phase 19 work: **0**.
- Commits created: **0**.
