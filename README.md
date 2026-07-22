# data_stores

DuckDB-backed time-series warehouse for the Morning Intel Brief. Holds the daily
numeric series the briefing already parses (on-chain, price, and — in later
increments — markets, credit, ETF flows, node health), so signals and analysis
can query history instead of re-scraping.

Repo name is `data_stores` (umbrella for one or more datastore packages). The
current package is `market_warehouse`.

## Architecture

```
briefing collectors (unchanged) ─► $TMPDIR/*.txt
compose_briefing.py
    ├─ parses every domain → numeric locals   (already happens)
    ├─ renders + prints the brief             (unchanged)
    └─ write_snapshot(date, payload)  ─────────► ~/data/market.duckdb
                                                      ▲
psignals.py / Kai / CLI / charts ── read_only ────────┘
```

- **Single writer.** The composer is the sole writer, once per briefing, after
  its parse step. Everything else opens the file `read_only=True`. This matches
  DuckDB's single-writer-per-file constraint with no coordination.
- **Composer owns the write point.** All domains are simultaneously in typed
  form only inside `compose_briefing.py`. The persistence hook lives there — no
  collector changes, no re-parsing of formatted text.
- **Fail-soft.** `write_snapshot` never raises; it returns `True`/`False`. The
  brief is the load-bearing function — persistence is a side effect and must not
  break delivery. Call it *after* the brief has been printed.
- **Idempotent.** Upsert keys on `date` (delete-by-date + insert in one
  transaction), so `--force` reruns and debug invocations don't duplicate rows.

## Data location

The `.duckdb` file lives **outside every repo**, default `~/data/market.duckdb`,
overridable via `MARKET_WAREHOUSE_DB`. Data is backed up on its own schedule and
is never committed (`.gitignore` excludes `*.duckdb`).

## Seam

```python
from market_warehouse import write_snapshot   # writer (composer)
from market_warehouse import latest, moving_average, apathy_streak  # readers
```

`write_snapshot(date, payload)` where `payload` is
`{"onchain": {...}, "btc": {...}}` — missing domains are skipped, missing metrics
within a domain are written as NULL (a market-closed day with null equities is
correct data, not missing data).

## Schema (v1)

- `onchain(date PK, hash_rate_ehs, hash_rate_7d, difficulty_t, retarget_proj,
  fee_subsidy, blocks_24h, block_fullness, p50_fee, miner_rev, tx_rate,
  tx_rate_7d)`
- `btc(date PK, price, sma200, sma200_pct)`
- `schema_version(version, applied_at)`

Scalar domains are wide (one named column per metric). Multi-entity domains added
later (`markets`, `etf_flows`, `credit`) will be long-format
(`date, entity, ...`) so new instruments are new rows, not schema changes.

## Install / test

```
pip install -e ".[dev]"
pytest
```

## Roadmap

1. `onchain` + `btc` tables, wired into the composer. **(this increment)**
2. `markets` + `credit` + `node` (long-format for multi-entity).
3. `etf_flows` from the existing `farside_btc.json`.
4. `psignals.py` reads read-only; apathy-duration and miner-stress flags as SQL.
5. Backfill: on-chain via `getblockstats` over historical heights; price via an
   OHLCV source. Markets/credit/flows accumulate forward-only.
