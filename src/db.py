"""
ezadmin 경량 SQLite 스토어.
포트폴리오별 daily snapshot(총자산, 당일 실현손익)을 `data/ezadmin.db`에 저장한다.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def _db_path(project_root):
    return os.path.join(project_root, "data", "ezadmin.db")


def _connect(project_root):
    path = _db_path(project_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(project_root):
    """스키마 초기화. 앱 시작 시 1회 호출."""
    with _connect(project_root) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_daily (
                portfolio_name TEXT NOT NULL,
                date TEXT NOT NULL,
                total_asset REAL NOT NULL,
                realized_pl REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (portfolio_name, date)
            )
        """)


def upsert_today(project_root, portfolio_name, total_asset, realized_pl):
    """오늘(KST) 스냅샷을 upsert. realized_pl은 None 가능."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    now = datetime.now(KST).isoformat(timespec="seconds")
    with _connect(project_root) as c:
        c.execute("""
            INSERT INTO portfolio_daily (portfolio_name, date, total_asset, realized_pl, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(portfolio_name, date) DO UPDATE SET
                total_asset = excluded.total_asset,
                realized_pl = excluded.realized_pl,
                updated_at = excluded.updated_at
        """, (portfolio_name, today, float(total_asset),
              None if realized_pl is None else float(realized_pl), now))


def get_recent_snapshots(project_root, portfolio_name, days=30):
    """최근 `days`일(오늘 포함) 스냅샷을 날짜 오름차순으로 반환."""
    start = (datetime.now(KST) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    with _connect(project_root) as c:
        cur = c.execute("""
            SELECT date, total_asset, realized_pl
            FROM portfolio_daily
            WHERE portfolio_name = ? AND date >= ?
            ORDER BY date
        """, (portfolio_name, start))
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
