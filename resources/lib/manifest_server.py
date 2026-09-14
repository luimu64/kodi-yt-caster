"""Tiny localhost HTTP server that serves generated HLS master manifests to Kodi.

Why this exists: YouTube now serves split-rendition HLS (detached EXT-X-MEDIA audio). Kodi's native
demuxer plays such a master video-only (no sound), and inputstream.adaptive refuses local file paths.
Serving the master over http://127.0.0.1:<port>/ gives IA a real URL, so it merges the detached audio
playlist and playback carries sound.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

logger = logging.getLogger("ytlounge.manifest_server")

_MANIFESTS: Dict[str, str] = {}
_LOCK = threading.Lock()
_SERVER: Optional["ManifestServer"] = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # keep Kodi's log clean
        logger.debug("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        name = self.path.lstrip("/").split("?")[0]
        if name.startswith("preload/"):
            from . import preloader
            preloader.handle_request(self, name[len("preload/"):])
            return
        if name.startswith("resolve/"):
            _handle_resolve(self, name[len("resolve/"):])
            return
        with _LOCK:
            body = _MANIFESTS.get(name)
        if body is None:
            self.send_error(404, "Not Found")
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


class ManifestServer:
    def __init__(self, port: int = 0):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="ManifestHTTP")

    def start(self) -> None:
        self._thread.start()

    def url_for(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


# Resolver hookup: set by service.py at boot so plugin invocations (separate
# Kodi processes) can resolve video IDs against the service's warm caches
# (yt-dlp resolve cache + preload segment cache) over localhost HTTP.
_RESOLVER = None


def set_resolver(resolver) -> None:
    global _RESOLVER
    _RESOLVER = resolver


def _send_json(handler, code: int, obj) -> None:
    import json
    data = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


def _handle_resolve(handler, video_id: str) -> None:
    """GET /resolve/<video_id> -> stream info JSON from the service resolver."""
    if not video_id or _RESOLVER is None:
        handler.send_error(503 if _RESOLVER is None else 404, "No resolver")
        return
    try:
        info = _RESOLVER.resolve(video_id)
        _send_json(handler, 200, info)
    except Exception as e:
        _send_json(handler, 500, {"error": str(e)})


def fetch_manifest(url: str) -> str:
    """Return the body previously published under this localhost URL."""
    name = url.rsplit("/", 1)[-1]
    if "?" in name:
        name = name.split("?")[0]
    with _LOCK:
        return _MANIFESTS.get(name, "")


def server_url_for(name: str) -> str:
    """Public helper: URL for a name without publishing manifest content."""
    global _SERVER
    if _SERVER is None:
        publish("__warm__", "")  # boot the server
        with _LOCK:
            _MANIFESTS.pop("__warm__", None)
    assert _SERVER is not None
    return _SERVER.url_for(name)


def publish(name: str, body: str) -> str:
    """Store a manifest and return its http URL, starting the server on first use."""
    global _SERVER
    if _SERVER is None:
        _SERVER = ManifestServer()
        _SERVER.start()
        logger.info("Manifest server listening on port %s", _SERVER.port)
    with _LOCK:
        _MANIFESTS[name] = body
    return _SERVER.url_for(name)


def server_port() -> Optional[int]:
    """Port of the running manifest server (None if not started)."""
    return _SERVER.port if _SERVER is not None else None
