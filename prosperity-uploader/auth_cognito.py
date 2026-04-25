"""Cognito auto-auth helper for Prosperity Uploader.

Reads credentials from ~/.prosperity_creds (key=value format) and returns
a fresh JWT id_token via AWS Cognito SRP authentication.

Usage:
    # As script (prints token to stdout):
    python auth_cognito.py

    # As module:
    from auth_cognito import get_token
    token = get_token()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

USER_POOL_ID = "eu-west-1_wKiTmHXUE"
CLIENT_ID = "5kgp0jm69aeb91paqj1hnps838"
CREDS_PATH = Path.home() / ".prosperity_creds"


def _read_creds() -> tuple[str, str]:
    if not CREDS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {CREDS_PATH}\n"
            "Create it with:\n  email=your@email.com\n  password=yourpassword\n"
            "Then: chmod 600 ~/.prosperity_creds"
        )
    data = {}
    for line in CREDS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    try:
        return data["email"], data["password"]
    except KeyError as e:
        raise ValueError(f"Missing key {e} in {CREDS_PATH}") from e


def get_token() -> str:
    """Fetch a fresh JWT id_token via Cognito SRP auth."""
    from pycognito import Cognito  # lazy import so module load is cheap

    email, password = _read_creds()
    u = Cognito(
        user_pool_id=USER_POOL_ID,
        client_id=CLIENT_ID,
        username=email,
    )
    u.authenticate(password=password)
    return u.id_token


if __name__ == "__main__":
    try:
        print(get_token())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
