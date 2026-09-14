"""In-process yt-dlp resolver: imports the yt-dlp zipapp as a module.

The subprocess bridge spawns a ~30-40MB Python process per resolve; on
Pi-class hardware that is several seconds of pure interpreter startup +
import cost paid on EVERY track change. The same yt-dlp release file is a
zipapp importable via sys.path, so a persistent YoutubeDL instance reuses
its HTTP session (connection pooling, warm TLS) across resolves:

  measured (4K test video): 2.0s cold, ~1.0s warm in-process
                            vs 4-6s per subprocess spawn

Thread safety: YoutubeDL is NOT thread-safe. All extract_info calls are
serialized behind a lock; concurrent callers (play + prefetch) queue up
briefly instead of corrupting shared extractor state.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("ytlounge.inproc")

_IMPORT_LOCK = threading.Lock()
_INSTANCE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"tried": False, "ok": False, "ydl": None, "path": None}


def _ensure_zipapp_on_path(binary_path: Optional[str]) -> Optional[str]:
    """Make the yt-dlp zipapp importable; returns the path used or None."""
    candidates = []
    if binary_path and os.path.isfile(binary_path):
        candidates.append(binary_path)
    for p in list(sys.path):
        if p and os.path.basename(p) in ("yt-dlp", "yt-dlp.exe") and os.path.isfile(p):
            candidates.append(p)
    for path in candidates:
        try:
            import zipfile
            if not zipfile.is_zipfile(path):
                continue
        except Exception:
            continue
        if path not in sys.path:
            sys.path.insert(0, path)
        return path
    return None


def try_init(binary_path: Optional[str] = None) -> bool:
    """Attempt to import yt-dlp in-process. Idempotent; safe from any thread."""
    with _IMPORT_LOCK:
        if _STATE["tried"]:
            return _STATE["ok"]
        _STATE["tried"] = True
        try:
            path = _ensure_zipapp_on_path(binary_path)
            if path is None:
                logger.info("inproc yt-dlp: no importable zipapp found")
                return False
            import yt_dlp  # type: ignore
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "socket_timeout": 30,
                # No color/progress output anywhere.
                "color": "never",
            }
            ydl = yt_dlp.YoutubeDL(opts)
            _STATE["ydl"] = ydl
            _STATE["path"] = path
            _STATE["ok"] = True
            logger.info("inproc yt-dlp ready (version %s, from %s)",
                        getattr(getattr(yt_dlp, "version", None), "__version__", "?"), path)
            return True
        except Exception as e:
            logger.info("inproc yt-dlp unavailable (%s); subprocess fallback", e)
            return False


def available() -> bool:
    return bool(_STATE["ok"]) or try_init(_STATE.get("path"))


def resolve(video_id: str, cookies_path: Optional[str] = None) -> Dict[str, Any]:
    """extract_info via the persistent instance. Raises on failure."""
    if not available():
        raise RuntimeError("in-process yt-dlp not initialized")
    ydl = _STATE["ydl"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    # Cookies: YoutubeDL accepts a cookiefile at construction; per-call swap
    # would rebuild the jar, so only re-instantiate when the path changed.
    with _INSTANCE_LOCK:
        if cookies_path and getattr(ydl, "_ytcfg_cookiefile", None) != cookies_path:
            import yt_dlp  # type: ignore
            opts = {
                "quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "socket_timeout": 30, "color": "never",
                "cookiefile": cookies_path,
            }
            ydl = yt_dlp.YoutubeDL(opts)
            ydl._ytcfg_cookiefile = cookies_path
            _STATE["ydl"] = ydl
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError(f"yt-dlp returned no info for {video_id}")
    return info
