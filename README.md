# data_stores

DuckDB-backed time-series warehouse for the Morning Intel Brief. Holds daily
UTC-close series (on-chain from the node, price from an OHLCV source, and — in
later increments — markets, credit, ETF flows), populated by a dedicated
ingester so signals and analysis can query deep history instead of re-scraping.
The briefing reads it; it no longer writes it.

Repo name is `data_stores` (umbrella for one or more datastore packages). The
current package is `market_warehouse`.

## Architecture

```
bitcoind ──► aggregate_day(UTC date)  ┐
OHLCV source ──► daily close           ├─► write_snapshot ─► ~/data/market.duckdb
     (systemd: daily @ 02:00 UTC        ┘                          ▲
      + one-shot historical backfill)                              │ read_only
compose_briefing.py / psignals.py / Kai / CLI / charts ────────────┘
```

- **Single writer.** A dedicated systemd-managed ingester is the sole writer:
  a one-shot historical backfill (since 2016), then a daily job that appends the
  latest complete UTC day. Everyone else — the briefing included — opens the file
  `read_only=True`. Matches DuckDB's single-writer-per-file constraint with no
  coordination. (Superseded the earlier composer-as-writer design; see DECISIONS
  #12.)
- **UTC calendar-day bucketing.** A row dated `D` aggregates blocks whose header
  timestamp falls in `[D 00:00 UTC, D+1 00:00 UTC)`. Only complete days are
  written. `aggregate_day` is the single day-aggregation definition, shared by
  backfill, the daily job, and gap-fill.
- **Fail-soft.** `write_snapshot` never raises; it returns `True`/`False`. The
  briefing reads fail-soft too — a missing/locked DB degrades one line, never the
  section, never delivery.
- **Idempotent.** Upsert keys on `date` (delete-by-date + insert in one
  transaction), so reruns, gap-fill, and re-backfill don't duplicate rows.

## Data location

The `.duckdb` file lives **outside every repo**, default `~/data/market.duckdb`,
overridable via `MARKET_WAREHOUSE_DB`. Data is backed up on its own schedule and
is never committed (`.gitignore` excludes `*.duckdb`).

## Seam

```python
from market_warehouse import aggregate_day, write_snapshot, write_snapshots  # ingester
from market_warehouse import latest, moving_average, apathy_streak            # readers
from market_warehouse import hash_rate_7d, tx_rate_7d, day_pace_retarget      # derivations
from market_warehouse import sma200, sma200_pct                              # derivations
```

`aggregate_day(date, rpc)` returns the `onchain` payload for one UTC day, computed
from a node-RPC (injectable, so it is unit-testable without a node). `write_snapshot(date, payload)`
where `payload` is `{"onchain": {...}, "btc": {...}}` — missing domains are
skipped, missing metrics within a domain are written as NULL (NULL is valid data,
not missing data).

The `*_7d` change figures and the day-pace retarget are **not stored** — they are
query-time derivations of the daily series (`hash_rate_7d`, `tx_rate_7d`,
`day_pace_retarget`), computed by DATE range so a gap can't mislabel a "7d"
window. See DECISIONS #13.

Consumers own their own presentation. The briefing's read+format adapter lives in
its repo (`scripts/warehouse_view.py`) and imports this package's query helpers
plus the domain constants (`MIN_BLOCKS_FOR_PROJ`, `RETARGET_INTERVAL`) so they are
never copied — see DECISIONS #16.

## Schema (v4)

- `onchain(date PK, hash_rate_ehs, difficulty_t, blocks_day, block_fullness,
  p50_fee, miner_rev, fee_subsidy, tx_rate, retarget_proj)` — raw daily facts,
  UTC-day bucketed. `retarget_proj` is the cumulative (period-pace) projection;
  the day-pace variant is query-time.
- `btc(date PK, close, kraken_vol, kraken_trades)` — daily bars from Kraken.
  `close` is unqualified because price arbitrages across venues; volume does not,
  so it carries the `kraken_` prefix as an honest single-venue proxy (DECISIONS
  #14). Distinct from the brief's live-spot display. The 200-day SMA is a query
  helper (`sma200`/`sma200_pct`), **not** stored — same raw-facts-only shape as
  `onchain`.
- `schema_version(version, applied_at)`

Scalar domains are wide (one named column per metric). Multi-entity domains added
later (`markets`, `etf_flows`, `credit`) will be long-format
(`date, entity, ...`) so new instruments are new rows, not schema changes.

## Install / test

```
pip install -e ".[dev]"
pytest
```

## Operation

The ingester runs on the target host (not from tests): a one-shot backfill, then
a daily systemd timer that appends the latest complete UTC day. Console scripts:
`market-warehouse-backfill` (on-chain), `market-warehouse-btc-backfill --csv`
(price), `market-warehouse-daily` (the timer's job). See `deploy/README.md` for
the full Pi procedure and the `.service`/`.timer` units.

## Roadmap

1. Schema v3 + `aggregate_day` + query helpers (Mac-tested). **(done)**
2. Systemd daily writer (`daily_update`, gap-filling, sole writer) + units in
   `deploy/`. **(live on Pi)**
3. One-shot resumable on-chain backfill since 2016 (`getblockstats` over
   historical heights). **(live on Pi — 2016→tip)**
4. `btc.close` from Kraken (CSV for 2016 depth + REST for the daily edge), folded
   into the daily writer + a one-shot `btc-backfill`. **(live on Pi)**
5. Refactor `compose_briefing.py`: read the latest complete-day row read-only,
   split Live vs Day (UTC) render. **(done — pending first Pi brief)**
6. `markets` + `credit` + `node` (long-format for multi-entity); `etf_flows` from
   `farside_btc.json`.
7. Point `psignals.py` at the DB read-only; miner-stress + apathy regime flags as
   SQL over the now-deep history.
