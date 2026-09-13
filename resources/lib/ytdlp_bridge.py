"""Subprocess bridge to invoke yt-dlp for video stream extraction."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ytlounge.ytdlp")

# ponytail: simple binary search ladder; bundled binary -> PATH -> common locations
POSSIBLE_BIN_NAMES = ["yt-dlp", "yt-dlp.exe"]


def find_ytdlp_binary(custom_path: Optional[str] = None) -> Optional[str]:
    """Locate yt-dlp executable."""
    if custom_path and os.path.isfile(custom_path) and os.access(custom_path, os.X_OK):
        return os.path.abspath(custom_path)

    # Check Kodi user profile addon_data bin directory
    try:
        import xbmcaddon
        import xbmcvfs
        profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
        for name in POSSIBLE_BIN_NAMES:
            profile_path = os.path.join(profile, "bin", name)
            if os.path.isfile(profile_path) and os.access(profile_path, os.X_OK):
                return profile_path
    except Exception:
        pass

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


def build_hls_master_manifest(formats: List[Dict[str, Any]], video_id: str) -> Optional[str]:
    """Generate an HLS Master Playlist (m3u8) exposing video resolutions and audio streams.

    Codec filtering: one codec family per resolution (avc1 preferred, vp09 as
    fallback) so the OSD picker never offers streams a device cannot decode, and
    resolutions are not duplicated.
    """
    hls_video: List[Dict[str, Any]] = []
    hls_audio: List[Dict[str, Any]] = []

    for f in formats:
        f_url = f.get("url", "")
        proto = str(f.get("protocol") or "")
        if "manifest/hls_playlist" in f_url or proto == "m3u8_native" or ".m3u8" in f_url or "m3u8" in proto:
            vcodec = str(f.get("vcodec") or "")
            res = str(f.get("resolution") or "")
            note = str(f.get("format_note") or "").lower()
            if vcodec == "none" or "audio" in res or "audio" in note:
                hls_audio.append(f)
            else:
                hls_video.append(f)

    if not hls_video:
        return None

    # One codec family per resolution: prefer H.264 (universally decodable),
    # fall back to VP9, then anything else.
    by_res: Dict[tuple, Dict[str, Any]] = {}
    for v in hls_video:
        height = v.get("height") or 0
        fps = int(round(float(v.get("fps") or 0)))
        key = (height, fps)
        cur = by_res.get(key)
        if cur is None:
            by_res[key] = v
            continue
        vcodec = str(v.get("vcodec") or "")
        cur_codec = str(cur.get("vcodec") or "")
        v_is_avc = vcodec.startswith("avc1")
        cur_is_avc = cur_codec.startswith("avc1")
        if (v_is_avc and not cur_is_avc) or (v_is_avc == cur_is_avc and (v.get("tbr") or 0) > (cur.get("tbr") or 0)):
            by_res[key] = v
    hls_video = list(by_res.values())

    # Default audio = highest quality. HLS audio formats often lack abr/tbr,
    # so fall back to the numeric format id (higher itag = better stream).
    def _audio_quality(a: Dict[str, Any]) -> float:
        for key in ("abr", "tbr"):
            val = a.get(key)
            if val:
                return float(val)
        fid = str(a.get("format_id") or "")
        return float(fid) if fid.isdigit() else 0.0

    hls_audio.sort(key=_audio_quality, reverse=True)

    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    has_audio = bool(hls_audio)

    for i, a in enumerate(hls_audio):
        name = a.get("format_note") or f"Audio {i + 1}"
        is_default = "YES" if i == 0 else "NO"
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{name}",DEFAULT={is_default},AUTOSELECT=YES,URI="{a["url"]}"'
        )

    # Sort video streams from highest to lowest resolution/bitrate
    hls_video.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0), reverse=True)
    for v in hls_video:
        bw = int((v.get("tbr") or 1000) * 1000)
        res = v.get("resolution") or (f"{v.get('width')}x{v.get('height')}" if v.get("width") else "")
        vcodec = v.get("vcodec", "")
        attrs = [f"BANDWIDTH={bw}"]
        if res and "x" in res:
            attrs.append(f"RESOLUTION={res}")
        fps = v.get("fps")
        if fps:
            attrs.append(f"FRAME-RATE={fps:.3f}")
        if vcodec:
            attrs.append(f'CODECS="{vcodec}"')
        if has_audio:
            attrs.append('AUDIO="audio"')
        lines.append(f'#EXT-X-STREAM-INF:{",".join(attrs)}')
        lines.append(v["url"])

    # Unique temp file: predictable shared names in the temp dir are symlink
    # clobber targets and collide across concurrent resolves.
    fd, manifest_path = tempfile.mkstemp(prefix=f"yt_{video_id}_", suffix=".m3u8")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return manifest_path


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

        # 1. Build multi-rendition HLS master playlist for native Kodi resolution switching
        formats = data.get("formats", [])
        master_path = build_hls_master_manifest(formats, video_id)
        if master_path:
            playable_url = master_path
            stream_type = "hls_master"

        # 2. Look for single HLS playlist fallback
        if not playable_url:
            for f in formats:
                proto = f.get("protocol", "")
                f_url = f.get("url", "")
                if "manifest/hls_playlist" in f_url or proto == "m3u8_native":
                    playable_url = f_url
                    stream_type = "hls"
                    break

        # 3. Look for DASH manifest
        if not playable_url:
            for f in formats:
                proto = f.get("protocol", "")
                f_url = f.get("url", "")
                if "manifest/dash" in f_url or proto == "http_dash_segments":
                    playable_url = f_url
                    stream_type = "dash"
                    break

        # 4. Progressive pre-merged video+audio
        if not playable_url:
            for f in formats:
                if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url"):
                    playable_url = f["url"]
                    stream_type = "progressive"
                    break

        # 5. Fallback to top-level url
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
