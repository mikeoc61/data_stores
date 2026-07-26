# Architecture Decisions

Rationale behind the `data_stores` / `market_warehouse` design. Kept so any
future session starts from the *why*, not just the *what*.

## Context

The Morning Intel Brief (Raspberry Pi, OpenClaw) collects heterogeneous daily
market/system data in parallel, composes it via `compose_briefing.py`, and
delivers by email + Signal. Historically it computed and **discarded** the
numbers each morning. This warehouse persists them so `psignals.py`, the Kai
briefing agent, and ad-hoc analysis can query history.

## Decisions

### 1. Storage engine: DuckDB (not CSV, not SQLite)
- The briefing is 6+ heterogeneous domains, explicitly expanding — a small
  time-series warehouse, not one flat log.
- The core workload is **cross-domain joins** (on-chain apathy × ETF flow
  conviction × DXY × portfolio drawdown). That's the analytical edge; CSV can't
  do it and SQLite does it less ergonomically than DuckDB's columnar engine +
  richer SQL.
- DuckDB → pandas/polars is one import chain for Medium charts and research.
- Consolidates the separately-planned "macro warehouse" roadmap item into one
  store.
- SQLite would have won for a *single* series (zero-dep stdlib); the multi-domain
  join workload flips it. At one series, SQLite; at the briefing-as-warehouse,
  DuckDB.

### 2. Scope unit: one file per dataset (writer + lifecycle), not per agent
- Agents are ephemeral consumers; data is the durable asset. Don't scope storage
  to a compute identity.
- "Per agent" mismatches who-writes-what (one agent touches many datasets; one
  dataset read by many agents). "Per OpenClaw instance" is too coarse (one write
  lock for unrelated data, shared blast radius — already burned once by the
  OpenClaw memory-index corruption).
- Current scope = one long-lived, single-writer, multi-reader dataset →
  **one file, `~/data/market.duckdb`**. A second file only when a dataset has a
  *different sole writer*, *different retention*, or *never joins* the warehouse.

### 3. Table shape: wide for scalar domains, long for multi-entity
- Scalar domains (`onchain`, `btc`, `node`) — one named column per metric.
- Multi-entity domains (`markets`, `etf_flows`, `credit`) — long-format
  (`date, entity, ...`) so new instruments/issuers/tickers are new **rows**,
  never schema changes. Schema-evolution-proof for the parts that grow.

### 4. Write point: the composer, not the collectors
- **SUPERSEDED by #12 (2026-07-23).** The composer was the writer because it was
  the only place all domains were simultaneously typed. But it writes a
  mid-session, HST-keyed, display-derived scrape — the wrong timing, basis, and
  fidelity for a time series. A dedicated ingester replaces it. Kept for the
  *why*: don't reverse-parse display text (still true — the ingester reads the
  node directly, not the brief).
- `compose_briefing.py` already parses every domain into typed, null-guarded
  numeric locals. That's the only place all domains are simultaneously typed.
- So: **capture what the composer already parsed** — do not refactor collectors
  to emit JSON (double-format maintenance) and do not re-parse formatted text
  (brittle reverse-parsing of display strings).
- One new module, one call site at the end of the composer.

### 5. Single-writer discipline enforced at the connection level
- **AMENDED by #12 (2026-07-23):** the single-writer *discipline* holds; the
  *identity* of the writer moves from the composer to the async ingester. The
  composer now opens `read_only=True` like every other consumer.
- Composer opens read-write; every other consumer opens `read_only=True`.
- DuckDB is single-writer per file; the once-daily serial composer is the
  natural sole writer with no locking coordination. Concurrent reads are fine.

### 6. Fail-soft persistence, strictly last
- `write_snapshot` returns bool, never raises. Delivery is load-bearing; the
  warehouse is a side effect. Persist runs *after* the brief is printed, so a
  write failure (lock/disk/corruption) can't block the send.
- Mirrors the collectors' existing fail-soft `run()` pattern.

### 7. Idempotent upsert keyed on date
- Delete-by-date + insert in one transaction. Handles `--force` reruns and
  debug invocations without duplicate rows. Matches the briefing's existing
  at-most-once-per-day idempotency (`SENT_MARKER`).

### 8. NULL is valid data, not missing data
- The composer yields `None` for market-closed days and failed collectors.
  Persist writes the nulls. A weekend row with null equities correctly records
  "markets were closed."

### 9. Data lives outside all repos
- `~/data/market.duckdb`, `.gitignore`d. Referenced by path from all consumers.
  Repos hold code; the file holds data; never the same tree. Backed up on its
  own schedule.

### 10. Backfill strategy
- On-chain: backfills cleanly from the node (`getblockstats` over historical
  heights) → deep retroactive fee/subsidy/fullness/miner-rev series.
- Price/SMA: from any OHLCV source.
- Markets / credit / ETF flows / portfolio: **forward-only** (no clean
  historical source worth the reverse-parse). The domains where long history
  matters most for regime thresholds are exactly the ones that backfill cleanly.

