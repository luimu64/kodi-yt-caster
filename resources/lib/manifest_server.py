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
