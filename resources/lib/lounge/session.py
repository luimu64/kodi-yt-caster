"""YouTube Lounge session management and status reporting."""

from __future__ import annotations

import json
import logging
import random
import re
import string
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from .client import BASE_URL, DEFAULT_HEADERS, LoungeError, LoungeTokenExpiredError

logger = logging.getLogger("ytlounge.session")
CMD_PATTERN = re.compile(r"\[(?P<code>\d+),\[\"(?P<cmd>.+?)\"(?:,(?P<data>.*?))?\]\]")


def parse_frames(body: str) -> List[Tuple[int, str, Any]]:
    """Parse length-delimited frames or fallback regex from /bc/bind responses."""
    commands: List[Tuple[int, str, Any]] = []
    pos = 0
    while pos < len(body):
        newline = body.find("\n", pos)
        if newline == -1:
            break
        length_str = body[pos:newline].strip()
        if not length_str:
            pos = newline + 1
            continue
        try:
            length = int(length_str)
        except ValueError:
            break
        start = newline + 1
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

    if not commands:
        for match in CMD_PATTERN.finditer(body):
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

    return commands


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

        for _, name, data_val in parse_frames(body):
            if name == "c":
                self.sid = str(data_val)
            elif name == "S":
                self.gsessionid = str(data_val)

        if not self.sid or not self.gsessionid:
            raise LoungeError("Failed to extract SID/gsessionid from handshake")

        return self.sid, self.gsessionid

    def post_action(self, sc: str, data: Dict[str, Any]) -> None:
        """Report back player or device state to Lounge via /bc/bind."""
        if not self.sid:
            return

        self.ofs += 1
        post_data = {
            "count": "1",
            "ofs": str(self.ofs),
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
            logger.debug("Failed to post action %s: %s", sc, e)

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
