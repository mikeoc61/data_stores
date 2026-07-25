# market_warehouse — working context

DuckDB warehouse for the Morning Intel Brief. Persists the daily numeric series
the briefing already parses, so signals/analysis query history instead of
re-scraping. Full rationale in DECISIONS.md (read it first); design + seam +
schema in README.md.

## Current state
Decoupling the warehouse from the briefing per `BACKFILL_REFACTOR_SPEC.md`. The
composer is now report-only; a dedicated systemd ingester becomes the sole
writer, UTC-day bucketed, backfillable to 2016.
- **Done (Mac-tested, 27 passing):** composer no longer writes; **schema v2**
  (`schema.py`, `write.py` col tuples) — renamed `blocks_24h`→`blocks_day`,
  `price`→`close`; dropped `hash_rate_7d`/`tx_rate_7d` (now query-time);
  `build_payload` removed. **`aggregate.py`** — `aggregate_day(date, rpc)` with an
  injectable `NodeRPC` (unit-tested via a fake chain) + `BitcoinCliRPC` for the
  Pi. **Query helpers** — `hash_rate_7d`/`tx_rate_7d` (7d DATE-range) +
  `day_pace_retarget`. **Step 3 daily writer** — `daily_update.py` (sole writer,
  gap-filling, UTC-complete-day logic, fail-soft per day; `main()` inject-tested)
  + `deploy/` systemd .service/.timer (02:00 UTC, `Persistent=true`, idle sched) +
  `market-warehouse-daily` console script. On-chain only — btc is blocked on
  step 4. Decisions #12–#14 recorded; #4/#5/#11 annotated as superseded.
- **Pending (need Pi/node/network, unverifiable on Mac):** deploy+run the daily
  timer on the Pi; one-shot resumable backfill since block ~420000 (2016, step 6);
  `btc.close` OHLCV source + backfill (step 4); compose_briefing refactor (read
  latest complete-day row read-only, split Live vs Day(UTC) render, step 5). See
  `BACKFILL_REFACTOR_SPEC.md` steps 4–6 + ordering.
- Validated on Python 3.14 / DuckDB 1.5.5 arm64.

## Next increment
One-shot resumable on-chain backfill (spec step 6), then `btc.close` OHLCV
(step 4) and the compose_briefing read/render refactor (step 5).

## Subsequent increments
1. `markets` + `credit` + `node` tables — long-format (`date, entity, ...`);
   `etf_flows` from `~/.openclaw/cache/farside_btc.json`.
2. Point `psignals.py` at the DB read-only; miner-stress + apathy regime flags as
   SQL over the now-deep history.

## Hard constraints
- Persistence MUST never break briefing delivery (the load-bearing function).
- The async ingester is the ONLY writer; the briefing and everyone else open
  `read_only=True`. (Was: composer-as-writer — superseded 2026-07-23, DECISIONS
  #12.)
- One day-aggregation definition (`aggregate_day`), shared by daily + backfill +
  gap-fill. No second implementation.
- No stored derived columns: `*_7d` and day-pace retarget are SQL; cumulative
  `retarget_proj` IS stored.
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
