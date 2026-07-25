from __future__ import annotations

import datetime
import pathlib

import duckdb
import pytest

from market_warehouse import write_snapshot, latest
from market_warehouse.daily_update import last_complete_utc_day, main
from market_warehouse.write import _ONCHAIN_COLS

UTC = datetime.timezone.utc


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


def _payload(day: datetime.date) -> dict:
    oc = {c: 1.0 for c in _ONCHAIN_COLS}
    oc["blocks_day"] = 144
    return {"onchain": oc}


def _fake_aggregate(calls: list | None = None):
    def agg(day):
        if calls is not None:
            calls.append(day)
        return _payload(day)

    return agg


def _row_dates(db: pathlib.Path) -> list[datetime.date]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT date FROM onchain ORDER BY date").fetchall()]
    finally:
        con.close()


def test_last_complete_day_after_02utc_is_yesterday():
    now = datetime.datetime(2026, 7, 25, 2, 0, tzinfo=UTC)
    assert last_complete_utc_day(now) == datetime.date(2026, 7, 24)


def test_last_complete_day_before_02utc_is_two_days_back():
    now = datetime.datetime(2026, 7, 25, 1, 0, tzinfo=UTC)
    assert last_complete_utc_day(now) == datetime.date(2026, 7, 23)


def test_empty_warehouse_writes_only_last_complete_day(db):
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    rc = main(["--db", str(db)], aggregate=_fake_aggregate(), now=now)
    assert rc == 0
    assert _row_dates(db) == [datetime.date(2026, 7, 24)]


def test_gap_fill_appends_missing_days(db):
    write_snapshot("2026-07-21", _payload(datetime.date(2026, 7, 21)), db_path=db)
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    calls: list = []
    rc = main(["--db", str(db)], aggregate=_fake_aggregate(calls), now=now)
    assert rc == 0
    assert calls == [datetime.date(2026, 7, d) for d in (22, 23, 24)]
    assert _row_dates(db) == [datetime.date(2026, 7, d) for d in (21, 22, 23, 24)]


def test_up_to_date_is_noop(db):
    write_snapshot("2026-07-24", _payload(datetime.date(2026, 7, 24)), db_path=db)
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    calls: list = []
    rc = main(["--db", str(db)], aggregate=_fake_aggregate(calls), now=now)
    assert rc == 0
    assert calls == []
    assert _row_dates(db) == [datetime.date(2026, 7, 24)]


def test_single_date_mode_writes_that_day(db):
    rc = main(["--db", str(db), "--date", "2020-01-02"], aggregate=_fake_aggregate())
    assert rc == 0
    assert _row_dates(db) == [datetime.date(2020, 1, 2)]


def test_dry_run_does_not_write(db):
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    rc = main(["--db", str(db), "--dry-run"], aggregate=_fake_aggregate(), now=now)
    assert rc == 0
    assert not db.exists() or _row_dates(db) == []


def test_one_bad_day_logs_and_continues(db):
    write_snapshot("2026-07-21", _payload(datetime.date(2026, 7, 21)), db_path=db)
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    def agg(day):
        if day == datetime.date(2026, 7, 23):
            raise RuntimeError("node hiccup")
        return _payload(day)

    rc = main(["--db", str(db)], aggregate=agg, now=now)
    assert rc == 0
    assert _row_dates(db) == [datetime.date(2026, 7, d) for d in (21, 22, 24)]


def test_total_failure_returns_nonzero(db):
    write_snapshot("2026-07-23", _payload(datetime.date(2026, 7, 23)), db_path=db)
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    def agg(day):
        raise RuntimeError("node down")

    rc = main(["--db", str(db)], aggregate=agg, now=now)
    assert rc == 1
