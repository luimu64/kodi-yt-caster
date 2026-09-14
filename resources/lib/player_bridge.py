"""Kodi Player bridge and playback state synchronizer."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Union
from .resolver import VideoResolver
from .lounge.session import LoungeSession
from . import preloader

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
        music_visualizer: str = "auto",
    ):
        if isinstance(session, list):
            self.sessions = session
        else:
            self.sessions = [session]
        self.session = self.sessions[0]
        self.resolver = resolver or VideoResolver()
        self.stream_selection_type = stream_selection_type
        self.max_resolution = max_resolution
        self.music_visualizer = music_visualizer
        self.playlist: List[str] = []
        self.current_index: int = 0
        self.current_video_id: Optional[str] = None
        self.current_duration: int = 0
        self.pending_seek: Optional[float] = None
        # 'm' when the current item was cast from YouTube Music, 'cl' from
        # YouTube; None until the first cast.
        self.current_theme: Optional[str] = None
        self.state = PlayerState.STOPPED
        self._lock = threading.Lock()
        self._play_gen = 0  # monotonic play-request epoch; supersedes stale requests
        self._prefetch_id: Optional[str] = None
        self._active_gen = 0  # generation of the item currently loaded in Kodi
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

    def _resync_index(self) -> None:
        """Re-derive current_index from current_video_id after queue edits."""
        with self._lock:
            if self.playlist and self.current_video_id in self.playlist:
                self.current_index = self.playlist.index(self.current_video_id)
            elif self.playlist:
                self.current_index = max(0, min(self.current_index, len(self.playlist) - 1))
            else:
                self.current_index = 0

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

            self.current_theme = str(data.get("_theme") or self.current_theme or "")
            target_id = self.playlist[self.current_index] if self.playlist else video_id
            if target_id:
                # Dedup: Google relays the same setPlaylist on both Lounge sessions (and resends
                # on rebind), so the same video can arrive 2-4x in a burst. If we are already
                # resolving/playing exactly this video, the duplicate is a no-op.
                if target_id == self.current_video_id and self.state != PlayerState.STOPPED:
                    return
                self.pending_seek = current_time if current_time > 0 else None
                self._play_gen += 1
                threading.Thread(target=self._play_video, args=(target_id, self._play_gen, self.current_theme), daemon=True).start()

    def update_playlist(self, data: Dict[str, Any]) -> None:
        """Handle queue modifications (add, remove, reorder)."""
        with self._lock:
            video_ids_str = data.get("videoIds", "")
            if video_ids_str:
                self.playlist = [v for v in video_ids_str.split(",") if v]
        self._resync_index()

    def play_video_id(self, video_id: str, seek_time: float = 0.0, theme: Optional[str] = None) -> None:
        with self._lock:
            self.current_video_id = video_id
            self.pending_seek = seek_time if seek_time > 0 else None
            self._play_gen += 1
            self._resync_index_locked()
            threading.Thread(target=self._play_video, args=(video_id, self._play_gen, theme), daemon=True).start()

    def _resync_index_locked(self) -> None:
        # caller holds self._lock
        if self.playlist and self.current_video_id in self.playlist:
            self.current_index = self.playlist.index(self.current_video_id)
        elif self.playlist:
            self.current_index = max(0, min(self.current_index, len(self.playlist) - 1))
        else:
            self.current_index = 0

    def _notify(self, message: str, title: str = "YouTube Cast", error: bool = False) -> None:
        """Show an on-screen notification; safe to call from any thread."""
        if not (KODI_AVAILABLE and xbmcgui):
            return
        try:
            icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
            xbmcgui.Dialog().notification(title, message, icon, 5000)
        except Exception:
            logger.debug("notification failed", exc_info=True)

    def _kick_prefetch(self) -> None:
        """Resolve the next queue item in the background (populates the resolver cache)."""
        with self._lock:
            if not self.playlist:
                return
            idx = self.current_index + 1
            if idx >= len(self.playlist):
                return
            next_id = self.playlist[idx]
        if next_id == self._prefetch_id:
            return
        self._prefetch_id = next_id

        def _run() -> None:
            try:
                logger.info("Prefetching next video: %s", next_id)
                info = self.resolver.resolve(next_id)
                # Preload the first ~60s of media itself: track change then
                # starts from warm disk instead of a cold CDN round-trip.
                preloader.preload(next_id, info)
            except Exception:
                logger.debug("Prefetch of %s failed", next_id, exc_info=True)

        threading.Thread(target=_run, name="Prefetch", daemon=True).start()

    def _play_video(self, video_id: str, gen: int, theme: Optional[str] = None) -> None:
        logger.info("TIMING %s: _play_video start (gen=%s)", video_id, gen)
        self._notify("YouTube Cast", "Loading video…")
        try:
            info = self.resolver.resolve(video_id)
        except Exception as e:
            logger.error("Failed to resolve video %s: %s", video_id, e)
            with self._lock:
                if gen == self._play_gen:
                    if KODI_AVAILABLE and xbmcgui:
                        xbmcgui.Dialog().notification("YouTube Cast", f"Failed to resolve video: {e}", xbmcgui.NOTIFICATION_ERROR)
                else:
                    logger.info("Resolve failure for superseded request %s, not notifying", video_id)
            return

        playable_url = info.get("playable_url")
        if not playable_url:
            logger.error("No playable URL found for %s", video_id)
            self._notify("YouTube Cast", "No playable stream found", error=True)
            return

        # If the next-item preload already cached this stream's prefix, play
        # through the local proxy: instant startup, seamless remote splice.
        try:
            proxied = preloader.proxy_url(video_id, info)
        except Exception:
            proxied = None
        if proxied:
            playable_url = proxied

        with self._lock:
            if gen != self._play_gen:
                logger.info("Play request for %s superseded, dropping", video_id)
                return
            self.current_video_id = video_id
            self.current_duration = int(info.get("duration", 0))
            self._active_gen = gen

        title = info.get("title") or "YouTube Video"
        logger.info("TIMING %s: resolved -> now calling player.play", video_id)
        self._notify("YouTube Cast", f"Now playing: {title}")

        # Prefetch the next queue item while this one plays: auto-advance then starts
        # instantly instead of paying the full yt-dlp resolve on track change.
        self._kick_prefetch()

        if KODI_AVAILABLE and self._kodi_player:
            # Music visualizer mode: play audio-only so Kodi routes it to the
            # audio player and shows its visualization instead of a static
            # album-art video. "always" applies to every track, "auto" only to
            # detected static-art songs.
            # Auto = detected still-image songs only (metadata/title/bitrate
            # heuristics in the resolver). The casting app (YT vs YT Music) is
            # deliberately NOT a signal: real music videos are cast from the
            # YT Music app constantly and must stay in video mode.
            # Visualizer applies to MUSIC-APP content only: a cast from the
            # YouTube app is video even if the setting is "always".
            music_theme = theme if theme is not None else self.current_theme
            is_music_cast = music_theme == "m"
            audio_mode = (
                is_music_cast
                and info.get("audio_url")
                and (
                    self.music_visualizer == "always"
                    or (self.music_visualizer != "never" and info.get("is_static_art"))
                )
            )
            if audio_mode:
                playable_url = info["audio_url"]

            list_item = xbmcgui.ListItem(info.get("title", "YouTube Video"))
            if audio_mode:
                list_item.setInfo("music", {
                    "title": info.get("title", ""),
                    "duration": self.current_duration,
                    "artist": info.get("artist", ""),
                    "album": info.get("album", ""),
                })
            else:
                list_item.setInfo("video", {
                    "title": info.get("title", ""),
                    "duration": self.current_duration,
                })
            if info.get("thumbnail"):
                list_item.setArt({"thumb": info["thumbnail"], "icon": info["thumbnail"]})

            stream_type = "" if audio_mode else info.get("stream_type")
            # HLS: play natively over the localhost http manifest. Do NOT set inputstream.adaptive —
            # IA stalls the audio stream on these VOD playlists (CVideoPlayerAudio 'stream stalled'),
            # while Kodi's ffmpeg demuxer merges the detached audio group correctly.
            if stream_type == "dash":
                list_item.setMimeType("application/dash+xml")
                list_item.setProperty("inputstream", "inputstream.adaptive")
                list_item.setProperty("inputstream.adaptive.manifest_type", "mpd")
                if self.stream_selection_type:
                    list_item.setProperty("inputstream.adaptive.stream_selection_type", self.stream_selection_type)
                if self.max_resolution and self.max_resolution != "auto":
                    list_item.setProperty("inputstream.adaptive.chooser_resolution_max", self.max_resolution)

            if audio_mode:
                self._play_music_queue(video_id, info, list_item)
            else:
                player = xbmc.Player()
                player.play(playable_url, list_item)
        else:
            self.state = PlayerState.PLAYING
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, 0, self.current_duration)
                    s.report_now_playing(self.current_video_id, 0, self.current_duration, self.state)
                except Exception:
                    pass

    def _play_music_queue(self, video_id: str, info: Dict[str, Any], list_item) -> None:
        """Play through Kodi's music playlist so the YT Music queue is visible
        in the music queue view and auto-advance is Kodi-native.

        Items are plugin:// URLs; Kodi invokes the addon's plugin entry per
        item, which resolves against the service's warm caches over localhost.
        """
        try:
            import xbmc
            playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
            position = max(0, self.current_index)
            playlist.clear()
            time.sleep(0.1)  # Kodi needs a beat after clear

            def _item(vid):
                # Current item carries resolved metadata; upcoming items get
                # a thumbnail (URL-derivable, no resolve) and their title via
                # a cheap keyless oEmbed lookup, cached across track changes.
                if vid == video_id:
                    return list_item
                li = xbmcgui.ListItem(self._queue_titles.get(vid) or vid)
                li.setArt({"thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                           "icon": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"})
                return li

            # Titles must be in the cache BEFORE add() (Kodi snapshots labels
            # at add time); the sync burst covers the visible window, the
            # background thread the rest.
            self._fetch_queue_titles(list(self.playlist or [video_id]))
            for vid in self.playlist or [video_id]:
                playlist.add(f"plugin://plugin.service.ytlounge-cast/?play={vid}", _item(vid))
            xbmc.Player().play(playlist, list_item, False, position)
            self._activate_visualizer()
        except Exception:
            logger.debug("music queue playback failed; direct play fallback", exc_info=True)
            xbmc.Player().play(info.get("audio_url") or info.get("playable_url"), list_item)
            self._activate_visualizer()

    _queue_titles: Dict[str, str] = {}

    @staticmethod
    def _fetch_title_sync(video_id: str) -> Optional[str]:
        """Single oEmbed title lookup (~100-300ms)."""
        try:
            import urllib.request
            import json
            url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8")).get("title")
        except Exception:
            return None

    def _fetch_queue_titles(self, video_ids: List[str]) -> None:
        """Fill titles for queue items via YouTube oEmbed (keyless, CJK-safe).

        Kodi snapshots labels at add() time, so the first ~12 uncached items
        after the current position are fetched synchronously (bounded burst,
        ~2s max) BEFORE the playlist is built; the background thread covers
        the rest and caches across track changes.
        """
        todo = [v for v in video_ids if v not in self._queue_titles]
        if not todo:
            return
        for vid in todo[:12]:
            title = self._fetch_title_sync(vid)
            if title:
                self._queue_titles[vid] = title
        rest = [v for v in todo if v not in self._queue_titles]
        if not rest:
            return

        def _run() -> None:
            for vid in rest:
                title = self._fetch_title_sync(vid)
                if title:
                    self._queue_titles[vid] = title
        threading.Thread(target=_run, name="QueueTitles", daemon=True).start()

    def _activate_visualizer(self) -> None:
        """Route fullscreen video player to the music/visualisation window."""
        if not (KODI_AVAILABLE and xbmc):
            return

        def _run() -> None:
            for _ in range(20):  # wait up to ~10s for playback start
                try:
                    if self._kodi_player and self._kodi_player.isPlayingAudio():
                        # 12006 = music visualisation window (12005 is the
                        # fullscreen VIDEO window — that showed a frozen
                        # frame over the GUI).
                        xbmc.executebuiltin("ActivateWindow(12006)")
                        return
                except Exception:
                    pass
                time.sleep(0.5)
        threading.Thread(target=_run, daemon=True, name="VizActivator").start()

    def _is_paused(self) -> bool:
        # xbmc.Player.isPlaying() returns True WHILE PAUSED, so it cannot
        # distinguish pause from play. Ask Kodi's GUI conditions instead.
        if KODI_AVAILABLE and xbmc:
            try:
                return xbmc.getCondVisibility("Player.Paused")
            except Exception:
                pass
        return self.state == PlayerState.PAUSED

    def pause(self) -> None:
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            # pause() TOGGLES: guard so pause-while-paused does not resume.
            if not self._is_paused():
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
            if self._is_paused():
                # Kodi's pause() toggles pause/resume.
                self._kodi_player.pause()
                return
            if self._kodi_player.isPlaying():
                # Actively playing: nothing to do.
                return
            if self.current_video_id:
                # Stopped/idle: restart the current item where we left off.
                self.play_video_id(self.current_video_id, self.get_time())
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
            with self._lock:
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
                vol = data.get("result", {}).get("volume")
                if vol is None:
                    # JSON-RPC error: report last known value rather than a
                    # fabricated 100.
                    return getattr(self, "_last_volume", 100)
                vol = max(0, min(int(vol), 100))
                self._last_volume = vol
                return vol
            except Exception:
                return getattr(self, "_last_volume", 100)
        return 100

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(int(volume), 100))
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
        self._last_volume = volume
        # Keep the phone's volume slider in sync.
        for s in self.sessions:
            try:
                s.report_volume(volume)
            except Exception:
                pass

    # Callbacks from Kodi player
    def _on_playback_started(self) -> None:
        logger.info("TIMING %s: Kodi onPlayBackStarted fired (video visible)", self.current_video_id)
        self.state = PlayerState.PLAYING
        cur_time = self.get_time()
        with self._lock:
            pending = self.pending_seek
            self.pending_seek = None
        if pending is not None and pending > 0:
            if KODI_AVAILABLE and self._kodi_player:
                # Adaptive streams (HLS/DASH) are often not seekable the instant
                # onPlayBackStarted fires: the demuxer is still opening and the
                # seek is silently dropped, so a cast that should resume at T
                # starts from 0 (position desync vs the phone). Retry briefly.
                for attempt in range(5):
                    try:
                        self._kodi_player.seekTime(pending)
                    except Exception:
                        pass
                    time.sleep(0.4)
                    try:
                        if self._kodi_player.isPlaying() and abs(self._kodi_player.getTime() - pending) < 2.0:
                            break
                    except Exception:
                        continue
            cur_time = int(pending)

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
        # Kodi fires Stopped after Ended for a naturally-finished item too;
        # if a newer play generation is already active, this stop is stale.
        with self._lock:
            if self._active_gen != self._play_gen:
                logger.debug("Ignoring stale onPlayBackStopped (active=%s, latest=%s)",
                             self._active_gen, self._play_gen)
                return
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
                self._play_gen += 1
                threading.Thread(target=self._play_video, args=(next_id, self._play_gen, self.current_theme), daemon=True).start()
