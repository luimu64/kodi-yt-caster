"""Resolver module with in-memory caching for video streams."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple
from .ytdlp_bridge import YtDlpBridge

logger = logging.getLogger("ytlounge.resolver")


class VideoResolver:
    def __init__(self, bridge: Optional[YtDlpBridge] = None):
        self.bridge = bridge or YtDlpBridge()
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def resolve(self, video_id: str) -> Dict[str, Any]:
        """Resolve video_id, serving from cache if valid."""
        now = time.time()
        if video_id in self._cache:
            expires_at, cached_data = self._cache[video_id]
            if now < expires_at:
                logger.debug("Serving %s from resolver cache", video_id)
                return cached_data

        info = self.bridge.resolve(video_id)
        # Cache TTL: duration or 3600 seconds (minimum 300 seconds)
        ttl = max(300, min(info.get("duration", 3600), 3600))
        self._cache[video_id] = (now + ttl, info)
        return info

    def clear_cache(self) -> None:
        self._cache.clear()
