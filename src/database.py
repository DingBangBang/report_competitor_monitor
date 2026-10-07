"""SQLite persistence layer shared by both pipelines.

Tables
------
weekly_metrics      Normalised KPI rows for daily/weekly/monthly reports.
competitor_events   One row per competitor announcement (deduped by event_id).
fetch_state         Bookkeeping for incremental scraping (last fetch time).
run_log             One row per pipeline execution (status + summary).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type   TEXT    NOT NULL,          -- daily|weekly|monthly
    metric_key    TEXT    NOT NULL,          -- machine key, e.g. contract_volume
    metric_label  TEXT    NOT NULL,          -- human label for cards
    metric_value  REAL    NOT NULL,
    metric_unit   TEXT,                      -- $, %, count, ...
    change_pct    REAL,                      -- vs previous period
    period_start  TEXT    NOT NULL,
    period_end    TEXT    NOT NULL,
    source        TEXT,
    created_at    TEXT    NOT NULL,
    UNIQUE(report_type, metric_key, period_end)
);

CREATE TABLE IF NOT EXISTS competitor_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT    NOT NULL UNIQUE,
    exchange        TEXT    NOT NULL,
    category        TEXT    NOT NULL,        -- new_listing|delisting|fee_change|activity
    title           TEXT    NOT NULL,
    url             TEXT,
    published_at    TEXT,
    symbols         TEXT,                     -- JSON-encoded list
    raw_summary     TEXT,
    fetched_at      TEXT    NOT NULL,
    analyzed        INTEGER NOT NULL DEFAULT 0,
    impact_level    TEXT,
    impact_reason   TEXT,
    suggested_action TEXT,
    urgency_score   INTEGER
);

CREATE TABLE IF NOT EXISTS fetch_state (
    key           TEXT PRIMARY KEY,
    last_fetch_at TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline     TEXT NOT NULL,
    report_type  TEXT,
    status       TEXT NOT NULL,
    summary      TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    detail       TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")



class Database:
    """Thin, synchronous SQLite wrapper (connection per call)."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or Config.DB_PATH
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- low level -----------------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # -- weekly metrics ------------------------------------------------------
    def upsert_metric(
        self,
        report_type: str,
        metric_key: str,
        metric_label: str,
        metric_value: float,
        metric_unit: str,
        change_pct: float | None,
        period_start: str,
        period_end: str,
        source: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO weekly_metrics
                    (report_type, metric_key, metric_label, metric_value,
                     metric_unit, change_pct, period_start, period_end, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_type, metric_key, period_end) DO UPDATE SET
                    metric_value=excluded.metric_value,
                    change_pct=excluded.change_pct,
                    metric_label=excluded.metric_label,
                    metric_unit=excluded.metric_unit,
                    source=excluded.source,
                    created_at=excluded.created_at
                """,
                (
                    report_type, metric_key, metric_label, metric_value,
                    metric_unit, change_pct, period_start, period_end, source, _now(),
                ),
            )

    def get_metrics(self, report_type: str, period_end: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM weekly_metrics WHERE report_type = ?"
        params: list[Any] = [report_type]
        if period_end:
            query += " AND period_end = ?"
            params.append(period_end)
        query += " ORDER BY metric_value DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def latest_period_end(self, report_type: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(period_end) AS p FROM weekly_metrics WHERE report_type = ?",
                (report_type,),
            ).fetchone()
        return row["p"] if row and row["p"] else None

    # -- competitor events ---------------------------------------------------
    def insert_competitor_event(self, event: dict[str, Any]) -> bool:
        """Insert an event; returns True when it was new, False when a duplicate."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO competitor_events
                    (event_id, exchange, category, title, url, published_at,
                     symbols, raw_summary, fetched_at, analyzed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    event["event_id"],
                    event["exchange"],
                    event["category"],
                    event["title"],
                    event.get("url", ""),
                    event.get("published_at", ""),
                    json.dumps(event.get("symbols", []), ensure_ascii=False),
                    event.get("raw_summary", ""),
                    _now(),
                ),
            )
            return cur.rowcount > 0

    def get_unanalyzed_events(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM competitor_events WHERE analyzed = 0 ORDER BY published_at DESC"
            ).fetchall()
        return [self._decode_event(r) for r in rows]

    def get_events_since(self, since_iso: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM competitor_events
                WHERE published_at >= ?
                ORDER BY published_at DESC LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [self._decode_event(r) for r in rows]

    def get_recent_events(self, days: int = 7) -> list[dict[str, Any]]:
        from datetime import timedelta

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.get_events_since(since)

    def update_event_analysis(
        self,
        event_id: str,
        impact_level: str,
        impact_reason: str,
        suggested_action: str,
        urgency_score: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE competitor_events
                SET analyzed = 1, impact_level = ?, impact_reason = ?,
                    suggested_action = ?, urgency_score = ?
                WHERE event_id = ?
                """,
                (impact_level, impact_reason, suggested_action, urgency_score, event_id),
            )

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["symbols"] = json.loads(data.get("symbols") or "[]")
        except (json.JSONDecodeError, TypeError):
            data["symbols"] = []
        return data

    # -- fetch state ---------------------------------------------------------
    def get_last_fetch_at(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_fetch_at FROM fetch_state WHERE key = ?", (key,)
            ).fetchone()
        return row["last_fetch_at"] if row else None

    def set_last_fetch_at(self, key: str, value: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO fetch_state (key, last_fetch_at) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET last_fetch_at=excluded.last_fetch_at
                """,
                (key, value or _now()),
            )

    # -- run log -------------------------------------------------------------
    def log_run(
        self,
        pipeline: str,
        status: str,
        summary: str = "",
        report_type: str | None = None,
        detail: str | None = None,
        started_at: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO run_log
                    (pipeline, report_type, status, summary, started_at, finished_at, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pipeline, report_type, status, summary, started_at or _now(), _now(), detail),
            )
