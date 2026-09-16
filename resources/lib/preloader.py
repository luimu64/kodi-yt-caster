"""Background stream preloader: caches the first ~60s of the next queue item.

The resolve-cache prefetch already eliminates the yt-dlp crawl on track
change, but the media itself is still fetched cold: Kodi opens the
googlevideo URL, pays CDN TLS + TTFB, and the first seconds stutter on
slow links. YouTube now serves only split HLS (no progressive formats),
so this module preloads at the SEGMENT level:

  - preload(): fetches the media playlist for the variant+audio the local
    master exposes, and background-downloads the first ~60s of segments.
    Segment bytes land in an in-memory cache.
  - The manifest server routes /preload/<vid>/... requests here:
      playlist  -> remote media playlist with segment URLs rewritten to
                   local /preload/<vid>/seg/<n> placeholders
      seg/<n>   -> cached bytes when warm, else fetch-on-demand from the
                   remote URL that placeholder maps to (seeks keep working)

Progressive streams (YouTube no longer serves them; kept for other
resolvers) use the original byte-prefix disk cache with remote splice.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ytlounge.preloader")

PRELOAD_SECONDS = 60
MIN_BYTES = 4 * 1024 * 1024    # progressive prefix floor
MAX_BYTES = 24 * 1024 * 1024   # progressive prefix cap
SEG_CACHE_LIMIT = 48 * 1024 * 1024  # total cached segment bytes per video

_LOCK = threading.Lock()
_ITEMS: Dict[str, Dict[str, Any]] = {}   # video_id -> meta
_CACHE_DIR = tempfile.mkdtemp(prefix="ytc-preload-")
_KEEP = 2  # preload entries to retain

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _prefix_bytes(info: Dict[str, Any]) -> int:
    tbr = info.get("tbr") or 0
    try:
        est = int(float(tbr) * 1000 / 8 * PRELOAD_SECONDS)
    except (TypeError, ValueError):
        est = 0
    return max(MIN_BYTES, min(est or MIN_BYTES, MAX_BYTES))


def _request(url: str, range_header: Optional[str] = None) -> urllib.request.Request:
    headers = {"User-Agent": _UA}
    if range_header:
        headers["Range"] = range_header
    return urllib.request.Request(url, headers=headers)


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=timeout) as resp:
        return resp.read()


def _trim_cache(keep_id: str) -> None:
    """Keep only the most recent _KEEP preload entries."""
    with _LOCK:
        items = sorted(_ITEMS.values(), key=lambda m: m.get("mtime", 0.0), reverse=True)
        keep_ids = {m["id"] for m in items[:_KEEP]} | {keep_id}
        for m in items:
            if m["id"] not in keep_ids:
                path = m.get("path")
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                _ITEMS.pop(m["id"], None)


# ---------------------------------------------------------------- progressive

def _preload_progressive(video_id: str, info: Dict[str, Any]) -> None:
    url = info["playable_url"]
    path = os.path.join(_CACHE_DIR, f"{video_id}.prefix")
    limit = _prefix_bytes(info)
    meta: Dict[str, Any] = {
        "id": video_id, "kind": "progressive", "url": url, "path": path,
        "size": 0, "total": None, "ctype": "video/mp4", "state": "loading",
        "mtime": time.time(),
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
            logger.info("Preloaded %s (progressive): %.1f MB in %.1fs",
                        video_id, size / 1e6, time.monotonic() - t0)
            _trim_cache(video_id)
        except Exception:
            with _LOCK:
                meta["state"] = "failed"
            logger.debug("Preload of %s failed", video_id, exc_info=True)

    threading.Thread(target=_run, name=f"Preload-{video_id}", daemon=True).start()


# ----------------------------------------------------------------------- HLS

def _abs(base_url: str, url: str) -> str:
    return urllib.parse.urljoin(base_url, url)


def _is_normalized_audio_uri(url: str) -> bool:
    """True for the loudness-normalized audio rendition served by this addon.

    Such a URI is already local (the manifest server serves it straight from the
    rendered artifact), so the master rewrite leaves it untouched: proxying it
    through /preload would only add a hop and pin its segments in memory.
    """
    return "/audio_norm/" in url


def rewrite_master(body: str, video_id: str) -> Tuple[str, List[str]]:
    """Rewrite a master playlist's media-playlist URLs (variant lines and
    EXT-X-MEDIA URI= attributes) to local /preload/<vid>/<vkey> URLs.

    Returns (rewritten_body, remote_media_urls)."""
    urls: List[str] = []
    lines = body.splitlines()
    out: List[str] = []

    def _local(u: str) -> str:
        return f"/preload/{video_id}/{_vkey(u)}"

    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#EXT-X-MEDIA:") and "URI=" in s:
            # split at URI=, take the quoted URL, keep the rest verbatim
            pre, rest = s.split("URI=", 1)
            u = rest.split('"', 2)
            if len(u) >= 2:
                remote = _abs("", u[1])
                if _is_normalized_audio_uri(remote):
                    out.append(line)
                    continue
                urls.append(remote)
                out.append(pre + "URI=" + '"' + _local(remote) + '"' + u[2] if len(u) > 2 else pre + "URI=" + '"' + _local(remote) + '"')
                continue
            out.append(line)
        elif s.startswith("#EXT-X-STREAM-INF:"):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt and not nxt.startswith("#"):
                remote = nxt
                urls.append(remote)
                out.append(line)
                out.append(_local(remote))
                continue
        out.append(line)
    return "\n".join(out) + "\n", urls


def preload_hls(video_id: str, media_playlist_urls: List[str]) -> None:
    """Preload first ~PRELOAD_SECONDS of the given remote media playlists.

    Each playlist's segments are rewritten to local /preload/<vid>/<vkey>/<n>
    URLs; segment bytes for the first ~60s are prefetched into the cache."""
    meta: Dict[str, Any] = {
        "id": video_id, "kind": "hls", "state": "loading",
        "mtime": time.time(),
        "segments": {},   # (vkey, n) -> remote segment URL
        "cache": {},      # (vkey, n) -> bytes
        "cached_bytes": 0,
        "playlist_body": {},  # vkey -> rewritten playlist text
        "master_lines": [],   # vkey list, master order preserved
    }
    with _LOCK:
        _ITEMS[video_id] = meta

    def _run() -> None:
        try:
            t0 = time.monotonic()
            # Fetch all media playlist bodies in PARALLEL first (a 4K master
            # carries ~10 variants; serial fetches stretched ready-time to
            # 5-6s). Then process each body (pure string ops, fast) and only
            # mark ready once every variant body is built — the served master
            # references every variant, and a 404 on one Kodi happens to pick
            # would abort playback.
            bodies: List[Tuple[str, str]] = []
            fetch_lock = threading.Lock()

            def _fetch_playlist(vurl: str) -> None:
                try:
                    body = _http_get(vurl).decode("utf-8", errors="replace")
                except Exception:
                    logger.debug("playlist fetch failed during preload of %s", video_id)
                    return
                with fetch_lock:
                    bodies.append((vurl, body))

            workers = min(6, max(1, len(media_playlist_urls)))
            it = iter(media_playlist_urls)
            running: List[threading.Thread] = []
            while True:
                running = [t for t in running if t.is_alive()]
                while len(running) < workers:
                    try:
                        vurl = next(it)
                    except StopIteration:
                        break
                    t = threading.Thread(target=_fetch_playlist, args=(vurl,), daemon=True)
                    t.start()
                    running.append(t)
                if not running:
                    break
                running[0].join(0.2)

            fetch_plan: List[Tuple[str, List[int]]] = []
            for vurl, body in bodies:
                vkey = _vkey(vurl)
                target = 10.0
                segs_raw: List[str] = []
                for l in body.splitlines():
                    s = l.strip()
                    if not s:
                        continue
                    if s.startswith("#EXT-X-TARGETDURATION:"):
                        try:
                            target = float(s.split(":", 1)[1])
                        except ValueError:
                            pass
                    elif not s.startswith("#"):
                        segs_raw.append(s)
                want = max(1, int(PRELOAD_SECONDS / max(target, 1)))

                with _LOCK:
                    meta["master_lines"].append(vkey)
                    # Map ALL segment placeholders so fetch-on-demand can
                    # serve any seek position even before its bytes are warm.
                    for i, seg in enumerate(segs_raw):
                        meta["segments"][(vkey, i)] = _abs(vurl, seg)

                rebuilt: List[str] = []
                i = 0
                for l in body.splitlines():
                    s = l.strip()
                    if s and not s.startswith("#"):
                        rebuilt.append(f"/preload/{video_id}/{vkey}/{i}")
                        i += 1
                    else:
                        rebuilt.append(l)
                with _LOCK:
                    meta["playlist_body"][vkey] = "\n".join(rebuilt) + "\n"
                fetch_plan.append((vkey, list(range(min(want, len(segs_raw))))))

            with _LOCK:
                meta["state"] = "ready"  # all bodies served; segments warm next
                meta["mtime"] = time.time()

            # PASS 2 (bytes): parallel prefetch of the first ~60s per variant.
            # Serial fetch of ~6 segments x ~300-800ms each stretched preload
            # to 5s+; with a small thread pool the wall time is ~one segment.
            fetched = 0
            for vkey, idxs in fetch_plan:
                fetched += _prefetch_segments(meta, video_id, [(vkey, i) for i in idxs])

            logger.info("Preloaded %s (hls): %d segments in %.1fs",
                        video_id, fetched, time.monotonic() - t0)
            _trim_cache(video_id)
        except Exception:
            with _LOCK:
                meta["state"] = "failed"
            logger.debug("HLS preload of %s failed", video_id, exc_info=True)

    threading.Thread(target=_run, name=f"PreloadHLS-{video_id}", daemon=True).start()


def _prefetch_segments(meta: Dict[str, Any], video_id: str, keys: List[Tuple[str, int]]) -> int:
    """Fetch the given segment keys concurrently into the cache.

    Bounded 6-worker pool: wall time ~one segment instead of N serial
    round-trips. Fetch-on-demand serves any segment not yet warm, so an
    interrupted prefetch never breaks playback. Returns segments fetched.
    """
    fetched = 0
    lock = threading.Lock()

    def _fetch(key: Tuple[str, int]) -> None:
        nonlocal fetched
        with _LOCK:
            if key in meta["cache"]:
                return
            remote = meta["segments"].get(key)
        if not remote:
            return
        try:
            data = _http_get(remote)
            with _LOCK:
                _store_segment(meta, key[0], key[1], data)
                with lock:
                    fetched += 1
        except Exception:
            logger.debug("segment %s prefetch failed for %s", key[1], video_id)

    workers = min(6, max(1, len(keys)))
    it = iter(keys)
    running: List[threading.Thread] = []
    while True:
        running = [t for t in running if t.is_alive()]
        while len(running) < workers:
            try:
                key = next(it)
            except StopIteration:
                break
            t = threading.Thread(target=_fetch, args=(key,), daemon=True)
            t.start()
            running.append(t)
        if not running:
            break
        running[0].join(0.2)
    return fetched


def _vkey(remote_url: str) -> str:
    """Stable short key for a remote media-playlist URL."""
    return urllib.parse.quote(remote_url, safe="")


def _store_segment(meta: Dict[str, Any], vkey: str, n: int, data: bytes) -> None:
    # caller holds _LOCK
    key = (vkey, n)
    if key in meta["cache"]:
        return
    meta["cache"][key] = data
    meta["cached_bytes"] += len(data)
    # Bound the cache: drop the oldest (lowest-n) entries.
    while meta["cached_bytes"] > SEG_CACHE_LIMIT and len(meta["cache"]) > 1:
        oldest = min(meta["cache"].keys(), key=lambda k: k[1])
        meta["cached_bytes"] -= len(meta["cache"].pop(oldest))


# ---------------------------------------------------------------- public API

def preload(video_id: str, info: Dict[str, Any]) -> None:
    """Background-download the first ~60s of the next queue item's stream."""
    url = info.get("playable_url") or ""
    if not url:
        return
    stype = info.get("stream_type")
    with _LOCK:
        existing = _ITEMS.get(video_id)
        if existing and existing.get("state") == "ready":
            return
        if existing and existing.get("state") == "loading":
            return

    if stype == "progressive":
        _preload_progressive(video_id, info)
    elif stype == "hls_master":
        # url is our localhost master; rewrite its media-playlist URLs to
        # local ones and preload the first ~60s of segments behind them.
        try:
            from .manifest_server import fetch_manifest
            master_body = fetch_manifest(url)
            rewritten, urls = rewrite_master(master_body, video_id)
            if urls:
                preload_hls(video_id, urls)  # creates the meta entry
                with _LOCK:
                    m = _ITEMS.get(video_id)
                    if m and m.get("kind") == "hls":
                        m["master_body"] = rewritten
        except Exception:
            logger.debug("hls master preload setup failed", exc_info=True)
    elif stype == "hls":
        preload_hls(video_id, [url])
    # dash: not preloaded (adaptive segment URLs need IA-side selection)


