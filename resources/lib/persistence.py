"""Persistence layer for YouTube Lounge session and device identity.

Handles storage in Kodi addon settings (as a JSON blob) with seamless fallback
to a local JSON file when running outside Kodi.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

FALLBACK_STORE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".session_store.json")

SESSION_FIELDS = (
    "list_id",
    "playlist",
    "current_index",
    "current_video_id",
    "position",
    "cpn",
    "theme",
)


def empty_session_record() -> Dict[str, Any]:
    """Return a blank session record with all seven fields set to None."""
    return {
        "list_id": None,
        "playlist": None,
        "current_index": None,
        "current_video_id": None,
        "position": None,
        "cpn": None,
        "theme": None,
    }


class SessionStore:
    def __init__(self, fallback_path: Optional[str] = None, debounce_interval: float = 0.5):
        self.fallback_path = os.path.abspath(fallback_path or FALLBACK_STORE_PATH)
        self.debounce_interval = debounce_interval
        self._addon = None
        try:
            import xbmcaddon
            self._addon = xbmcaddon.Addon()
        except ImportError:
            self._addon = None

        self._lock = threading.Lock()
        self._debounce_timer: Optional[threading.Timer] = None
        self._pending_session: Optional[Dict[str, Any]] = None

    def load(self) -> Dict[str, Any]:
        """Load session bundle from Kodi settings or fallback file."""
        with self._lock:
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
                self._save_raw_locked(data)

            # Back-compat: ensure session fields exist or are nulls if session key absent/partial
            if "session" in data and isinstance(data["session"], dict):
                norm_session = empty_session_record()
                norm_session.update(data["session"])
                data["session"] = norm_session

            return data

    def _save_raw_locked(self, data: Dict[str, Any]) -> None:
        """Internal save without acquiring lock (caller holds self._lock)."""
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

    def save(self, data: Dict[str, Any]) -> None:
        """Save session bundle immediately."""
        with self._lock:
            self._save_raw_locked(data)

    def clear(self) -> None:
        """Clear tokens while preserving device_id."""
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            self._pending_session = None

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

            dev_id = data.get("device_id", str(uuid.uuid4()))
            new_data = {"device_id": dev_id}
            self._save_raw_locked(new_data)

    def load_session(self) -> Dict[str, Any]:
        """Load session record synchronously at startup or before reporting.

        Returns a dict containing:
          - list_id (str|None)
          - playlist (list|None)
          - current_index (int|None)
          - current_video_id (str|None)
          - position (float|None)
          - cpn (str|None)
          - theme (str|None)

        Back-compat: loading an old blob with no session fields yields nulls, not an error.
        """
        data = self.load()
        with self._lock:
            if self._pending_session is not None:
                record = dict(self._pending_session)
            elif isinstance(data.get("session"), dict):
                record = dict(data["session"])
            else:
                record = {}

        result = empty_session_record()
        for k in SESSION_FIELDS:
            if k in record:
                result[k] = record[k]
        return result

    def save_session(self, record: Optional[Dict[str, Any]], debounce: bool = True) -> None:
        """Save session record.

        Debounced by default (~500ms-1s) so frequent position ticks coalesce
        instead of hammering disk. If debounce=False, writes through immediately.
        Passing record=None clears the persisted session record.
        """
        with self._lock:
            if record is not None:
                norm = empty_session_record()
                for k in SESSION_FIELDS:
                    if k in record:
                        norm[k] = record[k]
                self._pending_session = norm
            else:
                self._pending_session = None

            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None

            if not debounce:
                self._flush_session_locked()
            else:
                timer = threading.Timer(self.debounce_interval, self._flush_session)
                timer.daemon = True
                self._debounce_timer = timer
                timer.start()

    def flush_session(self) -> None:
        """Force any debounced session writes to disk immediately."""
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            self._flush_session_locked()

    def _flush_session(self) -> None:
        with self._lock:
            self._flush_session_locked()

    def _flush_session_locked(self) -> None:
        # Load raw without recursion
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

        if self._pending_session is not None:
            data["session"] = dict(self._pending_session)
        else:
            data.pop("session", None)

        self._save_raw_locked(data)
