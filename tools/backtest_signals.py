#!/usr/bin/env python3
"""Backtest the signal helpers against known historical regimes.

Each helper anchors its window to max(date) of the table, so "as of" evaluation
works by copying the warehouse's onchain rows up to a cut-off date into a temp
database and running the helpers against that. The real warehouse is only ever
opened READ_ONLY.

    python3 tools/backtest_signals.py [--db PATH]

Washout dates should show a low fee/subsidy percentile (and usually an apathy
streak and/or hashrate drawdown); euphoria dates should stay quiet.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import tempfile

import duckdb

from market_warehouse import apathy_streak_pct, drawdown_from_high, percentile_rank
from market_warehouse.write import _db_path

REGIMES: list[tuple[str, str, str]] = [
    ("washout", "2018-12-15", "2018 bear bottom (BTC ~$3.2k)"),
    ("washout", "2021-07-02", "China ban trough (day before the -28% adjustment)"),
    ("washout", "2022-11-09", "FTX collapse day (VOLUME event)"),
    ("washout", "2022-11-21", "post-FTX capitulation low (PRICE event, 12d later)"),
    ("euphoria", "2017-12-17", "2017 blowoff top (fee mania)"),
    ("euphoria", "2021-04-14", "BTC ATH ~$64k / Coinbase IPO"),
    ("euphoria", "2021-11-09", "BTC ATH ~$69k"),
]


def _as_of_db(real: pathlib.Path, cutoff: str, tmpdir: pathlib.Path) -> pathlib.Path | None:
    out = tmpdir / f"asof_{cutoff}.duckdb"
    con = duckdb.connect(str(out))
    try:
        con.execute(f"ATTACH '{real}' AS src (READ_ONLY)")
        con.execute(
            f"CREATE TABLE onchain AS SELECT * FROM src.onchain WHERE date <= DATE '{cutoff}'"
        )
        n = con.execute("SELECT count(*) FROM onchain").fetchone()[0]
        try:
            con.execute(
                f"CREATE TABLE btc AS SELECT * FROM src.btc WHERE date <= DATE '{cutoff}'"
            )
        except Exception:
            pass
        con.execute("DETACH src")
    finally:
        con.close()
    return out if n else None


def _raw(db: pathlib.Path) -> tuple:
    """Latest row of the already-cut-off snapshot database."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(
            "SELECT date, fee_subsidy, hash_rate_ehs FROM onchain ORDER BY date DESC LIMIT 1"
        ).fetchone() or (None, None, None)
    finally:
        con.close()


def _fmt(v, spec=".1f", dash="—"):
    return dash if v is None else format(v, spec)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    real = pathlib.Path(args.db) if args.db else _db_path()
    if not real.exists():
        print(f"warehouse not found: {real}")
        return 1

    con = duckdb.connect(str(real), read_only=True)
    lo, hi, total = con.execute(
        "SELECT min(date), max(date), count(*) FROM onchain"
    ).fetchone()
    con.close()
    print(f"warehouse: {real}")
    print(f"coverage : {lo} .. {hi}  ({total} rows)\n")

    rows = REGIMES + [("current", str(hi), "latest complete day")]
    hdr = (
        f"{'regime':9} {'as-of':11} {'fee%':>6} {'pctile':>7} {'apathy':>7} "
        f"{'hr_dd':>8} {'vol%ile':>8}  note"
    )
    print(hdr)
    print("-" * len(hdr))

    with tempfile.TemporaryDirectory() as td:
        tmpdir = pathlib.Path(td)
        for regime, cutoff, note in rows:
            if datetime.date.fromisoformat(cutoff) < lo:
                print(f"{regime:9} {cutoff:11} {'—':>6} {'—':>7} {'—':>7} {'—':>8} {'—':>8}  before coverage")
                continue
            asof = _as_of_db(real, cutoff, tmpdir)
            if asof is None:
                print(f"{regime:9} {cutoff:11} {'—':>6} {'—':>7} {'—':>7} {'—':>8} {'—':>8}  no rows")
                continue
            _, fee, _hr = _raw(asof)
            pct = percentile_rank("fee_subsidy", window_days=730, db_path=asof)
            streak = apathy_streak_pct(percentile=10, window_days=730, db_path=asof)
            dd = drawdown_from_high("hash_rate_ehs", window_days=90, db_path=asof)
            # detrend_dow, not smooth_days: volume carries the same weekly cycle
            # as fee_subsidy (FTX week: Sat/Sun ~1/8th of the weekday spike), but
            # a volume event lasts 3-4 days, which a 7-day mean dilutes away.
            # Detrending removes the cycle while keeping daily resolution.
            try:
                vol = percentile_rank(
                    "kraken_vol", window_days=730, table="btc",
                    detrend_dow=True, db_path=asof,
                )
            except Exception:
                vol = None
            print(
                f"{regime:9} {cutoff:11} {_fmt(fee, '.2f'):>6} {_fmt(pct, '.1f'):>7} "
                f"{_fmt(streak, 'd'):>7} {_fmt(dd, '.1f'):>8} {_fmt(vol, '.1f'):>8}  {note}"
            )

    print(
        "\nexpect: washout rows at a LOW fee pctile with apathy/hashrate drawdown\n"
        "        present; euphoria rows at a HIGH fee pctile with apathy 0.\n"
        "        vol%ile tests whether volume catches the price/credit washouts\n"
        "        (2022-11 FTX) that the on-chain fee gauge structurally cannot."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
