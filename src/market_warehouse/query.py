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
    p50_max: float = 1.5,
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    con = _connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT fee_subsidy, p50_fee
            FROM onchain
            ORDER BY date DESC
            """
        ).fetchall()
        streak = 0
        for fee_subsidy, p50 in rows:
            if fee_subsidy is None or p50 is None:
                break
            if fee_subsidy < fee_subsidy_max and p50 <= p50_max:
                streak += 1
            else:
                break
        return streak
    finally:
        con.close()
