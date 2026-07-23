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
- `compose_briefing.py` already parses every domain into typed, null-guarded
  numeric locals. That's the only place all domains are simultaneously typed.
- So: **capture what the composer already parsed** — do not refactor collectors
  to emit JSON (double-format maintenance) and do not re-parse formatted text
  (brittle reverse-parsing of display strings).
- One new module, one call site at the end of the composer.

### 5. Single-writer discipline enforced at the connection level
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

## Repo/project relationship
- `data_stores` = local git repo (umbrella; package `market_warehouse` inside).
- Claude Project = working context. This `DECISIONS.md` + key source files go in
  Project Knowledge so every session inherits the architecture deterministically
  rather than via lossy memory. Project memory/search is project-scoped.

## Conventions
- Python ≥ 3.11, `src/` layout, pytest.
- No inline comments in code. Type hints throughout.
- Repo name ≠ package name is intentional and consistent with existing repos.
