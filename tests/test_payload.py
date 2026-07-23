from __future__ import annotations

import pathlib

import pytest

from market_warehouse import build_payload, write_snapshot, latest
from market_warehouse.write import _ONCHAIN_COLS, _BTC_COLS


SCHEMA_DOMAINS = [
    ("onchain", _ONCHAIN_COLS),
    ("btc", _BTC_COLS),
]


HASH_RATE = "877.84 EH/s ▲ +1.23% (7d)"
DIFFICULTY = "127.17 T ▼ -0.94% (7d)"


def _composer_locals() -> dict:
    return {
        "hash_rate": HASH_RATE,
        "difficulty": DIFFICULTY,
        "retarget_proj_num": -0.94,
        "fee_subsidy_num": 0.75,
        "blocks_24h": "113",
        "block_fullness": "98",
        "p50_fee": "1.0",
        "miner_rev": "355.8",
        "tx_rate_num": 7.65,
        "tx_rate_pct": -3.31,
        "btc_price_num": 65853.0,
        "btc_sma_num": 72814.0,
        "btc_sma_pct": -9.6,
    }


def test_display_strings_parse_to_numbers():
    p = build_payload(**_composer_locals())
    oc = p["onchain"]
    assert oc["hash_rate_ehs"] == 877.84
    assert oc["hash_rate_7d"] == 1.23
    assert oc["difficulty_t"] == 127.17
    assert oc["blocks_24h"] == 113
    assert isinstance(oc["blocks_24h"], int)
    assert oc["block_fullness"] == 98
    assert oc["p50_fee"] == 1.0
    assert oc["miner_rev"] == 355.8


def test_numeric_locals_pass_through():
    p = build_payload(**_composer_locals())
    assert p["onchain"]["retarget_proj"] == -0.94
    assert p["onchain"]["fee_subsidy"] == 0.75
    assert p["onchain"]["tx_rate"] == 7.65
    assert p["onchain"]["tx_rate_7d"] == -3.31
    assert p["btc"] == {"price": 65853.0, "sma200": 72814.0, "sma200_pct": -9.6}


def test_miner_rev_with_thousands_separator():
    p = build_payload(**{**_composer_locals(), "miner_rev": "1,234.5"})
    assert p["onchain"]["miner_rev"] == 1234.5


def test_empty_and_none_inputs_yield_null_not_error():
    p = build_payload()
    assert p["onchain"] == {
        "hash_rate_ehs": None,
        "hash_rate_7d": None,
        "difficulty_t": None,
        "retarget_proj": None,
        "fee_subsidy": None,
        "blocks_24h": None,
        "block_fullness": None,
        "p50_fee": None,
        "miner_rev": None,
        "tx_rate": None,
        "tx_rate_7d": None,
    }
    assert p["btc"] == {"price": None, "sma200": None, "sma200_pct": None}


def test_blank_strings_are_null():
    p = build_payload(hash_rate="", difficulty="", blocks_24h="", p50_fee="", miner_rev="")
    oc = p["onchain"]
    assert oc["hash_rate_ehs"] is None
    assert oc["hash_rate_7d"] is None
    assert oc["difficulty_t"] is None
    assert oc["blocks_24h"] is None
    assert oc["p50_fee"] is None
    assert oc["miner_rev"] is None


@pytest.mark.parametrize("domain, cols", SCHEMA_DOMAINS)
def test_build_payload_keys_exactly_match_schema(domain, cols):
    assert set(build_payload()[domain]) == set(cols)


def test_payload_keys_match_schema_via_roundtrip(tmp_path: pathlib.Path):
    db = tmp_path / "market.duckdb"
    payload = build_payload(**_composer_locals())
    assert write_snapshot("2026-07-22", payload, db_path=db) is True
    oc = latest("onchain", db_path=db)
    assert oc["hash_rate_ehs"] == 877.84
    assert oc["hash_rate_7d"] == 1.23
    assert oc["blocks_24h"] == 113
    assert oc["miner_rev"] == 355.8
    btc = latest("btc", db_path=db)
    assert btc["price"] == 65853.0
    assert btc["sma200_pct"] == -9.6
