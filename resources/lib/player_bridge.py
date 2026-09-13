"""Kodi Player bridge and playback state synchronizer."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Union
from .resolver import VideoResolver
from .lounge.session import LoungeSession

logger = logging.getLogger("ytlounge.player")

try:
    import xbmc
    import xbmcgui
    KODI_AVAILABLE = True
except ImportError:
    KODI_AVAILABLE = False
    xbmc = None  # type: ignore
    xbmcgui = None  # type: ignore


class PlayerState:
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


class KodiPlayerBridge:
    def __init__(
        self,
        session: Union[LoungeSession, List[LoungeSession]],
        resolver: Optional[VideoResolver] = None,
        stream_selection_type: str = "manual-osd",
        max_resolution: str = "auto",
    ):
        if isinstance(session, list):
            self.sessions = session
        else:
            self.sessions = [session]
        self.session = self.sessions[0]
        self.resolver = resolver or VideoResolver()
        self.stream_selection_type = stream_selection_type
        self.max_resolution = max_resolution
        self.playlist: List[str] = []
        self.current_index: int = 0
        self.current_video_id: Optional[str] = None
        self.current_duration: int = 0
        self.pending_seek: Optional[float] = None
        self.state = PlayerState.STOPPED
        self._lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        if KODI_AVAILABLE:
            self._kodi_player = self._create_kodi_player()
        else:
            self._kodi_player = None

    def _create_kodi_player(self):
        parent = self

        class SubclassPlayer(xbmc.Player):
            def onPlayBackStarted(self):
                parent._on_playback_started()

            def onPlayBackEnded(self):
                parent._on_playback_ended()

            def onPlayBackStopped(self):
                parent._on_playback_stopped()

            def onPlayBackPaused(self):
                parent._on_playback_paused()

            def onPlayBackResumed(self):
                parent._on_playback_resumed()

        return SubclassPlayer()

    def start_monitor(self) -> None:
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(name="PlaybackMonitor", target=self._position_loop, daemon=True)
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()

    def _position_loop(self) -> None:
        while not self._monitor_stop.is_set():
            time.sleep(2.0)
            if self.state == PlayerState.PLAYING and self.current_video_id:
                cur_time = self.get_time()
                for s in self.sessions:
                    try:
                        s.report_now_playing(
                            video_id=self.current_video_id,
                            current_time=int(cur_time),
                            duration=self.current_duration,
                            state=self.state,
                        )
                    except Exception:
                        pass

    def set_playlist(self, data: Dict[str, Any]) -> None:
        """Handle setPlaylist command from YouTube mobile app."""
        with self._lock:
            video_id = data.get("videoId")
            video_ids_str = data.get("videoIds", "")
            current_time = float(data.get("currentTime", 0.0) or 0.0)

            if video_ids_str:
                self.playlist = [v for v in video_ids_str.split(",") if v]
            elif video_id:
                self.playlist = [video_id]
            else:
                self.playlist = []

            if video_id and video_id in self.playlist:
                self.current_index = self.playlist.index(video_id)
            else:
                self.current_index = 0

            target_id = self.playlist[self.current_index] if self.playlist else video_id
            if target_id:
                self.pending_seek = current_time if current_time > 0 else None
                threading.Thread(target=self._play_video, args=(target_id,), daemon=True).start()

    def update_playlist(self, data: Dict[str, Any]) -> None:
        """Handle queue modifications (add, remove, reorder)."""
        with self._lock:
            video_ids_str = data.get("videoIds", "")
            if video_ids_str:
                self.playlist = [v for v in video_ids_str.split(",") if v]

    def play_video_id(self, video_id: str, seek_time: float = 0.0) -> None:
        with self._lock:
            self.current_video_id = video_id
            self.pending_seek = seek_time if seek_time > 0 else None
            threading.Thread(target=self._play_video, args=(video_id,), daemon=True).start()

    def _play_video(self, video_id: str) -> None:
        logger.info("Resolving video %s for playback", video_id)
        try:
            info = self.resolver.resolve(video_id)
        except Exception as e:
            logger.error("Failed to resolve video %s: %s", video_id, e)
            if KODI_AVAILABLE:
                xbmcgui.Dialog().notification("YouTube Cast", f"Failed to resolve video: {e}", xbmcgui.NOTIFICATION_ERROR)
            return

        playable_url = info.get("playable_url")
        if not playable_url:
            logger.error("No playable URL found for %s", video_id)
            return

        with self._lock:
            self.current_video_id = video_id
            self.current_duration = int(info.get("duration", 0))

        logger.info("Playing: %s (%s)", info.get("title"), playable_url[:60])

        if KODI_AVAILABLE and self._kodi_player:
            list_item = xbmcgui.ListItem(info.get("title", "YouTube Video"))
            list_item.setInfo("video", {
                "title": info.get("title", ""),
                "duration": self.current_duration,
            })
            if info.get("thumbnail"):
                list_item.setArt({"thumb": info["thumbnail"], "icon": info["thumbnail"]})

            stream_type = info.get("stream_type")
            if stream_type in ("hls", "hls_master"):
                list_item.setMimeType("application/x-mpegURL")
                list_item.setProperty("inputstream", "inputstream.adaptive")
                list_item.setProperty("inputstream.adaptive.manifest_type", "hls")
                if self.stream_selection_type:
                    list_item.setProperty("inputstream.adaptive.stream_selection_type", self.stream_selection_type)
                if self.max_resolution and self.max_resolution != "auto":
                    list_item.setProperty("inputstream.adaptive.chooser_resolution_max", self.max_resolution)
            elif stream_type == "dash":
                list_item.setMimeType("application/dash+xml")
                list_item.setProperty("inputstream", "inputstream.adaptive")
                list_item.setProperty("inputstream.adaptive.manifest_type", "mpd")
                if self.stream_selection_type:
                    list_item.setProperty("inputstream.adaptive.stream_selection_type", self.stream_selection_type)
                if self.max_resolution and self.max_resolution != "auto":
                    list_item.setProperty("inputstream.adaptive.chooser_resolution_max", self.max_resolution)

            self._kodi_player.play(playable_url, list_item)
        else:
            self.state = PlayerState.PLAYING
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, 0, self.current_duration)
                    s.report_now_playing(self.current_video_id, 0, self.current_duration, self.state)
                except Exception:
                    pass

    def pause(self) -> None:
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            self._kodi_player.pause()
        else:
            self.state = PlayerState.PAUSED
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, self.get_time(), self.current_duration)
                except Exception:
                    pass

    def resume(self) -> None:
        if KODI_AVAILABLE and self._kodi_player:
            if not self._kodi_player.isPlaying():
                self._kodi_player.pause()  # In Kodi, pause() toggles pause/resume
        else:
            self.state = PlayerState.PLAYING
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, self.get_time(), self.current_duration)
                except Exception:
                    pass

    def stop(self) -> None:
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            self._kodi_player.stop()
        else:
            self.state = PlayerState.STOPPED
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, 0, 0)
                except Exception:
                    pass

    def seek_to(self, seconds: float) -> None:
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            self._kodi_player.seekTime(seconds)
        else:
            self.pending_seek = seconds
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, int(seconds), self.current_duration)
                except Exception:
                    pass

    def get_time(self) -> int:
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            try:
                return int(self._kodi_player.getTime())
            except Exception:
                return 0
        return 0

    def get_volume(self) -> int:
        if KODI_AVAILABLE:
            try:
                import json
                resp = xbmc.executeJSONRPC(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "Application.GetProperties",
                    "params": {"properties": ["volume"]},
                    "id": 1
                }))
                data = json.loads(resp)
                return int(data.get("result", {}).get("volume", 100))
            except Exception:
                return 100
        return 100

    def set_volume(self, volume: int) -> None:
        if KODI_AVAILABLE:
            try:
                import json
                xbmc.executeJSONRPC(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "Application.SetVolume",
                    "params": {"volume": volume},
                    "id": 1
                }))
            except Exception:
                pass

    # Callbacks from Kodi player
    def _on_playback_started(self) -> None:
        self.state = PlayerState.PLAYING
        cur_time = self.get_time()
        if self.pending_seek is not None and self.pending_seek > 0:
            seek_val = self.pending_seek
            self.pending_seek = None
            if KODI_AVAILABLE and self._kodi_player:
                self._kodi_player.seekTime(seek_val)
            cur_time = int(seek_val)

        if self.current_video_id:
            for s in self.sessions:
                try:
                    s.report_state_change(PlayerState.PLAYING, cur_time, self.current_duration)
                    s.report_now_playing(self.current_video_id, cur_time, self.current_duration, PlayerState.PLAYING)
                except Exception:
                    pass

    def _on_playback_paused(self) -> None:
        self.state = PlayerState.PAUSED
        cur_time = self.get_time()
        for s in self.sessions:
            try:
                s.report_state_change(PlayerState.PAUSED, cur_time, self.current_duration)
            except Exception:
                pass

    def _on_playback_resumed(self) -> None:
        self.state = PlayerState.PLAYING
        cur_time = self.get_time()
        for s in self.sessions:
            try:
                s.report_state_change(PlayerState.PLAYING, cur_time, self.current_duration)
            except Exception:
                pass

    def _on_playback_stopped(self) -> None:
        self.state = PlayerState.STOPPED
        for s in self.sessions:
            try:
                s.report_state_change(PlayerState.STOPPED, 0, 0)
            except Exception:
                pass

    def _on_playback_ended(self) -> None:
        self.state = PlayerState.STOPPED
        for s in self.sessions:
            try:
                s.report_state_change(PlayerState.STOPPED, 0, 0)
            except Exception:
                pass

        # Advance playlist
        with self._lock:
            if self.playlist and self.current_index + 1 < len(self.playlist):
                self.current_index += 1
                next_id = self.playlist[self.current_index]
                logger.info("Auto-advancing to next video: %s", next_id)
                threading.Thread(target=self._play_video, args=(next_id,), daemon=True).start()
