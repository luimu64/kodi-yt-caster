"""Lounge session listener thread and command dispatcher."""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple
from .client import BASE_URL, DEFAULT_HEADERS, LoungeError, LoungeTokenExpiredError
from .session import LoungeSession, parse_frames

logger = logging.getLogger("ytlounge.listener")


class CommandDispatcher:
    """Dispatches Lounge commands to application / Kodi player handlers."""

    def __init__(self) -> None:
        self.on_remote_connected: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_remote_disconnected: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_set_playlist: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_update_playlist: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_play: Optional[Callable[[], None]] = None
        self.on_pause: Optional[Callable[[], None]] = None
        self.on_stop: Optional[Callable[[], None]] = None
        self.on_seek: Optional[Callable[[float], None]] = None
        self.on_set_volume: Optional[Callable[[int], None]] = None
        self.on_get_volume: Optional[Callable[[], int]] = None
        self.on_get_now_playing: Optional[Callable[[], None]] = None


class LoungeListener(threading.Thread):
    def __init__(
        self,
        session: LoungeSession,
        dispatcher: CommandDispatcher,
        on_token_expired: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name="LoungeListener", daemon=True)
        self.session = session
        self.dispatcher = dispatcher
        self.on_token_expired = on_token_expired
        self._stop_event = threading.Event()
        self.consecutive_failures = 0

    def stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> None:
        logger.info("LoungeListener thread started")
        backoff = 2.0

        while not self.is_stopped():
            try:
                if not self.session.sid:
                    logger.info("Performing handshake...")
                    self.session.handshake()
                    logger.info("Handshake OK: SID=%s", self.session.sid)

                self._listen_stream()
                self.consecutive_failures = 0
                backoff = 2.0
            except LoungeTokenExpiredError:
                logger.error("Lounge token rejected by server")
                if self.on_token_expired:
                    self.on_token_expired()
                # Re-handshake with the (possibly refreshed) token instead of
                # dying: the handler resets session.sid/gsessionid.
                self.session.sid = None
                self.session.gsessionid = None
                self.session.last_code = -1
                self.consecutive_failures = 0
                backoff = 2.0
                for _ in range(50):
                    if self.is_stopped():
                        return
                    time.sleep(0.1)
            except Exception as e:
                self.consecutive_failures += 1
                logger.warning("Listener error (%s consecutive): %s", self.consecutive_failures, e)
                if self.consecutive_failures >= 8:
                    logger.error("Consecutive failures exceeded threshold, re-handshaking session")
                    self.session.sid = None
                    self.consecutive_failures = 0
                    # Transient network trouble must NOT destroy the pairing:
                    # only a server-side token rejection (LoungeTokenExpiredError)
                    # triggers re-registration via on_token_expired.
                    backoff = 5.0
                else:
                    # Exponential backoff (2, 4, 8, 16, 32, max 60s)
                    sleep_time = min(backoff, 60.0)
                    backoff = min(backoff * 2.0, 60.0)
                sleep_time = min(backoff, 60.0)
                for _ in range(int(sleep_time * 10)):
                    if self.is_stopped():
                        return
                    time.sleep(0.1)

        logger.info("LoungeListener thread finished")

    def _listen_stream(self) -> None:
        # ofs is shared with the post worker — Lounge silently drops reports
        # with duplicate offsets, so the increment must take the same lock
        # _do_post uses (race: listener + position loop incrementing
        # concurrently produced colliding offsets).
        with self.session._ofs_lock:
            self.session.ofs += 1
        params = self.session._base_params()
        params.update({
            "RID": "rpc",
            "AID": "3",
            "CI": "0",
            "TYPE": "xmlhttp",
            "SID": self.session.sid,
            "zx": self.session._random_zx(),
        })
        if self.session.gsessionid:
            params["gsessionid"] = self.session.gsessionid

        url = f"{BASE_URL}/bc/bind?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)

        # 120-second timeout for streaming long-poll
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            buf = ""
            while not self.is_stopped():
                # read1(): returns as soon as ANY bytes are buffered. resp.read(n)
                # blocks until all n bytes arrive — and Lounge command frames are
                # ~100-300 bytes followed by silence, so each command sat in the
                # socket buffer until the NEXT command supplied the remaining
                # bytes (every command relayed one late).
                chunk = resp.read1(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                commands, consumed = parse_frames(buf)
                if consumed:
                    # Keep any partial trailing frame buffered; its remainder
                    # arrives in a later chunk.
                    buf = buf[consumed:]
                if commands:
                    for code, name, data in commands:
                        if code > self.session.last_code:
                            self.session.last_code = code
                            self._handle_command(name, data)

    def _handle_command(self, name: str, data: Any) -> None:
        logger.info("Lounge command: %s (data: %s)", name, data)
        # Stamp the originating app on dict payloads: 'm' = YouTube Music
        # sender, 'cl' = YouTube. The player uses it for music-visualizer
        # auto mode (static-art detection is unreliable for 1080p art videos).
        if isinstance(data, dict):
            data.setdefault("_theme", self.session.theme)
        try:
            if name == "remoteConnected":
                if self.dispatcher.on_remote_connected and isinstance(data, dict):
                    self.dispatcher.on_remote_connected(data)
            elif name == "remoteDisconnected":
                if self.dispatcher.on_remote_disconnected and isinstance(data, dict):
                    self.dispatcher.on_remote_disconnected(data)
            elif name == "setPlaylist":
                if self.dispatcher.on_set_playlist and isinstance(data, dict):
                    self.dispatcher.on_set_playlist(data)
            elif name == "updatePlaylist":
                if self.dispatcher.on_update_playlist and isinstance(data, dict):
                    self.dispatcher.on_update_playlist(data)
            elif name in ("play", "playVideo"):
                if self.dispatcher.on_play:
                    self.dispatcher.on_play()
            elif name in ("pause", "pauseVideo"):
                if self.dispatcher.on_pause:
                    self.dispatcher.on_pause()
            elif name == "stopVideo":
                if self.dispatcher.on_stop:
                    self.dispatcher.on_stop()
            elif name == "seekTo":
                if self.dispatcher.on_seek and isinstance(data, dict) and "newTime" in data:
                    self.dispatcher.on_seek(float(data["newTime"]))
            elif name == "setVolume":
                if self.dispatcher.on_set_volume and isinstance(data, dict) and "volume" in data:
                    self.dispatcher.on_set_volume(int(data["volume"]))
            elif name == "getVolume":
                if self.dispatcher.on_get_volume:
                    self.dispatcher.on_get_volume()
                    # R9: no hand-rolled reply report — re-send the shared
                    # snapshot (its onVolumeChanged is the answer) on this channel.
                    self.session.force_publish()
            elif name == "getNowPlaying":
                if self.dispatcher.on_get_now_playing:
                    self.dispatcher.on_get_now_playing()
        except Exception as e:
            logger.exception("Error handling command %s: %s", name, e)
