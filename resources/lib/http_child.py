#!/usr/bin/env python3
"""Out-of-process HTTP front end for the cast receiver.

Kodi STATs the item it just stopped (``DoWork - Saving file state``) and blocks
its own player-stop handling on the answer. That STAT goes to the addon's
localhost HTTP server — and the addon's Python lives inside kodi.bin, sharing one
interpreter with ~a dozen other addons (jellycon websocket reconnects,
themoviedb.helper, embuary, skinshortcuts, service.libreelec.settings, ...).
When any of them stalls the interpreter, the in-process server stops answering,
the STAT times out (20s x2 observed on device), OnPlayBackStopped/PlaybackCleanup
is delayed with it, the next queue item never starts and a stopped video's last
frame stays on screen.

So the port Kodi talks to is owned by THIS process, started with the system
python3 and fed by the parent over stdin. It answers manifests from its own
memory (instant, independent of kodi.bin) and forwards the slow, dynamic
endpoints (/resolve, /preload) to the parent's in-process server.

Usage: python3 http_child.py <parent_port>
       stdin: one JSON object per line, {"name": "...", "body": "..."}
       stdout: "PORT <n>" once bound
"""
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [http_child] %(message)s",
                    stream=sys.stderr)
logger = logging.getLogger("http_child")

PARENT_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 0
MANIFESTS = {}
LOCK = threading.Lock()


def _stdin_reader():
    """Read {"name","body"} lines from the parent and serve them instantly."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        name = msg.get("name")
        if not name:
            continue
        with LOCK:
            MANIFESTS.pop(name, None)
            while len(MANIFESTS) >= 50:
                MANIFESTS.pop(next(iter(MANIFESTS)))
            MANIFESTS[name] = msg.get("body", "")
    logger.info("parent closed; exiting")
    sys.exit(0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.debug(fmt % args)

    def _forward(self):
        """Proxy /resolve, /preload, ... to the parent's in-process server."""
        url = f"http://127.0.0.1:{PARENT_PORT}{self.path}"
        try:
            req = urllib.request.Request(url, method=self.command)
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                self.send_response(resp.status)
                ctype = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except Exception as e:
            logger.warning("forward %s failed: %s", self.path, e)
            self.send_response(504)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    def _local(self):
        name = self.path.lstrip("/").split("?")[0]
        if name.startswith("preload/") or name.startswith("resolve/"):
            self._forward()
            return
        started = time.monotonic()
        if self.command == "HEAD":
            logger.info("HEAD %s", self.path)
        with LOCK:
            body = MANIFESTS.get(name)
        self.close_connection = True
        if body is None:
            self.send_error(404, "Not Found")
        else:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        elapsed = time.monotonic() - started
        if elapsed > 2.0:
            logger.warning("slow %s %s took %.1fs", self.command, self.path, elapsed)

    def do_GET(self):   # noqa: N802
        self._local()

    def do_HEAD(self):  # noqa: N802
        self._local()


def main():
    threading.Thread(target=_stdin_reader, daemon=True, name="ParentFeed").start()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.request_queue_size = 32
    port = httpd.server_address[1]
    print(f"PORT {port}", flush=True)
    logger.info("listening on 127.0.0.1:%s (parent 127.0.0.1:%s)", port, PARENT_PORT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
