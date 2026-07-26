from __future__ import annotations

import datetime
import pathlib

import duckdb
import pytest

from market_warehouse import onchain_day_view, write_snapshot

DAY = datetime.date(2026, 7, 24)

PI_ROW = {
    "hash_rate_ehs": 915.6224206722721,
    "difficulty_t": 127.1705004290352,
    "blocks_day": 154,
    "block_fullness": 98.30543181818182,
    "p50_fee": 1.0,
    "miner_rev": 484.42393013,
    "fee_subsidy": 0.6595179490909091,
    "tx_rate": 8.521840277777779,
    "retarget_proj": -0.7926730540439686,
}


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


@pytest.fixture
def seeded(db) -> pathlib.Path:
    write_snapshot(DAY.isoformat(), {"onchain": PI_ROW}, db_path=db)
    return db


def test_day_line_matches_pi_render(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.day_line == (
        "Day (UTC 2026-07-24): 154 blks | 98% full | p50 1.0 sat/vB "
        "| fee/subsidy 0.66% | miner rev 484.4 BTC"
    )


def test_not_stale_at_one_day_behind(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.stale_line is None


def test_not_stale_at_exactly_two_days_behind(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 26))
    assert view.stale_line is None


def test_stale_beyond_threshold(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 29))
    assert view.stale_line == "⚠ warehouse 5d behind (latest complete day 2026-07-24)"


def test_none_when_db_missing(db):
    assert onchain_day_view(db_path=db, today=DAY) is None


def test_none_when_table_empty(seeded):
    con = duckdb.connect(str(seeded))
    con.execute("DELETE FROM onchain")
    con.close()
    assert onchain_day_view(db_path=seeded, today=DAY) is None


def test_partial_row_renders_available_metrics(db):
    write_snapshot("2026-07-24", {"onchain": {"blocks_day": 144}}, db_path=db)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 7, 25))
    assert view.day_line == "Day (UTC 2026-07-24): 144 blks"


def test_all_null_metrics_yields_no_view(db):
    write_snapshot("2026-07-24", {"onchain": {"hash_rate_ehs": 900.0}}, db_path=db)
    assert onchain_day_view(db_path=db, today=datetime.date(2026, 7, 25)) is None


def test_day_pace_is_read_from_blocks_day(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.day_pace == pytest.approx((154 / 144.0 - 1) * 100)


def test_retarget_fragment_uses_cumulative_when_period_established(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.retarget_fragment("-0.79", "1800") == "retarget proj -0.79%"


def test_retarget_fragment_falls_back_below_min_blocks(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    # 2010 left -> elapsed 6 < MIN_BLOCKS_FOR_PROJ -> single-block noise
    assert view.retarget_fragment("-53.69", "2010") == "retarget +6.94% (day-pace)"


def test_retarget_fragment_keeps_cumulative_for_large_early_sample(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    # 1246 left -> elapsed 770 (China-ban shape) -> real signal, not noise
    assert view.retarget_fragment("-28.0", "1246") == "retarget proj -28.0%"


def test_retarget_fragment_defaults_to_cumulative_when_blocks_left_unparsable(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.retarget_fragment("-0.79", "") == "retarget proj -0.79%"


def test_retarget_fragment_empty_when_nothing_available(db):
    write_snapshot("2026-07-24", {"onchain": {"p50_fee": 1.0}}, db_path=db)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 7, 25))
    assert view.day_pace is None
    assert view.retarget_fragment(None, "2010") == ""
