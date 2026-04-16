"""Auto-refresh Cognito tokens for unattended search runs."""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

COGNITO_REGION = "eu-west-1"
COGNITO_CLIENT_ID = "5kgp0jm69aeb91paqj1hnps838"
TOKEN_REFRESH_SECONDS = 55 * 60  # refresh 5 min before expiry


class AutoAuth:
    """Manages Cognito authentication with auto-refresh."""

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._token: Optional[str] = None
        self._obtained_at: float = 0

    def get_token(self) -> str:
        """Get a valid token, refreshing if needed."""
        if self._token is None or self._is_stale():
            self._refresh()
        return self._token  # type: ignore

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._obtained_at) > TOKEN_REFRESH_SECONDS

    def _refresh(self) -> None:
        import boto3

        logger.info("Refreshing Cognito token...")
        client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
        resp = client.initiate_auth(
            ClientId=COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": self._email,
                "PASSWORD": self._password,
            },
        )
        self._token = resp["AuthenticationResult"]["IdToken"]
        self._obtained_at = time.monotonic()
        logger.info("Token refreshed (valid for 60 min)")
