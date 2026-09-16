"""Resolver module with in-memory caching for video streams."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional, Tuple
from . import manifest_server
from .ytdlp_bridge import YtDlpBridge

logger = logging.getLogger("ytlounge.resolver")

# The DEFAULT=YES audio rendition of a generated master. Only that rendition is
# replaced with our normalized track: alternate tracks (auto-dubs) keep
# YouTube's originals, matching the rendition the OSD reports as selected.
_AUDIO_MEDIA_RE = re.compile(r"^#EXT-X-MEDIA:.*TYPE=AUDIO.*DEFAULT=YES.*$")


class VideoResolver:
    def __init__(self, bridge: Optional[YtDlpBridge] = None, normalizer=None):
        self.bridge = bridge or YtDlpBridge()
        self.normalizer = normalizer
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def resolve(self, video_id: str, prefetch: bool = False) -> Dict[str, Any]:
        """Resolve video_id, serving from cache if valid."""
        now = time.time()
        if video_id in self._cache:
            expires_at, cached_data = self._cache[video_id]
            if now < expires_at:
                logger.debug("Serving %s from resolver cache", video_id)
                return self._with_audio(video_id, cached_data)

        try:
            info = self.bridge.resolve(video_id, prefetch=prefetch)
        except TypeError:
            info = self.bridge.resolve(video_id)
        # Cache TTL: duration or 3600 seconds (minimum 300 seconds)
        ttl = max(300, min(info.get("duration", 3600), 3600))
        expired = [k for k, (exp, _) in self._cache.items() if now >= exp]
        for k in expired:
            self._cache.pop(k, None)
        self._cache.pop(video_id, None)
        while len(self._cache) >= 50:
            self._cache.pop(next(iter(self._cache)))
        # The cache stores the RAW stream info; the normalized variant is
        # re-derived on every resolve so an artifact rendered in the background
        # is picked up by the next play without paying a re-resolve.
        self._cache[video_id] = (now + ttl, info)
        return self._with_audio(video_id, info)

    def clear_cache(self) -> None:
        self._cache.clear()

    # --------------------------------------------------------------- audio
    def _with_audio(self, video_id: str, info: Dict[str, Any]) -> Dict[str, Any]:
        """Swap in normalized audio when it exists, otherwise queue a render.

        Never blocks and never fails a resolve: normalization is an
        enhancement, so a missing ffmpeg, a dead worker or a bad measurement all
        degrade to playing the original stream.
        """
        normalizer = self.normalizer
        if normalizer is None:
            return info
        try:
            if not normalizer.enabled:
                return info
            if normalizer.has_artifact(video_id):
                out = dict(info)
                if out.get("stream_type") == "hls_master":
                    self._retarget_master_audio(video_id, out)
                local = normalizer.progressive_url(video_id)
                if local and out.get("audio_url"):
                    out["audio_url"] = local
                    out["audio_normalized"] = True
                return out
            normalizer.request(
                video_id,
                info.get("audio_url"),
                int(info.get("duration") or 0),
            )
        except Exception:
            logger.debug("audio normalization hook failed for %s", video_id, exc_info=True)
        return info

    def _retarget_master_audio(self, video_id: str, info: Dict[str, Any]) -> bool:
        """Point the generated master's default audio rendition at our track.

        The master is built by the bridge and cached, so a video cast before its
        render finished still carries YouTube's audio URI; patching the
        published body in place is what makes a re-play of that video use the
        normalized audio without rebuilding the master.
        """
        try:
            normalizer = self.normalizer
            if normalizer is None:
                return False
            url = str(info.get("playable_url") or "")
            if not url:
                return False
            local = normalizer.local_playlist_url(video_id)
            if not local:
                return False
            body = manifest_server.fetch_manifest(url)
            if not body:
                return False
            lines = body.splitlines()
            changed = False
            for i, line in enumerate(lines):
                if _AUDIO_MEDIA_RE.match(line.strip()) and "URI=" in line:
                    if f'URI="{local}"' in line:
                        return False  # already retargeted
                    lines[i] = re.sub(r'URI="[^"]*"', f'URI="{local}"', line)
                    changed = True
                    break
            if not changed:
                return False
            name = url.rsplit("/", 1)[-1].split("?")[0]
            manifest_server.publish(name, "\n".join(lines) + "\n")
            logger.info("Master audio rendition for %s retargeted to normalized audio", video_id)
            return True
        except Exception:
            logger.debug("master retarget failed for %s", video_id, exc_info=True)
            return False
