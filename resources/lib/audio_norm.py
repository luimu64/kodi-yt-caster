"""Loudness normalization for cast audio (EBU R128 / ITU-R BS.1770).

Why this exists: YouTube's per-upload loudness is inconsistent, so a cast queue
jumps between loud and quiet items. Kodi offers no linear gain control for a
stream — its only per-item knob ("volume amplification") maps to
``SetDynamicRangeCompression``, i.e. DRC, not gain — so matching loudness means
rendering the audio track again: measure the integrated loudness with ffmpeg's
ebur128 meter, then re-encode the audio with a static gain toward the target.

Cost is real on Pi-class hardware (measured on a Pi 4: ~23x realtime to measure,
~7x realtime to AAC-encode), so normalization NEVER blocks playback. One
background worker renders a single item at a time into the addon profile cache;
the artifacts are reused for every later play of that video, and the next
resolve is what picks them up. A cold item plays its original audio.

Artifacts per video id (``<profile>/audio_norm/<video_id>/``):

* ``norm.m4a``  — the normalized progressive track (audio/visualiser lane)
* ``audio.m3u8`` + ``segNNNN.ts`` — the same track segmented for the HLS video
  lane's detached ``EXT-X-MEDIA`` audio rendition
* ``meta.json`` — measured loudness, applied gain, duration
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ytlounge.audio_norm")

DEFAULT_TARGET_LUFS = -14.0
DEFAULT_MAX_GAIN_DB = 12.0
# Never let the applied gain push the true peak past this: a boost that clips is
# worse than a quiet track.
PEAK_CEILING_DBTP = -1.0
ENCODE_BITRATE = "128k"
HLS_SEGMENT_SECONDS = 6.0
CACHE_LIMIT_BYTES = 768 * 1024 * 1024
MAX_DURATION_MINUTES = 20

# Static ffmpeg builds: the addon fetches one on first use (like yt-dlp).
# Kodi on LibreELEC ships no ffmpeg CLI, and there is no gain control in Kodi's
# Python API that could replace it.
_FFMPEG_ASSETS = {
    "aarch64": "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
    "arm64": "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
    "x86_64": "ffmpeg-master-latest-linux64-gpl.tar.xz",
    "amd64": "ffmpeg-master-latest-linux64-gpl.tar.xz",
    "armv7l": "ffmpeg-master-latest-linuxarmhf-gpl.tar.xz",
}
_FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{asset}"

_I_RE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)
_PEAK_RE = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.MULTILINE)

# Injected by the emulator (see emulator/README.md): replaces the ffmpeg
# subprocess so scenarios stay offline and deterministic.
_RUNNER: Optional[Callable[..., bool]] = None


def set_test_runner(fn: Optional[Callable[..., bool]]) -> None:
    global _RUNNER
    _RUNNER = fn


def parse_ebur128(stderr: str) -> Optional[Tuple[float, float]]:
    """Integrated loudness (LUFS) and true peak (dBTP) from an ebur128 summary.

    ffmpeg prints the summary on stderr when the filter graph ends:

        Integrated loudness:
          I:         -13.7 LUFS
        True peak:
          Peak:       -0.4 dBFS

    Returns None when the summary is absent (the filter never ran), so callers
    skip normalization instead of acting on a garbage measurement.
    """
    if not stderr:
        return None
    hits = _I_RE.findall(stderr)
    if not hits:
        return None
    try:
        lufs = float(hits[-1])
    except ValueError:
        return None
    peaks = _PEAK_RE.findall(stderr)
    try:
        true_peak = float(peaks[-1]) if peaks else 0.0
    except ValueError:
        true_peak = 0.0
    return lufs, true_peak


def compute_gain_db(
    lufs: float,
    true_peak: float,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    max_gain_db: float = DEFAULT_MAX_GAIN_DB,
    peak_ceiling: float = PEAK_CEILING_DBTP,
) -> float:
    """Static gain toward ``target_lufs``, peak-guarded and boost-capped.

    The boost cap keeps noise floors and near-silent tracks from being amplified
    into audible hiss; the peak guard is what stops a boost from clipping.
    """
    try:
        gain = float(target_lufs) - float(lufs)
    except (TypeError, ValueError):
        return 0.0
    try:
        gain = min(gain, float(peak_ceiling) - float(true_peak))
    except (TypeError, ValueError):
        pass
    limit = abs(float(max_gain_db))
    return round(max(-limit, min(gain, limit)), 2)


def asset_for_machine(machine: Optional[str] = None) -> Tuple[str, str]:
    """(download URL, member path inside the tarball) for this CPU."""
    import platform

    arch = (machine or platform.machine() or "").lower()
    asset = _FFMPEG_ASSETS.get(arch)
    if not asset:
        raise RuntimeError(f"no static ffmpeg build for arch {arch!r}")
    return _FFMPEG_URL.format(asset=asset), "bin/ffmpeg"


def fetch_ffmpeg(dest_dir: str, notify: Optional[Callable[[str, str], None]] = None) -> Optional[str]:
    """Download the static ffmpeg build and extract just the ffmpeg binary.

    Blocking; callers run it on a background thread. Returns the binary path, or
    None on failure (a failure is never fatal to casting — normalization is
    simply skipped until it succeeds).
    """
    import tarfile

    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, "ffmpeg")
    try:
        url, member = asset_for_machine()
    except RuntimeError as e:
        logger.warning("Audio normalization unavailable: %s", e)
        return None

    tar_path = os.path.join(dest_dir, "ffmpeg.download.tar.xz")
    tmp_bin = os.path.join(dest_dir, "ffmpeg.part")
    if notify:
        notify("YouTube Cast", "Downloading ffmpeg for audio normalization…")
    try:
        logger.info("Fetching ffmpeg from %s", url)
        with urllib.request.urlopen(url, timeout=60) as resp, open(tar_path, "wb") as out:
            shutil.copyfileobj(resp, out, 1024 * 1024)
        with tarfile.open(tar_path, "r:xz") as tf:
            found = None
            for m in tf.getmembers():
                if m.name.endswith(member) or m.name.endswith("/" + member):
                    found = m
                    break
            if found is None:
                raise RuntimeError(f"{member} not found in {url}")
            src = tf.extractfile(found)
            if src is None:
                raise RuntimeError(f"{member} is not a regular file in {url}")
            with src, open(tmp_bin, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        os.chmod(tmp_bin, 0o755)
        smoke = subprocess.run([tmp_bin, "-version"], capture_output=True, timeout=60)
        if smoke.returncode != 0:
            raise RuntimeError("downloaded ffmpeg does not run")
        os.replace(tmp_bin, target)
        logger.info("ffmpeg ready at %s", target)
        if notify:
            notify("YouTube Cast", "Audio normalization is ready")
        return target
    except Exception as e:
        logger.warning("ffmpeg fetch failed: %s", e)
        if notify:
            notify("YouTube Cast", f"ffmpeg download failed: {e}", True)
        return None
    finally:
        for path in (tar_path, tmp_bin):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


class AudioNormalizer:
    """Background loudness-normalization worker + artifact store."""

    def __init__(
        self,
        settings: Optional[Callable[[], Dict[str, Any]]] = None,
        cache_dir: Optional[str] = None,
        hold_check: Optional[Callable[[], bool]] = None,
        notify: Optional[Callable[[str, str, bool], None]] = None,
        fetch_dir: Optional[str] = None,
    ) -> None:
        self._settings = settings or (lambda: {})
        self.cache_dir = cache_dir or ""
        self._hold_check = hold_check
        self._notify = notify
        self._fetch_dir = fetch_dir or (self.cache_dir or "/tmp")
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queue: List[str] = []            # video ids, FIFO
        self._sources: Dict[str, str] = {}     # video id -> remote audio URL
        self._queued: set = set()
        self._running = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ffmpeg: Optional[str] = None
        self._ffmpeg_checked = False
        self._ffmpeg_fetching = False
        self._last_gain: Dict[str, float] = {}

    # ------------------------------------------------------------ settings
    def settings(self) -> Dict[str, Any]:
        try:
            return dict(self._settings() or {})
        except Exception:
            logger.debug("audio_norm settings read failed", exc_info=True)
            return {}

    @property
    def enabled(self) -> bool:
        raw = self.settings().get("audio_normalize")
        return str(raw).lower() not in ("false", "0", "")

    def target_lufs(self) -> float:
        try:
            return float(self.settings().get("audio_norm_target", DEFAULT_TARGET_LUFS))
        except (TypeError, ValueError):
            return DEFAULT_TARGET_LUFS

    def max_gain_db(self) -> float:
        try:
            return float(self.settings().get("audio_norm_max_gain", DEFAULT_MAX_GAIN_DB))
        except (TypeError, ValueError):
            return DEFAULT_MAX_GAIN_DB

    def max_duration_seconds(self) -> int:
        try:
            minutes = float(self.settings().get("audio_norm_max_minutes", MAX_DURATION_MINUTES))
        except (TypeError, ValueError):
            minutes = float(MAX_DURATION_MINUTES)
        return int(max(1.0, minutes) * 60)

    # ------------------------------------------------------------ ffmpeg
    def ffmpeg_path(self) -> Optional[str]:
        """ffmpeg binary path: explicit setting -> fetched copy -> PATH."""
        if self._ffmpeg and os.path.exists(self._ffmpeg):
            return self._ffmpeg
        configured = str(self.settings().get("ffmpeg_path") or "").strip()
        if configured and os.path.exists(configured) and os.access(configured, os.X_OK):
            self._ffmpeg = configured
            return configured
        if configured:
            # Tolerate a directory (users often point at a folder).
            candidate = os.path.join(configured, "ffmpeg")
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                self._ffmpeg = candidate
                return candidate
        fetched = os.path.join(self._fetch_dir, "ffmpeg")
        if os.path.exists(fetched) and os.access(fetched, os.X_OK):
            self._ffmpeg = fetched
            return fetched
        self._ffmpeg_checked = True
        return None

    def ensure_ffmpeg(self, notify_user: bool = True) -> None:
        """Kick a one-off background download if no ffmpeg binary is available."""
        if self.ffmpeg_path() is not None:
            return
        with self._lock:
            if self._ffmpeg_fetching:
                return
            self._ffmpeg_fetching = True

        def _run() -> None:
            try:
                callback = _user_notifier(self._notify) if notify_user else None
                fetched = fetch_ffmpeg(self._fetch_dir, notify=callback)
                if fetched:
                    self._ffmpeg = fetched
            finally:
                with self._lock:
                    self._ffmpeg_fetching = False

        threading.Thread(target=_run, name="FFmpegFetch", daemon=True).start()

    def set_hold_check(self, fn: Optional[Callable[[], bool]]) -> None:
        """Register a predicate: True = a playback handoff is in flight.

        Heavy work is held (SIGSTOP/SIGCONT on the ffmpeg child) while it holds,
        because a saturated service process starves the localhost server Kodi
        STATs the outgoing file against and stalls the handoff itself.
        """
        self._hold_check = fn

    # ------------------------------------------------------------ artifacts
    def dir_for(self, video_id: str) -> str:
        return os.path.join(self.cache_dir, video_id) if self.cache_dir else ""

    def progressive_path(self, video_id: str) -> str:
        return os.path.join(self.dir_for(video_id), "norm.m4a")

    def playlist_path(self, video_id: str) -> str:
        return os.path.join(self.dir_for(video_id), "audio.m3u8")

    def has_artifact(self, video_id: str) -> bool:
        if not video_id or not self.cache_dir:
            return False
        return os.path.exists(self.progressive_path(video_id)) and os.path.exists(
            self.playlist_path(video_id)
        )

    def local_playlist_url(self, video_id: str) -> Optional[str]:
        """Local HLS audio playlist URL, or None when nothing is rendered yet.

        Used as the ``EXT-X-MEDIA`` URI provider for the video lane: substituting
        the detached audio rendition is what makes a video cast normalized.
        """
        if not self.has_artifact(video_id):
            return None
        try:
            from .manifest_server import server_url_for

            return server_url_for(f"audio_norm/{video_id}/audio.m3u8")
        except Exception:
            logger.debug("playlist url unavailable", exc_info=True)
            return None

    def progressive_url(self, video_id: str) -> Optional[str]:
        """Local URL of the normalized progressive track (audio lane)."""
        if not self.has_artifact(video_id):
            return None
        try:
            from .manifest_server import server_url_for

            return server_url_for(f"audio_norm/{video_id}/norm.m4a")
        except Exception:
            logger.debug("progressive url unavailable", exc_info=True)
            return None

    def gain_of(self, video_id: str) -> Optional[float]:
        try:
            with open(os.path.join(self.dir_for(video_id), "meta.json"), "r", encoding="utf-8") as f:
                return float(json.load(f).get("gain_db"))
        except Exception:
            return None

    # ------------------------------------------------------------ job queue
    def request(self, video_id: str, source_url: Optional[str], duration: int = 0) -> bool:
        """Queue a render for this video if it is worth doing. Never blocks."""
        if not video_id or not source_url:
            return False
        if not self.enabled or not self.cache_dir:
            return False
        if self.has_artifact(video_id):
            return False
        if self.ffmpeg_path() is None:
            # No ffmpeg yet: start the fetch, skip this item (no dead air while
            # 120MB lands, and the artifact is picked up on the next play).
            self.ensure_ffmpeg()
            return False
        if duration and int(duration) > self.max_duration_seconds():
            logger.info("Skipping normalization for %s: %ss exceeds the cap", video_id, duration)
            return False
        with self._cv:
            if video_id in self._queued or video_id == self._running_id:
                return False
            self._sources[video_id] = source_url
            self._queue.append(video_id)
            self._queued.add(video_id)
            self._cv.notify_all()
        logger.info("Audio normalization queued for %s", video_id)
        return True

    _running_id: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="AudioNorm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._queue and not self._stop.is_set():
                    self._cv.wait(1.0)
                if self._stop.is_set():
                    return
                video_id = self._queue.pop(0)
                source = self._sources.pop(video_id, None)
                self._queued.discard(video_id)
                self._running_id = video_id
            try:
                if source and self.enabled and not self.has_artifact(video_id):
                    self._render(video_id, source)
            except Exception:
                logger.warning("Audio normalization failed for %s", video_id, exc_info=True)
            finally:
                with self._cv:
                    self._running_id = None

    def _render(self, video_id: str, source_url: str) -> None:
        """Measure, then render both artifacts. Runs on the worker thread."""
        started = time.monotonic()
        ffmpeg = self.ffmpeg_path()
        if not ffmpeg:
            return
        out_dir = self.dir_for(video_id)
        tmp_dir = out_dir + ".part"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)

        measured = self._measure(ffmpeg, source_url)
        if measured is None:
            logger.warning("No loudness measurement for %s; leaving it unnormalized", video_id)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        lufs, true_peak = measured
        gain = compute_gain_db(lufs, true_peak, self.target_lufs(), self.max_gain_db())

        progressive = os.path.join(tmp_dir, "norm.m4a")
        args = [
            ffmpeg, "-hide_banner", "-nostats", "-y",
            "-i", source_url,
            "-vn",
            "-af", f"aresample=async=1:first_pts=0,volume={gain}dB",
            "-c:a", "aac", "-b:a", ENCODE_BITRATE,
            "-movflags", "+faststart",
            progressive,
        ]
        if not self._run(ffmpeg, args):
            logger.warning("Audio encode failed for %s", video_id)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        # Segmented rendition for the HLS video lane. A remux (copy) of the file
        # we just encoded keeps the timeline continuous from 0, which is what
        # keeps the detached audio in sync with the video rendition.
        playlist = os.path.join(tmp_dir, "audio.m3u8")
        seg_args = [
            ffmpeg, "-hide_banner", "-nostats", "-y",
            "-i", progressive,
            "-c:a", "copy",
            "-f", "hls",
            "-hls_time", str(HLS_SEGMENT_SECONDS),
            "-hls_playlist_type", "vod",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", os.path.join(tmp_dir, "seg%04d.ts"),
            playlist,
        ]
        segmented = self._run(ffmpeg, seg_args)
        if not segmented:
            logger.warning("Audio segmentation failed for %s", video_id)

        meta = {
            "id": video_id,
            "lufs": round(lufs, 2),
            "true_peak": round(true_peak, 2),
            "gain_db": gain,
            "target_lufs": self.target_lufs(),
            "segmented": bool(segmented),
            "created": time.time(),
        }
        try:
            with open(os.path.join(tmp_dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except OSError:
            pass

        # Publish atomically: a half-written artifact must never be served.
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp_dir, out_dir)
        self._last_gain[video_id] = gain
        logger.info(
            "Normalized %s: %.1f LUFS / %.1f dBTP -> %+.1f dB in %.0fs",
            video_id, lufs, true_peak, gain, time.monotonic() - started,
        )
        self.trim_cache()

    def _measure(self, ffmpeg: str, source_url: str) -> Optional[Tuple[float, float]]:
        args = [
            ffmpeg, "-hide_banner", "-nostats",
            "-i", source_url,
            "-vn",
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ]
        stderr = self._run_capture(ffmpeg, args)
        if stderr is None:
            return None
        return parse_ebur128(stderr)

    # ------------------------------------------------------------ ffmpeg run
    def _run(self, ffmpeg: str, args: List[str]) -> bool:
        return self._run_capture(ffmpeg, args) is not None

    def _run_capture(self, ffmpeg: str, args: List[str]) -> Optional[str]:
        """Run ffmpeg, holding it with SIGSTOP while a handoff is in flight."""
        if _RUNNER is not None:
            return "" if _RUNNER(ffmpeg, list(args)) else None
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=_lower_priority,
            )
        except Exception:
            logger.warning("could not start ffmpeg", exc_info=True)
            return None
        held = False
        try:
            while True:
                try:
                    _, err = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    desired = bool(self._hold_check and self._hold_check())
                    if desired and not held:
                        _signal(proc, signal.SIGSTOP)
                        held = True
                        logger.info("Normalization held for a playback handoff")
                    elif held and not desired:
                        _signal(proc, signal.SIGCONT)
                        held = False
        finally:
            if held:
                _signal(proc, signal.SIGCONT)
        if proc.returncode != 0:
            return None
        return (err or b"").decode("utf-8", errors="replace")

    # ------------------------------------------------------------ cache
    def cache_bytes(self) -> int:
        total = 0
        try:
            for entry in os.scandir(self.cache_dir):
                if not entry.is_dir():
                    continue
                for f in os.scandir(entry.path):
                    try:
                        total += f.stat().st_size
                    except OSError:
                        continue
        except OSError:
            return 0
        return total

    def trim_cache(self, limit: int = CACHE_LIMIT_BYTES) -> None:
        try:
            entries = [e for e in os.scandir(self.cache_dir) if e.is_dir()]
        except OSError:
            return
        sizes = {}
        for e in entries:
            size = 0
            try:
                for f in os.scandir(e.path):
                    try:
                        size += f.stat().st_size
                    except OSError:
                        pass
                sizes[e.path] = (e.stat().st_mtime, size)
            except OSError:
                continue
        total = sum(s for _, s in sizes.values())
        if total <= limit:
            return
        for path, (_, size) in sorted(sizes.items(), key=lambda kv: kv[1][0]):
            if total <= limit:
                break
            logger.info("Trimming normalization cache: %s (%.0f MB)", os.path.basename(path), size / 1e6)
            shutil.rmtree(path, ignore_errors=True)
            total -= size

    # ------------------------------------------------------------ serving
    def playlist_body(self, video_id: str, base_url: str) -> Optional[str]:
        """The generated audio playlist with absolute segment URLs."""
        try:
            with open(self.playlist_path(video_id), "r", encoding="utf-8") as f:
                body = f.read()
        except OSError:
            return None
        out = []
        for line in body.splitlines():
            if line and not line.startswith("#"):
                name = line.strip().rsplit("/", 1)[-1]
                out.append(f"{base_url}audio_norm/{video_id}/{name}")
            else:
                out.append(line)
        return "\n".join(out) + "\n"

    def file_path(self, video_id: str, name: str) -> Optional[str]:
        """Resolve a served name to a file inside this video's artifact dir."""
        if "/" in name or not name or video_id in ("", ".", ".."):
            return None
        if not re.fullmatch(r"norm\.m4a|audio\.m3u8|seg\d{4}\.ts|meta\.json", name):
            return None
        path = os.path.join(self.dir_for(video_id), name)
        return path if os.path.isfile(path) else None


