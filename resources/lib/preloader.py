"""Background stream preloader: caches the first ~60s of the next queue item.

The resolve-cache prefetch already eliminates the yt-dlp crawl on track
change, but the media itself is still fetched cold: Kodi opens the
googlevideo URL, pays CDN TLS + TTFB, and the first seconds stutter on
slow links. This module downloads the first N bytes of the *next* video's
progressive stream while the current one plays, and serves playback
through the localhost manifest server:

  - requests that fall inside the cached prefix are served from disk
    (instant, no network),
  - requests beyond it are seamlessly spliced onto the remote URL from
    the right byte offset (seek support stays intact).

HLS/DASH manifests are not preloaded (their segment playlists rotate);
those keep the resolve-cache-only path.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("ytlounge.preloader")

PRELOAD_SECONDS = 60
MIN_BYTES = 4 * 1024 * 1024    # always cache at least ~4 MB
MAX_BYTES = 24 * 1024 * 1024   # cap for high-bitrate progressive

_LOCK = threading.Lock()
_ITEMS: Dict[str, Dict[str, Any]] = {}   # video_id -> meta
_CACHE_DIR = tempfile.mkdtemp(prefix="ytc-preload-")
_KEEP = 2  # prefix files to retain

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _prefix_bytes(info: Dict[str, Any]) -> int:
    tbr = info.get("tbr") or 0
    try:
        est = int(float(tbr) * 1000 / 8 * PRELOAD_SECONDS)
    except (TypeError, ValueError):
        est = 0
    return max(MIN_BYTES, min(est or MIN_BYTES, MAX_BYTES))


def _request(url: str, range_header: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": _UA, "Range": range_header})


def _trim_cache(keep_id: str) -> None:
    """Keep only the most recent _KEEP prefix files."""
    with _LOCK:
        items = sorted(_ITEMS.values(), key=lambda m: m.get("mtime", 0.0), reverse=True)
        keep_paths = {m["id"] for m in items[:_KEEP]} | {keep_id}
        for m in items:
            if m["id"] not in keep_paths:
                path = m.get("path")
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                _ITEMS.pop(m["id"], None)


def preload(video_id: str, info: Dict[str, Any]) -> None:
    """Background-download the first ~60s of a progressive stream."""
    url = info.get("playable_url") or ""
    if info.get("stream_type") != "progressive" or not url:
        return
    with _LOCK:
        existing = _ITEMS.get(video_id)
        if existing and existing.get("state") == "ready":
            return

    path = os.path.join(_CACHE_DIR, f"{video_id}.prefix")
    limit = _prefix_bytes(info)
    meta: Dict[str, Any] = {
        "id": video_id, "url": url, "path": path, "size": 0,
        "total": None, "ctype": "video/mp4", "state": "loading",
        "mtime": time.time(), "limit": limit,
    }
    with _LOCK:
        _ITEMS[video_id] = meta

    def _run() -> None:
        try:
            t0 = time.monotonic()
            size = 0
            total: Optional[int] = None
            ctype = meta["ctype"]
            with open(path, "wb") as f:
                with urllib.request.urlopen(_request(url, "bytes=0-"), timeout=30.0) as resp:
                    cr = resp.headers.get("Content-Range")
                    if cr and "/" in cr:
                        tail = cr.rsplit("/", 1)[1]
                        if tail.isdigit():
                            total = int(tail)
                    ct = resp.headers.get("Content-Type")
                    if ct:
                        ctype = ct
                    while size < limit:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        size += len(chunk)
            with _LOCK:
                meta.update(size=size, total=total, ctype=ctype, state="ready", mtime=time.time())
            logger.info("Preloaded %s: %.1f MB of %s in %.1fs",
                        video_id, size / 1e6, f"{total / 1e6:.0f}MB" if total else "?",
                        time.monotonic() - t0)
            _trim_cache(video_id)
        except Exception:
            with _LOCK:
                meta["state"] = "failed"
            logger.debug("Preload of %s failed", video_id, exc_info=True)

    threading.Thread(target=_run, name=f"Preload-{video_id}", daemon=True).start()


def proxy_url(video_id: str, info: Dict[str, Any]) -> Optional[str]:
    """Local URL when a usable prefix exists for this progressive stream."""
    if info.get("stream_type") != "progressive":
        return None
    with _LOCK:
        meta = _ITEMS.get(video_id)
    if not meta or meta["url"] != (info.get("playable_url") or ""):
        return None
    if meta["state"] != "ready" or meta["size"] < 1024:
        return None
    from .manifest_server import server_url_for
    return server_url_for(f"preload/{video_id}")


def _parse_range(header: Optional[str]) -> Optional[Tuple[int, Optional[int]]]:
    """'bytes=a-b' -> (a, b); 'bytes=a-' -> (a, None). Suffix/invalid -> None."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    if not a.isdigit():
        return None  # suffix range (-n) or invalid
    start = int(a)
    end = int(b) if b.isdigit() else None
    return start, end


