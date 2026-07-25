# Implementation Spec: Async Daily Writer + Schema v2 + Backfill

Context: the `onchain`/`btc` tables currently receive a synchronous write from
`compose_briefing.py` using a rolling-24h window ending at briefing time (~06:00
HST / 16:00 UTC). This is being replaced. The briefing becomes a pure reader for
daily aggregates; a new async job becomes the sole writer, bucketing by UTC
calendar day; the same range-aggregate function powers both the daily job and a
historical backfill.

Read `DECISIONS.md` and `CLAUDE.md` in `~/projects/data_stores` first. All prior
architectural constraints still hold (single-writer, fail-soft never breaks
delivery, NULL-is-valid, `.duckdb` outside repos, key-drift test).

---

## Definitions (lock these; they are the source of the whole refactor)

- **Bucketing:** UTC calendar day. A row dated `D` aggregates all blocks whose
  block-header timestamp falls in `[D 00:00:00 UTC, D+1 00:00:00 UTC)`.
- **Completeness:** only complete UTC days are written. At briefing time the most
  recent complete UTC day is ~16h old; that is expected and correct.
- **One aggregate function, three callers:** implement `aggregate_day(date)` (or
  `aggregate_range(start, end)`) once. Backfill calls it over a large range; the
  daily job calls it for missing days; gap-fill is the same call. There must be
  no second implementation of day aggregation anywhere.

---

## Step 1 — Schema v2 (`schema.py`)

No production data to migrate (the two existing rows are on the superseded
rolling definition and are discarded). Bump `SCHEMA_VERSION = 2` and rewrite DDL.

### `onchain` table — column changes

RENAME:
- `blocks_24h` -> `blocks_day` (it is now a calendar-day count, not a rolling
  window). Audit `block_fullness` and `p50_fee` for any implicit rolling
  connotation in surrounding code; the column names themselves are fine.

DROP (these are query-time derivations, not stored facts):
- `hash_rate_7d`
- `tx_rate_7d`
  Rationale: pure functions of the stored series (value at t vs t-7d). Storing
  them denormalizes and risks silent inconsistency on any re-backfill or
  correction. Compute in SQL at query time (roadmap step 4).

