from __future__ import annotations

import os
import pathlib
from typing import Any

import duckdb

from .write import DEFAULT_DB_PATH


def _db_path() -> pathlib.Path:
    env = os.environ.get("MARKET_WAREHOUSE_DB")
    return pathlib.Path(env) if env else DEFAULT_DB_PATH


def _connect(db_path: str | os.PathLike[str] | None) -> duckdb.DuckDBPyConnection:
    path = pathlib.Path(db_path) if db_path else _db_path()
    return duckdb.connect(str(path), read_only=True)


def latest(table: str, db_path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    con = _connect(db_path)
    try:
        rel = con.execute(f"SELECT * FROM {table} ORDER BY date DESC LIMIT 1")
        cols = [d[0] for d in rel.description]
        row = rel.fetchone()
        return dict(zip(cols, row)) if row else None
    finally:
        con.close()


def moving_average(
    table: str,
    column: str,
    window: int,
    db_path: str | os.PathLike[str] | None = None,
) -> list[tuple[Any, float | None]]:
    con = _connect(db_path)
    try:
        return con.execute(
            f"""
            SELECT date,
                   avg({column}) OVER (
                       ORDER BY date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW
                   ) AS ma
            FROM {table}
            ORDER BY date
            """
        ).fetchall()
    finally:
        con.close()


def _pct_change_7d(column: str, db_path: str | os.PathLike[str] | None) -> float | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            f"""
            WITH s AS (
                SELECT date, {column} AS v FROM onchain WHERE {column} IS NOT NULL
            ),
            cur AS (SELECT date, v FROM s ORDER BY date DESC LIMIT 1)
            SELECT
                cur.v,
                (SELECT v FROM s
                 WHERE s.date <= cur.date - INTERVAL 7 DAY
                 ORDER BY s.date DESC LIMIT 1)
            FROM cur
            """
        ).fetchone()
        if not row:
            return None
        now_v, prev_v = row
        if now_v is None or prev_v is None or prev_v == 0:
            return None
        return (now_v - prev_v) / prev_v * 100
    finally:
        con.close()


def hash_rate_7d(db_path: str | os.PathLike[str] | None = None) -> float | None:
    return _pct_change_7d("hash_rate_ehs", db_path)


def tx_rate_7d(db_path: str | os.PathLike[str] | None = None) -> float | None:
    return _pct_change_7d("tx_rate", db_path)


def _sma200_row(db_path: str | os.PathLike[str] | None) -> tuple[int, float | None, float | None]:
    con = _connect(db_path)
    try:
        return con.execute(
            """
            WITH recent AS (
                SELECT close FROM btc WHERE close IS NOT NULL ORDER BY date DESC LIMIT 200
            )
            SELECT
                (SELECT count(*) FROM recent),
                (SELECT avg(close) FROM recent),
                (SELECT close FROM btc WHERE close IS NOT NULL ORDER BY date DESC LIMIT 1)
            """
        ).fetchone()
    finally:
        con.close()


def sma200(db_path: str | os.PathLike[str] | None = None) -> float | None:
    n, sma, _latest = _sma200_row(db_path)
    return sma if n >= 200 else None


def sma200_pct(db_path: str | os.PathLike[str] | None = None) -> float | None:
    n, sma, latest = _sma200_row(db_path)
    if n < 200 or not sma or latest is None:
        return None
    return (latest - sma) / sma * 100


def day_pace_retarget(db_path: str | os.PathLike[str] | None = None) -> float | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT blocks_day FROM onchain "
            "WHERE blocks_day IS NOT NULL ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row or row[0] is None:
            return None
        return (row[0] / 144.0 - 1) * 100
    finally:
        con.close()