def _send(handler, code: int, headers: Dict[str, str]) -> None:
    handler.send_response(code)
    for k, v in headers.items():
        handler.send_header(k, v)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()


def _proxy_remote(handler, url: str, range_header: str, ctype: str) -> None:
    """Forward a request verbatim to the remote stream and pipe it back."""
    try:
        with urllib.request.urlopen(_request(url, range_header), timeout=30.0) as resp:
            hdrs = {"Content-Type": resp.headers.get("Content-Type", ctype),
                    "Accept-Ranges": "bytes"}
            cl = resp.headers.get("Content-Length")
            cr = resp.headers.get("Content-Range")
            if cl:
                hdrs["Content-Length"] = cl
            if cr:
                hdrs["Content-Range"] = cr
            _send(handler, 206 if cr else 200, hdrs)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception:
        logger.debug("Remote proxy failed", exc_info=True)


def handle_request(handler, video_id: str) -> None:
    """Serve a preload prefix, splicing to the remote stream past its end."""
    with _LOCK:
        meta = _ITEMS.get(video_id)
    if not meta or not os.path.isfile(meta["path"]):
        handler.send_error(404, "Not Found")
        return

    size = os.path.getsize(meta["path"])
    total = meta["total"]  # Optional[int]
    ctype = str(meta["ctype"])
    url = str(meta["url"])
    range_header = handler.headers.get("Range")

    # Suffix or absent range handling: open-ended start=0 is the common
    # playback case and is served by the normal path; anything we cannot
    # map to (start, end) exactly is proxied verbatim to the remote.
    parsed = _parse_range(range_header)
    if parsed is None:
        if range_header:  # suffix range etc: let the origin handle it
            _proxy_remote(handler, url, range_header, ctype)
        else:
            parsed = (0, None)  # no Range header: full-file disk path below
    if parsed is None:
        return

    start, end = parsed
    if end is not None and total is not None:
        end = min(end, total - 1)

    if start >= size:
        # Entirely past the cached prefix.
        rh = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        _proxy_remote(handler, url, rh, ctype)
        return

    # Serve the warm disk prefix [start, min(end, size-1)] ...
    disk_end = size - 1 if end is None else min(end, size - 1)
    real_end = end if end is not None else ((total - 1) if total is not None else None)
    hdrs = {"Content-Type": ctype, "Accept-Ranges": "bytes"}
    if total is not None and real_end is not None:
        content_len = real_end - start + 1
        hdrs["Content-Range"] = f"bytes {start}-{real_end}/{total}"
        hdrs["Content-Length"] = str(content_len)
    elif end is not None:
        hdrs["Content-Length"] = str(end - start + 1)
    _send(handler, 206, hdrs)
    try:
        with open(meta["path"], "rb") as f:
            f.seek(start)
            remaining = disk_end - start + 1
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
        # ... then splice onto the remote for bytes past the prefix.
        past_prefix = end is None or end >= size
        if past_prefix and (total is None or total > size):
            rh = f"bytes={size}-" if end is None else (
                f"bytes={size}-{end}" if end >= size else None)
            if rh is not None:
                with urllib.request.urlopen(_request(url, rh), timeout=30.0) as resp:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass  # client closed (seek/stop) — normal
    except Exception:
        logger.debug("Prefix serve for %s failed", video_id, exc_info=True)
