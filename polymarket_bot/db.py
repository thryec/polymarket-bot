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


def get_calibration_stats(conn: sqlite3.Connection) -> dict:
    """Compute Brier score and per-bucket calibration from resolved trades."""
    rows = conn.execute(
        "SELECT estimated_prob, result FROM trades "
        "WHERE result IS NOT NULL AND estimated_prob IS NOT NULL"
    ).fetchall()

    if not rows:
        return {"brier": None, "n": 0, "buckets": []}

    total_sq_error = 0.0
    # Buckets: [0-0.2), [0.2-0.4), [0.4-0.6), [0.6-0.8), [0.8-1.0]
    bucket_bounds = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    bucket_data = [{"sum_prob": 0.0, "sum_outcome": 0.0, "n": 0} for _ in bucket_bounds]

    for row in rows:
        prob = row[0]
        outcome = 1.0 if row[1] == "WIN" else 0.0
        total_sq_error += (prob - outcome) ** 2

        for i, (lo, hi) in enumerate(bucket_bounds):
            if lo <= prob < hi:
                bucket_data[i]["sum_prob"] += prob
                bucket_data[i]["sum_outcome"] += outcome
                bucket_data[i]["n"] += 1
                break

    n = len(rows)
    brier = total_sq_error / n

    buckets = []
    for i, (lo, hi) in enumerate(bucket_bounds):
        bd = bucket_data[i]
        if bd["n"] > 0:
            buckets.append({
                "range": f"{lo:.0%}-{min(hi, 1.0):.0%}",
                "avg_prob": bd["sum_prob"] / bd["n"],
                "actual_win_rate": bd["sum_outcome"] / bd["n"],
                "n": bd["n"],
            })

    return {"brier": brier, "n": n, "buckets": buckets}


def get_category_win_rates(conn: sqlite3.Connection) -> dict[str, dict]:
    """Compute win rates per market category from resolved trades."""
    import re
    from .risk import CATEGORY_PATTERNS

    rows = conn.execute(
        "SELECT question, result FROM trades WHERE result IS NOT NULL"
    ).fetchall()
    if not rows:
        return {}

    stats: dict[str, dict] = {}
    for row in rows:
        question = row[0] or ""
        result = row[1]
        words = set(re.findall(r"[a-z]{2,}", question.lower()))
        best_cat, best_n = "other", 0
        for cat, kw in CATEGORY_PATTERNS.items():
            n = len(words & kw)
            if n > best_n:
                best_cat, best_n = cat, n
        if best_cat not in stats:
            stats[best_cat] = {"wins": 0, "losses": 0}
        if result == "WIN":
            stats[best_cat]["wins"] += 1
        else:
            stats[best_cat]["losses"] += 1

    result_dict = {}
    for cat, s in stats.items():
        total = s["wins"] + s["losses"]
        if total >= 3:
            result_dict[cat] = {"win_rate": s["wins"] / total, "n": total}
    return result_dict
