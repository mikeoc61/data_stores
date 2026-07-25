from __future__ import annotations

import pathlib

import duckdb
import pytest

from market_warehouse import write_snapshot, latest, apathy_streak
from market_warehouse.write import _ONCHAIN_COLS, _BTC_COLS


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


def _payload(fee_subsidy: float, p50: float) -> dict:
    return {
        "onchain": {
            "hash_rate_ehs": 877.84,
            "difficulty_t": 127.17,
            "blocks_day": 144,
            "block_fullness": 97.3,
            "p50_fee": p50,
            "miner_rev": 355.8,
            "fee_subsidy": fee_subsidy,
            "tx_rate": 7.65,
            "retarget_proj": -0.94,
        },
        "btc": {"close": 65853.0, "sma200": 72814.0, "sma200_pct": -9.6},
    }


def _table_columns(db: pathlib.Path, table: str) -> set[str]:
    write_snapshot("2016-01-01", {}, db_path=db)
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    finally:
        con.close()
    return {r[1] for r in rows if r[1] != "date"}


@pytest.mark.parametrize(
    "table, cols",
    [("onchain", _ONCHAIN_COLS), ("btc", _BTC_COLS)],
)
def test_writer_cols_match_ddl(db, table, cols):
    assert _table_columns(db, table) == set(cols)


def test_write_creates_and_reads(db):
    assert write_snapshot("2026-07-22", _payload(0.75, 1.0), db_path=db) is True
    row = latest("onchain", db_path=db)
    assert row is not None
    assert row["fee_subsidy"] == 0.75
    assert row["blocks_day"] == 144
    btc = latest("btc", db_path=db)
    assert btc["close"] == 65853.0


def test_upsert_is_idempotent(db):
    write_snapshot("2026-07-22", _payload(0.75, 1.0), db_path=db)
    write_snapshot("2026-07-22", _payload(0.80, 1.0), db_path=db)
    con = duckdb.connect(str(db), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM onchain WHERE date = '2026-07-22'").fetchone()[0]
    finally:
        con.close()
    assert n == 1
    assert latest("onchain", db_path=db)["fee_subsidy"] == 0.80


def test_missing_metric_writes_null(db):
    payload = _payload(0.75, 1.0)
    del payload["onchain"]["miner_rev"]
    write_snapshot("2026-07-22", payload, db_path=db)
    assert latest("onchain", db_path=db)["miner_rev"] is None


def test_partial_payload_btc_only(db):
    write_snapshot("2026-07-22", {"btc": {"close": 65853.0, "sma200": None, "sma200_pct": None}}, db_path=db)
    assert latest("btc", db_path=db)["close"] == 65853.0
    assert latest("onchain", db_path=db) is None


def test_apathy_streak(db):
    write_snapshot("2026-07-20", _payload(0.73, 1.0), db_path=db)
    write_snapshot("2026-07-21", _payload(0.75, 1.0), db_path=db)
    write_snapshot("2026-07-22", _payload(0.75, 1.0), db_path=db)
    assert apathy_streak(db_path=db) == 3


def test_apathy_streak_breaks_on_demand_return(db):
    write_snapshot("2026-07-20", _payload(0.73, 1.0), db_path=db)
    write_snapshot("2026-07-21", _payload(3.20, 4.0), db_path=db)
    write_snapshot("2026-07-22", _payload(0.75, 1.0), db_path=db)
    assert apathy_streak(db_path=db) == 1
