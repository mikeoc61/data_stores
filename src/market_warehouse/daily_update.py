from __future__ import annotations

import argparse
import datetime
import logging
import pathlib
from typing import Any, Callable

import duckdb

from .aggregate import BitcoinCliRPC, aggregate_day
from .write import _db_path, write_snapshot

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


def _max_onchain_date(db_path: pathlib.Path) -> datetime.date | None:
    if not db_path.exists():
        return None
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT max(date) FROM onchain").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()


def _pending_days(
    db_path: pathlib.Path, now: datetime.datetime | None
) -> list[datetime.date]:
    last = last_complete_utc_day(now)
    max_date = _max_onchain_date(db_path)
    if max_date is None:
        log.info(
            "empty warehouse — writing only last complete day %s "
            "(backfill owns deep history)",
            last,
        )
        return [last]
    return _date_range(max_date + ONE_DAY, last)


def main(
    argv: list[str] | None = None,
    *,
    aggregate: Aggregator | None = None,
    now: datetime.datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="market-warehouse-daily",
        description="Append the latest complete UTC on-chain day(s) to the warehouse.",
    )
    parser.add_argument("--db", default=None, help="warehouse path (default: MARKET_WAREHOUSE_DB or ~/data/market.duckdb)")
    parser.add_argument("--date", default=None, help="process one specific UTC date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="aggregate but do not write")
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
        days = [datetime.date.fromisoformat(args.date)]
    else:
        try:
            days = _pending_days(db_path, now)
        except Exception:
            log.exception("cannot read warehouse (%s) — aborting", db_path)
            return 1
        if not days:
            log.info("warehouse up to date; nothing to do")
            return 0

    written = 0
    failed = 0
    for day in days:
        try:
            payload = aggregate(day)
            if payload is None:
                log.warning("no blocks for %s — skipping", day)
                continue
            blocks = payload.get("onchain", {}).get("blocks_day")
            if args.dry_run:
                log.info("dry-run %s: %s blocks (not written)", day, blocks)
                written += 1
                continue
            if write_snapshot(day.isoformat(), payload, db_path=db_path):
                log.info("wrote onchain %s (%s blocks)", day, blocks)
                written += 1
            else:
                log.error("write_snapshot returned False for %s", day)
                failed += 1
        except Exception:
            log.exception("failed to process %s — continuing", day)
            failed += 1

    log.info("done: %d written, %d failed", written, failed)
    if written == 0:
        log.error("no days written out of %d attempted", len(days))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
