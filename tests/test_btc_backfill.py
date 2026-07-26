from __future__ import annotations

import datetime
import pathlib

import duckdb
import pytest

from market_warehouse import latest, write_snapshot
from market_warehouse.btc_backfill import main

UTC = datetime.timezone.utc


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


class FakeSource:
    def __init__(self, bars: dict[datetime.date, dict]) -> None:
        self._bars = bars

    def bars(self) -> dict[datetime.date, dict]:
        return self._bars


def _closes(start: str, values: list[float]) -> dict[datetime.date, dict]:
    base = datetime.date.fromisoformat(start)
    return {
        base + datetime.timedelta(days=i): {
            "close": v, "kraken_vol": v * 10, "kraken_trades": 100 + i
        }
        for i, v in enumerate(values)
    }


def _btc_dates(db: pathlib.Path) -> list[datetime.date]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT date FROM btc ORDER BY date").fetchall()]
    finally:
        con.close()


def test_backfill_writes_closes_in_range(db):
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0, 102.0, 103.0, 104.0]))
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-05"],
        price_source=src,
    )
    assert rc == 0
    assert _btc_dates(db) == [datetime.date(2016, 1, d) for d in range(1, 6)]
    assert latest("btc", db_path=db)["close"] == 104.0


def test_backfill_clips_to_requested_end(db):
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0, 102.0, 103.0, 104.0]))
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-03"],
        price_source=src,
    )
    assert rc == 0
    assert _btc_dates(db) == [datetime.date(2016, 1, d) for d in (1, 2, 3)]


def test_backfill_resumes_from_existing_max(db):
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0, 102.0, 103.0, 104.0]))
    main(["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-02"], price_source=src)
    rc = main(["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-05"], price_source=src)
    assert rc == 0
    assert _btc_dates(db) == [datetime.date(2016, 1, d) for d in range(1, 6)]


def test_dry_run_writes_nothing(db):
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0]))
    rc = main(["--db", str(db), "--dry-run", "--start-date", "2016-01-01", "--end-date", "2016-01-02"], price_source=src)
    assert rc == 0
    assert not db.exists() or _btc_dates(db) == []


def test_empty_range_returns_nonzero(db):
    src = FakeSource(_closes("2016-01-01", [100.0]))
    rc = main(["--db", str(db), "--start-date", "2020-01-01", "--end-date", "2020-01-05"], price_source=src)
    assert rc == 1


def test_backfill_when_btc_table_dropped(db):
    write_snapshot("2016-01-01", {"onchain": {"hash_rate_ehs": 1.0}}, db_path=db)
    con = duckdb.connect(str(db))
    con.execute("DROP TABLE btc")
    con.close()
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0]))
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-02"],
        price_source=src,
    )
    assert rc == 0
    assert _btc_dates(db) == [datetime.date(2016, 1, 1), datetime.date(2016, 1, 2)]


def test_end_date_defaults_to_last_complete_utc_day(db):
    now = datetime.datetime(2016, 1, 6, 12, 0, tzinfo=UTC)
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]))
    rc = main(["--db", str(db), "--start-date", "2016-01-01"], price_source=src, now=now)
    assert rc == 0
    assert _btc_dates(db)[-1] == datetime.date(2016, 1, 5)


def test_backfill_persists_volume_and_trades(db):
    src = FakeSource(_closes("2016-01-01", [100.0, 101.0]))
    assert main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-02"],
        price_source=src,
    ) == 0
    row = latest("btc", db_path=db)
    assert row["close"] == 101.0
    assert row["kraken_vol"] == 1010.0
    assert row["kraken_trades"] == 101


def test_default_start_takes_everything_the_source_provides(db):
    # Guards a footgun: a hardcoded default start silently truncates history
    # when a later CSV reaches further back than the default.
    src = FakeSource(_closes("2013-10-06", [100.0, 101.0, 102.0]))
    assert main(["--db", str(db), "--end-date", "2013-10-08"], price_source=src) == 0
    assert _btc_dates(db)[0] == datetime.date(2013, 10, 6)


def test_explicit_start_still_clips(db):
    src = FakeSource(_closes("2013-10-06", [100.0, 101.0, 102.0]))
    assert main(
        ["--db", str(db), "--start-date", "2013-10-07", "--end-date", "2013-10-08"],
        price_source=src,
    ) == 0
    assert _btc_dates(db)[0] == datetime.date(2013, 10, 7)
