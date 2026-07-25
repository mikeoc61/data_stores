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

1. **Backfill first** (spec step 6 — not yet built). Deep history since 2016 is
   owned by the backfill, not the daily job. If the warehouse is empty, the daily
   job writes only the single last-complete day (it will not walk all of history).
2. Dry-run a single day to confirm node access and aggregation:

   ```bash
   python3 -m market_warehouse.daily_update --date 2026-07-24 --dry-run --verbose
   ```

3. Force-write one specific day (the spec's single-day checkpoint):

   ```bash
   python3 -m market_warehouse.daily_update --date 2026-07-24 --verbose
   python3 -c "from market_warehouse import latest; print(latest('onchain'))"
   ```

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
- **On-chain only for now.** `btc.close` ingest is blocked on the OHLCV source
  (spec step 4) and will be added to this same job.

## Logs

```bash
journalctl -u market-warehouse-daily.service -n 50 --no-pager
```
