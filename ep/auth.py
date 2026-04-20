"""
ep/auth.py — EPO OPS OAuth2 token manager
=========================================

EPO Open Patent Services (OPS) uses OAuth2 client credentials. Tokens expire
every 20 minutes (1200 s). This module manages token lifecycle: fetch on first
use, auto-refresh before expiry.

Usage:
    from ep.auth import get_ops_token
    token = get_ops_token()   # returns a valid access_token string
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

Credentials are loaded from .env via python-dotenv:
    EPO_CLIENT_ID      = Consumer Key
    EPO_CLIENT_SECRET  = Consumer Secret
"""

from __future__ import annotations

import base64
import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

OPS_TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"

# Refresh tokens 60 s before they actually expire, to avoid race conditions
# where a long request is sent with a token that expires mid-flight.
_TOKEN_SAFETY_MARGIN = 60


class _TokenCache:
    """Thread-safe cache for the OPS access token with transparent refresh."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> tuple[str, float]:
        client_id     = os.environ.get("EPO_CLIENT_ID")
        client_secret = os.environ.get("EPO_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "EPO_CLIENT_ID and EPO_CLIENT_SECRET must be set in environment / .env. "
                "Register at https://developers.epo.org and drop the Consumer Key/Secret "
                "into the project .env file."
            )

        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        for attempt in range(3):
            try:
                r = requests.post(
                    OPS_TOKEN_URL,
                    headers={
                        "Authorization": f"Basic {creds}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data="grant_type=client_credentials",
                    timeout=15,
                )
                if r.status_code == 200:
                    body = r.json()
                    token = body["access_token"]
                    expires_in = int(body.get("expires_in", 1200))
                    return token, time.time() + expires_in - _TOKEN_SAFETY_MARGIN
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(
                    f"EPO OPS token request failed [{r.status_code}]: {r.text[:200]}"
                )
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("EPO OPS token request failed after 3 attempts")

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            self._token, self._expires_at = self._fetch()
            return self._token


_cache = _TokenCache()


def get_ops_token() -> str:
    """Return a valid OPS access token, refreshing transparently if needed."""
    return _cache.get()


def ops_auth_headers(accept: str = "application/json") -> dict[str, str]:
    """Shortcut: return a headers dict with Authorization + Accept set."""
    return {"Authorization": f"Bearer {get_ops_token()}", "Accept": accept}
