# market_warehouse — working context

DuckDB warehouse for the Morning Intel Brief. Persists the daily numeric series
the briefing already parses, so signals/analysis query history instead of
re-scraping. Full rationale in DECISIONS.md (read it first); design + seam +
schema in README.md.

## Current state
Fully live on the Pi. The briefing no longer writes; a systemd ingester is the
sole writer, UTC-day bucketed, and the brief reads it back read-only.

- **Warehouse (schema v4):** `onchain` 2016→tip (~3.9k rows, `getblockstats`),
  `btc` 2013-10→tip (~4.7k rows, Kraken bulk CSV + REST edge, with volume).
  Ingest: `daily_update` on a 02:00 UTC timer (gap-filling, fail-soft per day);
  one-shot `backfill` / `btc_backfill` for history.
- **Signal layer:** `percentile_rank` (with `smooth_days` / `detrend_dow`),
  `drawdown_from_high`, `apathy_streak`, `apathy_streak_pct`, `sma200`,
  `hash_rate_7d` / `tx_rate_7d`, `day_pace_retarget`. Backtested against four
  historical regimes via `tools/backtest_signals.py`.
- **Briefing integration:** `scripts/warehouse_view.py` (in the briefing repo)
  renders the `Day (UTC … Sat)` and `Signal:` lines AND injects the same facts
  into the LLM analyst's context, so the Take reasons from a 2-year distribution
  instead of hardcoded thresholds. Standalone-runnable for debugging.
- **Tests:** 93 here, 40 in the briefing repo (`conftest.py` puts `scripts/` on
  `sys.path`; run with an env that has pytest + duckdb + `market_warehouse`).
- **Runtime:** Python 3.14 (Mac) / 3.11.2 (Pi) / DuckDB 1.5.5. Pi node is
  unpruned and fully synced.

Read `DECISIONS.md` for the *why* — especially #17–#19 (signal semantics: weekly
seasonality, relative vs absolute thresholds, and that each washout type needs a
different detector). `README.md` has the schema and seam; `deploy/README.md` has
the Pi procedures and CSV provenance. `BACKFILL_REFACTOR_SPEC.md` is the
completed spec, kept for history.

## Next increment
`markets` + `credit` + `node` as long-format (`date, entity, ...`) tables — the
first real use of the multi-entity shape from #3 — plus `etf_flows` from
`~/.openclaw/cache/farside_btc.json`, which the brief currently reads as a raw
line. Then point `psignals.py` at the DB read-only.

## Hard constraints
- Persistence MUST never break briefing delivery (the load-bearing function).
- The async ingester is the ONLY writer; the briefing and everyone else open
  `read_only=True`. (Was: composer-as-writer — superseded 2026-07-23, DECISIONS
  #12.)
- One day-aggregation definition (`aggregate_day`), shared by daily + backfill +
  gap-fill. No second implementation.
- No stored derived columns: `*_7d`, day-pace retarget, percentiles and the `btc`
  SMA (`sma200`/`sma200_pct`) are SQL query helpers. Cumulative `retarget_proj` IS
  stored (not recoverable from the daily columns). `btc` holds raw daily bars
  (`close`, `kraken_vol`, `kraken_trades`) — same raw-facts shape as `onchain`.
- Any daily threshold or percentile on `fee_subsidy` or `kraken_vol` MUST account
  for weekly seasonality (~27% weekend dip) via `smooth_days` or `detrend_dow` —
  otherwise it substantially reports the day of the week. DECISIONS #17.
- UTC calendar-day bucketing everywhere; block timestamps are non-monotonic near
  boundaries — resolve ranges by actual timestamp with margin.
- Do NOT refactor collectors to emit JSON. Do NOT re-parse formatted display
  text back into numbers.
- NULL is valid data (market-closed day = null equities, correctly recorded),
  not an error.

## Data location
`~/data/market.duckdb` — outside every repo, gitignored, backed up separately.
Override path via `MARKET_WAREHOUSE_DB` env var (tests use it for tmp dirs).

## Environment
- Mac (dev): Homebrew Python is PEP 668 externally-managed. Use the venv at
  `.venv/` — `source .venv/bin/activate`, or invoke tools as `.venv/bin/pytest`,
  `.venv/bin/python`. Do NOT use `--break-system-packages` on the Mac.
- Pi (target): uses `--break-system-packages` (single-purpose appliance). The
  repo carries the dependency spec (`pyproject.toml`); each machine builds its
  own environment.

## Conventions
- Python 3.11+, `src/` layout (structure is load-bearing for the editable
  build), pytest.
- No inline comments in code. Type hints throughout.
- Repo name (`data_stores`) ≠ package name (`market_warehouse`), intentionally.
