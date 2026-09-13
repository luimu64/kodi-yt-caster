"""HTTP client for YouTube Lounge API using Python standard library."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("ytlounge.client")

BASE_URL = "https://www.youtube.com/api/lounge"
DEFAULT_HEADERS = {
    "Origin": "https://www.youtube.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class LoungeError(Exception):
    """Base exception for Lounge API failures."""
    pass


class LoungeTokenExpiredError(LoungeError):
    """Raised when loungeToken or screenId is rejected by YouTube."""
    pass


def request(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    base_url: str = BASE_URL,
) -> str:
    """Execute an HTTP request to YouTube Lounge API."""
    url = f"{base_url}/{endpoint.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    encoded_data = None
    if data is not None:
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")

    req = urllib.request.Request(url, data=encoded_data, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", errors="replace")
        if status in (400, 404) and ("lounge_token" in body or "token" in body):
            raise LoungeTokenExpiredError(f"Token rejected (HTTP {status}): {body}") from e
        raise LoungeError(f"HTTP {status} on {endpoint}: {body}") from e
    except Exception as e:
        raise LoungeError(f"Network error on {endpoint}: {e}") from e