KEEP as stored raw daily facts:
- `hash_rate_ehs`   (as of day close)
- `difficulty_t`    (as of day close)
- `blocks_day`      (count of blocks in the UTC day)
- `block_fullness`  (mean total_weight/4e6 * 100 over the day's blocks)
- `p50_fee`         (median of per-block feerate_percentiles p50 over the day)
- `miner_rev`       (sum of subsidy + totalfee over the day, in BTC)
- `fee_subsidy`     (sum(totalfee)/sum(subsidy)*100 over the day)
- `tx_rate`         (day tx count / 86400, tx/s — see note in Step 2)
- `retarget_proj`   (CUMULATIVE projection — see Step 1a)

`date` stays PRIMARY KEY.

### Step 1a — `retarget_proj` (the "both" decision)

Store the CUMULATIVE projection as a raw daily fact. Do NOT drop it — it is not
recoverable from the other daily columns (it depends on cumulative block pace
across the current difficulty period, up to 2016 blocks, not aligned to day
boundaries).

- STORED column `retarget_proj`: cumulative projection as of the day's closing
  tip. Computed from headers: identify the retarget period containing the
  closing block, take elapsed_time / blocks_elapsed as pace, projection =
  (600/pace - 1) * 100.
- QUERY-TIME (do NOT store): the day-pace variant, `(blocks_day/144.0 - 1)*100`,
  computed in SQL. It is more responsive (no period-average dilution) and is the
  PREFERRED value for miner-stress signal thresholds.
- Add a note to `DECISIONS.md`: both variants exist; cumulative is stored and
  matches the brief's display; day-pace is SQL-derived and preferred for signals.

### `btc` table

Change semantics: store DAILY CLOSE, not spot-at-briefing.
- **CORRECTION (2026-07-25):** the `btc` table is `date` PK + `close` **only**.
  `sma200`/`sma200_pct` are **NOT stored** — they are `query.py` helpers (Step 7),
  consistent with #13 (no stored derived series). The 200-day SMA is a pure
  function of the close series; materializing it denormalizes and risks staleness
  on any correction or re-backfill — the exact reasoning that dropped `*_7d`. This
  makes the OHLCV writer and the on-chain writer the **same shape: both store raw
  daily facts, nothing derived.**
- `date` PK, `close` (rename from `price`).
- Source: external OHLCV (see Step 4). The briefing continues to display LIVE
  SPOT from its own collector — that is a display value, not a stored one. Two
  different numbers for two different purposes; document this in `DECISIONS.md`.

### Update the key-drift test

The existing `test_..._keys_match_schema` must be updated to the v2 columns
(bidirectional set equality against the new `_ONCHAIN_COLS`/`_BTC_COLS`). This
test is what guards the whole refactor — it must be green before proceeding.

---

## Step 2 — The aggregate function (new module, e.g. `aggregate.py`)

Implement `aggregate_day(date: datetime.date) -> dict` returning the `onchain`
payload for one UTC day (the daily facts above), computed from the node.

Requirements:
- Resolve the block-height range for the UTC day from header timestamps. Block
  timestamps are NOT strictly monotonic (consensus allows up to ~2h forward
  drift, bounded by median-time-past), so do not assume the first block with
  `time >= D` is exactly the day boundary — scan a small margin around the
  boundary and select by actual timestamp.
- Aggregate per-day from `getblockstats` over the resolved range, using the same
  field set as the live script (`time`, `totalfee`, `subsidy`,
  `feerate_percentiles`, `total_weight`, `txs`).
- `tx_rate` = day tx count / 86400. (Note: this differs from the live script's
  `getchaintxstats` 28d-window rate. Decide explicitly and document: the daily
  stored value should be the per-day rate, NOT the 28d rate, so it is a genuine
  daily fact. The 28d smoothed rate, if wanted, is a query-time window over the
  daily series.)
- `hash_rate_ehs` / `difficulty_t` / `retarget_proj`: as of the day's closing
  block (Step 1a for retarget).
- Pure and testable: given a date, it returns a dict. No writing, no display
  formatting. This replaces the role `build_payload` played (which was mapping
  composer locals; that job no longer exists).

`aggregate_range(start, end)` may wrap `aggregate_day` or share height-resolution
to avoid re-scanning boundaries; either is fine as long as there is ONE
day-aggregation definition.

---

## Step 3 — Async daily writer (new entrypoint + systemd timer)

New script (e.g. `daily_update.py` or a shell wrapper) that is the SOLE writer.

Behavior (gap-filling by design — this is the failure mitigation):
1. Open the DB, `SELECT max(date) FROM onchain`.
2. Determine the last complete UTC day (yesterday UTC, or earlier if run before
   02:00 UTC — compute from current UTC time, do not assume).
3. For every missing day in `(max_date, last_complete_day]`, call
   `aggregate_day`, collect rows, write via `write_snapshot` (upsert).
   - If the table is empty (post-backfill this won't happen, but handle it),
     write only the last complete day, not all of history — backfill owns deep
     history, the daily job owns the recent edge.
4. Same for `btc` daily close (Step 4 source), same gap-fill logic.
5. Fail-soft on a per-day basis: one bad day logs and continues; it does not
   abort the whole run. Return nonzero exit only on total failure (DB
   unreachable), so the timer surfaces hard failures.

systemd timer:
- `OnCalendar=*-*-* 02:00:00 UTC` (16:00 HST day D for UTC day D-1 fully closed;
  clear of the 06:00 HST briefing and other jobs).
- `Persistent=true` (a missed run — Pi down/reboot — fires on next boot; combined
  with gap-fill, no day is ever permanently lost).
- Runs as `mikeoc` with `HOME` set (user-site package import requires it — see
  `CLAUDE.md` Pi runtime note; verify with
  `env -i HOME=$HOME /usr/bin/python3 -c "from market_warehouse import ..."`).
- `nice`/`ionice` idle class so it never competes with Core or the briefing.

---

## Step 4 — `btc` daily close source

- External OHLCV source for historical daily close + enough history to compute a
  200-day SMA (needs 200 prior closes before the first SMA value).
- Same gap-fill pattern as onchain.
- The briefing's live-spot display is unchanged and unrelated — do not route it
  through the DB.

---

## Step 5 — Refactor `compose_briefing.py` (briefing repo)

REMOVE:
- The synchronous `write_snapshot` call added in the prior increment.
- The payload-construction for writing (`build_payload` or equivalent), IF it
  exists only to feed the write. Keep any parsing still needed for display.

KEEP:
- All live `bitcoin-cli` collection for POINT-IN-TIME metrics (mempool depth, fee
  estimates, live hashrate/difficulty, retarget countdown). These must stay live;
  a stale mempool reading in a morning brief is useless.

ADD:
- A read of the latest complete-day `onchain` row (and `btc` close if displayed)
  via `market_warehouse` query helpers, opened `read_only=True`.
- Fail-soft read: a missing/locked DB or absent row degrades the affected line
  only (e.g. "Day (UTC): unavailable"), never the section, never delivery.
- Optional staleness guard: if latest `onchain.date` is > ~2 days behind the last
  complete UTC day, print a warning line in the brief. Cheap monitoring via a job
  already trusted to run and be read.

RENDER (split the two measurement kinds explicitly — this is a readability win,
not just plumbing):
```
=== BITCOIN NETWORK SNAPSHOT ===
Live:  <hashrate> <7d Δ from SQL> | <difficulty> | retarget <countdown> (<cum proj>)
       Fees est <fast/1hr/1d> | Mempool <tx> / <vMB>
Day (UTC <date>): <blocks_day> blks | <fullness>% full | p50 <fee> | fee/subsidy <pct>% | <miner_rev> BTC
```
Label the day row with its UTC date so the period is unambiguous. Expect
`blocks_day` ~144 (true day) vs the old rolling 117-119.

---

## Step 6 — Backfill (new entrypoint, one-shot)

Script that calls `aggregate_range` from a start height/date to the last complete
UTC day, writing via `write_snapshot`.

- START: UTC day of block ~420,000 (~Jul 2016) for the getblockstats-derived
  fee/blockspace metrics — that is the semantic floor below which full-block fee
  metrics stop meaning "demand" (pre-2016 blocks were routinely non-full).
  Measured rate ~78 blocks/s at height 500k; full range to tip est ~3h.
- Hashrate/difficulty/timing have NO semantic floor and are cheap (headers only).
  If trivial to do, backfill those deeper (toward genesis) in a separate pass;
  otherwise same 2016 floor is acceptable for v1.
- RESUMABLE: checkpoint last-completed height (or date) so an interruption
  resumes cleanly — this is a multi-hour run and will be interrupted.
- `nice`/`ionice` idle; must not contend with Core or the 02:00/06:00 jobs.
- Batch DuckDB writes (do not open/commit per day-row across 3600 rows).
- Route through `write_snapshot` so upsert + schema contract hold identically for
  backfilled and daily rows.
- Derived columns (`*_7d`, day-pace retarget) are NOT written — they are
  query-time. `retarget_proj` (cumulative) IS written per Step 1a.

Optional speed lever (probably skip for a one-shot): ~80% of runtime is
`bitcoin-cli` process-spawn + RPC round-trip (user+sys was 2.4s of 12.9s wall).
A persistent HTTP RPC session (python `requests` vs spawning bitcoin-cli 539k
times) could cut 3-5x. Not worth the code for a single overnight run unless the
measured full-range time is inconvenient.

---

## Step 7 — Query helpers (`query.py`, roadmap step 4 down-payment)

Add read-only helpers, all `read_only=True`, for the derivations removed from the
schema:
- `hash_rate_7d`, `tx_rate_7d`: 7-DAY window by DATE RANGE, not positional LAG.
  `RANGE BETWEEN INTERVAL 7 DAY PRECEDING AND CURRENT ROW` (or a dense date
  spine). Positional `LAG(7)` silently spans >7 calendar days across any gap and
  mislabels itself — must not be used for a "7d" figure.
- day-pace retarget: `(blocks_day/144.0 - 1)*100`.
- `sma200`, `sma200_pct` (CORRECTION 2026-07-25): the 200-day SMA of `btc.close`
  and the latest close's % distance from it — query helpers, not stored columns.
  Needs ≥200 closes; returns None below that.
- Keep `apathy_streak`; re-verify it against renamed columns.

---

## Ordering / checkpoints

1. Schema v2 + update key-drift test. Test green on Mac before anything else.
2. `aggregate_day` + unit tests (fixture heights → expected dict) on Mac.
3. Backfill script; DRY-RUN a small range (e.g. 10 days) on the Pi, verify rows.
4. Async daily writer + systemd timer; dry-run `--force`-style single day on Pi.
5. Compose_briefing refactor (remove write, add read, split render).
6. btc OHLCV source + backfill.
7. Query helpers.
8. Drop the two legacy rows (or delete the .duckdb), run full backfill overnight
   on the Pi, then let the daily timer take over.

Verification after full backfill:
```
python3 -c "from market_warehouse import latest; print(latest('onchain'))"
python3 -c "from market_warehouse import apathy_streak; print(apathy_streak())"
# row count ~ (tip_date - 2016-07) in days; apathy_streak now has real history
```

Constraints recap (do not violate):
- Async job is the ONLY writer. Briefing and everyone else open read_only=True.
- Briefing read is fail-soft; persistence/read failure never blocks delivery.
- One day-aggregation implementation, shared by daily + backfill + gap-fill.
- No stored derived columns; `*_7d` and day-pace retarget are SQL. Cumulative
  retarget IS stored.
- UTC calendar-day bucketing everywhere; block timestamps are UTC and
  non-monotonic near boundaries — resolve ranges by actual timestamp with margin.
- Pi import needs HOME set (user-site install).
