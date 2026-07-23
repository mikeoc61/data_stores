from __future__ import annotations

import re
from typing import Any


def _lead_float(text: str | None) -> float | None:
    if not text:
        return None
    m = re.match(r"\s*([0-9][0-9,]*\.?[0-9]*)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _trailing_pct(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"([+-][0-9.]+)\s*%", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _to_float(text: Any) -> float | None:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _to_int(text: Any) -> int | None:
    value = _to_float(text)
    return int(value) if value is not None else None


def build_payload(
    *,
    hash_rate: str | None = None,
    difficulty: str | None = None,
    retarget_proj_num: float | None = None,
    fee_subsidy_num: float | None = None,
    blocks_24h: str | None = None,
    block_fullness: str | None = None,
    p50_fee: str | None = None,
    miner_rev: str | None = None,
    tx_rate_num: float | None = None,
    tx_rate_pct: float | None = None,
    btc_price_num: float | None = None,
    btc_sma_num: float | None = None,
    btc_sma_pct: float | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        "onchain": {
            "hash_rate_ehs": _lead_float(hash_rate),
            "hash_rate_7d": _trailing_pct(hash_rate),
            "difficulty_t": _lead_float(difficulty),
            "retarget_proj": retarget_proj_num,
            "fee_subsidy": fee_subsidy_num,
            "blocks_24h": _to_int(blocks_24h),
            "block_fullness": _to_int(block_fullness),
            "p50_fee": _to_float(p50_fee),
            "miner_rev": _to_float(miner_rev),
            "tx_rate": tx_rate_num,
            "tx_rate_7d": tx_rate_pct,
        },
        "btc": {
            "price": btc_price_num,
            "sma200": btc_sma_num,
            "sma200_pct": btc_sma_pct,
        },
    }
