from __future__ import annotations

import datetime
import json
import statistics
import subprocess
from typing import Any, Mapping, Protocol

HASHPS_WINDOW = 1008
RETARGET_INTERVAL = 2016
BOUNDARY_MARGIN = 20
STAT_FIELDS: tuple[str, ...] = (
    "time",
    "totalfee",
    "subsidy",
    "feerate_percentiles",
    "total_weight",
    "txs",
)


class NodeRPC(Protocol):
    def block_count(self) -> int: ...
    def block_time(self, height: int) -> int: ...
    def block_difficulty(self, height: int) -> float: ...
    def block_stats(self, height: int) -> Mapping[str, Any]: ...
    def net_hashps(self, nblocks: int, height: int) -> float: ...


def _day_bounds(date: datetime.date) -> tuple[int, int]:
    start = datetime.datetime(
        date.year, date.month, date.day, tzinfo=datetime.timezone.utc
    )
    start_ts = int(start.timestamp())
    return start_ts, start_ts + 86400


def _first_height_at_or_after(rpc: NodeRPC, ts: int, tip: int) -> int:
    lo, hi = 0, tip
    while lo < hi:
        mid = (lo + hi) // 2
        if rpc.block_time(mid) >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _day_heights(rpc: NodeRPC, start_ts: int, end_ts: int, tip: int) -> list[int]:
    start_h = _first_height_at_or_after(rpc, start_ts, tip)
    end_h = _first_height_at_or_after(rpc, end_ts, tip)
    lo = max(0, start_h - BOUNDARY_MARGIN)
    hi = min(tip, end_h - 1 + BOUNDARY_MARGIN)
    heights = []
    for h in range(lo, hi + 1):
        if start_ts <= rpc.block_time(h) < end_ts:
            heights.append(h)
    return heights


def _retarget_proj(rpc: NodeRPC, height: int) -> float | None:
    retarget_start = height - height % RETARGET_INTERVAL
    blocks_elapsed = height - retarget_start
    if blocks_elapsed <= 0:
        return None
    start_time = rpc.block_time(retarget_start)
    tip_time = rpc.block_time(height)
    if tip_time <= start_time:
        return None
    pace = (tip_time - start_time) / blocks_elapsed
    return (600 / pace - 1) * 100


def aggregate_day(date: datetime.date, rpc: NodeRPC) -> dict[str, dict[str, Any]] | None:
    start_ts, end_ts = _day_bounds(date)
    tip = rpc.block_count()
    heights = _day_heights(rpc, start_ts, end_ts, tip)
    if not heights:
        return None
    stats = [rpc.block_stats(h) for h in heights]
    closing_h = heights[-1]

    fees = sum(s["totalfee"] for s in stats)
    subsidy = sum(s["subsidy"] for s in stats)
    fullness = statistics.mean(s["total_weight"] for s in stats) / 4e6 * 100
    p50 = statistics.median(s["feerate_percentiles"][2] for s in stats)
    txs = sum(s["txs"] for s in stats)

    onchain = {
        "hash_rate_ehs": rpc.net_hashps(HASHPS_WINDOW, closing_h) / 1e18,
        "difficulty_t": rpc.block_difficulty(closing_h) / 1e12,
        "blocks_day": len(stats),
        "block_fullness": fullness,
        "p50_fee": p50,
        "miner_rev": (fees + subsidy) / 1e8,
        "fee_subsidy": (fees / subsidy * 100) if subsidy else None,
        "tx_rate": txs / 86400,
        "retarget_proj": _retarget_proj(rpc, closing_h),
    }
    return {"onchain": onchain}


def aggregate_range(
    start: datetime.date, end: datetime.date, rpc: NodeRPC
) -> dict[datetime.date, dict[str, dict[str, Any]]]:
    out: dict[datetime.date, dict[str, dict[str, Any]]] = {}
    day = start
    step = datetime.timedelta(days=1)
    while day <= end:
        payload = aggregate_day(day, rpc)
        if payload is not None:
            out[day] = payload
        day += step
    return out


class BitcoinCliRPC:
    def __init__(self, cli: str = "bitcoin-cli") -> None:
        self._cli = cli
        self._fields = json.dumps(list(STAT_FIELDS))
        self._header_cache: dict[int, Mapping[str, Any]] = {}
        self._hash_cache: dict[int, str] = {}

    def _run(self, *args: str) -> str:
        return subprocess.check_output([self._cli, *args], text=True).strip()

    def _hash(self, height: int) -> str:
        if height not in self._hash_cache:
            self._hash_cache[height] = self._run("getblockhash", str(height))
        return self._hash_cache[height]

    def _header(self, height: int) -> Mapping[str, Any]:
        if height not in self._header_cache:
            self._header_cache[height] = json.loads(
                self._run("getblockheader", self._hash(height))
            )
        return self._header_cache[height]

    def block_count(self) -> int:
        return int(self._run("getblockcount"))

    def block_time(self, height: int) -> int:
        return int(self._header(height)["time"])

    def block_difficulty(self, height: int) -> float:
        return float(self._header(height)["difficulty"])

    def block_stats(self, height: int) -> Mapping[str, Any]:
        return json.loads(self._run("getblockstats", str(height), self._fields))

    def net_hashps(self, nblocks: int, height: int) -> float:
        return float(self._run("getnetworkhashps", str(nblocks), str(height)))
