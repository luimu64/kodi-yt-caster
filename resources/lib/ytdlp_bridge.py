"""Subprocess bridge to invoke yt-dlp for video stream extraction."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from .manifest_server import publish
from . import ytdlp_inproc as inproc

logger = logging.getLogger("ytlounge.ytdlp")

# Loudness-normalization hook, set by service.py. Given a video id it returns
# the localhost URL of a rendered normalized audio playlist, or None. When it
# returns a URL, the master's default audio rendition points at our re-encoded
# track instead of YouTube's — that is what normalizes a video-lane cast.
_AUDIO_URI_PROVIDER: Optional[Callable[[str], Optional[str]]] = None


def set_audio_uri_provider(fn: Optional[Callable[[str], Optional[str]]]) -> None:
    global _AUDIO_URI_PROVIDER
    _AUDIO_URI_PROVIDER = fn


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

    # Strip YouTube auto-dubs: keep only creator-made audio (original and human dubs).
    # Auto-dub tracks carry "dubbed-auto" in format_note; they are machine-TTS and
    # nobody wants them in the picker.
    real_audio = [a for a in hls_audio if "dubbed-auto" not in str(a.get("format_note") or "").lower()]
    hls_audio = real_audio or hls_audio  # fall back to everything if filtering removed all

    # Dedupe by track name, keeping the highest-itag variant: the same audio track arrives
    # as a low (233-x) and high (234-x) rendition and offering both makes Kodi's default
    # selection ambiguous — the low variant can be the one that plays silent.
    def _itag_num(a: Dict[str, Any]) -> int:
        fid = str(a.get("format_id") or "")
        try:
            return int(fid.split("-")[0])
        except ValueError:
            return 0

    best_by_name: Dict[str, Dict[str, Any]] = {}
    for a in hls_audio:
        name = str(a.get("format_note") or "").split(" - ")[0] or str(a.get("language") or a.get("format_id"))
        cur = best_by_name.get(name)
        if cur is None or _itag_num(a) > _itag_num(cur):
            best_by_name[name] = a
    hls_audio = list(best_by_name.values())
    # Original track first so the later DEFAULT assignment picks it deterministically.
    def _is_original_marker(a: Dict[str, Any]) -> bool:
        return "original" in str(a.get("format_note") or "").lower()
    hls_audio.sort(key=lambda a: (not _is_original_marker(a), -_itag_num(a)))

    # Default audio = the ORIGINAL track when the video carries multiple audio renditions
    # (YouTube auto-dubs: 19 dubs + 1 original). Choosing by quality alone leaves all dub
    # tracks tied at 0 (format ids like "233-0" are not numeric), and list order then promotes
    # the first auto-dub (e.g. Bengali, often silent) to DEFAULT — the "no sound" bug.
    def _is_original(a: Dict[str, Any]) -> bool:
        return "original" in str(a.get("format_note") or "").lower()

    originals = [a for a in hls_audio if _is_original(a)]
    if originals:
        default_track = originals[0]
    else:
        default_track = max(hls_audio, key=_audio_quality) if hls_audio else None

    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    has_audio = bool(hls_audio)

    for i, a in enumerate(hls_audio):
        name = a.get("format_note") or f"Audio {i + 1}"
        # Keep the readable part ("American English - original"), drop the parenthesised suffix.
        if " - " in str(name):
            name = str(name).split(" - ")[0]
        is_default = "YES" if a is default_track else "NO"
        lang = str(a.get("language") or "").replace("-", "").replace("_", "") or "und"
        autoselect = "YES" if a is default_track else "NO"
        uri = str(a["url"])
        # Normalized audio for the default rendition only: the alternate tracks
        # (dubs) keep YouTube's originals, matching the track the OSD shows as
        # selected. See resources/lib/audio_norm.py.
        if a is default_track and _AUDIO_URI_PROVIDER is not None:
            try:
                local = _AUDIO_URI_PROVIDER(video_id)
            except Exception:
                local = None
                logger.debug("audio uri provider failed for %s", video_id, exc_info=True)
            if local:
                uri = local
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{name}",LANGUAGE="{lang}",DEFAULT={is_default},AUTOSELECT={autoselect},URI="{uri}"'
        )

    # Sort video streams from highest to lowest resolution/bitrate
    # Sort video streams highest-first, but H.264 ahead of VP9 at equal height: a Pi 4 cannot hardware-decode
    # VP9, so leading with VP9 gives a black screen or a stall rather than playback.
    hls_video.sort(
        key=lambda x: (
            1 if str(x.get("vcodec") or "").startswith("avc1") else 0,
            x.get("height") or 0,
            x.get("tbr") or 0,
        ),
        reverse=True,
    )
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
    # Serve the master over localhost http instead of writing a local file: split-rendition masters carry
    # detached EXT-X-MEDIA audio, which only inputstream.adaptive merges, and IA refuses local paths.
    name = f"yt_{video_id}.m3u8"
    return publish(name, "\n".join(lines))


class YtDlpBridge:
    def __init__(self, binary_path: Optional[str] = None, cookies_path: Optional[str] = None):
        self.binary_path = binary_path or find_ytdlp_binary()
        self.cookies_path = cookies_path
        self._cache: Dict[str, tuple] = {}  # video_id -> (monotonic_ts, info)

    def resolve(self, video_id: str, prefetch: bool = False) -> Dict[str, Any]:
        """Resolve YouTube video ID to playable stream details."""
        t0 = time.monotonic()
        if not self.binary_path and not inproc.available():
            raise RuntimeError("yt-dlp executable not found. Please install yt-dlp or configure its path.")

        # Cache: resolve results live for 30 min. Playback URLs (googlevideo) expire ~6h, and a
        # hit costs 0ms instead of a multi-second yt-dlp crawl — this is what makes auto-advance
        # and repeated casts of the same track fast.
        now = time.monotonic()
        cached = self._cache.get(video_id)
        if cached and now - cached[0] < 1800:
            logger.info("TIMING %s: cache hit (+%.0fms total)", video_id, (time.monotonic() - t0) * 1000)
            return cached[1]

        # In-process resolve first: persistent YoutubeDL instance reuses its
        # HTTP session across resolves (warm TLS + pooling), no ~30-40MB
        # subprocess spawn per track. Falls back to subprocess below.
        if inproc.try_init(self.binary_path):
            t1 = time.monotonic()
            try:
                data = inproc.resolve(video_id, cookies_path=self.cookies_path, prefetch=prefetch)
                logger.info("TIMING %s: inproc extract %.2fs", video_id, time.monotonic() - t1)
            except Exception as e:
                logger.warning("inproc resolve failed (%s); trying subprocess", e)
                data = None
            if data is not None:
                info = self._extract_stream_info(data)
                self._cache.pop(video_id, None)
                while len(self._cache) >= 50:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[video_id] = (time.monotonic(), info)
                logger.info("TIMING %s: total %.2fs (inproc)", video_id, time.monotonic() - t0)
                return info

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

        logger.info("TIMING %s: launching yt-dlp subprocess", video_id)
        t1 = time.monotonic()
        # Explicit UTF-8 stdio: LibreELEC's default locale is not UTF-8 and
        # text=True would decode with ASCII, breaking/mojibaking every
        # non-ASCII (CJK, etc.) title and corrupting the JSON parse.
        proc = subprocess.run(
            cmd, capture_output=True, timeout=45,
            encoding="utf-8", errors="replace",
        )
        t2 = time.monotonic()
        logger.info("TIMING %s: yt-dlp subprocess took %.2fs (rc=%s, %dB stdout)", video_id, t2 - t1, proc.returncode, len(proc.stdout or ""))
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp error ({proc.returncode}): {proc.stderr.strip()[:300]}")

        data = json.loads(proc.stdout)
        t3 = time.monotonic()
        info = self._extract_stream_info(data)
        logger.info("TIMING %s: json parse %.0fms, extract %.0fms, total %.2fs", video_id, (t3 - t2) * 1000, (time.monotonic() - t3) * 1000, time.monotonic() - t0)
        self._cache.pop(video_id, None)
        while len(self._cache) >= 50:
            self._cache.pop(next(iter(self._cache)))
        self._cache[video_id] = (time.monotonic(), info)
        return info

    def _extract_stream_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        video_id = data.get("id", "")
        title = data.get("title", "")
        duration = int(data.get("duration", 0) or 0)
        thumbnail = data.get("thumbnail", "")

        formats = data.get("formats", [])

        playable_url = None
        stream_type = "progressive"

        # 1b. Audio-only stream for music visualizer mode (Kodi shows its
        #     audio visualizer only for audio-player playback).
        audio_candidates = [
            f for f in formats
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
            and str(f.get("protocol") or "").startswith("http")
            and ".m3u8" not in str(f.get("url") or "")
        ]
        best_audio = (
            max(audio_candidates, key=lambda f: float(f.get("abr") or f.get("tbr") or 0))
            if audio_candidates else None
        )

        # Still-image song detection for the music visualizer. Bitrate alone
        # CANNOT separate these: real old music videos run ~700kbps and modern
        # art videos ~500-1600kbps (verified empirically). Signals that DO
        # identify audio uploads, by confidence:
        #   1. track/album metadata — only music uploads carry it
        #   2. explicit audio-upload title patterns
        #   3. trivially low video bitrate (<500kbps = near-static frames)
        heights = [f.get("height") or 0 for f in formats if f.get("vcodec") != "none"]
        tbrs = [float(f.get("tbr") or 0) for f in formats if f.get("vcodec") != "none"]
        max_video_tbr = max(tbrs) if tbrs else 0.0
        title_l = str(data.get("title") or "").lower()
        AUDIO_TITLE_MARKERS = (
            "(audio)", "official audio", "audio only", "(lyric video)",
            "lyric video", "official lyrics", "visualizer", "album version",
        )
        has_track_meta = bool(data.get("track") or data.get("album"))
        title_is_audio = any(m in title_l for m in AUDIO_TITLE_MARKERS)
        is_static_art = bool(heights) and (
            has_track_meta or title_is_audio
            or max(heights) <= 144
            or (0 < max_video_tbr < 600.0)
        )

        # 1. Prefer a single progressive (muxed audio+video) stream: Kodi's native player plays it with
        #    audio and needs no local manifest. A split-rendition HLS master with detached EXT-X-MEDIA
        #    audio renders VIDEO ONLY in Kodi (the external audio playlist is dropped) — that was the
        #    "picture but no sound" bug. H.264 first: the Pi 4 cannot hardware-decode VP9.
        progressive: List[Dict[str, Any]] = []
        for f in formats:
            proto = str(f.get("protocol") or "")
            f_url = str(f.get("url") or "")
            vcodec = str(f.get("vcodec") or "")
            acodec = str(f.get("acodec") or "")
            if proto.startswith("http") and ".m3u8" not in f_url and vcodec and vcodec != "none" and acodec and acodec != "none":
                progressive.append(f)

        if progressive:
            def _prog_key(f: Dict[str, Any]) -> tuple:
                is_avc = 1 if str(f.get("vcodec") or "").startswith("avc1") else 0
                return (is_avc, f.get("height") or 0, f.get("tbr") or 0)

            best_prog = max(progressive, key=_prog_key)
            playable_url = best_prog["url"]
            stream_type = "progressive"

        # 2. Fall back to the multi-rendition HLS master playlist (resolution switching in the Kodi OSD)
        if not playable_url:
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
            "audio_url": best_audio.get("url") if best_audio else None,
            "is_static_art": is_static_art,
            "max_video_tbr": max_video_tbr,
            "artist": data.get("artist") or data.get("uploader") or "",
            "album": data.get("album") or "",
        }