### 11. Payload construction (`build_payload`) lives in the package, not the composer
- **REMOVED by #13 (2026-07-23).** `build_payload` existed to coerce the
  composer's display strings into typed columns. Once the composer stopped
  writing (#12), that job vanished; `aggregate_day` now produces the payload from
  node RPC numbers directly. The drift-guard *principle* below survives — the
  same static-key, bidirectional set-equality test now runs against
  `aggregate_day` output and the DDL (`test_writer_cols_match_ddl`,
  `test_aggregate_day_keys_match_schema`). Kept for the reasoning, which still
  governs the replacement.
- The mechanical string→number coercion (parsing the `hash_rate` display string
  into `hash_rate_ehs` + `hash_rate_7d`, the string counters into int/float,
  null-safety) is a pure function, `build_payload`, in `market_warehouse` — the
  composer imports it alongside `write_snapshot`.
- **Why not inline in `compose_briefing.py`:** that script isn't import-safe
  (`TMP = pathlib.Path(sys.argv[1])` runs at module top, and the whole body
  executes on import), so a function defined there can't be unit-tested from this
  repo without refactoring the load-bearing script. Co-locating it with the
  schema also keeps the payload↔column contract in one repo, tested against the
  real tables via a `write_snapshot` round-trip.
- **This does not weaken decision #4** ("all domains are typed there"). The
  composer's `build_payload(...)` call site still declares every
  domain→local mapping explicitly; the package function only does the generic
  coercion. Typing ownership — *which* local feeds *which* column — stays in the
  composer. `build_payload` is a keyword-only function so that mapping can't drift
  positionally.
- **Drift guard is test-only, and that is exhaustive — not a compromise.**
  `build_payload`'s key set is statically determined (hardcoded literals, not
  data-dependent): it emits the same keys on every call regardless of collector
  output. So a commit-time test asserting bidirectional set-equality between the
  payload keys and the writer's column tuples (`_ONCHAIN_COLS` / `_BTC_COLS`)
  covers every reachable state; no runtime input can produce a key the test
  didn't see. Do NOT add a runtime assert in `build_payload` — it would re-verify
  at 5am what commit time already proved, on a path we deliberately keep boring.
  The valuable direction is schema→builder: a column added to the schema but not
  to `build_payload` is otherwise an invisible always-NULL failure; the equality
  test turns it red. Structured as one `(domain, cols)` pair per table
  (`SCHEMA_DOMAINS` in `tests/test_payload.py`) so each new table (increments 2–3)
  extends it by one line.

### 12. Warehouse population decoupled from the briefing; async ingester is sole writer
- The briefing becomes a pure **reader** of daily aggregates. A dedicated
  systemd-managed job is the **sole writer** (supersedes #4/#5's writer identity;
  the single-writer discipline itself is unchanged — everyone else, composer
  included, opens `read_only=True`).
- **Why:** the composer wrote a rolling-24h window ending at briefing time
  (~06:00 HST / 16:00 UTC) — a mid-session, HST-keyed, display-reverse-parsed
  scrape. Wrong timing (not a close), wrong basis (HST vs on-chain UTC), wrong
  fidelity (strings, not node numbers), and **impossible to backfill**. Analytics
  (SMA, regime thresholds, apathy streaks) need consistent UTC daily-close bars
  with deep history.
- **UTC calendar-day bucketing.** Row dated `D` aggregates blocks with header
  timestamp in `[D 00:00 UTC, D+1 00:00 UTC)`. Only **complete** days are
  written; at briefing time the newest complete UTC day is ~16h old — expected.
- **One aggregation definition, three callers.** `aggregate_day(date, rpc)` is
  the single implementation; backfill, the daily job, and gap-fill all call it.
  No second day-aggregation anywhere. It is pure and node-RPC-injectable, hence
  unit-testable on the Mac without a node (`test_aggregate.py`).
- **Block timestamps are UTC and non-monotonic near boundaries** (consensus
  allows ~2h forward drift, bounded by median-time-past). Day ranges are resolved
  by *actual* timestamp with a `BOUNDARY_MARGIN` scan, never by assuming the first
  block past a boundary is the boundary.

### 13. Schema v2 — stored raw daily facts; derived series are query-time SQL
- Renames: `blocks_24h` → `blocks_day` (a calendar-day count, not a rolling
  window); `btc.price` → `btc.close`.
- **Dropped `hash_rate_7d`, `tx_rate_7d`.** They are pure functions of the stored
  series (value at t vs t−7d); storing them denormalizes and risks silent
  inconsistency on any re-backfill/correction. Computed in `query.py` as a
  **7-day DATE-RANGE** window (`<= date − 7d`), never positional `LAG` — `LAG`
  silently spans >7 calendar days across a gap and mislabels itself as "7d".