def _lower_priority() -> None:
    try:
        os.nice(10)
    except Exception:
        pass


def _user_notifier(fn: Optional[Callable[..., None]]) -> Optional[Callable[..., None]]:
    """Wrap the Kodi notification callback so a UI failure never kills the job."""
    if fn is None:
        return None

    def _cb(title: str, message: str, error: bool = False) -> None:
        try:
            fn(title, message, error)
        except Exception:
            pass

    return _cb


def _signal(proc: "subprocess.Popen", sig: int) -> None:
    try:
        if proc.poll() is None:
            proc.send_signal(sig)
    except Exception:
        logger.debug("could not signal ffmpeg", exc_info=True)


_INSTANCE: Optional[AudioNormalizer] = None


def set_instance(normalizer: Optional[AudioNormalizer]) -> None:
    global _INSTANCE
    _INSTANCE = normalizer


def handle_head(handler, path: str) -> None:
    """HEAD for /audio_norm/...: real length for files, empty for playlists."""
    parts = path.split("/")
    video_id = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    size = 0
    if _INSTANCE is not None and name and name != "audio.m3u8":
        found = _INSTANCE.file_path(video_id, name)
        if found:
            try:
                size = os.path.getsize(found)
            except OSError:
                size = 0
    handler.send_response(200)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Connection", "close")
    handler.end_headers()


