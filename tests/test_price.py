from __future__ import annotations

import datetime

import pytest

from market_warehouse.price import _parse_csv, _parse_ohlc_json

# 1451606400 = 2016-01-01 UTC, 1451692800 = 2016-01-02 UTC
CSV = """\
unixtime,open,high,low,close,volume,trades
1451606400,430.0,435.0,428.0,433.0,1000.0,500
1451692800,433.0,440.0,430.0,438.0,1200.0,600

"""

OHLC_JSON = {
    "error": [],
    "result": {
        "XXBTZUSD": [
            [1451606400, "430.0", "435.0", "428.0", "433.0", "432.0", "1000.0", 500],
            [1451692800, "433.0", "440.0", "430.0", "438.0", "437.0", "1200.0", 600],
        ],
        "last": 1451692800,
    },
}


def test_parse_csv_close_by_utc_date():
    closes = _parse_csv(CSV)
    assert closes == {
        datetime.date(2016, 1, 1): 433.0,
        datetime.date(2016, 1, 2): 438.0,
    }


def test_parse_csv_skips_header_and_blank_lines():
    assert len(_parse_csv(CSV)) == 2


def test_parse_ohlc_json_close_by_utc_date():
    closes = _parse_ohlc_json(OHLC_JSON)
    assert closes == {
        datetime.date(2016, 1, 1): 433.0,
        datetime.date(2016, 1, 2): 438.0,
    }


def test_parse_ohlc_json_raises_on_error():
    with pytest.raises(ValueError):
        _parse_ohlc_json({"error": ["EGeneral:Invalid arguments"], "result": {}})


def test_parse_ohlc_json_finds_pair_key_generically():
    payload = {"error": [], "result": {"XBTUSD": [[1451606400, "1", "1", "1", "9.5", "1", "1", 1]], "last": 1451606400}}
    assert _parse_ohlc_json(payload) == {datetime.date(2016, 1, 1): 9.5}