- **`retarget_proj` — the "both" decision.** The **cumulative** projection (pace
  over the current difficulty period, up to 2016 blocks, not day-aligned) is
  **stored** — it is not recoverable from the other daily columns and it matches
  the brief's display. The **day-pace** variant `(blocks_day/144−1)·100` is
  **query-time** (`day_pace_retarget`) — more responsive (no period-average
  dilution) and the **preferred** value for miner-stress signal thresholds.
  Stored `retarget_proj` is **NULL for the first `MIN_BLOCKS_FOR_PROJ`=10 blocks**
  of a period: at `blocks_elapsed` ≤ ~5 the pace is single-block noise (e.g. one
  33s block → +1718%). The threshold is **10, not 144** — by ~10 blocks the pace
  is stable, and real early-period signal must survive: the first slow-block days
  of the 2021 China-ban capitulation closed at `blocks_elapsed` ~16–72, already
  projecting toward the historic −28% adjustment. The composer's live line falls
  back to the day-pace variant only in that <10 window.
- `block_fullness` is now a DOUBLE (daily mean of `total_weight/4e6·100`), not the
  old point-in-time integer.

### 14. `btc` stores only the daily `close`; SMA is query-time; it ≠ the brief's live spot
- **`btc(date, close)` only** (corrected 2026-07-25). `sma200`/`sma200_pct` are
  **not stored** — they are `query.py` helpers (`sma200`, `sma200_pct`), the same
  #13 reasoning that dropped `*_7d`: the 200-day SMA is a pure function of the
  close series, so materializing it denormalizes and risks staleness on any
  correction or re-backfill. This makes the OHLCV writer and the on-chain writer
  the **same shape — both store raw daily facts, nothing derived.** (Source is an
  external OHLCV feed; needs ≥200 closes before the first SMA value.)
- The stored `close` is a **daily close**; the briefing continues to display
  **live spot** from its own collector — a display value, deliberately *not*
  routed through the DB. Same asset, two numbers, two purposes: a historical close
  bar for analytics vs. "what is BTC right now." Do not reconcile them.

### 15. Kraken as the `btc.close` source: CSV for deep history, REST for the daily edge
- **Two access paths, one canonical venue.** Kraken's public REST OHLC endpoint
  only serves the last ~720 candles (a hard cap), so it cannot reach 2016. Deep
  history comes from Kraken's **downloadable OHLCVT CSV** (`XBTUSD_1440.csv`), a
  one-time manual download consumed by the one-shot `btc_backfill`. The daily
  edge comes from the **REST** endpoint (720 days ≫ the 1-day gap), used by
  `daily_update`. Same venue's close either way, so the series is consistent.
- **Close = index 4 in both formats** (CSV `ts,o,h,l,close,vol,trades`; REST
  `[ts,o,h,l,close,vwap,vol,count]`); the date is the candle's UTC open day. The
  in-progress current day is naturally excluded because writers cap the range at
  `last_complete_utc_day`.
- **One writer preserved.** btc daily is folded into `daily_update` (on-chain
  then btc, sequential in one process), not a second service — honours #12. btc
  is **fail-soft within that run**: a Kraken outage logs a WARN and does not flip
  the exit code or block the on-chain write (the node data is load-bearing; the
  price edge self-heals via gap-fill). The one-shot `btc_backfill` is a separate
  operator-run entrypoint, like the on-chain backfill.

### 16. The brief's warehouse read + formatting lives in `view.py`, not the composer
- `market_warehouse/view.py` — `onchain_day_view()` returns an `OnchainDayView`
  (`day_line`, `stale_line`, `day_pace`, `retarget_fragment()`), presentation-ready.
  The composer's DB code drops from ~35 lines to ~6: call it, render the strings.
- **Why in the package, not the briefing repo** — the same reasoning as #11:
  `compose_briefing.py` is not import-safe (`sys.argv[1]` at module top), and the
  briefing repo has **no test infrastructure** (no pytest, rsync-deployed
  appliance). Code placed there is untestable in practice. In the package it is
  covered by the existing suite (`tests/test_view.py`), and the column→display
  contract sits next to the schema that defines it.
- **It also removes duplicated domain constants.** The composer previously
  hardcoded `2016` and `10` (`RETARGET_INTERVAL`, `MIN_BLOCKS_FOR_PROJ`) — a
  silent-drift bug: re-tuning the guard in `aggregate.py` would leave the brief
  disagreeing. `retarget_fragment()` imports both, so there is one home.
- **Tradeoff, accepted knowingly:** this puts brief-flavoured presentation into an
  otherwise consumer-agnostic library. Contained by keeping `view.py` a thin
  adapter, strictly separate from the core (`schema`/`write`/`query`/`aggregate`)
  and importing only public query helpers. If a second consumer ever wants a
  different format, add a function here — do not push formatting back into a
  consumer that cannot test it.

## Repo/project relationship
- `data_stores` = local git repo (umbrella; package `market_warehouse` inside).
- Claude Project = working context. This `DECISIONS.md` + key source files go in
  Project Knowledge so every session inherits the architecture deterministically
  rather than via lossy memory. Project memory/search is project-scoped.

## Conventions
- Python ≥ 3.11, `src/` layout, pytest.
- No inline comments in code. Type hints throughout.
- Repo name ≠ package name is intentional and consistent with existing repos.