def proxy_url(video_id: str, info: Dict[str, Any]) -> Optional[str]:
    """Local URL when a usable preload exists for this stream."""
    with _LOCK:
        meta = _ITEMS.get(video_id)
        if not meta or meta.get("state") != "ready":
            return None
        if meta["kind"] == "progressive":
            if meta["url"] != (info.get("playable_url") or "") or meta["size"] < 1024:
                return None
        # kind == "hls": playlist bodies already rewritten to local URLs
    from .manifest_server import server_url_for
    return server_url_for(f"preload/{video_id}/master.m3u8")


# ------------------------------------------------------- manifest server glue

def handle_request(handler, path: str) -> None:
    """Serve /preload/<vid>/... requests. path excludes the 'preload/' prefix."""
    parts = path.split("/")
    if len(parts) < 2:
        handler.send_error(404, "Not Found")
        return
    video_id = parts[0]

    with _LOCK:
        meta = _ITEMS.get(video_id)
    if not meta:
        handler.send_error(404, "Not Found")
        return

    if meta["kind"] == "progressive" and len(parts) == 1:
        _serve_progressive(handler, meta)
        return

    if meta["kind"] == "hls":
        rest = "/".join(parts[1:])
        if rest == "master.m3u8":
            _serve_hls_master(handler, meta)
            return
        # rest = <vkey>/<n>  (segment) or <vkey> (media playlist);
        # vkey is already the percent-encoded form used as dict key.
        seg_parts = rest.split("/")
        vkey = seg_parts[0]
        if len(seg_parts) == 1:
            _serve_hls_playlist(handler, meta, vkey)
            return
        if len(seg_parts) == 2 and seg_parts[1].isdigit():
            _serve_hls_segment(handler, meta, vkey, int(seg_parts[1]))
            return

    handler.send_error(404, "Not Found")


