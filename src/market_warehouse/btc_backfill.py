from __future__ import annotations

import argparse
import datetime
import logging
import pathlib

from .daily_update import ONE_DAY, _max_date, last_complete_utc_day
from .price import KrakenCsvSource, PriceSource
from .write import _db_path, write_snapshots

DEFAULT_START = datetime.date(2016, 1, 1)

log = logging.getLogger("market_warehouse.btc_backfill")


def main(
    argv: list[str] | None = None,
    *,
    price_source: PriceSource | None = None,
    now: datetime.datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="market-warehouse-btc-backfill",
        description="One-shot resumable btc daily-close backfill from a Kraken OHLCVT CSV.",
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--csv", default=None, help="Kraken OHLCVT CSV (e.g. XBTUSD_1440.csv)")
    parser.add_argument("--start-date", default=DEFAULT_START.isoformat())
    parser.add_argument("--end-date", default=None, help="default: last complete UTC day")
    parser.add_argument("--chunk", type=int, default=1000)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if price_source is None:
        if not args.csv:
            log.error("provide --csv PATH (a Kraken daily OHLCVT CSV)")
            return 2
        price_source = KrakenCsvSource(args.csv)

    db_path = pathlib.Path(args.db) if args.db else _db_path()
    start = datetime.date.fromisoformat(args.start_date)
    end = (
        datetime.date.fromisoformat(args.end_date)
        if args.end_date
        else last_complete_utc_day(now)
    )

    if not args.no_resume:
        existing = _max_date(db_path, "btc")
        if existing is not None and existing + ONE_DAY > start:
            log.info("resuming: btc has data through %s", existing)
            start = existing + ONE_DAY

    if start > end:
        log.info("nothing to backfill (start %s > end %s)", start, end)
        return 0

    bars = price_source.bars()
    rows = [
        (d.isoformat(), {"btc": bar})
        for d, bar in sorted(bars.items())
        if start <= d <= end
    ]
    log.info("btc backfill %s .. %s: %d bar(s) from source", start, end, len(rows))
    if not rows:
        log.warning("no closes in range — check the CSV covers %s..%s", start, end)
        return 1

    if args.dry_run:
        log.info("dry-run: %d btc close(s) (nothing written); latest %s", len(rows), rows[-1][0])
        return 0

    n = write_snapshots(rows, db_path=db_path, chunk_size=args.chunk)
    log.info("done: %d btc close(s) written through %s", n, rows[-1][0])
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
