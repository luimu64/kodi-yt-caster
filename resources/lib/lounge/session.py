"""YouTube Lounge session management and status reporting."""

from __future__ import annotations

import http.client
import json
import logging
import queue
import random
import re
import socket
import string
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from .client import BASE_URL, DEFAULT_HEADERS, LoungeError, LoungeTokenExpiredError

logger = logging.getLogger("ytlounge.session")
CMD_PATTERN = re.compile(r"\[(?P<code>\d+),\[\"(?P<cmd>.+?)\"(?:,(?P<data>.*?))?\]\]")


def parse_frames(body: str) -> Tuple[List[Tuple[int, str, Any]], int]:
    """Parse length-delimited frames from /bc/bind responses.

    Returns (commands, consumed) where `consumed` is the number of bytes safely
    processed. Callers MUST keep body[consumed:] buffered — it may contain the
    beginning of a not-yet-complete frame whose remainder arrives in a later
    chunk.
    """
    commands: List[Tuple[int, str, Any]] = []
    pos = 0
    consumed = 0
    while pos < len(body):
        newline = body.find("\n", pos)
        if newline == -1:
            break
        length_str = body[pos:newline].strip()
        if not length_str:
            pos = newline + 1
            consumed = pos
            continue
        try:
            length = int(length_str)
        except ValueError:
            break
        start = newline + 1
        if start + length > len(body):
            # Incomplete frame: wait for the rest in a later chunk.
            break
        payload = body[start : start + length]
        try:
            items = json.loads(payload)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, list) and len(item) >= 2:
                        idx = item[0]
                        action = item[1]
                        if isinstance(action, list) and action:
                            cmd_name = action[0]
                            cmd_data = action[1] if len(action) > 1 else None
                            commands.append((idx, cmd_name, cmd_data))
        except Exception:
            pass
        pos = start + length
        consumed = pos

    if not commands:
        for match in CMD_PATTERN.finditer(body[:consumed] if consumed else body):
            code = int(match.group("code"))
            name = match.group("cmd")
            raw_data = match.group("data")
            data = None
            if raw_data:
                try:
                    data = json.loads(raw_data)
                except Exception:
                    data = raw_data
            commands.append((code, name, data))

    return commands, consumed


