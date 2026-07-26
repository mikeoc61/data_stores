from __future__ import annotations

import datetime
import json
import pathlib
import urllib.request
from typing import Any, Mapping, Protocol

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIR = "XXBTZUSD"
DAILY_INTERVAL = 1440

# The two Kraken sources order their columns DIFFERENTLY. Read them by name here
# so a future edit cannot silently shift a field:
#   CSV  (OHLCVT dump) : ts, open, high, low, close, volume, trades
#   REST (/0/public/OHLC): ts, open, high, low, close, vwap, volume, count
# vwap exists only in REST, so it is deliberately NOT stored — it would be NULL
# across the CSV-backfilled history (2015→) and present only for the ~720-day
# REST edge, which is useless for percentile work over that history.
_CSV_IDX = {"close": 4, "kraken_vol": 5, "kraken_trades": 6}
_REST_IDX = {"close": 4, "kraken_vol": 6, "kraken_trades": 7}

Bar = dict[str, float | int | None]


class PriceSource(Protocol):
    def bars(self) -> dict[datetime.date, Bar]: ...


def _utc_date(ts: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()


def _bar(fields: list, idx: Mapping[str, int]) -> Bar | None:
    try:
        close = float(fields[idx["close"]])
    except (ValueError, IndexError, TypeError):
        return None

    def _opt(key: str, cast) -> float | int | None:
        try:
            return cast(fields[idx[key]])
        except (ValueError, IndexError, TypeError):
            return None

    return {
        "close": close,
        "kraken_vol": _opt("kraken_vol", float),
        "kraken_trades": _opt("kraken_trades", lambda v: int(float(v))),
    }


def _parse_csv(text: str) -> dict[datetime.date, Bar]:
    out: dict[datetime.date, Bar] = {}
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 5:
            continue
        try:
            ts = int(float(parts[0]))
        except ValueError:
            continue
        bar = _bar(parts, _CSV_IDX)
        if bar is not None:
            out[_utc_date(ts)] = bar
    return out


def _parse_ohlc_json(payload: Mapping[str, Any]) -> dict[datetime.date, Bar]:
    errors = payload.get("error") or []
    if errors:
        raise ValueError(f"Kraken OHLC error: {errors}")
    result = payload.get("result", {})
    candles = None
    for key, value in result.items():
        if key != "last":
            candles = value
            break
    if candles is None:
        return {}
    out: dict[datetime.date, Bar] = {}
    for c in candles:
        try:
            ts = int(c[0])
        except (ValueError, IndexError, TypeError):
            continue
        bar = _bar(c, _REST_IDX)
        if bar is not None:
            out[_utc_date(ts)] = bar
    return out


class KrakenCsvSource:
    def __init__(self, path: str | pathlib.Path) -> None:
        self._path = pathlib.Path(path)

    def bars(self) -> dict[datetime.date, Bar]:
        return _parse_csv(self._path.read_text())


class KrakenApiSource:
    def __init__(
        self,
        pair: str = KRAKEN_PAIR,
        interval: int = DAILY_INTERVAL,
        url: str = KRAKEN_OHLC_URL,
        timeout: float = 30.0,
    ) -> None:
        self._pair = pair
        self._interval = interval
        self._url = url
        self._timeout = timeout

    def bars(self) -> dict[datetime.date, Bar]:
        req = urllib.request.Request(
            f"{self._url}?pair={self._pair}&interval={self._interval}",
            headers={"User-Agent": "market-warehouse/0.1"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload = json.load(resp)
        return _parse_ohlc_json(payload)
