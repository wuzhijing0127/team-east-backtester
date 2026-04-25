"""Submission discovery and status polling.

Confirmed response schema (from observed browser traffic):

    {
      "success": true,
      "status": 200,
      "data": {
        "items": [
          {
            "id": 179369,
            "teamId": ...,
            "roundId": ...,
            "submittedAt": "2026-04-15T...",
            "submittedBy": {"firstName": ..., "lastName": ...},
            "status": "FINISHED",
            "filename": "v4o_L1_14.py",
            "active": true,
            "simulationApplicationAlgoSubmissionIdentifier": ...
          },
          ...
        ],
        "page": 1,
        "pageSize": 50,
        "total": 42
      }
    }
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from client import APIClient
from config import Config
from exceptions import SubmissionNotFoundError, SubmissionTimeoutError
from models import SubmissionRecord, SubmissionStatus
from utils import save_json

logger = logging.getLogger(__name__)


# ── Status mapping ────────────────────────────────────────────────
# Maps platform status strings (case-insensitive) to our enum.

_STATUS_MAP: dict[str, SubmissionStatus] = {
    "finished": SubmissionStatus.FINISHED,
    "pending": SubmissionStatus.PENDING,
    "queued": SubmissionStatus.PENDING,
    "processing": SubmissionStatus.RUNNING,
    "running": SubmissionStatus.RUNNING,
    "simulating": SubmissionStatus.RUNNING,
    "completed": SubmissionStatus.COMPLETED,
    "done": SubmissionStatus.COMPLETED,
    "failed": SubmissionStatus.FAILED,
    "error": SubmissionStatus.ERROR,
}


def _parse_status(raw: str | None, config: Config) -> SubmissionStatus:
    """Map a raw status string to SubmissionStatus.

    Uses the config's status_finished / status_failed_values for custom mappings,
    then falls back to the built-in map.
    """
    if raw is None:
        return SubmissionStatus.UNKNOWN

    raw_stripped = str(raw).strip()
    raw_lower = raw_stripped.lower()

    # Check explicit config values first
    if raw_stripped == config.status_finished or raw_lower == config.status_finished.lower():
        return SubmissionStatus.FINISHED

    for fv in config.status_failed_values:
        if raw_stripped == fv or raw_lower == fv.lower():
            return SubmissionStatus.FAILED

    return _STATUS_MAP.get(raw_lower, SubmissionStatus.UNKNOWN)


# ── ISO timestamp parsing ─────────────────────────────────────────

def _parse_iso_datetime(val: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string or epoch number into a tz-aware datetime."""
    if val is None:
        return None

    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except (OSError, ValueError):
            return None

    if not isinstance(val, str):
        return None

    # Try fromisoformat first (Python 3.11+ handles trailing Z)
    cleaned = val.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Fallback strptime patterns
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(val, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    logger.debug("Could not parse datetime: %r", val)
    return None


# ── Dot-notation extraction ───────────────────────────────────────

def _extract_nested(data: dict[str, Any], field_path: str) -> Any:
    """Extract a value from a nested dict using dot-notation (e.g. 'data.items')."""
    obj: Any = data
    for key in field_path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


# ── List / parse submissions ─────────────────────────────────────

def list_submissions(
    client: APIClient,
    config: Config,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SubmissionRecord], dict[str, Any]]:
    """Fetch the submissions list from the API.

    Returns:
        Tuple of (parsed SubmissionRecords, raw response dict).
    """
    url = config.submissions_url(page=page, page_size=page_size)
    logger.info("Fetching submissions list: %s", url)

    raw_data = client.get_json(url)

    # Extract items array using configurable path
    items_raw = _extract_nested(raw_data, config.submission_items_path)
    if not isinstance(items_raw, list):
        logger.warning(
            "Could not find items at '%s'. Top-level keys: %s",
            config.submission_items_path,
            list(raw_data.keys()) if isinstance(raw_data, dict) else type(raw_data).__name__,
        )
        return [], raw_data

    total = _extract_nested(raw_data, config.submission_total_path)
    results: list[SubmissionRecord] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        results.append(_parse_submission_record(item, config))

    logger.info(
        "Found %d submissions on page %d (total: %s)",
        len(results),
        page,
        total if total is not None else "?",
    )
    return results, raw_data


