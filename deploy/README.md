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

Deep price history comes from Kraken's downloadable OHLCVT CSV (the REST API only
serves ~720 recent days). The daily timer keeps the recent edge current from REST.

1. Download the daily CSV once from Kraken's historical market data
   (`XBT/USD`, 1440-min interval → `XBTUSD_1440.csv`) and copy it to the Pi.
2. Make sure the `btc` table matches the current schema. It is rebuilt from the
   CSV in seconds, so on any `btc` schema change just drop and re-backfill --
   there is no equivalent of the 3h on-chain sweep to protect:

   ```bash
   python3 -c "import duckdb; c=duckdb.connect('$HOME/data/market.duckdb'); c.execute('DROP TABLE IF EXISTS btc'); c.close()"
   ```

   (Schema v4 added `kraken_vol` / `kraken_trades`; a v3 table lacks them and
   `CREATE TABLE IF NOT EXISTS` will not add them.)
3. Backfill (resumable, upsert — safe to re-run). For a 200-day SMA valid from
   2016-01-01, start ~200 days earlier:

   ```bash
   python3 -m market_warehouse.btc_backfill --csv ~/XBTUSD_1440.csv --start-date 2015-06-15 --verbose
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
