"""Configuration management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from exceptions import ConfigError


_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@dataclass
class Config:
    """Application configuration with sensible defaults."""

    # API endpoints
    base_api: str = "https://3dzqiahkw1.execute-api.eu-west-1.amazonaws.com/prod"
    upload_endpoint: str = "/submission/algo"
    submissions_endpoint: str = "/submissions/algo/{round_id}"
    graph_endpoint: str = "/submissions/algo/{submission_id}/graph"
    round_id: int = 2  # current active competition round

    # Timing
    poll_interval_seconds: float = 10.0
    poll_timeout_seconds: float = 600.0
    upload_interval_seconds: float = 15.0
    request_timeout_seconds: float = 60.0

    # Retries
    max_retries: int = 5
    retry_backoff_base: float = 2.0
    retry_backoff_max: float = 60.0

    # Output
    output_dir: str = "./runs"
    results_csv: str = "./results/summary.csv"
    sqlite_db: str = "./results/results.sqlite"

    # Batch
    concurrency: int = 1  # sequential by default

    # Auth — never stored in config file, loaded from env/CLI
    bearer_token: Optional[str] = None

    # Submission list schema (confirmed from observed responses)
    submission_id_field: str = "id"
    submission_status_field: str = "status"
    submission_filename_field: str = "filename"
    submission_time_field: str = "submittedAt"
    submission_items_path: str = "data.items"  # dot-notation to the items array
    submission_total_path: str = "data.total"  # dot-notation to total count

    # Status values
    status_finished: str = "FINISHED"
    status_failed_values: list[str] = field(default_factory=list)  # e.g. ["FAILED", "ERROR"]

    # Submission matching
    submission_match_time_tolerance_seconds: float = 120.0

    # Graph endpoint — returns JSON with data.url pointing to signed S3 artifact.
    # Placeholder path: update once exact graph request path is confirmed from
    # browser DevTools. Look for a request after clicking a completed submission
    # that returns {"success": true, "data": {"url": "https://...s3..."}}.
    graph_url_field: str = "data.url"  # dot-notation path to artifact URL

    def upload_url(self) -> str:
        return self.base_api + self.upload_endpoint

    def submissions_url(self, page: int = 1, page_size: int = 50) -> str:
        """Build submissions list URL."""
        endpoint = self.submissions_endpoint.format(round_id=self.round_id)
        base = f"{self.base_api}{endpoint}"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}page={page}&pageSize={page_size}"

    def graph_url(self, submission_id: str) -> str:
        endpoint = self.graph_endpoint.format(submission_id=submission_id)
        return self.base_api + endpoint

    @classmethod
    def load(
        cls,
        config_path: Optional[str | Path] = None,
        cli_overrides: Optional[dict[str, Any]] = None,
    ) -> Config:
        """Load config from YAML file, env vars, and CLI overrides (in priority order)."""
        data: dict[str, Any] = {}

        # 1. Load from YAML if it exists
        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        if path.exists():
            with open(path, "r") as f:
                file_data = yaml.safe_load(f) or {}
            data.update(file_data)

        # 2. Env var overrides (PROSPERITY_ prefix)
        env_map = {
            "PROSPERITY_BASE_API": "base_api",
            "PROSPERITY_TOKEN": "bearer_token",
            "PROSPERITY_OUTPUT_DIR": "output_dir",
            "PROSPERITY_POLL_INTERVAL": "poll_interval_seconds",
            "PROSPERITY_UPLOAD_INTERVAL": "upload_interval_seconds",
            "PROSPERITY_TIMEOUT": "request_timeout_seconds",
            "PROSPERITY_MAX_RETRIES": "max_retries",
            "PROSPERITY_CONCURRENCY": "concurrency",
        }
        for env_key, config_key in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                data[config_key] = val

        # 3. CLI overrides (highest priority)
        if cli_overrides:
            data.update({k: v for k, v in cli_overrides.items() if v is not None})

        # Coerce types
        for float_field in (
            "poll_interval_seconds",
            "poll_timeout_seconds",
            "upload_interval_seconds",
            "request_timeout_seconds",
            "retry_backoff_base",
            "retry_backoff_max",
            "submission_match_time_tolerance_seconds",
        ):
            if float_field in data and not isinstance(data[float_field], float):
                data[float_field] = float(data[float_field])

        for int_field in ("max_retries", "concurrency", "round_id"):
            if int_field in data and not isinstance(data[int_field], int):
                data[int_field] = int(data[int_field])

        # Build config, ignoring unknown keys
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def validate(self) -> None:
        """Check that required settings are present; auto-fetch token if missing."""
        if not self.bearer_token:
            # Auto-fetch via Cognito if ~/.prosperity_creds exists
            try:
                from auth_cognito import get_token
                import logging
                logging.getLogger("prosperity").info(
                    "No token provided — fetching fresh token via Cognito auto-auth"
                )
                self.bearer_token = get_token()
            except Exception as e:
                raise ConfigError(
                    f"Bearer token is required. Set via PROSPERITY_TOKEN env var, "
                    f"--token CLI flag, bearer_token in config.yaml, "
                    f"or create ~/.prosperity_creds for auto-auth "
                    f"(auto-auth failed: {e})"
                )
        if not self.base_api:
            raise ConfigError("base_api must be set")