def _send(handler, code: int, headers: Dict[str, str], body: Optional[bytes] = None) -> None:
    # HTTP/1.1 keep-alive server: every response needs an exact
    # Content-Length (or an explicit Connection: close), otherwise the
    # client hangs waiting for more bytes on the persistent connection.
    if body is not None and "Content-Length" not in headers:
        headers = dict(headers)
        headers["Content-Length"] = str(len(body))
    handler.send_response(code)
    for k, v in headers.items():
        handler.send_header(k, v)
    handler.end_headers()
    if body is not None:
        handler.wfile.write(body)


def _serve_hls_master(handler, meta: Dict[str, Any]) -> None:
    """Master playlist with media-playlist URLs rewritten to local ones."""
    with _LOCK:
        body = meta.get("master_body")
    if not body:
        body = "#EXTM3U\n" + "\n".join(
            f"/preload/{meta['id']}/{vk}" for vk in meta.get("master_lines", [])
        ) + "\n"
    _send(handler, 200, {"Content-Type": "application/vnd.apple.mpegurl"},
          body.encode("utf-8"))


def _serve_hls_playlist(handler, meta: Dict[str, Any], vkey: str) -> None:
    with _LOCK:
        body = meta["playlist_body"].get(vkey)
    if body is None:
        handler.send_error(404, "Not Found")
        return
    _send(handler, 200, {"Content-Type": "application/vnd.apple.mpegurl"},
          body.encode("utf-8"))


