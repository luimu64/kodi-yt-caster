"""YouTube Lounge session management and status reporting."""

from __future__ import annotations

import json
import logging
import queue
import random
import re
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
                return
            tag, sc, data = item
            # Coalesce: if a newer report with the same tag is already queued,
            # skip this stale one (only the latest position/state matters).
            pending: list = []
            newer = False
            while True:
                try:
                    nxt = self._post_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    pending.append(None)
                    break
                if nxt[0] == tag:
                    newer = True
                    # drop older same-tag item(s) queued before this one
                else:
                    pending.append(nxt)
            for p in pending:
                self._post_queue.put(p)
            if newer:
                continue
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

        url = f"{BASE_URL}/bc/bind?{urllib.parse.urlencode(params)}"
        encoded = urllib.parse.urlencode(post_data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded, headers=DEFAULT_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                resp.read()
        except Exception as e:
            # INFO-level: post failures break the phone-side session
            # (remote never sees our state) and must be visible in kodi.log.
            logger.info("Failed to post action %s: %s (ofs=%s)", sc, e, ofs)

    def report_now_playing(self, video_id: str, current_time: int, duration: int, state: int) -> None:
        """Report now playing status to Lounge (state: 1=playing, 2=paused, 0=stopped)."""
        self.post_action("nowPlaying", {
            "videoId": video_id,
            "currentTime": str(current_time),
            "duration": str(duration),
            "state": str(state),
            "cpn": "kodi",
        })

    def report_state_change(self, state: int, current_time: int, duration: int) -> None:
        """Report state change (1=playing, 2=paused, 0=stopped)."""
        self.post_action("onStateChange", {
            "state": str(state),
            "currentTime": str(current_time),
            "duration": str(duration),
            "cpn": "kodi",
        })

    def report_volume(self, volume: int, muted: bool = False) -> None:
        """Report volume level (0-100) and mute status."""
        self.post_action("onVolumeChanged", {
            "volume": str(volume),
            "muted": "true" if muted else "false",
        })
