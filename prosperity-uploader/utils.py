"""Utility functions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def file_sha256(path: str | Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_json_parse(text: str) -> dict[str, Any] | list | None:
    """Parse JSON, returning None on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def timestamp_slug() -> str:
    """Return a filesystem-safe timestamp string."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def strategy_name_from_path(path: str | Path) -> str:
    """Extract strategy name from file path (stem without extension)."""
    return Path(path).stem


def save_json(data: Any, path: str | Path) -> None:
    """Save data as formatted JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def load_json(path: str | Path) -> Any:
    """Load JSON from file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mask_token(token: str) -> str:
    """Mask a bearer token for safe logging."""
    if len(token) <= 12:
        return "***"
    return token[:6] + "..." + token[-4:]
