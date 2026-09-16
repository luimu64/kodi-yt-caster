"""Tiny localhost HTTP server that serves generated HLS master manifests to Kodi.

Why this exists: YouTube now serves split-rendition HLS (detached EXT-X-MEDIA audio). Kodi's native
demuxer plays such a master video-only (no sound), and inputstream.adaptive refuses local file paths.
Serving the master over http://127.0.0.1:<port>/ gives IA a real URL, so it merges the detached audio
playlist and playback carries sound.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

logger = logging.getLogger("ytlounge.manifest_server")

_MANIFESTS: Dict[str, str] = {}
_LOCK = threading.Lock()
_SERVER: Optional["ManifestServer"] = None

# Out-of-process front end (http_child.py): the port Kodi talks to. Kodi STATs
# the stopped item against it and blocks its player-stop handling on the answer,
# while this addon's Python shares kodi.bin's interpreter with ~a dozen other
# addons — a stall over there (observed 39s) stops an in-process server from
# answering and freezes playback state. See http_child.py.
_CHILD_PROC = None
_CHILD_PORT: Optional[int] = None
_CHILD_LOCK = threading.Lock()
_SYSTEM_PYTHON = "/usr/bin/python3"


def _ensure_server():
    """Start the in-process server (the child forwards dynamic paths to it)."""
    global _SERVER
    if _SERVER is None:
        _SERVER = ManifestServer()
        _SERVER.start()
        logger.info("Manifest server listening on port %s", _SERVER.port)
    return _SERVER


def start_child() -> Optional[int]:
    """Start the out-of-process HTTP front end; None if it could not start."""
    global _CHILD_PROC, _CHILD_PORT
    with _CHILD_LOCK:
        if _CHILD_PORT:
            return _CHILD_PORT
        python = _SYSTEM_PYTHON if os.path.exists(_SYSTEM_PYTHON) else sys.executable
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "http_child.py")
        try:
            parent_port = _ensure_server().port
            proc = subprocess.Popen(
                [python, script, str(parent_port)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
            line = proc.stdout.readline()
            port = int(line.split()[1])
            _CHILD_PROC, _CHILD_PORT = proc, port
            logger.info("Out-of-process HTTP front end on port %s", port)
            with _LOCK:
                for name, body in list(_MANIFESTS.items()):
                    _push_to_child(name, body)
        except Exception:
            logger.warning("HTTP front end failed to start; serving in-process",
                           exc_info=True)
            _CHILD_PROC, _CHILD_PORT = None, None
        return _CHILD_PORT


def _push_to_child(name: str, body: str) -> None:
    if _CHILD_PROC is None or _CHILD_PROC.stdin is None:
        return
    try:
        _CHILD_PROC.stdin.write(json.dumps({"name": name, "body": body}) + "\n")
        _CHILD_PROC.stdin.flush()
    except Exception:
        logger.warning("HTTP front end gone; claiming in-process", exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive: Kodi's demuxer fetches dozens of segments in
    # quick succession; with the 1.0 default every segment pays a fresh
    # TCP connect + accept on the Python (GIL-bound) server thread.
    # Every response below carries an exact Content-Length to keep the
    # persistent connection well-formed.
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # keep Kodi's log clean
        logger.debug("%s - %s", self.client_address[0], format % args)

    def _manifest_body(self):
        name = self.path.lstrip("/").split("?")[0]
        with _LOCK:
            return _MANIFESTS.get(name)

    def do_HEAD(self) -> None:  # noqa: N802 (http.server API)
        """Answer HEAD instantly — this is what Kodi STATs the playing file with.

        Kodi's ``DoWork - Saving file state`` STATs the item it just stopped, and
        that call runs *before* the player's stop is processed: a slow answer
        (the service process is GIL-bound while resolving/preloading) delays
        ``OnPlayBackStopped``/``PlaybackCleanup`` for the whole curl timeout —
        which is what left a stopped video's last frame on screen and kept the
        next (audio) item from ever starting. Answer with headers only, no body
        work, and record anything slow enough to matter.
        """
        started = time.monotonic()
        name = self.path.lstrip("/").split("?")[0]
        if name.startswith("preload/") or name.startswith("resolve/"):
            # Dynamic endpoints: answer 200 with no length (STAT only needs the
            # headers; never trigger the fetch work for a HEAD).
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            body = self._manifest_body()
            if body is None:
                self.send_error(404, "Not Found")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.end_headers()
        elapsed = time.monotonic() - started
        if elapsed > 2.0:
            logger.warning("slow HEAD %s took %.1fs", self.path, elapsed)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        started = time.monotonic()
        name = self.path.lstrip("/").split("?")[0]
        if name.startswith("preload/"):
            from . import preloader
            preloader.handle_request(self, name[len("preload/"):])
            self._log_if_slow(started)
            return
        if name.startswith("resolve/"):
            _handle_resolve(self, name[len("resolve/"):])
            self._log_if_slow(started)
            return
        with _LOCK:
            body = _MANIFESTS.get(name)
        if body is None:
            self.send_error(404, "Not Found")
            self._log_if_slow(started)
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self._log_if_slow(started)

    def _log_if_slow(self, started: float) -> None:
        # The demuxer and Kodi's post-playback STAT share this server with the
        # resolver/preloader threads: a request that takes seconds means the
        # service process is starved and playback start/stop will stall too.
        elapsed = time.monotonic() - started
        if elapsed > 2.0:
            logger.warning("slow GET %s took %.1fs", self.path, elapsed)


class ManifestServer:
    def __init__(self, port: int = 0):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        # A demuxer opens several connections at once and Kodi STATs the file
        # right after playback stops; the default backlog of 5 can refuse them.
        self._httpd.request_queue_size = 32
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


def _public_port() -> Optional[int]:
    """Port Kodi (and the plugin) should talk to: the front end, else in-process."""
    if _CHILD_PORT:
        return _CHILD_PORT
    return _ensure_server().port


def server_url_for(name: str) -> str:
    """Public helper: URL for a name without publishing manifest content."""
    global _SERVER
    if _SERVER is None:
        publish("__warm__", "")  # boot the servers
        with _LOCK:
            _MANIFESTS.pop("__warm__", None)
    if _CHILD_PORT is None:
        start_child()   # the out-of-process front end owns the public port
    return f"http://127.0.0.1:{_public_port() or _SERVER.port}/{name}"


def publish(name: str, body: str) -> str:
    """Store a manifest and return its http URL, starting the server on first use."""
    _ensure_server()
    if _CHILD_PORT is None:
        start_child()
    with _LOCK:
        _MANIFESTS.pop(name, None)
        while len(_MANIFESTS) >= 50:
            _MANIFESTS.pop(next(iter(_MANIFESTS)))
        _MANIFESTS[name] = body
    _push_to_child(name, body)
    return f"http://127.0.0.1:{_public_port()}/{name}"


def server_port() -> Optional[int]:
    """Port that serves manifests (the out-of-process front end when it is up)."""
    if _CHILD_PORT:
        return _CHILD_PORT
    return _SERVER.port if _SERVER is not None else None