def _serve_hls_segment(handler, meta: Dict[str, Any], vkey: str, n: int) -> None:
    with _LOCK:
        data = meta["cache"].get((vkey, n))
        remote = meta["segments"].get((vkey, n))
    if data is not None:
        _send(handler, 200, {"Content-Type": "video/mp4"}, data)
        return
    if remote is None:
        handler.send_error(404, "Not Found")
        return
    try:
        data = _http_get(remote, timeout=30.0)
    except Exception:
        handler.send_error(502, "Upstream fetch failed")
        return
    with _LOCK:
        _store_segment(meta, vkey, n, data)
    _send(handler, 200, {"Content-Type": "video/mp4"}, data)


# ------------------------------------------------------- progressive serving

def _parse_range(header: Optional[str]) -> Optional[Tuple[int, Optional[int]]]:
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    if not a.isdigit():
        return None
    return int(a), (int(b) if b.isdigit() else None)


def _proxy_remote(handler, url: str, range_header: str, ctype: str) -> None:
    headers = {"Connection": "close"}  # streamed body, no length known up front
    try:
        with urllib.request.urlopen(_request(url, range_header), timeout=30.0) as resp:
            hdrs = {"Content-Type": resp.headers.get("Content-Type", ctype),
                    "Accept-Ranges": "bytes", "Connection": "close"}
            hdrs.update(headers)
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


def _serve_progressive(handler, meta: Dict[str, Any]) -> None:
    if not os.path.isfile(meta["path"]):
        handler.send_error(404, "Not Found")
        return
    size = os.path.getsize(meta["path"])
    total = meta["total"]
    ctype = str(meta["ctype"])
    url = str(meta["url"])
    range_header = handler.headers.get("Range")

    parsed = _parse_range(range_header)
    if parsed is None:
        if range_header:
            _proxy_remote(handler, url, range_header, ctype)
            return
        parsed = (0, None)

    start, end = parsed
    if end is not None and total is not None:
        end = min(end, total - 1)

    if start >= size:
        rh = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        _proxy_remote(handler, url, rh, ctype)
        return

    disk_end = size - 1 if end is None else min(end, size - 1)
    real_end = end if end is not None else ((total - 1) if total is not None else None)
    hdrs = {"Content-Type": ctype, "Accept-Ranges": "bytes"}
    if total is not None and real_end is not None:
        hdrs["Content-Range"] = f"bytes {start}-{real_end}/{total}"
        hdrs["Content-Length"] = str(real_end - start + 1)
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
        pass
    except Exception:
        logger.debug("Prefix serve failed", exc_info=True)
