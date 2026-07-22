from __future__ import annotations

import duckdb

SCHEMA_VERSION = 1

_DDL: dict[str, str] = {
    "schema_version": """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """,
    "onchain": """
        CREATE TABLE IF NOT EXISTS onchain (
            date            DATE    NOT NULL PRIMARY KEY,
            hash_rate_ehs   DOUBLE,
            hash_rate_7d    DOUBLE,
            difficulty_t    DOUBLE,
            retarget_proj   DOUBLE,
            fee_subsidy     DOUBLE,
            blocks_24h      INTEGER,
            block_fullness  INTEGER,
            p50_fee         DOUBLE,
            miner_rev       DOUBLE,
            tx_rate         DOUBLE,
            tx_rate_7d      DOUBLE
        )
    """,
    "btc": """
        CREATE TABLE IF NOT EXISTS btc (
            date        DATE   NOT NULL PRIMARY KEY,
            price       DOUBLE,
            sma200      DOUBLE,
            sma200_pct  DOUBLE
        )
    """,
}

TABLES: tuple[str, ...] = ("onchain", "btc")


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    for ddl in _DDL.values():
        con.execute(ddl)
    row = con.execute("SELECT max(version) FROM schema_version").fetchone()
    current = row[0] if row else None
    if current is None or current < SCHEMA_VERSION:
        con.execute("INSERT INTO schema_version (version) VALUES (?)", [SCHEMA_VERSION])
