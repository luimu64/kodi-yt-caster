"""Persistence layer for YouTube Lounge session and device identity.

Handles storage in Kodi addon settings (as a JSON blob) with seamless fallback
to a local JSON file when running outside Kodi.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional

FALLBACK_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".session_store.json")


class SessionStore:
    def __init__(self, fallback_path: Optional[str] = None):
        self.fallback_path = os.path.abspath(fallback_path or FALLBACK_STORE_PATH)
        self._addon = None
        try:
            import xbmcaddon
            self._addon = xbmcaddon.Addon()
        except ImportError:
            self._addon = None

    def load(self) -> Dict[str, Any]:
        """Load session bundle from Kodi settings or fallback file."""
        raw = ""
        if self._addon:
            try:
                raw = self._addon.getSetting("session_data")
            except Exception:
                raw = ""
        elif os.path.exists(self.fallback_path):
            try:
                with open(self.fallback_path, "r", encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                raw = ""

        data: Dict[str, Any] = {}
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = {}

        if not data.get("device_id"):
            data["device_id"] = str(uuid.uuid4())
            self.save(data)

        return data

    def save(self, data: Dict[str, Any]) -> None:
        """Save session bundle."""
        raw = json.dumps(data)
        if self._addon:
            try:
                self._addon.setSetting("session_data", raw)
            except Exception:
                pass
        else:
            try:
                with open(self.fallback_path, "w", encoding="utf-8") as f:
                    f.write(raw)
            except Exception:
                pass

    def clear(self) -> None:
        """Clear tokens while preserving device_id."""
        data = self.load()
        dev_id = data.get("device_id", str(uuid.uuid4()))
        new_data = {"device_id": dev_id}
        self.save(new_data)
