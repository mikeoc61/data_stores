from __future__ import annotations

import datetime
import pathlib

import duckdb
import pytest

from market_warehouse import latest
from market_warehouse.backfill import main
from market_warehouse.write import _ONCHAIN_COLS

UTC = datetime.timezone.utc


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


def _payload(day: datetime.date, hashrate: float = 100.0) -> dict:
    oc = {c: 1.0 for c in _ONCHAIN_COLS}
    oc["blocks_day"] = 144
    oc["hash_rate_ehs"] = hashrate
    return {"onchain": oc}


def _fake_aggregate(calls: list | None = None, hashrate: float = 100.0):
    def agg(day):
        if calls is not None:
            calls.append(day)
        return _payload(day, hashrate)

    return agg


def _row_dates(db: pathlib.Path) -> list[datetime.date]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT date FROM onchain ORDER BY date").fetchall()]
    finally:
        con.close()


def test_backfill_full_range(db):
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-05"],
        aggregate=_fake_aggregate(),
    )
    assert rc == 0
    assert _row_dates(db) == [datetime.date(2016, 1, d) for d in range(1, 6)]


def test_backfill_resumes_from_existing_max(db):
    main(["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-03"], aggregate=_fake_aggregate())
    calls: list = []
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-06"],
        aggregate=_fake_aggregate(calls),
    )
    assert rc == 0
    assert calls == [datetime.date(2016, 1, d) for d in (4, 5, 6)]
    assert _row_dates(db) == [datetime.date(2016, 1, d) for d in range(1, 7)]


def test_no_resume_reprocesses_and_overwrites(db):
    main(["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-03"], aggregate=_fake_aggregate(hashrate=100.0))
    rc = main(
        ["--db", str(db), "--no-resume", "--start-date", "2016-01-01", "--end-date", "2016-01-03"],
        aggregate=_fake_aggregate(hashrate=999.0),
    )
    assert rc == 0
    assert _row_dates(db) == [datetime.date(2016, 1, d) for d in (1, 2, 3)]
    assert latest("onchain", db_path=db)["hash_rate_ehs"] == 999.0


def test_dry_run_writes_nothing(db):
    rc = main(
        ["--db", str(db), "--dry-run", "--start-date", "2016-01-01", "--end-date", "2016-01-05"],
        aggregate=_fake_aggregate(),
    )
    assert rc == 0
    assert not db.exists() or _row_dates(db) == []


def test_bad_day_is_skipped_run_continues(db):
    def agg(day):
        if day == datetime.date(2016, 1, 3):
            raise RuntimeError("rpc hiccup")
        return _payload(day)

    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-05"],
        aggregate=agg,
    )
    assert rc == 0
    assert _row_dates(db) == [datetime.date(2016, 1, d) for d in (1, 2, 4, 5)]


def test_total_failure_returns_nonzero(db):
    def agg(day):
        raise RuntimeError("node down")

    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-05"],
        aggregate=agg,
    )
    assert rc == 1


def test_end_date_defaults_to_last_complete_utc_day(db):
    now = datetime.datetime(2016, 1, 6, 12, 0, tzinfo=UTC)
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01"],
        aggregate=_fake_aggregate(),
        now=now,
    )
    assert rc == 0
    assert _row_dates(db)[-1] == datetime.date(2016, 1, 5)


def test_chunking_commits_all_rows(db):
    rc = main(
        ["--db", str(db), "--start-date", "2016-01-01", "--end-date", "2016-01-10", "--chunk", "3"],
        aggregate=_fake_aggregate(),
    )
    assert rc == 0
    assert len(_row_dates(db)) == 10
