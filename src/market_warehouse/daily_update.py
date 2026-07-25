from __future__ import annotations

import argparse
import datetime
import logging
import pathlib
from typing import Any, Callable

import duckdb

from .aggregate import BitcoinCliRPC, aggregate_day
from .price import KrakenApiSource, PriceSource
from .write import _db_path, write_snapshot, write_snapshots

COMPLETENESS_BUFFER = datetime.timedelta(hours=2)
ONE_DAY = datetime.timedelta(days=1)

log = logging.getLogger("market_warehouse.daily_update")

Payload = dict[str, dict[str, Any]]
Aggregator = Callable[[datetime.date], Payload | None]


def last_complete_utc_day(now: datetime.datetime | None = None) -> datetime.date:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - COMPLETENESS_BUFFER).date() - ONE_DAY


def _date_range(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    days = []
    day = start
    while day <= end:
        days.append(day)
        day += ONE_DAY
    return days


def _max_date(db_path: pathlib.Path, table: str) -> datetime.date | None:
    if not db_path.exists():
        return None
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(f"SELECT max(date) FROM {table}").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def _pending_days(db_path: pathlib.Path, table: str, now: datetime.datetime | None) -> list[datetime.date]:
    last = last_complete_utc_day(now)
    mx = _max_date(db_path, table)
    if mx is None:
        log.info("%s empty — writing only last complete day %s (backfill owns history)", table, last)
        return [last]
    return _date_range(mx + ONE_DAY, last)


def _process_onchain(
    days: list[datetime.date],
    aggregate: Aggregator,
    db_path: pathlib.Path,
    dry_run: bool,
) -> tuple[int, int]:
    written = failed = 0
    for day in days:
        try:
            payload = aggregate(day)
            if payload is None:
                log.warning("no blocks for %s — skipping", day)
                continue
            blocks = payload.get("onchain", {}).get("blocks_day")
            if dry_run:
                log.info("dry-run onchain %s: %s blocks (not written)", day, blocks)
                written += 1
                continue
            if write_snapshot(day.isoformat(), payload, db_path=db_path):
                log.info("wrote onchain %s (%s blocks)", day, blocks)
                written += 1
            else:
                log.error("write_snapshot returned False for %s", day)
                failed += 1
        except Exception:
            log.exception("failed to process onchain %s — continuing", day)
            failed += 1
    return written, failed


def _process_btc(
    source: PriceSource,
    db_path: pathlib.Path,
    now: datetime.datetime | None,
    dry_run: bool,
) -> None:
    need = _pending_days(db_path, "btc", now)
    if not need:
        log.info("btc up to date")
        return
    closes = source.closes()
    rows = [(d.isoformat(), {"btc": {"close": closes[d]}}) for d in need if d in closes]
    missing = [d for d in need if d not in closes]
    if missing:
        log.warning("btc source missing %d day(s) incl %s", len(missing), missing[0])
    if dry_run:
        log.info("dry-run btc: %d close(s) available (not written)", len(rows))
        return
    if rows:
        n = write_snapshots(rows, db_path=db_path)
        log.info("wrote %d btc close(s) through %s", n, rows[-1][0])
    else:
        log.warning("btc: no closes available for the pending range")


def main(
    argv: list[str] | None = None,
    *,
    aggregate: Aggregator | None = None,
    price_source: PriceSource | None = None,
    now: datetime.datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="market-warehouse-daily",
        description="Append the latest complete UTC day(s) to the warehouse (on-chain + btc close).",
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--date", default=None, help="process one specific UTC date for on-chain (YYYY-MM-DD); skips btc")
    parser.add_argument("--no-btc", action="store_true", help="skip the btc close update")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = pathlib.Path(args.db) if args.db else _db_path()

    if aggregate is None:
        rpc = BitcoinCliRPC()
        aggregate = lambda day: aggregate_day(day, rpc)

    if args.date:
        onchain_days = [datetime.date.fromisoformat(args.date)]
    else:
        try:
            onchain_days = _pending_days(db_path, "onchain", now)
        except Exception:
            log.exception("cannot read warehouse (%s) — aborting", db_path)
            return 1
        if not onchain_days:
            log.info("onchain up to date")

    written_oc, failed_oc = _process_onchain(onchain_days, aggregate, db_path, args.dry_run)

    if not args.date and not args.no_btc:
        if price_source is None:
            price_source = KrakenApiSource()
        try:
            _process_btc(price_source, db_path, now, args.dry_run)
        except Exception:
            log.exception("btc update failed (non-fatal) — will retry next run")

    log.info("done: onchain %d written, %d failed", written_oc, failed_oc)
    if onchain_days and written_oc == 0:
        log.error("no onchain days written out of %d attempted", len(onchain_days))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
