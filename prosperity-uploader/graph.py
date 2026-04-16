"""Graph endpoint and artifact download."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from client import APIClient
from config import Config
from exceptions import ArtifactDownloadError, GraphError
from models import GraphResult
from utils import load_json, save_json

logger = logging.getLogger(__name__)


def get_graph_url(client: APIClient, config: Config, submission_id: str) -> GraphResult:
    """Call the graph endpoint and extract the signed artifact URL.

    Args:
        client: Authenticated API client.
        config: Application config.
        submission_id: The submission to get the graph for.

    Returns:
        GraphResult containing the signed S3 URL.

    Raises:
        GraphError: If the endpoint fails or the URL cannot be extracted.
    """
    url = config.graph_url(submission_id)
    logger.info("Fetching graph for submission %s: %s", submission_id, url)

    data = client.get_json(url)

    artifact_url = _extract_url(data, config.graph_url_field)
    if not artifact_url:
        logger.error(
            "Could not extract artifact URL from graph response. "
            "Expected field path: %s. Response keys: %s",
            config.graph_url_field,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
        raise GraphError(
            f"No artifact URL found in graph response for {submission_id}. "
            f"Response: {str(data)[:300]}"
        )

    logger.info("Got artifact URL: %s", artifact_url[:80] + "...")
    return GraphResult(
        submission_id=submission_id,
        artifact_url=artifact_url,
        raw_response=data,
    )


def download_artifact(
    client: APIClient,
    artifact_url: str,
    out_path: str,
) -> dict[str, Any]:
    """Download the signed S3 artifact JSON and return parsed data.

    Downloads immediately since signed URLs are temporary.

    Args:
        client: API client (uses session for connection reuse, no auth headers).
        artifact_url: The signed S3 URL from the graph endpoint.
        out_path: Local path to save the raw JSON.

    Returns:
        Parsed JSON data as a dict or list.
    """
    logger.info("Downloading artifact to %s", out_path)

    try:
        client.download(artifact_url, out_path)
    except Exception as e:
        raise ArtifactDownloadError(
            f"Failed to download artifact from {artifact_url[:80]}: {e}"
        ) from e

    # Parse and return
    data = load_json(out_path)
    logger.info(
        "Artifact downloaded and parsed: %s (%s, %s entries)",
        out_path,
        type(data).__name__,
        len(data) if isinstance(data, (list, dict)) else "?",
    )
    return data


def _extract_url(data: dict[str, Any], field_path: str) -> Optional[str]:
    """Extract a URL from a nested dict using dot-notation path (e.g. 'data.url')."""
    obj: Any = data
    for key in field_path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return str(obj) if obj else None
