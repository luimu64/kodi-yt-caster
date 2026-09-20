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
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    from ..session_state import SessionState, PlayState, StateOwner, _diff_field_groups, published_index
except ImportError:
    from resources.lib.session_state import SessionState, PlayState, StateOwner, _diff_field_groups, published_index

from .client import BASE_URL, DEFAULT_HEADERS, LoungeError, LoungeTokenExpiredError
from .vocabulary import coverage_line

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
        owner: Optional[StateOwner] = None,
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

        # Version-driven state publication (R7)
        self._ofs_lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None
        self._conn_host: Optional[str] = None
        self._conn_scheme: Optional[str] = None

        self._owner: Optional[StateOwner] = owner
        self._last_published: Optional[SessionState] = None
        self._wake_event = threading.Event()
        self._stopped = threading.Event()
        self._heartbeat_interval = 2.0  # <= 1 Hz

        self._publisher_thread = threading.Thread(
            target=self._publisher_loop, daemon=True, name=f"LoungePublisher-{theme}"
        )
        self._post_worker = self._publisher_thread  # backwards compat
        self._publisher_thread.start()
        # R10: declare the outbound vocabulary once per session, so a family we
        # never emit is a logged fact, not a silent default on the phone.
        logger.info(coverage_line())

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
        with self._ofs_lock:
            self.ofs = 0

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

        self._wake_event.set()
        return self.sid, self.gsessionid

    def attach_state_owner(self, owner: StateOwner) -> None:
        """Attach the StateOwner handle to this session."""
        self._owner = owner
        self._wake_event.set()

    def notify_state_changed(self, snapshot: Optional[SessionState] = None) -> None:
        """Wake publication on SessionState.version changes."""
        self._wake_event.set()

    def force_publish(self) -> None:
        """Re-publish the shared snapshot on this channel on the next cycle.

        Marks the channel dirty (R9): the same absolute state is re-sent without
        inventing a second report path. Used by the connect and getNowPlaying
        handshakes, where the phone expects an immediate report but the state
        itself is unchanged.
        """
        self._last_published = None
        self._wake_event.set()

    def flush(self, timeout: float = 1.0) -> None:
        """Wait until any pending dirty state has been published."""
        deadline = time.monotonic() + timeout
        self._wake_event.set()
        while time.monotonic() < deadline:
            if self._owner is None:
                break
            snap = self._owner.snapshot()
            if self._last_published is not None and self._last_published.version >= snap.version:
                break
            time.sleep(0.01)

    def _publisher_loop(self) -> None:
        last_heartbeat = time.monotonic()
        while not self._stopped.is_set():
            try:
                now = time.monotonic()
                time_since_heartbeat = now - last_heartbeat
                remaining_heartbeat = max(0.05, self._heartbeat_interval - time_since_heartbeat)

                self._wake_event.wait(timeout=remaining_heartbeat)
                self._wake_event.clear()

                if self._stopped.is_set():
                    break

                owner = self._owner
                if owner is None:
                    continue

                snapshot = owner.snapshot()

                # Change-driven publication
                is_dirty = (
                    self._last_published is None
                    or snapshot.version != self._last_published.version
                )

                if is_dirty:
                    success = self.publish_snapshot(snapshot, heartbeat=False)
                    if success:
                        last_heartbeat = time.monotonic()
                else:
                    # Heartbeat check
                    now = time.monotonic()
                    if now - last_heartbeat >= self._heartbeat_interval:
                        if self.sid:
                            success = self.publish_snapshot(snapshot, heartbeat=True)
                            if success:
                                last_heartbeat = time.monotonic()
            except Exception as e:
                logger.debug("Publisher loop exception: %s", e, exc_info=True)
                time.sleep(0.1)

        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def publish_snapshot(self, snapshot: SessionState, heartbeat: bool = False) -> bool:
        """Publish state snapshot diffed against the last published state."""
        if not heartbeat:
            old = self._last_published or SessionState(version=-1, volume=-1, play_state=-1)
            changed_groups = _diff_field_groups(old, snapshot)
            if not changed_groups:
                return True

            actions_to_emit: List[Tuple[str, Dict[str, Any]]] = []

            # 1. Identity group
            if "identity" in changed_groups:
                actions_to_emit.append(("nowPlaying", self._build_now_playing(snapshot)))
                if snapshot.playlist:
                    actions_to_emit.append(("nowPlayingPlaylist", self._build_now_playing_playlist(snapshot)))
                # R10: up-next is derivable from the stored queue — emit it when
                # we know the next item, omit it (no guess) when we do not.
                up_next = self._build_up_next(snapshot)
                if up_next is not None:
                    actions_to_emit.append(("autoplayUpNext", up_next))

            # 2. Playback group
            if "playback" in changed_groups:
                if "identity" not in changed_groups:
                    actions_to_emit.append(("nowPlaying", self._build_now_playing(snapshot)))
                should_emit_state_change = (
                    self._last_published is None
                    or self._last_published.play_state != snapshot.play_state
                    or (self._last_published.duration == 0 and snapshot.duration > 0)
                    or (snapshot.play_state == PlayState.PAUSED and self._last_published.position != snapshot.position)
                )
                if should_emit_state_change:
                    actions_to_emit.append(("onStateChange", self._build_state_change(snapshot)))

            # R10: the receiver resolves locally (yt-dlp) and never injects ads,
            # so "no ad" is a known fact, not a guess. Reassert it whenever the
            # item or the playback changes, so a skip control is backed by a
            # real family rather than a phone-side default.
            if "identity" in changed_groups or "playback" in changed_groups:
                actions_to_emit.append(("onAdStateChange", self._build_ad_state()))
                if "identity" in changed_groups:
                    actions_to_emit.append(("onAdPlaying", self._build_ad_state()))

            # 3. Volume group
            if "volume" in changed_groups:
                actions_to_emit.append(("onVolumeChanged", self._build_volume(snapshot)))

            # 4. Lane group: no outbound RPC action

            groups_str = ", ".join(changed_groups)
            action_names = [a[0] for a in actions_to_emit]
            logger.info("PUBLISH v%d %s -> %s", snapshot.version, groups_str, action_names)

            all_ok = True
            for sc, payload in actions_to_emit:
                try:
                    res = self.post_action(sc, payload)
                except TypeError:
                    res = self.post_action(sc, payload)
                if res is False:
                    all_ok = False
                    break

            if all_ok:
                self._last_published = snapshot
                return True
            else:
                return False
        else:
            # Full snapshot heartbeat (<= 1 Hz)
            heartbeat_actions: List[Tuple[str, Dict[str, Any]]] = [
                ("nowPlaying", self._build_now_playing(snapshot)),
            ]
            if snapshot.playlist:
                heartbeat_actions.append(("nowPlayingPlaylist", self._build_now_playing_playlist(snapshot)))
                up_next = self._build_up_next(snapshot)
                if up_next is not None:
                    heartbeat_actions.append(("autoplayUpNext", up_next))
            heartbeat_actions.append(("onVolumeChanged", self._build_volume(snapshot)))

            action_names = [a[0] for a in heartbeat_actions]
            logger.info("PUBLISH v%d heartbeat=true -> %s", snapshot.version, action_names)

            all_ok = True
            for sc, payload in heartbeat_actions:
                try:
                    res = self.post_action(sc, payload, heartbeat=True)
                except TypeError:
                    res = self.post_action(sc, payload)
                if res is False:
                    all_ok = False
                    break
            return all_ok

    def _build_now_playing(self, snapshot: SessionState) -> Dict[str, Any]:
        dur = max(0, int(snapshot.duration or 0))
        cur = max(0, int(snapshot.position or 0))
        state = snapshot.play_state
        payload = {
            "videoId": snapshot.current_video_id or "",
            "currentTime": str(cur),
            "duration": str(dur),
            "state": str(state),
            "cpn": snapshot.cpn or "kodi",
        }
        if dur > 0:
            payload["seekableStartTime"] = "0"
            payload["seekableEndTime"] = str(dur)
            payload["loadedTime"] = str(dur if state == PlayState.PLAYING else cur)
        if snapshot.current_index is not None and snapshot.current_index >= 0:
            payload["currentIndex"] = str(snapshot.current_index)
        if snapshot.list_id:
            payload["listId"] = str(snapshot.list_id)
        # R5: the index is derived from the queue this report carries, never a
        # carried field, so it can never point outside the phone's list.
        payload["currentIndex"] = str(published_index(snapshot))
        return payload

    def _build_now_playing_playlist(self, snapshot: SessionState) -> Dict[str, Any]:
        dur = max(0, int(snapshot.duration or 0))
        cur = max(0, int(snapshot.position or 0))
        vid = snapshot.current_video_id or ""
        vids = ",".join(snapshot.playlist) if snapshot.playlist else vid
        payload = {
            "videoIds": vids,
            "videoId": vid,
            "currentIndex": str(published_index(snapshot)),
            "currentTime": str(cur),
            "duration": str(dur),
            "state": str(snapshot.play_state),
        }
        if dur > 0:
            payload["seekableStartTime"] = "0"
            payload["seekableEndTime"] = str(dur)
        if snapshot.list_id:
            payload["listId"] = str(snapshot.list_id)
        return payload

    def _build_state_change(self, snapshot: SessionState) -> Dict[str, Any]:
        dur = max(0, int(snapshot.duration or 0))
        cur = max(0, int(snapshot.position or 0))
        state = snapshot.play_state
        payload = {
            "state": str(state),
            "currentTime": str(cur),
            "duration": str(dur),
            "cpn": snapshot.cpn or "kodi",
        }
        if dur > 0:
            payload["seekableStartTime"] = "0"
            payload["seekableEndTime"] = str(dur)
            payload["loadedTime"] = str(dur if state == PlayState.PLAYING else cur)
        return payload

    def _build_volume(self, snapshot: SessionState) -> Dict[str, Any]:
        return {
            "volume": str(max(0, min(int(snapshot.volume), 100))),
            "muted": "false",
        }

    def _build_up_next(self, snapshot: SessionState) -> Optional[Dict[str, Any]]:
        """autoplayUpNext (R10): the queue item after the one playing.

        Derivable from the stored queue, so it is emitted when known. Omitted
        entirely when there is no next item — the receiver must not guess (R6).
        """
        if not snapshot.playlist:
            return None
        nxt = published_index(snapshot) + 1
        if nxt >= len(snapshot.playlist):
            return None
        payload: Dict[str, Any] = {"videoId": snapshot.playlist[nxt]}
        if snapshot.list_id:
            payload["listId"] = str(snapshot.list_id)
        return payload

    def _build_ad_state(self) -> Dict[str, Any]:
        """onAdStateChange / onAdPlaying (R10): no ad is playing.

        Streams are resolved locally by yt-dlp and no ad is ever injected, so
        this is a known fact. An unknown ad state would be omitted instead.
        """
        return {
            "adState": "0",
            "adDuration": "0",
            "adPosition": "0",
            "isSkippable": "false",
        }

    def post_action(self, sc: str, data: Dict[str, Any], heartbeat: bool = False) -> bool:
        """Post a player/device state report. Returns True on success, False on failure."""
        if not self.sid:
            return False
        return self._do_post(sc, data, heartbeat=heartbeat)

    def _do_post(self, sc: str, data: Dict[str, Any], heartbeat: bool = False) -> bool:
        if not self.sid:
            return False

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

        hb_flag = " heartbeat=true" if heartbeat else ""
        if sc == "nowPlaying":
            logger.info(
                "REPORT nowPlaying vid=%s idx=%s t=%s dur=%s state=%s listId=%s%s",
                data.get("videoId"), data.get("currentIndex"), data.get("currentTime"),
                data.get("duration"), data.get("state"), data.get("listId") or "-", hb_flag
            )
        else:
            logger.info("REPORT %s data=%s%s", sc, data, hb_flag)

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
                return True
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
                return True
        except Exception as e:
            logger.info("Failed to post action %s: %s (ofs=%s)", sc, e, ofs)
            return False

    def close(self) -> None:
        self._stopped.set()
        self._wake_event.set()
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