def apathy_streak(
    fee_subsidy_max: float = 1.0,
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Consecutive days (from latest backward) with fee_subsidy below an ABSOLUTE
    threshold — the regime-duration signal. Unlike the percentile variants it can
    express "we have been in the basement for N days", because its threshold does
    not recalibrate to the window it measures (see apathy_streak_pct).

    A second gate `p50_max=1.5` was removed (2026-07-26). It was not an
    independent guard: at high block fullness p50_fee and fee_subsidy are
    near-mechanically linked (a full block is ~1e6 vB against a 3.125e8 sat
    subsidy, so fee_subsidy ~ p50 * 0.3-0.7%). `p50 <= 1.5` therefore implied
    fee_subsidy < ~0.7%, STRICTER than the 1.0% this function documents — and
    because getblockstats returns integer sat/vB, at current fee levels p50 is
    quantized to {0,1,2}, so the coarser measurement silently controlled a signal
    named for the finer one. Observed 2026-07-15: fee_subsidy 0.8392% (passes)
    broken by p50 2.00. p50_fee remains valuable as a displayed number.
    """
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT fee_subsidy FROM onchain ORDER BY date DESC"
        ).fetchall()
        streak = 0
        for (fee_subsidy,) in rows:
            if fee_subsidy is None or fee_subsidy >= fee_subsidy_max:
                break
            streak += 1
        return streak
    finally:
        con.close()


MIN_WINDOW_ROWS = 30


def percentile_rank(
    column: str,
    window_days: int = 730,
    table: str = "onchain",
    smooth_days: int = 1,
    detrend_dow: bool = False,
    db_path: str | os.PathLike[str] | None = None,
) -> float | None:
    """Percentile (0-100) of the latest value within its trailing window.
    Returns None if fewer than MIN_WINDOW_ROWS valid rows in-window.
    Self-calibrating alternative to absolute thresholds: 'fee_subsidy at the
    3rd percentile of 2y' survives network regime shifts that '< 1.0%' does not.

    `smooth_days` first replaces each day with a trailing mean over that many
    DAYS (by date range, so gaps cannot mislabel the window). Use 7 for any
    column with weekly seasonality: `fee_subsidy` runs ~27% lower at weekends,
    which puts 73% of its bottom decile on Sat/Sun against a 29% baseline — so
    the raw daily percentile substantially reports the day of the week. A 7-day
    mean spans exactly one of each weekday, cancelling that exactly.

    `detrend_dow` is the alternative treatment: rank the residual
    (value - the window's mean for that same ISO weekday) instead of the level.
    It removes the same seasonality but keeps DAILY resolution, where smoothing
    is a low-pass filter that blurs single-day moves. Takes precedence over
    `smooth_days`. Note it answers a RELATIVE question ("low for a Tuesday?"),
    so like any self-referencing measure it cannot describe a sustained regime.
    """
    con = _connect(db_path)
    if detrend_dow:
        series = f"""
            base AS (
                SELECT date, {column} AS v, isodow(date) AS dw
                FROM {table}
                WHERE {column} IS NOT NULL
                  AND date > (SELECT max(date) FROM {table}) - INTERVAL '{window_days}' DAY
            ),
            dm AS (SELECT dw, avg(v) AS m FROM base GROUP BY dw),
            w AS (SELECT b.date, b.v - dm.m AS v FROM base b JOIN dm ON b.dw = dm.dw)
        """
    else:
        series = f"""
            s AS (
                SELECT date,
                       avg({column}) OVER (
                           ORDER BY date
                           RANGE BETWEEN INTERVAL '{max(0, smooth_days - 1)}' DAY PRECEDING
                                     AND CURRENT ROW
                       ) AS v
                FROM {table}
                WHERE {column} IS NOT NULL
            ),
            w AS (
                SELECT date, v FROM s
                WHERE date > (SELECT max(date) FROM {table}) - INTERVAL '{window_days}' DAY
            )
        """
    try:
        row = con.execute(
            f"""
            WITH {series},
            latest AS (SELECT v FROM w ORDER BY date DESC LIMIT 1)
            SELECT
                (SELECT count(*) FROM w),
                (SELECT 100.0 * count(*) FROM w, latest WHERE w.v < latest.v)
            """
        ).fetchone()
        n = row[0]
        if n is None or n < MIN_WINDOW_ROWS:
            return None
        return float(row[1]) / n
    finally:
        con.close()


def drawdown_from_high(
    column: str,
    window_days: int = 90,
    table: str = "onchain",
    db_path: str | os.PathLike[str] | None = None,
) -> float | None:
    """Latest non-NULL value as signed % relative to the trailing-window max
    (0 at a new high, negative when below). For hash_rate_ehs this is a stabler
    miner-stress gauge than the 7d point-to-point delta. None if < MIN_WINDOW_ROWS.
    """
    con = _connect(db_path)
    try:
        row = con.execute(
            f"""
            WITH w AS (
                SELECT date, {column} AS v FROM {table}
                WHERE {column} IS NOT NULL
                  AND date > (SELECT max(date) FROM {table}) - INTERVAL '{window_days}' DAY
            )
            SELECT
                (SELECT count(*) FROM w),
                (SELECT v FROM w ORDER BY date DESC LIMIT 1),
                (SELECT max(v) FROM w)
            """
        ).fetchone()
        n, latest_v, mx = row
        if not n or n < MIN_WINDOW_ROWS or latest_v is None or not mx:
            return None
        return (latest_v - mx) / mx * 100
    finally:
        con.close()


def apathy_streak_pct(
    percentile: float = 10.0,
    window_days: int = 730,
    db_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Consecutive days (from latest backward) where fee_subsidy sits below its
    trailing-window Nth-percentile threshold.

    A COMPLEMENT to apathy_streak(), not a successor — they answer different
    questions. The threshold is drawn from the same window it measures, so by
    construction only `percentile`% of days can ever fall below it however
    depressed the regime: this detects a NEW LEG DOWN relative to recent history,
    and cannot report the duration of a sustained regime (use apathy_streak's
    absolute threshold for that). Compounding it, fee_subsidy is ~27% lower at
    weekends, so the bottom decile is 73% Sat/Sun and short streaks largely track
    the calendar. Returns None if the threshold can't be computed.
    """
    con = _connect(db_path)
    try:
        thr = con.execute(
            f"""
            SELECT quantile_cont(fee_subsidy, {percentile / 100.0})
            FROM onchain
            WHERE fee_subsidy IS NOT NULL
              AND date > (SELECT max(date) FROM onchain) - INTERVAL '{window_days}' DAY
            """
        ).fetchone()[0]
        if thr is None:
            return None
        rows = con.execute(
            "SELECT fee_subsidy FROM onchain ORDER BY date DESC"
        ).fetchall()
        streak = 0
        for (fs,) in rows:
            if fs is None:
                break
            if fs < thr:
                streak += 1
            else:
                break
        return streak
    finally:
        con.close()