class LoungeSession:
    def __init__(
        self,
        screen_id: str,
        lounge_token: str,
        device_id: str,
        screen_name: str = "Kodi",
        theme: str = "cl",
    ):
        self.screen_id = screen_id
        self.lounge_token = lounge_token
        self.device_id = device_id
        self.screen_name = screen_name
        self.theme = theme
        self.sid: Optional[str] = None
        self.gsessionid: Optional[str] = None
        self.ofs = 0
        self.last_code = -1
        # Async reporting: posts to /bc/bind open a fresh TLS connection each
        # time and can take seconds. Doing them on the listener thread stalls
        # command delivery (seek/pause lag by seconds); doing them on the
        # position loop drifts its cadence. So report_* enqueue and a single
        # worker thread posts, coalescing repeated reports (last wins per tag)
        # so a slow network cannot build an unbounded backlog of stale states.
        self._post_queue: "queue.Queue[Optional[Tuple[str, str, Dict[str, Any]]]]" = queue.Queue()
        self._ofs_lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None
        self._conn_host: Optional[str] = None
        self._conn_scheme: Optional[str] = None
        self._post_worker = threading.Thread(
            target=self._post_worker_loop, daemon=True, name=f"LoungePoster-{theme}"
        )
        self._post_worker.start()

    def _random_zx(self) -> str:
        return "".join(random.choices(string.ascii_letters + string.digits, k=12))

    def _base_params(self) -> Dict[str, str]:
        return {
            "device": "LOUNGE_SCREEN",
            "id": self.device_id,
            "name": self.screen_name,
            "app": "kodi-ytcast",
            "theme": self.theme,
            "capabilities": "dsp,mic,dpa,ntb,que,mus",
            "mdx-version": "2",
            "loungeIdToken": self.lounge_token,
            "VER": "8",
            "v": "2",
            "t": "1",
        }

    def handshake(self) -> Tuple[str, str]:
        """Perform initial bind handshake to obtain SID and gsessionid."""
        params = self._base_params()
        params.update({
            "RID": "1337",
            "AID": "42",
            "zx": self._random_zx(),
            "CVER": "1",
        })
        url = f"{BASE_URL}/bc/bind?{urllib.parse.urlencode(params)}"
        data = urllib.parse.urlencode({"count": "0"}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=DEFAULT_HEADERS)

        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise LoungeTokenExpiredError(f"Session token invalid: HTTP {e.code}") from e
            raise LoungeError(f"Handshake failed: HTTP {e.code}") from e
        except Exception as e:
            raise LoungeError(f"Handshake network error: {e}") from e

        for _, name, data_val in parse_frames(body)[0]:
            if name == "c":
                self.sid = str(data_val)
            elif name == "S":
                self.gsessionid = str(data_val)

        if not self.sid or not self.gsessionid:
            raise LoungeError("Failed to extract SID/gsessionid from handshake")

        return self.sid, self.gsessionid

    def _post_worker_loop(self) -> None:
        while True:
            item = self._post_queue.get()
            if item is None:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                return
            tag, sc, data = item
            # Coalesce: if newer reports with the same tag are already queued,
            # take the latest one and drop the stale ones.
            # nowPlaying is EXEMPT: it carries the videoId, and dropping it
            # loses the "this song started" signal the phone needs after a
            # TV-side queue pick.
            if sc != "nowPlaying":
                pending: list = []
                while True:
                    try:
                        nxt = self._post_queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        pending.append(None)
                        break
                    if nxt[0] == tag:
                        item = nxt
                        tag, sc, data = item
                    else:
                        pending.append(nxt)
                for p in pending:
                    self._post_queue.put(p)
            try:
                self._do_post(sc, data)
            except Exception:
                logger.debug("Async post %s failed", sc, exc_info=True)

    def post_action(self, sc: str, data: Dict[str, Any]) -> None:
        """Enqueue a player/device state report; posted asynchronously by the
        session's worker so callers (listener thread, position loop) never block."""
        if not self.sid:
            return
        self._post_queue.put((sc, sc, data))

    def _do_post(self, sc: str, data: Dict[str, Any]) -> None:
        if not self.sid:
            return

        with self._ofs_lock:
            self.ofs += 1
            ofs = self.ofs
        post_data = {
            "count": "1",
            "ofs": str(ofs),
            "req0__sc": sc,
        }
        for k, v in data.items():
            post_data[f"req0_{k}"] = str(v)

        params = self._base_params()
        params.update({
            "RID": "1337",
            "AID": "42",
            "SID": self.sid,
            "zx": self._random_zx(),
        })
        if self.gsessionid:
            params["gsessionid"] = self.gsessionid

        parsed = urllib.parse.urlparse(BASE_URL)
        host = parsed.netloc
        scheme = parsed.scheme or "https"
        path = f"{parsed.path}/bc/bind?{urllib.parse.urlencode(params)}"
        encoded = urllib.parse.urlencode(post_data).encode("utf-8")
        headers = dict(DEFAULT_HEADERS)
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
        headers["Content-Length"] = str(len(encoded))

        def _get_conn() -> http.client.HTTPConnection:
            if self._conn is not None and self._conn_host == host and self._conn_scheme == scheme:
                return self._conn
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            if scheme == "https":
                self._conn = http.client.HTTPSConnection(host, timeout=10.0)
            else:
                self._conn = http.client.HTTPConnection(host, timeout=10.0)
            self._conn_host = host
            self._conn_scheme = scheme
            return self._conn

        try:
            conn = _get_conn()
            try:
                conn.request("POST", path, body=encoded, headers=headers)
                resp = conn.getresponse()
                resp.read()
            except (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                    ConnectionResetError, BrokenPipeError, socket.error):
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                conn = _get_conn()
                conn.request("POST", path, body=encoded, headers=headers)
                resp = conn.getresponse()
                resp.read()
        except Exception as e:
            # INFO-level: post failures break the phone-side session
            # (remote never sees our state) and must be visible in kodi.log.
            logger.info("Failed to post action %s: %s (ofs=%s)", sc, e, ofs)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def report_now_playing(
        self,
        video_id: str,
        current_time: int,
        duration: int,
        state: int,
        current_index: Optional[int] = None,
        list_id: str = "",
    ) -> None:
        """Report now playing status to Lounge (state: 1=playing, 2=paused, 0=stopped)."""
        dur = max(0, int(duration or 0))
        cur = max(0, int(current_time or 0))
        payload = {
            "videoId": video_id,
            "currentTime": str(cur),
            "duration": str(dur),
            "state": str(state),
            "cpn": "kodi",
        }
        if dur > 0:
            payload["seekableStartTime"] = "0"
            payload["seekableEndTime"] = str(dur)
            payload["loadedTime"] = str(dur if state == 1 else cur)
        if current_index is not None and current_index >= 0:
            payload["currentIndex"] = str(current_index)
        if list_id:
            payload["listId"] = str(list_id)

        logger.info(
            "REPORT nowPlaying vid=%s idx=%s t=%s dur=%s state=%s listId=%s",
            video_id, payload.get("currentIndex"), cur, dur, state, payload.get("listId") or "-",
        )
        self.post_action("nowPlaying", payload)

    def report_now_playing_playlist(
        self,
        video_ids: List[str],
        current_video_id: str,
        current_index: int,
        current_time: int,
        duration: int,
        state: int,
        list_id: str = "",
    ) -> None:
        """Report now playing playlist status to Lounge."""
        dur = max(0, int(duration or 0))
        cur = max(0, int(current_time or 0))
        payload = {
            "videoIds": ",".join(video_ids) if video_ids else current_video_id,
            "videoId": current_video_id,
            "currentIndex": str(max(0, current_index)),
            "currentTime": str(cur),
            "duration": str(dur),
            "state": str(state),
        }
        if dur > 0:
            payload["seekableStartTime"] = "0"
            payload["seekableEndTime"] = str(dur)
        if list_id:
            payload["listId"] = str(list_id)

        self.post_action("nowPlayingPlaylist", payload)

    def report_state_change(self, state: int, current_time: int, duration: int) -> None:
        """Report state change (1=playing, 2=paused, 0=stopped)."""
        dur = max(0, int(duration or 0))
        cur = max(0, int(current_time or 0))
        payload = {
            "state": str(state),
            "currentTime": str(cur),
            "duration": str(dur),
            "cpn": "kodi",
        }
        if dur > 0:
            payload["seekableStartTime"] = "0"
            payload["seekableEndTime"] = str(dur)
            payload["loadedTime"] = str(dur if state == 1 else cur)

        self.post_action("onStateChange", payload)

    def report_volume(self, volume: int, muted: bool = False) -> None:
        """Report volume level (0-100) and mute status."""
        self.post_action("onVolumeChanged", {
            "volume": str(volume),
            "muted": "true" if muted else "false",
        })
