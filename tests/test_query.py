from __future__ import annotations

import datetime
import pathlib

import pytest

from market_warehouse import (
    write_snapshot,
    write_snapshots,
    hash_rate_7d,
    tx_rate_7d,
    day_pace_retarget,
    sma200,
    sma200_pct,
)


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


def _seed_closes(db: pathlib.Path, closes: list[float], start: str = "2016-01-01") -> None:
    base = datetime.date.fromisoformat(start)
    rows = [
        ((base + datetime.timedelta(days=i)).isoformat(), {"btc": {"close": c}})
        for i, c in enumerate(closes)
    ]
    write_snapshots(rows, db_path=db)


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


def test_sma200_none_below_200_closes(db):
    _seed_closes(db, [100.0] * 199)
    assert sma200(db_path=db) is None
    assert sma200_pct(db_path=db) is None


def test_sma200_at_exactly_200_closes(db):
    _seed_closes(db, [100.0] * 200)
    assert sma200(db_path=db) == pytest.approx(100.0)
    assert sma200_pct(db_path=db) == pytest.approx(0.0)


def test_sma200_is_trailing_200_window(db):
    _seed_closes(db, [1000.0] + [100.0] * 200)
    assert sma200(db_path=db) == pytest.approx(100.0)
    assert sma200_pct(db_path=db) == pytest.approx(0.0)
