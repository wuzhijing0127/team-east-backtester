"""Authentication / token management."""

from __future__ import annotations

import logging
from typing import Optional

from exceptions import AuthenticationError
from utils import mask_token

logger = logging.getLogger(__name__)


class TokenProvider:
    """Manages the bearer token.

    Currently supports manual token input. Designed to be extended later
    with browser automation or refresh token flows.
    """

    def __init__(self, token: Optional[str] = None):
        self._token = token

    @property
    def token(self) -> str:
        if not self._token:
            raise AuthenticationError("No bearer token configured")
        return self._token

    def set_token(self, token: str) -> None:
        """Update the token (e.g. after manual refresh)."""
        self._token = token
        logger.info("Token updated: %s", mask_token(token))

    def auth_headers(self) -> dict[str, str]:
        """Return the Authorization header dict."""
        return {"Authorization": f"Bearer {self.token}"}

    def is_configured(self) -> bool:
        return self._token is not None and len(self._token) > 0
