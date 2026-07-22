# market_warehouse — working context

DuckDB warehouse for the Morning Intel Brief. Persists the daily numeric series
the briefing already parses, so signals/analysis query history instead of
re-scraping. Full rationale in DECISIONS.md (read it first); design + seam +
schema in README.md.

## Current state
- Scaffolded and tested: `onchain` + `btc` tables, fail-soft `write_snapshot`,
  read-only query helpers (`latest`, `moving_average`, `apathy_streak`).
- Tests: 6 passing (`tests/test_write.py`). Validated on Python 3.14 / DuckDB
  1.5.5 arm64.
- NOT yet wired into the briefing composer. Nothing writes to the DB in
  production yet.

## Next increment
Wire into `compose_briefing.py` (lives in the briefing repo at
`~/.openclaw/workspace-briefing/scripts/`, outside this repo):
- Build a `payload` dict from the numeric locals the composer already computes
  (`fee_subsidy_num`, `blocks_24h`, `block_fullness`, `p50_fee`, `miner_rev`,
  `tx_rate_num`, `tx_rate_pct`, `retarget_proj_num`, `btc_price_num`,
  `btc_sma_num`, `btc_sma_pct`, plus hash_rate/difficulty parsed to float).
- Add one `write_snapshot(today, payload)` call AFTER `print("\n".join(lines))`
  near line 824. Strictly last, fail-soft, so a write failure cannot block
  delivery. ~15-line diff to the composer.

## Subsequent increments
2. `markets` + `credit` + `node` tables — long-format (`date, entity, ...`) for
   the multi-entity domains.
3. `etf_flows` from the existing `~/.openclaw/cache/farside_btc.json`.
4. Point `psignals.py` at the DB read-only; implement miner-stress + apathy
   regime flags as SQL.
5. Backfill: on-chain via `getblockstats` over historical heights; price via an
   OHLCV source; markets/credit/flows forward-only.

## Hard constraints
- Persistence MUST never break briefing delivery (the load-bearing function).
- `compose_briefing.py` is the ONLY write point — all domains are typed there.
- Single-writer: composer writes; everyone else opens `read_only=True`.
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
