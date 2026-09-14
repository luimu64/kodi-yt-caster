"""Mock YouTube Lounge backend on 127.0.0.1.

Endpoints:
  GET  /pairing/generate_screen_id
  POST /pairing/get_lounge_token_batch
  POST /pairing/get_pairing_code
  POST /pairing/register_pairing_code
  POST /bc/bind    handshake (RID=1337) / report posts (SID present, no TYPE/RID=rpc)
  GET  /bc/bind    long-poll (RID=rpc) — stays open, delivers queued commands

Frame encoding is the exact inverse of the addon's parse_frames (checked at
import time against the real parser).
"""
import json
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from resources.lib.lounge.session import parse_frames  # repo root is on sys.path via emulator/__init__

from .frames import encode_frame, _normalized


def _roundtrip_check():
    """Every frame this server emits must parse via the addon's own parser."""
    samples = [
        [[0, ["c", "SID123"]], [1, ["S", "GSID456"]]],
        [[5, ["setPlaylist", {"videoId": "v1", "videoIds": "v1,v2", "currentTime": "12"}]]],
        [[7, ["pause"]]],
        [[9, ["remoteConnected", {"id": 1, "name": "Pixel 8", "type": "phone"}]]],
    ]
    for s in samples:
        cmds, _ = parse_frames(encode_frame(s).decode())
        assert [(c[0], c[1], c[2]) for c in cmds] == _normalized(s), s


_roundtrip_check()


class _Session:
    def __init__(self, sid, screen_id=None):
        self.sid = sid
        self.screen_id = screen_id
        self.gsessionid = None
        self.commands = queue.Queue()   # (cmd, data) pairs; codes assigned per session
        self.wake = threading.Event()   # set on stop() so long-poll threads exit
        self.next_code = 0
        self.polled = False             # a long-poll is actively reading this session
        self.killed = False             # server-side session loss -> poll must exit


class MockLoungeServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/api/lounge"
        self._screen_counter = [0]
        self._sid_counter = [0]
        self._lock = threading.Lock()
        self.sessions = {}          # sid -> _Session
        self.reported_screens = set()
        self.REPORTS = []           # dicts: {sc, params..., sid}
        self.PAIRING_CALLS = []     # (endpoint, decoded-body-or-None)
        self.registered_codes = []
        self.tokens = {}            # screen_id -> token
        self.expire_next_bind = threading.Event()   # 400 "token" once
        self.bad_screen_ids = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="MockLounge")

    # --- public API ---------------------------------------------------------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._lock:
            for sess in self.sessions.values():
                sess.wake.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self):
        return self.base_url

    def queue_command(self, item, sids=None):
        """Queue ONE command pair (cmd, data) to bound sessions.

        The relay assigns a monotonically increasing code per delivery —
        receivers drop anything <= last seen code, so codes are stamped here
        per session (never by the sender).
        """
        with self._lock:
            if sids is None:
                # A real sender drives ONE lounge: the first actively-polled
                # session. Delivering to both lounges double-fires every
                # command (cl + m listeners) and races the bridge dedup.
                polled = [sid for sid, sess in self.sessions.items() if sess.polled]
                targets = polled[:1]
            else:
                targets = list(sids)
            for sid in targets:
                sess = self.sessions.get(sid)
                if sess:
                    sess.next_code += 1
                    sess.commands.put([sess.next_code, item])

    def expire_token(self, screen_id):
        """Make binds for this screen fail with 400 'token' AND break any
        active long-poll for it, forcing the listener into its rebind path."""
        self.bad_screen_ids.add(screen_id)
        with self._lock:
            for sess in self.sessions.values():
                if sess.screen_id == screen_id:
                    sess.killed = True

    def find_session_by_screen(self, screen_id):
        with self._lock:
            for sess in self.sessions.values():
                if sess.screen_id == screen_id:
                    return sess
        return None

    # --- internals ----------------------------------------------------------
    def _new_screen_id(self):
        self._screen_counter[0] += 1
        return f"SCR{'0'*10}{self._screen_counter[0]:05d}"[-26:]

    def _new_sid(self):
        self._sid_counter[0] += 1
        return f"sid-{self._sid_counter[0]}"

    # --- report assertions surface ------------------------------------------
    def reports(self, sc=None):
        return [r for r in self.REPORTS if sc is None or r["sc"] == sc]

    def wait_for_report(self, sc=None, pred=None, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for r in self.reports(sc):
                if pred is None or pred(r):
                    return r
            time.sleep(0.02)
        return None

    def clear_reports(self):
        self.REPORTS.clear()


def _make_handler(server: MockLoungeServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MockLounge/1.0"

        def log_message(self, *a):
            pass

        # ---- helpers ----
        def _read_body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(n).decode("utf-8") if n else ""

        def _send(self, code, body, ctype="text/plain"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        # ---- pairing endpoints ----
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path.endswith("/pairing/generate_screen_id"):
                with server._lock:
                    sid = server._new_screen_id()
                self._send(200, sid)
                return
            if path.endswith("/bc/bind"):
                self._bind(parsed)
                return
            self._send(404, "not found")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self._read_body()
            form = dict(urllib.parse.parse_qsl(body)) if body else {}

            if path.endswith("/pairing/get_lounge_token_batch"):
                screens = form.get("screen_ids", "")
                out = []
                for sid in screens.split("|"):
                    if not sid:
                        continue
                    token = f"tok-{sid}"
                    with server._lock:
                        server.tokens[sid] = token
                    out.append({"loungeToken": token, "expiration": int(time.time() * 1000) + 7 * 86400_000})
                self._send_json(200, {"screens": out})
                return

            if path.endswith("/pairing/get_pairing_code"):
                code = f"{int(time.time() * 1000) % 10**12:012d}"
                server.PAIRING_CALLS.append(("get_pairing_code", dict(form)))
                self._send(200, code)
                return

            if path.endswith("/pairing/register_pairing_code"):
                server.PAIRING_CALLS.append(("register_pairing_code", dict(form)))
                server.registered_codes.append(form.get("pairing_code", ""))
                self._send(200, "OK")
                return

            if path.endswith("/bc/bind"):
                self._bind(parsed, post_body=body)
                return

            self._send(404, "not found")

        # ---- /bc/bind ----
        def _bind(self, parsed, post_body=""):
            q = dict(urllib.parse.parse_qsl(parsed.query))
            token = q.get("loungeIdToken", "")
            screen = next((s for s, t in server.tokens.items() if t == token), None) if token else None

            expire_now = server.expire_next_bind.is_set()
            if expire_now:
                server.expire_next_bind.clear()
            if expire_now or screen in server.bad_screen_ids:
                if screen is not None:
                    server.bad_screen_ids.discard(screen)
                self._send(400, json.dumps({"error": "invalid lounge_token"}), "application/json")
                return

            if q.get("RID") == "rpc":
                self._longpoll(q)
                return

            # handshake or report post (both POST; report carries count/ofs/req0__sc)
            if post_body:
                form = dict(urllib.parse.parse_qsl(post_body))
                if "req0__sc" in form:
                    report = {"sc": form.pop("req0__sc"), "sid": q.get("SID", "")}
                    report.update({k[len("req0_"):]: v for k, v in form.items()
                                   if k.startswith("req0_")})
                    report["ofs"] = form.get("ofs")
                    server.REPORTS.append(report)
                    self._send(200, "")
                    return

            # handshake
            with server._lock:
                sid = server._new_sid()
                sess = _Session(sid, screen_id=screen)
                server.sessions[sid] = sess
            gsid = f"g-{sid}"
            sess.gsessionid = gsid
            body = encode_frame([[0, ["c", sid, "", 8]], [1, ["S", gsid]]])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _longpoll(self, q):
            sid = q.get("SID", "")
            with server._lock:
                sess = server.sessions.get(sid)
            if sess is None:
                self._send(400, "unknown session")
                return
            sess.polled = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            # close-delimited streaming (no Content-Length): read1() on the
            # client returns each write as it arrives; the socket stays open
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def chunk(data: bytes):
                self.wfile.write(data)
                self.wfile.flush()

            while not server._stop.is_set() and not sess.wake.is_set() and not sess.killed:
                try:
                    items = sess.commands.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    chunk(encode_frame([items]))
                    # noop after each command, mirroring the real relay
                    sess.next_code += 1
                    chunk(encode_frame([[sess.next_code, ["noop"]]]))
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

    return Handler


if __name__ == "__main__":
    srv = MockLoungeServer().start()
    print("mock lounge on", srv.base_url)
    import subprocess, sys
    time.sleep(600)
