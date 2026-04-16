"""Data models for the prosperity uploader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class SubmissionStatus(str, Enum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "FINISHED"  # confirmed platform value
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"


@dataclass
class UploadResult:
    """Result from uploading an algorithm file."""

    file_path: str
    file_hash: str
    filename: str
    status_code: int
    response_body: dict[str, Any]
    timestamp: datetime
    submission_id: Optional[str] = None  # extracted if present in response

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UploadResult:
        d = dict(d)
        d["timestamp"] = datetime.fromisoformat(d["timestamp"])
        return cls(**d)


@dataclass
class SubmissionRecord:
    """A submission entry from the submissions list or polling."""

    submission_id: str
    status: SubmissionStatus = SubmissionStatus.UNKNOWN
    filename: Optional[str] = None
    timestamp: Optional[datetime] = None
    submitted_at: Optional[datetime] = None  # from submittedAt field
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            SubmissionStatus.FINISHED,
            SubmissionStatus.COMPLETED,
            SubmissionStatus.FAILED,
            SubmissionStatus.ERROR,
        )

    @property
    def is_success(self) -> bool:
        return self.status in (SubmissionStatus.FINISHED, SubmissionStatus.COMPLETED)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        if self.timestamp:
            d["timestamp"] = self.timestamp.isoformat()
        if self.submitted_at:
            d["submitted_at"] = self.submitted_at.isoformat()
        return d


@dataclass
class GraphResult:
    """Result from the graph endpoint."""

    submission_id: str
    artifact_url: str
    raw_response: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SummaryMetrics:
    """Computed metrics from a parsed artifact."""

    strategy_name: str
    submission_id: str
    filename: Optional[str] = None
    submitted_at: Optional[str] = None  # ISO string
    artifact_path: Optional[str] = None
    run_dir: Optional[str] = None
    final_pnl: float = 0.0
    max_pnl: float = 0.0
    min_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    first_positive_ts: Optional[float] = None
    pnl_slope: float = 0.0
    pnl_volatility: float = 0.0
    num_points: int = 0
    time_horizon: float = 0.0
    peak_to_final_drop: float = 0.0
    recovery_after_drawdown: float = 0.0
    trade_count: Optional[int] = None
    products: list[str] = field(default_factory=list)
    raw_fields_found: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_csv_row(self) -> dict[str, Any]:
        d = self.to_dict()
        d["products"] = ";".join(d["products"])
        d["raw_fields_found"] = ";".join(d["raw_fields_found"])
        return d

    @staticmethod
    def csv_headers() -> list[str]:
        return [
            "strategy_name",
            "submission_id",
            "filename",
            "submitted_at",
            "artifact_path",
            "run_dir",
            "final_pnl",
            "max_pnl",
            "min_pnl",
            "max_drawdown",
            "max_drawdown_pct",
            "first_positive_ts",
            "pnl_slope",
            "pnl_volatility",
            "num_points",
            "time_horizon",
            "peak_to_final_drop",
            "recovery_after_drawdown",
            "trade_count",
            "products",
            "raw_fields_found",
        ]


@dataclass
class RunRecord:
    """Full record for one strategy upload + backtest cycle."""

    strategy_name: str
    file_path: str
    file_hash: str
    upload_time: datetime
    upload_result: Optional[UploadResult] = None
    submission: Optional[SubmissionRecord] = None
    graph_result: Optional[GraphResult] = None
    summary: Optional[SummaryMetrics] = None
    error: Optional[str] = None
    run_dir: Optional[str] = None

    @property
    def submission_id(self) -> Optional[str]:
        if self.submission:
            return self.submission.submission_id
        if self.upload_result:
            return self.upload_result.submission_id
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "file_path": self.file_path,
            "file_hash": self.file_hash,
            "upload_time": self.upload_time.isoformat(),
            "submission_id": self.submission_id,
            "error": self.error,
            "run_dir": self.run_dir,
        }
