from __future__ import annotations

import pathlib

import pytest

from market_warehouse import (
    write_snapshot,
    hash_rate_7d,
    tx_rate_7d,
    day_pace_retarget,
)


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


def _oc(date: str, db: pathlib.Path, **cols) -> None:
    write_snapshot(date, {"onchain": cols}, db_path=db)


def test_hash_rate_7d_is_date_anchored_not_positional(db):
    _oc("2026-07-01", db, hash_rate_ehs=100.0)
    _oc("2026-07-08", db, hash_rate_ehs=110.0)
    _oc("2026-07-14", db, hash_rate_ehs=200.0)
    _oc("2026-07-15", db, hash_rate_ehs=121.0)
    assert hash_rate_7d(db_path=db) == pytest.approx(10.0)


def test_tx_rate_7d_is_date_anchored(db):
    _oc("2026-07-08", db, tx_rate=6.0)
    _oc("2026-07-15", db, tx_rate=6.6)
    assert tx_rate_7d(db_path=db) == pytest.approx(10.0)


def test_pct_change_7d_none_when_no_prior_window(db):
    _oc("2026-07-15", db, hash_rate_ehs=121.0)
    assert hash_rate_7d(db_path=db) is None


def test_day_pace_retarget_latest_row(db):
    _oc("2026-07-14", db, blocks_day=200)
    _oc("2026-07-15", db, blocks_day=144)
    assert day_pace_retarget(db_path=db) == pytest.approx(0.0)


def test_day_pace_retarget_above_pace(db):
    _oc("2026-07-15", db, blocks_day=180)
    assert day_pace_retarget(db_path=db) == pytest.approx((180 / 144.0 - 1) * 100)
