"""Persistence layer: file storage, CSV, and SQLite."""

from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import Config
from models import (
    GraphResult,
    RunRecord,
    SubmissionRecord,
    SummaryMetrics,
    UploadResult,
)
from utils import save_json, timestamp_slug

logger = logging.getLogger(__name__)


# ── Run directory management ──────────────────────────────────────

def create_run_dir(config: Config, strategy_name: str) -> Path:
    """Create a timestamped run directory for one strategy upload cycle."""
    slug = f"{timestamp_slug()}_{strategy_name}"
    run_dir = Path(config.output_dir) / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Created run directory: %s", run_dir)
    return run_dir


def save_upload_result(run_dir: Path, result: UploadResult) -> None:
    save_json(result.to_dict(), run_dir / "upload_response.json")


def save_submission(run_dir: Path, record: SubmissionRecord) -> None:
    save_json(record.to_dict(), run_dir / "submission.json")


def save_graph_result(run_dir: Path, result: GraphResult) -> None:
    save_json(result.to_dict(), run_dir / "graph_response.json")


def save_summary(run_dir: Path, summary: SummaryMetrics) -> None:
    save_json(summary.to_dict(), run_dir / "summary.json")


def save_raw_list_response(run_dir: Path, data: dict) -> None:
    save_json(data, run_dir / "submissions_list_response.json")


def save_error(run_dir: Path, error: str) -> None:
    save_json({"error": error, "timestamp": datetime.now().isoformat()}, run_dir / "error.json")


def artifact_path(run_dir: Path) -> Path:
    return run_dir / "artifact.json"


# ── CSV leaderboard ───────────────────────────────────────────────

def append_to_csv(config: Config, summary: SummaryMetrics) -> None:
    """Append a summary row to the results CSV."""
    csv_path = Path(config.results_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    file_exists = csv_path.exists()
    row = summary.to_csv_row()

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SummaryMetrics.csv_headers())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    logger.info("Appended to CSV: %s", csv_path)


def load_csv_leaderboard(config: Config) -> list[dict[str, Any]]:
    """Load the full CSV leaderboard as a list of dicts."""
    csv_path = Path(config.results_csv)
    if not csv_path.exists():
        return []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Sort by final_pnl descending
    rows.sort(key=lambda r: float(r.get("final_pnl", 0)), reverse=True)
    return rows


