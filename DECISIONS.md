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

### 11. Schema-drift is guarded at test time, not runtime (was: `build_payload` placement)
- **The function is gone; the guard is the part that mattered.** `build_payload`
  coerced the composer's display strings into typed columns. That job vanished
  when the composer stopped writing (#12) and `aggregate_day` began producing
  payloads from node RPC numbers directly. Its *placement* argument — put it in
  the package because the briefing repo had no tests — was later reversed by #16
  once that repo gained test infrastructure.
- **The surviving principle: guard payload↔schema drift with a commit-time test,
  never a runtime assert.** A producer's key set is statically determined
  (hardcoded literals, not data-dependent), so a bidirectional set-equality test
  against the writer's column tuples covers every reachable state — no runtime
  input can produce a key the test didn't see. A runtime assert would re-verify at
  02:00 UTC what commit time already proved, on a path deliberately kept boring.
- **The valuable direction is schema→producer:** a column added to the schema but
  not to the producer is otherwise an invisible always-NULL failure. Now enforced
  by `test_writer_cols_match_ddl` (introspects the real DDL) and
  `test_aggregate_day_keys_match_schema`. Extend by one `(domain, cols)` pair per
  new table.

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

### 13. Schema (v2, now v3) — stored raw daily facts; derived series are query-time SQL
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

### 16. The brief's warehouse read + formatting lives in the briefing repo, beside the composer
- `openclaw-briefing-agent/scripts/warehouse_view.py` — `onchain_day_view()`
  returns an `OnchainDayView` (`day_line`, `stale_line`, `day_pace`,
  `retarget_fragment()`), presentation-ready. The composer's DB code drops from
  ~35 lines to ~6: `import warehouse_view`, call it, render the strings.
- **Why beside the composer, not in the package.** Presentation is a *consumer*
  concern; the warehouse stays consumer-agnostic (#2 — data is the durable asset,
  consumers are ephemeral). It also matches the briefing's existing architecture:
  `scripts/` is full of standalone, individually-runnable helpers, imported as
  plain siblings (`import local_config`). A first pass put this in
  `market_warehouse/view.py`; that coupled the library to one consumer's line
  format and was reversed.
- **Standalone-runnable by design.** `./warehouse_view.py` (plus `--json`,
  `--db`, `--retarget-proj/--blocks-left`) prints exactly what the brief would
  show, so the DB query can be inspected on the Pi without running collectors —
  the same debuggability every other `scripts/` collector has.
- **Domain constants are imported, never copied.** The composer previously
  hardcoded `2016` and `10`; `warehouse_view` imports `RETARGET_INTERVAL` and
  `MIN_BLOCKS_FOR_PROJ` from `market_warehouse.aggregate`, so re-tuning the guard
  cannot leave the brief silently disagreeing. A test asserts the identity.
- **Testability is preserved, not traded away.** The briefing repo previously had
  no test infrastructure — the reason #11 pushed `build_payload` into the package.
  That gap is now closed: a root `conftest.py` puts `scripts/` on `sys.path` and
  `tests/test_warehouse_view.py` covers formatting, staleness, NULL/partial rows,
  the retarget branches, and the CLI. Run with any env that has pytest + duckdb +
  `market_warehouse`. New `scripts/` helpers should be import-safe and land tests
  here rather than being pushed into the warehouse package.

## Signals

Empirical findings about the data itself, established by backtesting the query
helpers against the backfilled history (2016→present) on the Pi. These were
expensive to learn; do not re-derive them from scratch.

### 17. `fee_subsidy` has strong weekly seasonality — any daily threshold must account for it
- Measured over a 730-day window: weekends run **27% lower** than weekdays on
  both mean (1.077 vs 1.468) and median (0.740 vs 1.013) — a genuine level shift,
  not skew. Full weekly cycle, Sunday lowest, Thursday highest.
- Consequence: **73% of the bottom decile falls on Sat/Sun** against a 28.6%
  baseline — a 2.5× over-representation. A raw daily percentile therefore reports
  substantially *what day of the week it is*. A day that IS exactly the average
  Saturday ranks at the **14th** percentile raw and the **50th** once corrected.
- Two corrections exist in `percentile_rank`, both cancelling the cycle exactly
  because a week contains one of each weekday: `smooth_days=7` (trailing mean;
  steadier, but a low-pass filter that blurs single-day moves) and
  `detrend_dow=True` (residual vs that weekday's own mean; keeps daily
  resolution). The brief's Signal line uses `smooth_days=7`.
- The **absolute** `apathy_streak` threshold (1.0%) is currently safe only because
  fees sit far below the 2-year median. At the median, weekday medians straddle
  1.0% (Thu 1.023, Fri 1.061) while weekends do not — the streak would break every
  Thursday and cap near **5 days**, becoming a weekend detector. Danger zone is
  roughly 0.9–1.2% average fee/subsidy; if the streak starts oscillating 0–5,
  raise the threshold rather than distrusting the number.
- The brief's `Day (UTC … Sat)` line names the weekday for the same reason: it is
  deliberately one day's raw facts, and 2 of 7 briefs (Sun/Mon HST) report a
  weekend UTC day.

### 18. Relative and absolute thresholds answer different questions — keep both
- A percentile threshold is drawn from the same window it measures, so by
  construction only `percentile`% of days can ever fall below it *however
  depressed the regime*. It detects a **new leg down**; it cannot express the
  **duration** of a sustained one. Observed: `apathy_streak_pct` = 1 while the
  absolute `apathy_streak` = 19 on the same data, both correct.
- So `apathy_streak_pct` is a **complement** to `apathy_streak`, never a
  successor. The brief shows the absolute one, because regime duration is what a
  long-horizon reader wants.
- Corollary, and a trap worth naming: converting every measure to a relative one
  makes them agree — that is tautology, not validation.
- `percentile_rank` must return `float`. DuckDB's `100.0 * count(*)` yields
  DECIMAL, so an uncast result raises `TypeError` on any float arithmetic while
  silently working inside `round()`.

### 19. "Washout" is not one phenomenon — hashrate drawdown is the reliable detector
- Backtested as-of three historical washouts (`tools/backtest_signals.py` copies
  rows up to a cut-off into a temp DB, since the helpers anchor to `max(date)`):

  | as-of | fee% | fee pctile | hashrate vs 90d high |
  |---|---|---|---|
  | 2018-12-15 bear bottom | 0.99 | **7.3** | **−33.2** |
  | 2021-07-02 China ban | 11.02 | 75.9 | **−50.2** |
  | 2022-11-21 post-FTX | 3.15 | 66.8 | −3.8 |
  | 2017-12-17 blowoff top | 23.15 | 94.3 | 0.0 |
  | 2021-04-14 ATH | 16.14 | 92.9 | −3.0 |
  | 2021-11-09 ATH | 2.45 | 37.7 | −1.9 |

- **A fee-apathy gauge catches only demand apathy** (2018). The China ban was a
  *supply* shock: hashrate fell 50%, blocks slowed to ~15–20 min, the mempool
  backed up and fees **spiked** to the 76th percentile — the signal inverts. FTX
  was a price/credit event with unremarkable on-chain fees (self-custody
  withdrawals arguably raised them). Do not expect `fee_subsidy` to mark price
  bottoms; it measures blockspace demand.
- **`drawdown_from_high("hash_rate_ehs", 90)` separates cleanly** with no overlap:
  −33%/−50% at miner-stress washouts vs 0%/−3% in euphoria. It is the strongest
  single regime discriminator found, and it is immune to the #17 weekly cycle
  (`getnetworkhashps` uses a 1008-block ≈ 1-week window; mining has no weekend).
- Euphoria never produces a false apathy signal (streak 0 in all three cases),
  though the percentile is only mid-range at the 2021-11 ATH — by then fees had
  cooled to 2.45% against a window still holding the 2021-04 fee mania. High
  percentile is sufficient evidence of euphoria, not necessary. The
  false-positive risk is low; the real risk is the **false negative** — missing a
  supply-side washout with a demand-side gauge.

## Repo/project relationship
- `data_stores` = local git repo (umbrella; package `market_warehouse` inside).
- Claude Project = working context. This `DECISIONS.md` + key source files go in
  Project Knowledge so every session inherits the architecture deterministically
  rather than via lossy memory. Project memory/search is project-scoped.

## Conventions
- Python ≥ 3.11, `src/` layout, pytest.
- No inline comments in code. Type hints throughout.
- Repo name ≠ package name is intentional and consistent with existing repos.
