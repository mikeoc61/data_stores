from __future__ import annotations

import os
import pathlib
from typing import Any, Mapping

import duckdb

from . import schema

DEFAULT_DB_PATH = pathlib.Path.home() / "data" / "market.duckdb"

_ONCHAIN_COLS = (
    "hash_rate_ehs",
    "hash_rate_7d",
    "difficulty_t",
    "retarget_proj",
    "fee_subsidy",
    "blocks_24h",
    "block_fullness",
    "p50_fee",
    "miner_rev",
    "tx_rate",
    "tx_rate_7d",
)

_BTC_COLS = (
    "price",
    "sma200",
    "sma200_pct",
)


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
            if "onchain" in payload:
                _upsert(con, "onchain", date, _ONCHAIN_COLS, payload["onchain"])
            if "btc" in payload:
                _upsert(con, "btc", date, _BTC_COLS, payload["btc"])
            con.execute("COMMIT")
        finally:
            con.close()
        return True
    except Exception:
        return False