# ── SQLite persistence ────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    upload_time TEXT NOT NULL,
    status_code INTEGER,
    submission_id TEXT,
    response_json TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL UNIQUE,
    status TEXT,
    filename TEXT,
    timestamp TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL,
    artifact_url TEXT,
    local_path TEXT,
    download_time TEXT,
    graph_response_json TEXT,
    FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    filename TEXT,
    submitted_at TEXT,
    artifact_path TEXT,
    run_dir TEXT,
    final_pnl REAL,
    max_pnl REAL,
    min_pnl REAL,
    max_drawdown REAL,
    max_drawdown_pct REAL,
    first_positive_ts REAL,
    pnl_slope REAL,
    pnl_volatility REAL,
    num_points INTEGER,
    time_horizon REAL,
    peak_to_final_drop REAL,
    recovery_after_drawdown REAL,
    trade_count INTEGER,
    products TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (submission_id) REFERENCES submissions(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_summaries_pnl ON summaries(final_pnl DESC);
CREATE INDEX IF NOT EXISTS idx_uploads_hash ON uploads(file_hash);
"""


class Database:
    """SQLite persistence for all run data."""

    def __init__(self, config: Config):
        db_path = Path(config.sqlite_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        # Migrate: add columns that may not exist in older databases
        for col, coltype in (
            ("filename", "TEXT"),
            ("submitted_at", "TEXT"),
            ("artifact_path", "TEXT"),
            ("run_dir", "TEXT"),
        ):
            try:
                self.conn.execute(f"ALTER TABLE summaries ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── Uploads ───────────────────────────────────────────────────

    def insert_upload(self, result: UploadResult, strategy_name: str) -> int:
        import json

        cur = self.conn.execute(
            """
            INSERT INTO uploads
                (strategy_name, file_path, file_hash, filename, upload_time,
                 status_code, submission_id, response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_name,
                result.file_path,
                result.file_hash,
                result.filename,
                result.timestamp.isoformat(),
                result.status_code,
                result.submission_id,
                json.dumps(result.response_body),
            ),
        )
        self.conn.commit()
        return cur.lastrowid  # type: ignore

    def find_upload_by_hash(self, file_hash: str) -> Optional[dict]:
        """Check if a file with this hash was already uploaded."""
        row = self.conn.execute(
            "SELECT * FROM uploads WHERE file_hash = ? ORDER BY id DESC LIMIT 1",
            (file_hash,),
        ).fetchone()
        return dict(row) if row else None

    # ── Submissions ───────────────────────────────────────────────

    def upsert_submission(self, record: SubmissionRecord) -> None:
        import json

        self.conn.execute(
            """
            INSERT INTO submissions (submission_id, status, filename, timestamp, raw_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(submission_id) DO UPDATE SET
                status = excluded.status,
                raw_json = excluded.raw_json
            """,
            (
                record.submission_id,
                record.status.value,
                record.filename,
                record.timestamp.isoformat() if record.timestamp else None,
                json.dumps(record.raw),
            ),
        )
        self.conn.commit()

    # ── Artifacts ─────────────────────────────────────────────────

    def insert_artifact(
        self,
        submission_id: str,
        artifact_url: str,
        local_path: str,
        graph_response: dict,
    ) -> None:
        import json

        self.conn.execute(
            """
            INSERT INTO artifacts
                (submission_id, artifact_url, local_path, download_time, graph_response_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                artifact_url,
                local_path,
                datetime.now().isoformat(),
                json.dumps(graph_response),
            ),
        )
        self.conn.commit()

    # ── Summaries ─────────────────────────────────────────────────

    def insert_summary(self, summary: SummaryMetrics) -> None:
        self.conn.execute(
            """
            INSERT INTO summaries
                (strategy_name, submission_id, filename, submitted_at,
                 artifact_path, run_dir, final_pnl, max_pnl, min_pnl,
                 max_drawdown, max_drawdown_pct, first_positive_ts, pnl_slope,
                 pnl_volatility, num_points, time_horizon, peak_to_final_drop,
                 recovery_after_drawdown, trade_count, products)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary.strategy_name,
                summary.submission_id,
                summary.filename,
                summary.submitted_at,
                summary.artifact_path,
                summary.run_dir,
                summary.final_pnl,
                summary.max_pnl,
                summary.min_pnl,
                summary.max_drawdown,
                summary.max_drawdown_pct,
                summary.first_positive_ts,
                summary.pnl_slope,
                summary.pnl_volatility,
                summary.num_points,
                summary.time_horizon,
                summary.peak_to_final_drop,
                summary.recovery_after_drawdown,
                summary.trade_count,
                ";".join(summary.products),
            ),
        )
        self.conn.commit()

    def get_leaderboard(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT strategy_name, submission_id, filename, submitted_at,
                   final_pnl, max_pnl, min_pnl, max_drawdown, num_points,
                   artifact_path, run_dir, pnl_slope, trade_count, created_at
            FROM summaries
            ORDER BY final_pnl DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_latest_run_by_hash(self, file_hash: str) -> Optional[dict]:
        """Find the most recent upload + run directory for a given file hash."""
        row = self.conn.execute(
            """
            SELECT u.strategy_name, u.file_hash, u.submission_id, u.upload_time,
                   s.status, s.filename,
                   a.local_path AS artifact_path
            FROM uploads u
            LEFT JOIN submissions s ON u.submission_id = s.submission_id
            LEFT JOIN artifacts a ON u.submission_id = a.submission_id
            WHERE u.file_hash = ?
            ORDER BY u.id DESC
            LIMIT 1
            """,
            (file_hash,),
        ).fetchone()
        return dict(row) if row else None

    def has_been_uploaded(self, file_hash: str) -> bool:
        """Check if this exact file was already uploaded."""
        return self.find_upload_by_hash(file_hash) is not None