def handle_request(handler, path: str) -> None:
    """Serve /audio_norm/<video_id>/{norm.m4a,audio.m3u8,segNNNN.ts}."""
    parts = path.split("/")
    if _INSTANCE is None or len(parts) < 2:
        handler.send_error(404, "Not Found")
        return
    video_id, name = parts[0], parts[1]
    if name == "audio.m3u8":
        base = _base_url(handler)
        body = _INSTANCE.playlist_body(video_id, base)
        if body is None:
            handler.send_error(404, "Not Found")
            return
        data = body.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return

    found = _INSTANCE.file_path(video_id, name)
    if not found:
        handler.send_error(404, "Not Found")
        return
    _serve_file(handler, found, name)


def _base_url(handler) -> str:
    return f"http://127.0.0.1:{handler.server.server_address[1]}/"


def _serve_file(handler, path: str, name: str) -> None:
    """Byte-serve a local artifact, honouring Range (seeking needs it)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        handler.send_error(404, "Not Found")
        return
    ctype = "audio/mp4" if name.endswith(".m4a") else "video/mp2t"
    start, end = 0, size - 1
    status = 200
    range_header = handler.headers.get("Range") if handler.headers else None
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        start, end = parsed
        status = 206
    length = max(0, end - start + 1)
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Accept-Ranges", "bytes")
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Content-Length", str(length))
    handler.end_headers()
    try:
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    try:
        spec = header.strip().split("=", 1)[1]
        first, _, last = spec.partition("-")
        if first == "":
            # suffix range: last N bytes
            n = int(last)
            if n <= 0:
                return None
            start = max(0, size - n)
            return start, size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except (IndexError, ValueError):
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)
