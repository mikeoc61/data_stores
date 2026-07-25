from __future__ import annotations

import datetime
import json
import pathlib
import urllib.request
from typing import Any, Mapping, Protocol

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIR = "XXBTZUSD"
DAILY_INTERVAL = 1440


class PriceSource(Protocol):
    def closes(self) -> dict[datetime.date, float]: ...


def _utc_date(ts: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()


def _parse_csv(text: str) -> dict[datetime.date, float]:
    out: dict[datetime.date, float] = {}
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 5:
            continue
        try:
            ts = int(float(parts[0]))
            close = float(parts[4])
        except ValueError:
            continue
        out[_utc_date(ts)] = close
    return out


def _parse_ohlc_json(payload: Mapping[str, Any]) -> dict[datetime.date, float]:
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
    out: dict[datetime.date, float] = {}
    for c in candles:
        try:
            ts = int(c[0])
            close = float(c[4])
        except (ValueError, IndexError, TypeError):
            continue
        out[_utc_date(ts)] = close
    return out


class KrakenCsvSource:
    def __init__(self, path: str | pathlib.Path) -> None:
        self._path = pathlib.Path(path)

    def closes(self) -> dict[datetime.date, float]:
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

    def closes(self) -> dict[datetime.date, float]:
        req = urllib.request.Request(
            f"{self._url}?pair={self._pair}&interval={self._interval}",
            headers={"User-Agent": "market-warehouse/0.1"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload = json.load(resp)
        return _parse_ohlc_json(payload)
