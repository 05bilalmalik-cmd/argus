# Scheduled Discovery (legacy no-login flow)

## What the scheduler does

`app/scouting/scheduler.py::_run_sweep_locked` runs one sweep pass each
interval (plus once ~20s after boot) when automation is explicitly enabled
(`ARGUS_AUTOMATION_MODE` non-OFF) and the interval is positive:

1. **Public HTTP harvest** — `app/scouting/sources/base.py::collect_all_report`,
   the same feed manual `POST /api/scout/full-sweep` uses via `collect_all`.
   Pure `httpx` + BeautifulSoup, no browser, no login. Greenhouse / Lever /
   Bright Network / RateMyPlacement / WikiJob plus watchlist slugs.
2. **Trackr supplements** — live Playwright scrape only when
   `ARGUS_ENABLE_TRACKR_LIVE=true`; saved-HTML drops from
   `data_dir/trackr_saves` always.
3. **Ingest + autopilot** — `ScoutService.ingest` then `run_autopilot(...,
   max_runs=10)`. Submission still requires ARMED/RUNNING, configured
   conflict rules, and an exact action-time confirmation id; scheduled runs
   pass none, so they stop at the review boundary. Existing target / CV /
   eligibility / consent gates are unchanged.

## Truthful per-source reporting

`collect_all_report(settings, sources=...)` returns `(items, report)` where
`items` is byte-identical to what `collect_all` returns (deduped +
early-careers filtered) and `report` carries:

- `attempted / succeeded / failed` source counts,
- `per_source` raw row counts keyed by source name,
- `errors` keyed by source name (`TypeName: detail`, truncated),
- `total_raw` (pre-dedupe) and `total` (post-filter).

One failing source logs a warning and contributes nothing; it never sinks
the sweep. The sweep result adds:

- `public_sources`: the report dict,
- `per_source`: merged counts over all scraped rows by `source` prefix
  (same convention as manual full-sweep),
- `discovery`: `ok | partial | failed | empty | disabled`.

An all-failed sweep reports `discovery: failed` — never an empty-success.
`partial` means some sources failed but rows were still collected.
A collector-level throw (registry construction, merge failure) sets
`collector_error` and likewise yields `failed` (no rows at all, including no
Trackr supplement) or `partial` (Trackr rows still ingested).

## Report hardening (follow-up)

- Materialization and row validation happen inside the per-source boundary:
  a generator that raises mid-iteration, or rows lacking source-row
  attributes, rejects that source alone — earlier good sources are never
  discarded by the later global dedupe. Partial rows from a failed fetch are
  not returned. Validation covers every attribute the identity key reads
  (`employer`, `role_title`, `source_url`, `location`, `division`), all of
  which must already be text — `source_identity_key` calls `.strip()` on
  each, so a non-string value would crash the merge and lose all sources.
- Per-source error text is a fixed whitelist — exception type plus a known
  category only (`HTTP <code>` for transport failures, `timed out` /
  `connection failed` / `TLS failed` for known transport kinds, bare type
  name otherwise). No arbitrary exception message is ever retained, since
  messages routinely echo URLs and credentials. The scheduler's collector
  catch likewise logs the type name only, never `logger.exception`.
- Duplicate source registrations are explicitly reported in
  `duplicate_sources` (plus an `errors["duplicate_sources"]` entry); counts
  merge under the shared name.
- Counters distinguish `registered` (known sources) from genuinely
  `attempted` (`succeeded + failed`); `registered` always equals
  `attempted + len(skipped)`.
- Bounded harvest: `collect_all_report` accepts `deadline_seconds` and an
  `is_cancelled` callback, checked between sources with unattempted names in
  `skipped` and `truncated` set to `"time_budget"` / `"cancelled"` — always
  surfaced as `partial`, never empty-success. The bound is cooperative, not
  strict: one active source may issue several HTTP calls and overrun it
  (each call still honors `HTTP_TIMEOUT = 20`); there is deliberately no
  threadpool, so inflight work is ever one request and the bound can never
  preempt a running source. `deadline_seconds=None` is the explicit
  backward-compatible unlimited mode; any other value must be finite and
  non-negative, otherwise nothing is fetched and the report carries a
  `collector_error`. A raising cancellation callback fails closed: fetching
  stops at once with the error recorded. The scheduled loop passes
  `_STOP.is_set` so shutdown stops the sweep between sources; manual callers
  get no callback by default because the global `_STOP` may already be set
  outside the loop. The scheduler harvest budget
  (`_PUBLIC_HARVEST_BUDGET_SECONDS = 300`) needs no new setting or trust.
- Explicit shutdown cancels the pipeline too: when the stop callback is set
  after harvest, the sweep returns `ingest: cancelled` with an autopilot
  `cancelled: scheduler_stopping` marker instead of touching the database,
  navigator, autopilot, or digest — already-collected rows wait for the next
  pass. Proven by synthetic cancel tests, not assumed.

## OFF and locks

- OFF (`autopilot_enabled == False`) is a hard stop: zero source fetches,
  zero ingest, zero autopilot. Result carries `skipped: True`,
  `reason: automation_off`, `public_sources: disabled`, `ingest: disabled`.
- Concurrency is unchanged: in-process `_SWEEP_LOCK` plus the
  `scout-sweep.lock` file lock. A held lock returns `skipped: True`,
  `reason: sweep_locked` without calling any source.
- `HTTP_TIMEOUT = 20` in `sources/base.py` is preserved; collection stays
  sequential — no new threads, no multiplied sweep work.

## Deliberate tracker separation (not a missing feature)

Per `docs/ARGUS_TRACKER_FIRST_DESIGN.md`, `app/tracker/` is an
intentionally standalone discovery/tracking service with its own SQLite
store and refresh worker; it holds no candidate or browser authority.
Transfer to the legacy preparation workspace is explicit via CSV export,
verified by `tests/tracker/test_legacy_transfer.py` (export round-trips
into `OpportunityService.import_csv` with `automation_url` and
`application_url` left `None` — no implicit automation authority).
This repair therefore completes the existing legacy public-source flow
instead of bridging the two databases.

## Tests

`tests/unit/test_scheduler_campaign.py` (injected callables only, no real
network): report parity with `collect_all`, OFF zero-calls, mixed/error/
empty/all-failed reporting, file-lock contention, repeated-sweep dedup,
no-submit confirmation, timeout/thread bounds.
