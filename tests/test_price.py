from __future__ import annotations

import datetime

import pytest

from market_warehouse.price import _parse_csv, _parse_ohlc_json

# 1451606400 = 2016-01-01 UTC, 1451692800 = 2016-01-02 UTC
# CSV OHLCVT column order: ts, open, high, low, close, volume, trades
CSV = """\
unixtime,open,high,low,close,volume,trades
1451606400,430.0,435.0,428.0,433.0,1234.5,500
1451692800,433.0,440.0,430.0,438.0,2345.6,600

"""

# REST column order: ts, open, high, low, close, VWAP, volume, count
# (vwap sits where the CSV keeps volume — the asymmetry these tests guard.)
OHLC_JSON = {
    "error": [],
    "result": {
        "XXBTZUSD": [
            [1451606400, "430.0", "435.0", "428.0", "433.0", "432.0", "1234.5", 500],
            [1451692800, "433.0", "440.0", "430.0", "438.0", "437.0", "2345.6", 600],
        ],
        "last": 1451692800,
    },
}

D1 = datetime.date(2016, 1, 1)
D2 = datetime.date(2016, 1, 2)


def test_parse_csv_close_by_utc_date():
    bars = _parse_csv(CSV)
    assert bars[D1]["close"] == 433.0
    assert bars[D2]["close"] == 438.0


def test_parse_csv_volume_and_trades():
    bars = _parse_csv(CSV)
    assert bars[D1]["kraken_vol"] == 1234.5
    assert bars[D1]["kraken_trades"] == 500
    assert bars[D2]["kraken_vol"] == 2345.6
    assert bars[D2]["kraken_trades"] == 600


def test_parse_csv_skips_header_and_blank_lines():
    assert len(_parse_csv(CSV)) == 2


def test_parse_ohlc_json_close_by_utc_date():
    bars = _parse_ohlc_json(OHLC_JSON)
    assert bars[D1]["close"] == 433.0
    assert bars[D2]["close"] == 438.0


def test_parse_ohlc_json_volume_and_trades():
    bars = _parse_ohlc_json(OHLC_JSON)
    assert bars[D1]["kraken_vol"] == 1234.5
    assert bars[D1]["kraken_trades"] == 500


def test_rest_volume_is_not_read_from_the_csv_position():
    # The REST vwap (432.0) sits at the index the CSV uses for volume. Reading
    # by the wrong table would silently store a price as a volume.
    bars = _parse_ohlc_json(OHLC_JSON)
    assert bars[D1]["kraken_vol"] != 432.0
    assert bars[D1]["kraken_vol"] == 1234.5


def test_both_sources_agree_on_the_same_bar():
    csv_bar = _parse_csv(CSV)[D1]
    rest_bar = _parse_ohlc_json(OHLC_JSON)[D1]
    assert csv_bar == rest_bar


def test_short_row_yields_null_extras_not_an_error():
    # Close is required; volume/trades degrade to NULL (valid data, DECISIONS #8).
    bars = _parse_csv("1451606400,430.0,435.0,428.0,433.0\n")
    assert bars[D1]["close"] == 433.0
    assert bars[D1]["kraken_vol"] is None
    assert bars[D1]["kraken_trades"] is None


def test_parse_ohlc_json_raises_on_error():
    with pytest.raises(ValueError):
        _parse_ohlc_json({"error": ["EGeneral:Invalid arguments"], "result": {}})


def test_parse_ohlc_json_finds_pair_key_generically():
    payload = {
        "error": [],
        "result": {
            "XBTUSD": [[1451606400, "1", "1", "1", "9.5", "1", "7.5", 3]],
            "last": 1451606400,
        },
    }
    bars = _parse_ohlc_json(payload)
    assert bars[D1]["close"] == 9.5
    assert bars[D1]["kraken_vol"] == 7.5
