from __future__ import annotations

import datetime

import pytest

from market_warehouse.aggregate import _retarget_proj, aggregate_day
from market_warehouse.write import _ONCHAIN_COLS

DAY = datetime.date(2020, 1, 2)
START = int(datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc).timestamp())
END = START + 86400


def _blk(time: int) -> dict:
    return {
        "time": time,
        "totalfee": 25_000_000,
        "subsidy": 625_000_000,
        "feerate_percentiles": [1, 5, 10, 20, 50],
        "total_weight": 3_600_000,
        "txs": 2500,
        "difficulty": 1.27e14,
    }


class FakeRPC:
    def __init__(self, blocks: list[dict]) -> None:
        self.blocks = blocks
        self.hashps_calls: list[tuple[int, int]] = []

    def block_count(self) -> int:
        return len(self.blocks) - 1

    def block_time(self, height: int) -> int:
        return self.blocks[height]["time"]

    def block_difficulty(self, height: int) -> float:
        return self.blocks[height]["difficulty"]

    def block_stats(self, height: int) -> dict:
        return self.blocks[height]

    def net_hashps(self, nblocks: int, height: int) -> float:
        self.hashps_calls.append((nblocks, height))
        return 1e20


def _chain() -> list[dict]:
    blocks = [_blk(START - (3 - i) * 600) for i in range(3)]          # heights 0,1,2 pre-day
    blocks += [_blk(START + i * 600) for i in range(144)]             # heights 3..146 in-day
    blocks += [_blk(END + i * 600) for i in range(3)]                 # heights 147..149 post-day
    # Non-monotonic swap across the END boundary: height 146 gets an
    # out-of-day timestamp (== END, excluded) while height 147 gets an
    # in-day timestamp. The in-day block now sits beyond the naive boundary
    # and is only reachable via BOUNDARY_MARGIN.
    blocks[146]["time"], blocks[147]["time"] = blocks[147]["time"], blocks[146]["time"]
    return blocks


def _expected_in_day(blocks: list[dict]) -> list[int]:
    return [h for h in range(len(blocks)) if START <= blocks[h]["time"] < END]


def test_aggregate_day_margin_catches_boundary_block():
    blocks = _chain()
    expected = _expected_in_day(blocks)
    assert 147 in expected and 146 not in expected
    rpc = FakeRPC(blocks)

    payload = aggregate_day(DAY, rpc)
    assert payload is not None
    oc = payload["onchain"]

    assert oc["blocks_day"] == len(expected) == 144
    assert oc["block_fullness"] == pytest.approx(90.0)
    assert oc["p50_fee"] == 10
    assert oc["fee_subsidy"] == pytest.approx(4.0)
    assert oc["miner_rev"] == pytest.approx(144 * 6.5)
    assert oc["tx_rate"] == pytest.approx(144 * 2500 / 86400)
    assert oc["hash_rate_ehs"] == pytest.approx(100.0)
    assert oc["difficulty_t"] == pytest.approx(127.0)


def test_aggregate_day_closing_block_is_max_height():
    blocks = _chain()
    rpc = FakeRPC(blocks)
    aggregate_day(DAY, rpc)
    assert rpc.hashps_calls[-1] == (1008, 147)


def test_aggregate_day_retarget_uses_period_pace():
    blocks = _chain()
    rpc = FakeRPC(blocks)
    payload = aggregate_day(DAY, rpc)
    pace = (blocks[147]["time"] - blocks[0]["time"]) / 147
    assert payload["onchain"]["retarget_proj"] == pytest.approx((600 / pace - 1) * 100)


def test_aggregate_day_keys_match_schema():
    rpc = FakeRPC(_chain())
    payload = aggregate_day(DAY, rpc)
    assert set(payload["onchain"]) == set(_ONCHAIN_COLS)


def test_aggregate_day_returns_none_for_day_with_no_blocks():
    rpc = FakeRPC(_chain())
    assert aggregate_day(datetime.date(2030, 1, 1), rpc) is None


def test_retarget_proj_none_below_min_blocks():
    rpc = FakeRPC(_chain())
    # height 9: blocks_elapsed = 9 < 10 -> single-block noise -> None
    assert _retarget_proj(rpc, 9) is None


def test_retarget_proj_computed_at_min_blocks():
    rpc = FakeRPC(_chain())
    # height 10: blocks_elapsed = 10 >= 10 -> stable enough to project (keeps
    # real early-period signal like the 2021 capitulation slow-block days)
    assert _retarget_proj(rpc, 10) is not None


def test_retarget_proj_large_early_sample_is_in_clamp_signal():
    # The invariant, independent of MIN_BLOCKS_FOR_PROJ's value: a large
    # blocks_elapsed sample is real signal, not noise, and must not be nulled.
    # China-ban capitulation shape: 770 blocks into a fresh period, ~14min blocks
    # (hashrate crashed) -> a large-negative but in-clamp projection near -28%.
    elapsed = 770
    pace = 850  # seconds/block, capitulation-slow
    start = 1_600_000_000
    times = {0: start, elapsed: start + elapsed * pace}

    class _RPC:
        def block_time(self, height: int) -> int:
            return times[height]

    proj = _retarget_proj(_RPC(), elapsed)
    assert proj is not None
    assert -75.0 < proj < 300.0  # within the difficulty adjustment clamp
    assert proj < 0  # a real capitulation-direction signal, not garbage
    assert proj == pytest.approx((600 / pace - 1) * 100)
