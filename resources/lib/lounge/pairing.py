"""Pairing operations for YouTube Lounge."""

from __future__ import annotations

import json
from typing import Tuple
from .client import request, LoungeError


def generate_screen_id() -> str:
    """Generate a new screenId from YouTube Lounge."""
    return request("pairing/generate_screen_id").strip()


def get_lounge_token_batch(screen_id: str) -> Tuple[str, int]:
    """Fetch loungeToken and expiration timestamp (epoch ms) for screen_id."""
    raw = request("pairing/get_lounge_token_batch", data={"screen_ids": screen_id})
    try:
        payload = json.loads(raw)
        screen = payload["screens"][0]
        return screen["loungeToken"], screen.get("expiration", 0)
    except Exception as e:
        raise LoungeError(f"Failed to parse lounge token batch: {e}") from e


def get_pairing_code(screen_id: str, lounge_token: str, screen_name: str = "Kodi") -> str:
    """Fetch 12-digit pairing code for entering into the YouTube app."""
    raw = request(
        "pairing/get_pairing_code",
        params={"ctx": "pair"},
        data={
            "access_type": "permanent",
            "app": "kodi-ytcast",
            "lounge_token": lounge_token,
            "screen_id": screen_id,
            "screen_name": screen_name,
        },
    ).strip()
    if len(raw) == 12:
        return f"{raw[0:3]}-{raw[3:6]}-{raw[6:9]}-{raw[9:12]}"
    return raw


def register_pairing_code(screen_id: str, pairing_code: str, screen_name: str = "Kodi", device_id: str = "") -> None:
    """Register a remote device pairing code."""
    request(
        "pairing/register_pairing_code",
        data={
            "access_type": "permanent",
            "app": "kodi-ytcast",
            "pairing_code": pairing_code,
            "screen_id": screen_id,
            "screen_name": screen_name,
            "device_id": device_id or screen_name,
        },
    )
