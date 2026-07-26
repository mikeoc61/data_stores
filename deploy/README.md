# Deploy — market_warehouse ingester (Pi)

systemd units for the daily on-chain writer. The **backfill** (spec step 6) is a
separate one-shot run, executed manually **before** enabling the timer.

These files assume the Pi user `mikeoc` and `HOME=/home/mikeoc`. Adjust `User=`,
`Environment=HOME=`, and the `Documentation=` path if yours differ.

## Prerequisites

The package must be importable by the system Python for the service user:

```bash
cd ~/projects/data_stores
pip install -e . --break-system-packages     # Pi appliance install (user site)
```

Verify the import resolves with only `HOME` in the environment (systemd starts
with a minimal env; user-site import needs `HOME`):

```bash
env -i HOME=$HOME /usr/bin/python3 -c "from market_warehouse import aggregate_day; print('ok')"
```

## Order of operations

Deep history since 2016 is owned by the **backfill**, not the daily job. If the
warehouse is empty, the daily job writes only the single last-complete day (it
will not walk all of history). So: backfill first, then enable the timer.

1. **Rebuild the DB (v1 → v3).** The old composer-era file is v1 and cannot be
   migrated in place (`CREATE TABLE IF NOT EXISTS`); move it aside so v3 is
   created fresh:

   ```bash
   mv ~/data/market.duckdb ~/data/market.duckdb.v1-legacy.bak
   ```

   If you already ran the on-chain backfill under the earlier v2 schema, don't
   rebuild (you'd lose the ~3h of on-chain history). Instead drop the empty `btc`
   table once so the corrected `(date, close)` shape is recreated (no data — the
   btc writer isn't live yet):

   ```bash
   python3 -c "import duckdb; c=duckdb.connect('$HOME/data/market.duckdb'); c.execute('DROP TABLE IF EXISTS btc'); c.close()"
   ```

2. **Smoke-test aggregation** with a dry-run over a small range (no writes):

   ```bash
   python3 -m market_warehouse.backfill --start-date 2016-07-01 --end-date 2016-07-10 --dry-run --verbose
   ```

3. **Verify a real small range** writes correct rows (the spec's 10-day
   checkpoint):

   ```bash
   python3 -m market_warehouse.backfill --start-date 2016-07-01 --end-date 2016-07-10 --verbose
   python3 -c "from market_warehouse import latest; print(latest('onchain'))"
   ```

4. **Full backfill overnight**, niced/idle so it never contends with the node.
   It is resumable — an interruption re-runs from the last committed chunk (the
   warehouse's own `max(date)`), so just run it again:

   ```bash
   nice -n 19 ionice -c3 python3 -m market_warehouse.backfill --verbose
   ```

   Est. ~3h to tip (~78 blocks/s at height 500k). Any days it skips (transient
   RPC errors) are listed at the end with the re-run command; a `--start-date/
   --end-date --no-resume` pass fills them.

5. **Verify** history landed, then let the timer take the recent edge:

   ```bash
   python3 -c "from market_warehouse import latest, apathy_streak; print(latest('onchain')); print('apathy streak:', apathy_streak())"
   ```

## btc close backfill (Step 4, one-shot)

Deep price history comes from Kraken's downloadable OHLCVT CSV; the daily timer
keeps the recent edge current from REST.

**Why both, and why the CSV cannot be dropped.** The REST endpoint is hard-capped
at the 720 most recent candles and `since` does NOT paginate backward — verified
2026-07-26: a plain request and one with `since=1381017600` (2013-10-06) returned
the *identical* 721 candles, 2024-08-05 .. 2026-07-26. So REST cannot see past
~2024-08; roughly 11 years of history exists only in the bulk archive. The two
overlap by design and the upsert is idempotent, so re-running either is safe.

1. Obtain the daily CSV once and copy it to the Pi. **This file is the sole
   provenance of all deep price history — record where you got it.**

   - Source: Kraken's **bulk historical OHLCVT download** — a single large
     archive covering *all* pairs and intervals, not a per-pair file. Extract
     `XBTUSD_1440.csv` (XBT/USD at the 1440-minute daily interval) and discard
     the rest. Expect a long download. Refreshed quarterly, so it always ends
     weeks-to-months short of today.
   - Format: **no header row**, 7 columns
     `unixtime,open,high,low,close,volume,trades`. The parser tolerates a header
     if one appears, but does not require it.
   - Fingerprint of the copy used for the 2026-07 backfill — check a replacement
     matches before trusting a re-run:

     ```bash
     head -1 ~/XBTUSD_1440.csv   # 1381017600,122.0,122.0,122.0,122.0,0.1,1
     tail -1 ~/XBTUSD_1440.csv   # last bar; quarterly dumps end well before today
     wc -l  < ~/XBTUSD_1440.csv  # 4457 rows @ 2013-10-06 .. 2025-12-31
     ```

     First bar is 2013-10-06 (Kraken's BTC launch: $122.00, 0.1 BTC, 1 trade).
     4457 rows across 4470 calendar days — the ~13 absent days are early
     zero-trade days, not corruption.
   - **Independent corroboration:** after backfilling, the warehouse's `sma200`
     matched the briefing's unrelated `btc_sma.sh` (CoinGecko/Binance) to ~0.4%.
     Worth repeating on any source change — two unrelated pipelines agreeing that
     closely is the cheapest available check that the data is genuine.
2. Make sure the `btc` table matches the current schema. It is rebuilt from the
   CSV in seconds, so on any `btc` schema change just drop and re-backfill --
   there is no equivalent of the 3h on-chain sweep to protect:

   ```bash
   python3 -c "import duckdb; c=duckdb.connect('$HOME/data/market.duckdb'); c.execute('DROP TABLE IF EXISTS btc'); c.close()"
   ```

   (Schema v4 added `kraken_vol` / `kraken_trades`; a v3 table lacks them and
   `CREATE TABLE IF NOT EXISTS` will not add them.)
3. Backfill (resumable, upsert — safe to re-run). With no `--start-date` it
   takes everything the CSV provides, currently back to 2013-10-06:

   ```bash
   python3 -m market_warehouse.btc_backfill --csv ~/XBTUSD_1440.csv --verbose
   ```

4. Fill the REST edge the quarterly CSV cannot cover, then verify:

   ```bash
   python3 -m market_warehouse.daily_update --verbose
   python3 -c "from market_warehouse import latest, sma200, sma200_pct; print(latest('btc')); print('sma200', sma200(), sma200_pct())"
   ```

The CSV is updated only quarterly, so it ends weeks short of today — that gap is
filled by the daily timer's REST fetch on its next run (720-day window covers it).

## Install the timer

```bash
sudo cp deploy/market-warehouse-daily.service /etc/systemd/system/
sudo cp deploy/market-warehouse-daily.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now market-warehouse-daily.timer
systemctl list-timers market-warehouse-daily.timer
```

## Behaviour

- Fires `*-*-* 02:00:00 UTC` (16:00 HST day D for UTC day D-1 fully closed; clear
  of the 06:00 HST briefing). `Persistent=true` re-fires a missed run after a
  reboot; combined with gap-fill no day is permanently lost.
- **Gap-filling:** each run writes every missing UTC day in
  `(max(onchain.date), last_complete_day]`. A day is "complete" once it is >2h
  past its UTC midnight close, so a run before 02:00 UTC conservatively targets
  the day before yesterday.
- **Fail-soft per day:** one bad day logs and continues. The run exits non-zero
  only when it writes nothing at all (DB unreachable or node down for the whole
  run), so the timer surfaces hard failures via `systemctl --failed`.
- Runs `Nice=19` / IO+CPU idle class so it never competes with the node or the
  briefing.
- **btc close** is folded into the same run (on-chain first, then btc from
  Kraken REST). btc is fail-soft *within* the run: a Kraken outage logs a WARN and
  does not fail the job or block the on-chain write — it self-heals via gap-fill
  next run. `--no-btc` skips it; `--date` (single on-chain day) skips it too.

## Logs

```bash
journalctl -u market-warehouse-daily.service -n 50 --no-pager
```
