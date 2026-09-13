"""Subprocess bridge to invoke yt-dlp for video stream extraction."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger("ytlounge.ytdlp")

# ponytail: simple binary search ladder; bundled binary -> PATH -> common locations
POSSIBLE_BIN_NAMES = ["yt-dlp", "yt-dlp.exe"]


def find_ytdlp_binary(custom_path: Optional[str] = None) -> Optional[str]:
    """Locate yt-dlp executable."""
    if custom_path and os.path.isfile(custom_path) and os.access(custom_path, os.X_OK):
        return os.path.abspath(custom_path)

    # Check bundled bin dir inside addon
    addon_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    bundled_bin_dir = os.path.join(addon_root, "resources", "bin")
    for name in POSSIBLE_BIN_NAMES:
        bundled_path = os.path.join(bundled_bin_dir, name)
        if os.path.isfile(bundled_path) and os.access(bundled_path, os.X_OK):
            return bundled_path

    # Check system PATH
    for name in POSSIBLE_BIN_NAMES:
        sys_path = shutil.which(name)
        if sys_path:
            return sys_path

    # Check virtual environments / common local paths
    venv_path = os.path.join(addon_root, ".venv", "bin", "yt-dlp")
    if os.path.isfile(venv_path) and os.access(venv_path, os.X_OK):
        return venv_path

    return None


class YtDlpBridge:
    def __init__(self, binary_path: Optional[str] = None, cookies_path: Optional[str] = None):
        self.binary_path = binary_path or find_ytdlp_binary()
        self.cookies_path = cookies_path

    def resolve(self, video_id: str) -> Dict[str, Any]:
        """Resolve YouTube video ID to playable stream details."""
        if not self.binary_path:
            raise RuntimeError("yt-dlp executable not found. Please install yt-dlp or configure its path.")

        url = f"https://www.youtube.com/watch?v={video_id}"
        cmd = [
            self.binary_path,
            "--dump-json",
            "--no-playlist",
            "--no-warnings",
        ]
        if self.cookies_path and os.path.isfile(self.cookies_path):
            cmd.extend(["--cookies", self.cookies_path])

        cmd.append(url)

        logger.debug("Executing: %s", " ".join(cmd[:4]))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp error ({proc.returncode}): {proc.stderr.strip()[:300]}")

        data = json.loads(proc.stdout)
        return self._extract_stream_info(data)

    def _extract_stream_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        video_id = data.get("id", "")
        title = data.get("title", "")
        duration = int(data.get("duration", 0) or 0)
        thumbnail = data.get("thumbnail", "")

        playable_url = None
        stream_type = "progressive"

        # 1. Look for HLS playlist (native or adaptive in Kodi)
        for f in data.get("formats", []):
            proto = f.get("protocol", "")
            f_url = f.get("url", "")
            if "manifest/hls_playlist" in f_url or proto == "m3u8_native":
                playable_url = f_url
                stream_type = "hls"
                break

        # 2. Look for DASH manifest
        if not playable_url:
            for f in data.get("formats", []):
                proto = f.get("protocol", "")
                f_url = f.get("url", "")
                if "manifest/dash" in f_url or proto == "http_dash_segments":
                    playable_url = f_url
                    stream_type = "dash"
                    break

        # 3. Progressive pre-merged video+audio
        if not playable_url:
            for f in data.get("formats", []):
                if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url"):
                    playable_url = f["url"]
                    stream_type = "progressive"
                    break

        # 4. Fallback to top-level url
        if not playable_url:
            playable_url = data.get("url")

        return {
            "id": video_id,
            "title": title,
            "duration": duration,
            "thumbnail": thumbnail,
            "playable_url": playable_url,
            "stream_type": stream_type,
        }
