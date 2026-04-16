"""Algorithm upload module."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from client import APIClient
from config import Config
from exceptions import UploadError
from models import UploadResult
from utils import file_sha256, strategy_name_from_path

logger = logging.getLogger(__name__)


def upload_algo(client: APIClient, config: Config, file_path: str) -> UploadResult:
    """Upload a Python strategy file to the IMC Prosperity platform.

    Args:
        client: Authenticated API client.
        config: Application config.
        file_path: Path to the .py strategy file.

    Returns:
        UploadResult with status code, response body, and extracted submission ID.

    Raises:
        UploadError: If the upload fails with a non-auth error.
        AuthenticationError: If the token is invalid.
    """
    path = Path(file_path)
    if not path.exists():
        raise UploadError(f"File not found: {file_path}")
    if not path.suffix == ".py":
        logger.warning("File %s is not a .py file — uploading anyway", file_path)

    file_hash = file_sha256(path)
    filename = path.name
    upload_url = config.upload_url()
    upload_time = datetime.now(timezone.utc)

    logger.info("Uploading %s (%s) to %s", filename, file_hash[:12], upload_url)

    with open(path, "rb") as f:
        files = {"file": (filename, f, "text/x-python-script")}
        resp = client.post(upload_url, files=files)

    logger.info(
        "Upload response: HTTP %d (%.0f bytes)",
        resp.status_code,
        len(resp.content),
    )

    # Parse response
    try:
        body = resp.json()
    except (ValueError, Exception):
        body = {"_raw_text": resp.text[:1000]}

    if resp.status_code not in (200, 201):
        raise UploadError(
            f"Upload failed with HTTP {resp.status_code}: {body}"
        )

    # Try to extract submission ID from response
    submission_id = _extract_submission_id(body)
    if submission_id:
        logger.info("Extracted submission ID from upload response: %s", submission_id)
    else:
        logger.info(
            "No submission ID found in upload response. "
            "Will need to discover via submissions list. "
            "Response keys: %s",
            list(body.keys()) if isinstance(body, dict) else type(body).__name__,
        )

    return UploadResult(
        file_path=str(path.resolve()),
        file_hash=file_hash,
        filename=filename,
        status_code=resp.status_code,
        response_body=body,
        timestamp=upload_time,
        submission_id=submission_id,
    )


def _extract_submission_id(body: dict[str, Any]) -> Optional[str]:
    """Attempt to extract a submission ID from the upload response.

    The exact response schema is not fully known. We try common patterns.
    """
    if not isinstance(body, dict):
        return None

    # Direct ID field
    for key in ("id", "submissionId", "submission_id", "ID"):
        if key in body:
            return str(body[key])

    # Nested under data
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("id", "submissionId", "submission_id", "ID"):
            if key in data:
                return str(data[key])

    # Nested under submission
    sub = body.get("submission")
    if isinstance(sub, dict):
        for key in ("id", "submissionId", "submission_id", "ID"):
            if key in sub:
                return str(sub[key])

    return None
