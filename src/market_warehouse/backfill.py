from __future__ import annotations

import argparse
import datetime
import logging
import os
import pathlib
from typing import Any, Callable, Iterator

from .aggregate import BitcoinCliRPC, aggregate_day
from .daily_update import ONE_DAY, _max_onchain_date, last_complete_utc_day
from .write import _db_path, write_snapshots

DEFAULT_START = datetime.date(2016, 1, 1)
FEE_ERA_FLOOR = datetime.date(2016, 7, 1)

log = logging.getLogger("market_warehouse.backfill")

Payload = dict[str, dict[str, Any]]
Aggregator = Callable[[datetime.date], Payload | None]


def _default_aggregate() -> Aggregator:
    rpc = BitcoinCliRPC()
    return lambda day: aggregate_day(day, rpc)


def _rows(
    start: datetime.date,
    end: datetime.date,
    aggregate: Aggregator,
    skipped: list[datetime.date],
) -> Iterator[tuple[str, Payload]]:
    total = (end - start).days + 1
    done = 0
    day = start
    while day <= end:
        done += 1
        try:
            payload = aggregate(day)
        except Exception:
            log.exception("aggregate failed for %s — skipping", day)
            skipped.append(day)
            day += ONE_DAY
            continue
        if payload is None:
            log.warning("no blocks for %s — skipping", day)
            skipped.append(day)
        else:
            if done % 100 == 0 or day == end:
                log.info("aggregated through %s (%d/%d)", day, done, total)
            yield (day.isoformat(), payload)
        day += ONE_DAY


def main(
    argv: list[str] | None = None,
    *,
    aggregate: Aggregator | None = None,
    now: datetime.datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="market-warehouse-backfill",
        description="One-shot resumable on-chain backfill (UTC days, since 2016).",
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--start-date", default=DEFAULT_START.isoformat())
    parser.add_argument("--end-date", default=None, help="default: last complete UTC day")
    parser.add_argument("--chunk", type=int, default=500, help="days committed per transaction")
    parser.add_argument("--no-resume", action="store_true", help="ignore existing max(date); reprocess from --start-date")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if aggregate is None:
        try:
            os.nice(19)
        except (OSError, AttributeError):
            pass
        aggregate = _default_aggregate()

    db_path = pathlib.Path(args.db) if args.db else _db_path()
    start = datetime.date.fromisoformat(args.start_date)
    end = (
        datetime.date.fromisoformat(args.end_date)
        if args.end_date
        else last_complete_utc_day(now)
    )

    if not args.no_resume:
        existing = _max_onchain_date(db_path)
        if existing is not None and existing + ONE_DAY > start:
            log.info("resuming: warehouse has data through %s", existing)
            start = existing + ONE_DAY

    if start > end:
        log.info("nothing to backfill (start %s > end %s)", start, end)
        return 0

    if start < FEE_ERA_FLOOR:
        log.info(
            "note: fee/blockspace metrics before ~%s (block ~420000) predate the "
            "consistently-full-block era and are weak demand signals",
            FEE_ERA_FLOOR,
        )

    log.info(
        "backfilling %s .. %s (%d days) into %s",
        start, end, (end - start).days + 1, db_path,
    )

    skipped: list[datetime.date] = []
    rows = _rows(start, end, aggregate, skipped)

    if args.dry_run:
        n = sum(1 for _ in rows)
        log.info("dry-run: %d days aggregated, %d skipped (nothing written)", n, len(skipped))
        return 0 if n else 1

    try:
        written = write_snapshots(rows, db_path=db_path, chunk_size=args.chunk)
    except Exception:
        log.exception("backfill write failed; committed chunks persist — re-run to resume")
        return 1

    log.info("done: %d days written, %d skipped", written, len(skipped))
    if skipped:
        shown = ", ".join(str(d) for d in skipped[:20]) + (" ..." if len(skipped) > 20 else "")
        log.warning("skipped %d day(s): %s", len(skipped), shown)
        log.warning("re-run each gap with: --no-resume --start-date <D> --end-date <D>")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
