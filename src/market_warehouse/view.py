from __future__ import annotations

import datetime
import os
from dataclasses import dataclass

from .aggregate import MIN_BLOCKS_FOR_PROJ, RETARGET_INTERVAL
from .query import day_pace_retarget, latest

STALE_AFTER_DAYS = 2


@dataclass(frozen=True)
class OnchainDayView:
    date: datetime.date
    day_line: str
    stale_line: str | None
    day_pace: float | None

    def retarget_fragment(
        self, cumulative: str | float | None, blocks_left: str | int | None
    ) -> str:
        elapsed = None
        if blocks_left not in (None, ""):
            try:
                elapsed = RETARGET_INTERVAL - int(blocks_left)
            except (TypeError, ValueError):
                elapsed = None
        if cumulative not in (None, "") and (
            elapsed is None or elapsed >= MIN_BLOCKS_FOR_PROJ
        ):
            return f"retarget proj {cumulative}%"
        if self.day_pace is not None:
            return f"retarget {self.day_pace:+.2f}% (day-pace)"
        return ""


def _format_day_line(row: dict, date: datetime.date) -> str | None:
    parts = []
    if row.get("blocks_day") is not None:
        parts.append(f"{row['blocks_day']} blks")
    if row.get("block_fullness") is not None:
        parts.append(f"{row['block_fullness']:.0f}% full")
    if row.get("p50_fee") is not None:
        parts.append(f"p50 {row['p50_fee']:.1f} sat/vB")
    if row.get("fee_subsidy") is not None:
        parts.append(f"fee/subsidy {row['fee_subsidy']:.2f}%")
    if row.get("miner_rev") is not None:
        parts.append(f"miner rev {row['miner_rev']:,.1f} BTC")
    if not parts:
        return None
    return f"Day (UTC {date}): " + " | ".join(parts)


def onchain_day_view(
    db_path: str | os.PathLike[str] | None = None,
    today: datetime.date | None = None,
) -> OnchainDayView | None:
    try:
        row = latest("onchain", db_path=db_path)
    except Exception:
        return None
    if not row or row.get("date") is None:
        return None
    date = row["date"]
    day_line = _format_day_line(row, date)
    if day_line is None:
        return None

    stale_line = None
    try:
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        behind = (today - date).days
        if behind > STALE_AFTER_DAYS:
            stale_line = f"⚠ warehouse {behind}d behind (latest complete day {date})"
    except Exception:
        pass

    try:
        pace = day_pace_retarget(db_path=db_path)
    except Exception:
        pace = None

    return OnchainDayView(date=date, day_line=day_line, stale_line=stale_line, day_pace=pace)
