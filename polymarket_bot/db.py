from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_conn: sqlite3.Connection | None = None


def get_conn(db_path: Path) -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(db_path))
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init_tables(_conn)
    return _conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            market_id TEXT NOT NULL,
            question TEXT,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size_usdc REAL NOT NULL,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'placed',
            edge REAL,
            confidence REAL,
            estimated_prob REAL,
            reasoning TEXT,
            result TEXT,
            pnl REAL
        );

        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            market_id TEXT NOT NULL,
            question TEXT,
            market_price REAL,
            estimated_prob REAL,
            confidence REAL,
            edge REAL,
            recommendation TEXT,
            reasoning TEXT,
            key_risks TEXT
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            bankroll REAL,
            exposure REAL,
            unrealized_pnl REAL,
            realized_pnl REAL,
            positions_json TEXT
        );
    """)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may not exist in older databases."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "result" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN result TEXT")
    if "pnl" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN pnl REAL")
    if "estimated_prob" not in existing:
        conn.execute("ALTER TABLE trades ADD COLUMN estimated_prob REAL")
    conn.commit()


def insert_trade(conn: sqlite3.Connection, **kwargs) -> int:
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    cur = conn.execute(
        f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()
    return cur.lastrowid


def insert_analysis(conn: sqlite3.Connection, **kwargs) -> int:
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join(["?"] * len(kwargs))
    cur = conn.execute(
        f"INSERT INTO analyses ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()
    return cur.lastrowid


def insert_snapshot(conn: sqlite3.Connection, bankroll: float, exposure: float,
                    unrealized_pnl: float, realized_pnl: float,
                    positions: dict) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots (bankroll, exposure, unrealized_pnl, realized_pnl, positions_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (bankroll, exposure, unrealized_pnl, realized_pnl, json.dumps(positions)),
    )
    conn.commit()
    return cur.lastrowid


def get_trades(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM analyses ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
