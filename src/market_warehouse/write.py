from __future__ import annotations

import os
import pathlib
from typing import Any, Iterable, Mapping

import duckdb

from . import schema

DEFAULT_DB_PATH = pathlib.Path.home() / "data" / "market.duckdb"

_ONCHAIN_COLS = (
    "hash_rate_ehs",
    "difficulty_t",
    "blocks_day",
    "block_fullness",
    "p50_fee",
    "miner_rev",
    "fee_subsidy",
    "tx_rate",
    "retarget_proj",
)

_BTC_COLS = ("close",)


def _db_path() -> pathlib.Path:
    env = os.environ.get("MARKET_WAREHOUSE_DB")
    return pathlib.Path(env) if env else DEFAULT_DB_PATH


def _upsert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    date: str,
    cols: tuple[str, ...],
    values: Mapping[str, Any],
) -> None:
    row = [values.get(c) for c in cols]
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    collist = ", ".join(("date", *cols))
    con.execute(f"DELETE FROM {table} WHERE date = ?", [date])
    con.execute(
        f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
        [date, *row],
    )


def _write_row(
    con: duckdb.DuckDBPyConnection,
    date: str,
    payload: Mapping[str, Mapping[str, Any]],
) -> None:
    if "onchain" in payload:
        _upsert(con, "onchain", date, _ONCHAIN_COLS, payload["onchain"])
    if "btc" in payload:
        _upsert(con, "btc", date, _BTC_COLS, payload["btc"])


def write_snapshot(
    date: str,
    payload: Mapping[str, Mapping[str, Any]],
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    path = pathlib.Path(db_path) if db_path else _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
        try:
            schema.apply_schema(con)
            con.execute("BEGIN TRANSACTION")
            _write_row(con, date, payload)
            con.execute("COMMIT")
        finally:
            con.close()
        return True
    except Exception:
        return False


def write_snapshots(
    rows: Iterable[tuple[str, Mapping[str, Mapping[str, Any]]]],
    db_path: str | os.PathLike[str] | None = None,
    chunk_size: int = 500,
) -> int:
    path = pathlib.Path(db_path) if db_path else _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    written = 0
    try:
        schema.apply_schema(con)
        batch: list[tuple[str, Mapping[str, Mapping[str, Any]]]] = []
        for date, payload in rows:
            batch.append((date, payload))
            if len(batch) >= chunk_size:
                written += _flush(con, batch)
                batch = []
        if batch:
            written += _flush(con, batch)
    finally:
        con.close()
    return written


def _flush(
    con: duckdb.DuckDBPyConnection,
    batch: list[tuple[str, Mapping[str, Mapping[str, Any]]]],
) -> int:
    con.execute("BEGIN TRANSACTION")
    try:
        for date, payload in batch:
            _write_row(con, date, payload)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(batch)