def _parse_submission_record(item: dict[str, Any], config: Config) -> SubmissionRecord:
    """Parse a single submission item dict into a SubmissionRecord."""
    sub_id = str(item.get(config.submission_id_field, item.get("id", "unknown")))
    status_raw = item.get(config.submission_status_field, item.get("status"))
    filename = item.get(config.submission_filename_field, item.get("filename"))

    # Parse submittedAt using the configured field name
    submitted_at = _parse_iso_datetime(
        item.get(config.submission_time_field, item.get("submittedAt"))
    )

    return SubmissionRecord(
        submission_id=sub_id,
        status=_parse_status(status_raw, config),
        filename=filename,
        timestamp=submitted_at,  # keep for backward compat
        submitted_at=submitted_at,
        raw=item,
    )


# ── Find submission for a recent upload ───────────────────────────

def find_submission_for_upload(
    client: APIClient,
    config: Config,
    filename: str,
    upload_time: datetime,
    submission_id_hint: Optional[str] = None,
    run_dir: Optional[Path] = None,
) -> SubmissionRecord:
    """Locate the submission created by a recent upload.

    Matching strategy:
    1. If submission_id_hint is available, find by ID.
    2. Otherwise, filter by exact filename + submittedAt >= (upload_time - tolerance).
    3. Pick the newest matching item.

    Args:
        run_dir: If provided, saves the raw list response for debugging.
    """
    subs, raw_response = list_submissions(client, config, page=1, page_size=50)

    # Save raw response for debugging
    if run_dir:
        save_json(raw_response, run_dir / "submissions_list_response.json")

    # Strategy 1: match by ID hint (with indexing-delay retries)
    if submission_id_hint:
        for retry in range(4):
            for s in subs:
                if s.submission_id == submission_id_hint:
                    logger.info(
                        "Found submission by ID hint: %s (status: %s)",
                        s.submission_id, s.status.value,
                    )
                    return s
            if retry < 3:
                logger.info(
                    "Submission %s not in list yet (retry %d/4) — server indexing lag, waiting 5s",
                    submission_id_hint, retry + 1,
                )
                time.sleep(5)
                subs, raw_response = list_submissions(client, config, page=1, page_size=50)
                if run_dir:
                    save_json(raw_response, run_dir / "submissions_list_response.json")
        # Trust the upload-response ID; let poll_submission_until_ready retry lookup
        logger.warning(
            "Submission %s still not in recent list after retries — trusting upload-response ID; "
            "poll step will retry until it appears.",
            submission_id_hint,
        )
        return SubmissionRecord(
            submission_id=submission_id_hint,
            status=SubmissionStatus.PENDING,
        )

    # Strategy 2: filter by filename + time tolerance
    tolerance = timedelta(seconds=config.submission_match_time_tolerance_seconds)
    cutoff = upload_time - tolerance

    # Ensure cutoff is tz-aware
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    candidates: list[SubmissionRecord] = []
    for s in subs:
        # Exact filename match
        if not s.filename or s.filename != filename:
            continue

        # Time filter: submittedAt must be after cutoff
        if s.submitted_at is not None and s.submitted_at >= cutoff:
            candidates.append(s)
        elif s.submitted_at is None:
            # No timestamp — include as weak candidate
            candidates.append(s)

    if not candidates:
        # Fallback: use most recent submission with any filename match
        loose = [s for s in subs if s.filename and filename.lower() in s.filename.lower()]
        if loose:
            loose.sort(key=lambda s: s.submitted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            best = loose[0]
            logger.warning(
                "No exact match with time tolerance. Using loose match: %s (submitted %s)",
                best.submission_id,
                best.submitted_at,
            )
            return best

        if subs:
            logger.warning(
                "No filename match for '%s'. Using most recent submission: %s",
                filename,
                subs[0].submission_id,
            )
            return subs[0]

        raise SubmissionNotFoundError(
            f"No submissions found after uploading {filename}"
        )

    # Pick the newest
    candidates.sort(
        key=lambda s: s.submitted_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    best = candidates[0]
    logger.info(
        "Matched submission %s for upload '%s' (submitted %s, %d candidates)",
        best.submission_id,
        filename,
        best.submitted_at,
        len(candidates),
    )
    return best


# ── Poll until ready ──────────────────────────────────────────────

def poll_submission_until_ready(
    client: APIClient,
    config: Config,
    submission_id: str,
    run_dir: Optional[Path] = None,
) -> SubmissionRecord:
    """Poll the submissions list until the given submission reaches a terminal status.

    Returns the final SubmissionRecord.
    Raises SubmissionTimeoutError if poll_timeout_seconds is exceeded.
    """
    start = time.monotonic()
    attempt = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed > config.poll_timeout_seconds:
            raise SubmissionTimeoutError(
                f"Submission {submission_id} did not complete within "
                f"{config.poll_timeout_seconds}s"
            )

        attempt += 1
        logger.info(
            "Polling submission %s (attempt %d, %.0fs elapsed)",
            submission_id,
            attempt,
            elapsed,
        )

        subs, raw_response = list_submissions(client, config, page=1, page_size=50)

        # Save each polling response in debug mode
        if run_dir and logger.isEnabledFor(logging.DEBUG):
            save_json(raw_response, run_dir / f"poll_response_{attempt}.json")

        match = next((s for s in subs if s.submission_id == submission_id), None)

        if match and match.is_terminal:
            logger.info(
                "Submission %s reached terminal status: %s",
                submission_id,
                match.status.value,
            )
            return match

        if match:
            logger.info("Submission %s status: %s", submission_id, match.status.value)
        else:
            logger.warning("Submission %s not found in list yet", submission_id)

        time.sleep(config.poll_interval_seconds)


# ── Schema inspection ─────────────────────────────────────────────

def inspect_submissions_response(data: dict[str, Any]) -> str:
    """Inspect a raw submissions list response and suggest field mappings.

    Used by the `inspect-submissions` CLI command.
    """
    lines: list[str] = []
    lines.append("=== Submissions Response Schema Inspection ===")
    lines.append("")

    # Top-level keys
    if isinstance(data, dict):
        lines.append(f"Top-level keys: {list(data.keys())}")
    else:
        lines.append(f"Top-level type: {type(data).__name__}")
        return "\n".join(lines)

    # Try to find the items array
    items = None
    items_path = None
    for path in ("data.items", "data", "items", "submissions", "results"):
        candidate = _extract_nested(data, path)
        if isinstance(candidate, list) and candidate:
            items = candidate
            items_path = path
            break

    if items:
        lines.append(f"Items array found at: '{items_path}' ({len(items)} items)")

        # Total count
        for total_path in ("data.total", "total", "data.count"):
            total = _extract_nested(data, total_path)
            if total is not None:
                lines.append(f"Total count at: '{total_path}' = {total}")
                break

        # Pagination
        for page_path in ("data.page", "page"):
            page = _extract_nested(data, page_path)
            if page is not None:
                lines.append(f"Page at: '{page_path}' = {page}")
                break

        # Inspect first item
        first = items[0]
        if isinstance(first, dict):
            lines.append("")
            lines.append(f"First item keys: {list(first.keys())}")
            lines.append("")

            # Suggest field mappings
            lines.append("Suggested config mappings:")
            for label, candidates in (
                ("submission_id_field", ("id", "submissionId", "ID")),
                ("submission_status_field", ("status", "state")),
                ("submission_filename_field", ("filename", "fileName", "file_name")),
                ("submission_time_field", ("submittedAt", "createdAt", "timestamp", "created_at")),
            ):
                found = None
                for c in candidates:
                    if c in first:
                        found = c
                        break
                if found:
                    val = first[found]
                    val_str = str(val)[:60]
                    lines.append(f"  {label}: \"{found}\"  (sample: {val_str})")
                else:
                    lines.append(f"  {label}: NOT FOUND (tried: {candidates})")

            # Show all field values from first item
            lines.append("")
            lines.append("All fields in first item:")
            for key, val in first.items():
                val_str = str(val)[:80]
                lines.append(f"  {key}: {type(val).__name__} = {val_str}")
    else:
        lines.append("Could not locate items array.")
        lines.append("Tried paths: data.items, data, items, submissions, results")

    return "\n".join(lines)
