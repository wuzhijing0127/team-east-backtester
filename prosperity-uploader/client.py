"""HTTP client with retries, backoff, and session reuse."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auth import TokenProvider
from config import Config
from exceptions import (
    AuthenticationError,
    RateLimitError,
    ProsperityUploaderError,
)
from utils import safe_json_parse

logger = logging.getLogger(__name__)


class APIClient:
    """Wraps requests.Session with auth, retries, and structured error handling."""

    def __init__(self, config: Config, token_provider: TokenProvider):
        self.config = config
        self.token_provider = token_provider
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()

        # Transport-level retries for connection errors / 502/503/504
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = self.token_provider.auth_headers()
        if extra:
            headers.update(extra)
        return headers

    def request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an HTTP request with application-level retry and backoff.

        Handles 401/403 (auth errors), 429 (rate limit), and 5xx (server errors).
        Raises typed exceptions for the caller.
        """
        retries = max_retries if max_retries is not None else self.config.max_retries
        req_timeout = timeout if timeout is not None else self.config.request_timeout_seconds

        # Merge auth headers into kwargs
        headers = self._headers(kwargs.pop("headers", None))
        kwargs["headers"] = headers
        kwargs["timeout"] = req_timeout

        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            start = time.monotonic()
            try:
                resp = self.session.request(method, url, **kwargs)
                elapsed = time.monotonic() - start

                logger.debug(
                    "%s %s -> %d (%.2fs, attempt %d/%d)",
                    method.upper(),
                    url,
                    resp.status_code,
                    elapsed,
                    attempt,
                    retries,
                )

                # Auth failures — don't retry, surface immediately
                if resp.status_code in (401, 403):
                    body = safe_json_parse(resp.text) or resp.text
                    raise AuthenticationError(
                        f"HTTP {resp.status_code}: {body}. "
                        "Token may be expired — refresh and retry."
                    )

                # Rate limit — backoff using Retry-After or exponential
                if resp.status_code == 429:
                    retry_after = _parse_retry_after(resp)
                    wait = retry_after or min(
                        self.config.retry_backoff_base ** attempt,
                        self.config.retry_backoff_max,
                    )
                    logger.warning(
                        "Rate limited (429). Waiting %.1fs before retry %d/%d",
                        wait,
                        attempt,
                        retries,
                    )
                    time.sleep(wait)
                    last_error = RateLimitError(retry_after=retry_after)
                    continue

                # 5xx server errors — retry with backoff
                if resp.status_code >= 500:
                    wait = min(
                        self.config.retry_backoff_base ** attempt,
                        self.config.retry_backoff_max,
                    )
                    logger.warning(
                        "Server error %d. Waiting %.1fs before retry %d/%d",
                        resp.status_code,
                        wait,
                        attempt,
                        retries,
                    )
                    time.sleep(wait)
                    last_error = ProsperityUploaderError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                    continue

                return resp

            except requests.exceptions.Timeout as e:
                wait = min(
                    self.config.retry_backoff_base ** attempt,
                    self.config.retry_backoff_max,
                )
                logger.warning(
                    "Request timeout. Waiting %.1fs before retry %d/%d",
                    wait,
                    attempt,
                    retries,
                )
                time.sleep(wait)
                last_error = e

            except requests.exceptions.ConnectionError as e:
                wait = min(
                    self.config.retry_backoff_base ** attempt,
                    self.config.retry_backoff_max,
                )
                logger.warning(
                    "Connection error. Waiting %.1fs before retry %d/%d",
                    wait,
                    attempt,
                    retries,
                )
                time.sleep(wait)
                last_error = e

        raise ProsperityUploaderError(
            f"Request failed after {retries} attempts: {last_error}"
        )

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request_with_retry("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request_with_retry("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """GET request that parses and returns JSON."""
        resp = self.get(url, **kwargs)
        return _parse_json_response(resp)

    def post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """POST request that parses and returns JSON."""
        resp = self.post(url, **kwargs)
        return _parse_json_response(resp)

    def download(self, url: str, out_path: str, **kwargs: Any) -> None:
        """Download a URL to a local file (no auth headers — for signed S3 URLs)."""
        timeout = kwargs.pop("timeout", self.config.request_timeout_seconds)
        resp = self.session.get(url, timeout=timeout, stream=True, **kwargs)
        resp.raise_for_status()
        from pathlib import Path

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info("Downloaded %s -> %s (%d bytes)", url[:80], out_path, Path(out_path).stat().st_size)


def _parse_json_response(resp: requests.Response) -> dict[str, Any]:
    """Parse a response as JSON with helpful error messages."""
    try:
        data = resp.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise ProsperityUploaderError(
            f"Expected JSON response but got {resp.status_code}: "
            f"{resp.text[:300]}"
        )

    if not isinstance(data, dict):
        return {"_raw": data}
    return data


def _parse_retry_after(resp: requests.Response) -> Optional[float]:
    """Extract Retry-After header value in seconds."""
    header = resp.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
