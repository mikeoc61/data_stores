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
    percentile_rank,
    drawdown_from_high,
    apathy_streak_pct,
)
from market_warehouse.query import MIN_WINDOW_ROWS


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


def _seed_onchain(db: pathlib.Path, values: list[dict], start: str = "2024-01-01") -> None:
    base = datetime.date.fromisoformat(start)
    rows = [
        ((base + datetime.timedelta(days=i)).isoformat(), {"onchain": v})
        for i, v in enumerate(values)
    ]
    write_snapshots(rows, db_path=db)


def test_percentile_rank_latest_near_bottom(db):
    # 100 descending values; the latest (1.0) is the smallest -> 0th percentile.
    _seed_onchain(db, [{"fee_subsidy": float(100 - i)} for i in range(100)])
    assert percentile_rank("fee_subsidy", window_days=730, db_path=db) == pytest.approx(0.0)


def test_percentile_rank_latest_near_top(db):
    _seed_onchain(db, [{"fee_subsidy": float(i + 1)} for i in range(100)])
    assert percentile_rank("fee_subsidy", window_days=730, db_path=db) == pytest.approx(99.0)


def test_percentile_rank_midpoint(db):
    vals = [{"fee_subsidy": float(i)} for i in range(1, 101)]
    vals.append({"fee_subsidy": 50.5})
    _seed_onchain(db, vals)
    assert percentile_rank("fee_subsidy", window_days=730, db_path=db) == pytest.approx(
        50 / 101 * 100
    )


def test_percentile_rank_none_below_min_window_rows(db):
    _seed_onchain(db, [{"fee_subsidy": float(i)} for i in range(MIN_WINDOW_ROWS - 1)])
    assert percentile_rank("fee_subsidy", window_days=730, db_path=db) is None


def test_percentile_rank_window_excludes_old_rows(db):
    # 40 old rows (low) then 40 recent (high); a 30d window sees only recent.
    _seed_onchain(db, [{"fee_subsidy": 1.0}] * 40, start="2020-01-01")
    _seed_onchain(db, [{"fee_subsidy": 10.0}] * 39 + [{"fee_subsidy": 20.0}], start="2024-01-01")
    assert percentile_rank("fee_subsidy", window_days=730, db_path=db) == pytest.approx(97.5)


def test_drawdown_from_high_eight_percent_below(db):
    _seed_onchain(db, [{"hash_rate_ehs": 100.0}] * 40 + [{"hash_rate_ehs": 92.0}])
    assert drawdown_from_high("hash_rate_ehs", window_days=90, db_path=db) == pytest.approx(-8.0)


def test_drawdown_from_high_at_new_high_is_zero(db):
    _seed_onchain(db, [{"hash_rate_ehs": float(50 + i)} for i in range(40)])
    assert drawdown_from_high("hash_rate_ehs", window_days=90, db_path=db) == pytest.approx(0.0)


def test_drawdown_window_measures_from_in_window_high(db):
    # An older peak outside the 90d window is deliberately not the reference.
    _seed_onchain(db, [{"hash_rate_ehs": 900.0}] * 5, start="2020-01-01")
    _seed_onchain(db, [{"hash_rate_ehs": 100.0}] * 39 + [{"hash_rate_ehs": 90.0}], start="2024-01-01")
    assert drawdown_from_high("hash_rate_ehs", window_days=90, db_path=db) == pytest.approx(-10.0)


def test_drawdown_none_below_min_window_rows(db):
    _seed_onchain(db, [{"hash_rate_ehs": 100.0}] * (MIN_WINDOW_ROWS - 1))
    assert drawdown_from_high("hash_rate_ehs", window_days=90, db_path=db) is None


def test_apathy_streak_pct_counts_trailing_sub_threshold_days(db):
    # 95 days at 5.0, then 5 trailing days at 0.1 -> the 10th pctile sits at 5.0,
    # so only the forced-low tail is below it.
    _seed_onchain(db, [{"fee_subsidy": 5.0}] * 95 + [{"fee_subsidy": 0.1}] * 5)
    assert apathy_streak_pct(percentile=10, window_days=730, db_path=db) == 5


def test_apathy_streak_pct_breaks_on_normal_day(db):
    vals = [{"fee_subsidy": 5.0}] * 95 + [{"fee_subsidy": 0.1}] * 3
    vals += [{"fee_subsidy": 5.0}, {"fee_subsidy": 0.1}]
    _seed_onchain(db, vals)
    assert apathy_streak_pct(percentile=10, window_days=730, db_path=db) == 1


def test_apathy_streak_pct_zero_when_latest_is_normal(db):
    _seed_onchain(db, [{"fee_subsidy": 0.1}] * 50 + [{"fee_subsidy": 9.0}])
    assert apathy_streak_pct(percentile=10, window_days=730, db_path=db) == 0


def test_apathy_streak_pct_none_on_empty_table(db):
    _seed_onchain(db, [{"hash_rate_ehs": 100.0}])
    assert apathy_streak_pct(percentile=10, window_days=730, db_path=db) is None


def test_percentile_rank_returns_float_not_decimal(db):
    # SQL "100.0 * count(*)" yields DECIMAL in DuckDB; the helper must cast so
    # consumers can do float arithmetic (declared return type is float | None).
    _seed_onchain(db, [{"fee_subsidy": float(i)} for i in range(1, 101)])
    v = percentile_rank("fee_subsidy", window_days=730, db_path=db)
    assert isinstance(v, float)
    assert v - 1.0 == pytest.approx(98.0)
