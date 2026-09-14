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
        self._current_duration: int = 0
        self.pending_seek: Optional[float] = None
        # 'm' when the current item was cast from YouTube Music, 'cl' from
        # YouTube; None until the first cast.
        self.current_theme: Optional[str] = None
        self.state = PlayerState.STOPPED
        self._lock = threading.RLock()
        self._play_gen = 0  # monotonic play-request epoch; supersedes stale requests
        self._requested_id: Optional[str] = None  # video the latest play request targets (set at ENQUEUE time)
        self._prefetch_id: Optional[str] = None
        self._active_gen = 0  # generation of the item currently loaded in Kodi
        # True while playback runs through Kodi's own music playlist (plugin
        # URLs): Kodi auto-advances natively, so _on_playback_ended must NOT
        # spawn its own play for the next item there (restart-from-0 race).
        self._kodi_queue_mode = False
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        if KODI_AVAILABLE:
            self._kodi_player = self._create_kodi_player()
        else:
            self._kodi_player = None

    @property
    def current_duration(self) -> int:
        if self._current_duration <= 0:
            player = getattr(self, "_kodi_player", None)
            if KODI_AVAILABLE and player:
                try:
                    if player.isPlaying():
                        total = player.getTotalTime()
                        if total is not None and total > 0:
                            self._current_duration = max(0, int(total))
                except Exception:
                    pass
        return max(0, self._current_duration)

    @current_duration.setter
    def current_duration(self, val: Any) -> None:
        try:
            self._current_duration = max(0, int(val or 0))
        except (TypeError, ValueError):
            self._current_duration = 0

    def get_duration(self) -> int:
        """Return total duration in seconds, querying Kodi player if unknown."""
        return self.current_duration

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
                cur_duration = self.current_duration
                cur_index = self.current_index
                cur_playlist = list(self.playlist or ([self.current_video_id] if self.current_video_id else []))
                for s in self.sessions:
                    try:
                        s.report_now_playing(
                            video_id=self.current_video_id,
                            current_time=int(cur_time),
                            duration=cur_duration,
                            state=self.state,
                            current_index=cur_index,
                        )
                        s.report_now_playing_playlist(
                            video_ids=cur_playlist,
                            current_video_id=self.current_video_id,
                            current_index=cur_index,
                            current_time=int(cur_time),
                            duration=cur_duration,
                            state=self.state,
                        )
                        s.report_state_change(
                            state=self.state,
                            current_time=int(cur_time),
                            duration=cur_duration,
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
                # Dedup on _requested_id, not current_video_id+state: a request
                # for this video may still be RESOLVING or waiting for the async
                # onPlayBackStarted (state stays STOPPED for hundreds of ms on
                # real Kodi while the demuxer opens) - the old check missed dups
                # in that window, bumping _play_gen and restarting playback from 0
                # (and arming the stale-gen guard to eat a later STOPPED report).
                # Cleared on real Ended/Stopped so a deliberate recast works.
                if target_id == self._requested_id:
                    if current_time > 0:
                        self.seek_to(current_time)
                    return
                self._requested_id = target_id
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
            self._requested_id = video_id
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

    def _notify(self, title: str = "YouTube Cast", message: str = "", error: bool = False) -> None:
        """Show an on-screen notification; safe to call from any thread."""
        if not (KODI_AVAILABLE and xbmcgui):
            return
        if not message:
            message = title
            title = "YouTube Cast"
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
            gen = self._play_gen
        if next_id == self._prefetch_id:
            return
        self._prefetch_id = next_id

        def _run() -> None:
            try:
                logger.info("Prefetching next video: %s", next_id)
                if self._play_gen != gen:
                    return
                info = self.resolver.resolve(next_id, prefetch=True)
                if self._play_gen != gen:
                    return
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
            should_notify = False
            with self._lock:
                if gen == self._play_gen:
                    self._requested_id = None
                    should_notify = True
                else:
                    logger.info("Resolve failure for superseded request %s, not notifying", video_id)
            if should_notify and KODI_AVAILABLE and xbmcgui:
                xbmcgui.Dialog().notification("YouTube Cast", f"Failed to resolve video: {e}", xbmcgui.NOTIFICATION_ERROR)
            return

        playable_url = info.get("playable_url")
        if not playable_url:
            logger.error("No playable URL found for %s", video_id)
            with self._lock:
                if gen == self._play_gen:
                    self._requested_id = None
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
                self._kodi_queue_mode = True
                self._play_music_queue(video_id, info, list_item)
            else:
                self._kodi_queue_mode = False
                player = xbmc.Player()
                player.play(playable_url, list_item)
        else:
            self._kodi_queue_mode = False
            self.state = PlayerState.PLAYING
            for s in self.sessions:
                try:
                    s.report_state_change(self.state, 0, self.current_duration)
                    s.report_now_playing(
                        self.current_video_id,
                        0,
                        self.current_duration,
                        self.state,
                        current_index=self.current_index,
                    )
                    if self.current_video_id:
                        s.report_now_playing_playlist(
                            self.playlist or [self.current_video_id],
                            self.current_video_id,
                            self.current_index,
                            0,
                            self.current_duration,
                            self.state,
                        )
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
                # Raw title straight through — never normalize/strip/re-encode
                # (CJK renders blank if anything touches the string).
                if vid == video_id:
                    return list_item
                li = xbmcgui.ListItem(label=self._queue_titles.get(vid) or vid)
                li.setArt({"thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                           "icon": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"})
                return li

            # Titles must be in the cache BEFORE add() (Kodi snapshots labels
            # at add time); the sync burst covers the visible window from the
            # current position, the background thread the rest (and patches
            # live entries in place).
            self._fetch_queue_titles(list(self.playlist or [video_id]), position)
            for vid in self.playlist or [video_id]:
                playlist.add(f"plugin://plugin.service.ytlounge-cast/?play={vid}", _item(vid))
            xbmc.Player().play(playlist, list_item, False, position)
            self._activate_visualizer()
        except Exception:
            logger.debug("music queue playback failed; direct play fallback", exc_info=True)
            xbmc.Player().play(info.get("audio_url") or info.get("playable_url"), list_item)
            self._activate_visualizer()

    _queue_titles: Dict[str, str] = {}
    _queue_titles_inflight: set = set()

    @classmethod
    def _store_queue_title(cls, vid: str, title: str) -> None:
        cls._queue_titles.pop(vid, None)
        while len(cls._queue_titles) >= 50:
            cls._queue_titles.pop(next(iter(cls._queue_titles)))
        cls._queue_titles[vid] = title

    @staticmethod
    def _fetch_title_sync(video_id: str) -> Optional[str]:
        """Single oEmbed title lookup (~100-300ms). Raw title, no processing."""
        try:
            import urllib.request
            import json
            url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                title = json.loads(resp.read().decode("utf-8")).get("title")
                return title or None
        except Exception:
            return None

    _TITLE_WORKERS = 6

    @classmethod
    def _fetch_titles_parallel(cls, video_ids: List[str], deadline: float) -> Dict[str, str]:
        """Fetch several oEmbed titles concurrently within a wall-clock budget.

        The old code fetched the visible window SERIALLY — 'up to ~2s' was
        actually 12 x (connect+TLS+request), i.e. worst case ~36s of dead
        air before playback started on a music cast. Parallel fetch turns
        the window into ~one request time (~300ms) and the hard deadline
        bounds it regardless of network state.
        """
        results: Dict[str, str] = {}
        lock = threading.Lock()
        remaining = list(video_ids)

        def _worker() -> None:
            while True:
                with lock:
                    if not remaining or time.monotonic() > deadline:
                        return
                    vid = remaining.pop(0)
                title = cls._fetch_title_sync(vid)
                if title:
                    with lock:
                        results[vid] = title

        threads = [threading.Thread(target=_worker, daemon=True, name="TitleFetch")
                   for _ in range(min(cls._TITLE_WORKERS, max(1, len(video_ids))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return results

    def _fetch_queue_titles(self, video_ids: List[str], position: int = 0) -> None:
        """Fill titles for queue items via YouTube oEmbed (keyless, CJK-safe).

        Kodi snapshots labels at add() time, so items in the visible window
        (from the current position) are fetched synchronously (bounded burst,
        ~2s max) BEFORE the playlist is built. The background thread covers
        the rest AND patches the live playlist entries in place, since Kodi
        never re-reads labels after add().

        Guarded against pile-up: a 100+ item queue would otherwise spawn one
        thread per track change, each holding the interpreter for seconds.
        """
        with self._lock:
            if len(self._queue_titles_inflight) >= 50:
                self._queue_titles_inflight.clear()
            todo = [v for v in video_ids if v not in self._queue_titles and v not in self._queue_titles_inflight]
            if not todo:
                return
            for v in todo:
                self._queue_titles_inflight.add(v)
        try:
            window = [v for v in video_ids[position:position + 12] if v in todo]
            # Hard 2.5s wall-clock budget for the synchronous burst: labels
            # are snapshotted at playlist.add() time, so this gates the time
            # from cast-command to first sound. Parallel workers (above)
            # make the typical case ~300ms; the deadline is the worst case.
            titles = self._fetch_titles_parallel(window, deadline=time.monotonic() + 2.5)
            for vid, title in titles.items():
                self._store_queue_title(vid, title)
            rest = [v for v in todo if v not in self._queue_titles]
            if not rest:
                return

            snapshot = list(video_ids)

            def _run() -> None:
                try:
                    for vid in rest:
                        title = self._fetch_title_sync(vid)
                        if not title:
                            continue
                        self._store_queue_title(vid, title)
                        self._patch_playlist_label(snapshot, vid, title)
                finally:
                    with self._lock:
                        for v in rest:
                            self._queue_titles_inflight.discard(v)
            threading.Thread(target=_run, name="QueueTitles", daemon=True).start()
        except Exception:
            with self._lock:
                for v in todo:
                    self._queue_titles_inflight.discard(v)

    def _patch_playlist_label(self, snapshot: List[str], video_id: str, title: str) -> None:
        """Update one live playlist entry's label after its title arrives.

        Only the currently playing item is left alone (remove+add on it
        could disrupt playback); already-played earlier items are safe to
        patch. Bails if the queue was replaced since (size/order mismatch).
        """
        if not KODI_AVAILABLE:
            return
        try:
            import xbmc
            import xbmcgui
            playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
            if playlist.size() != len(snapshot):
                return
            try:
                idx = snapshot.index(video_id)
            except ValueError:
                return
            if idx == playlist.getposition():
                return
            url = f"plugin://plugin.service.ytlounge-cast/?play={video_id}"
            # NOTE: pass the raw title straight through — no normalization,
            # stripping, or re-encoding. CJK renders blank if anything in the
            # chain touches the string, so hands off.
            li = xbmcgui.ListItem(label=title)
            li.setArt({"thumb": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
                       "icon": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"})
            playlist.remove(url)
            playlist.add(url, li, idx)
        except Exception:
            pass

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
        with self._lock:
            self._play_gen += 1
            self._active_gen = self._play_gen
            self._requested_id = None
            self.current_duration = 0
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
        if KODI_AVAILABLE and self._kodi_player:
            try:
                # isPlaying() is True while paused too, so getTime() works in
                # both states. Gating on it returned 0 for paused audio and
                # froze the phone's position display.
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
                # starts from 0 (position desync vs the phone). Retry briefly —
                # but on a background thread: this callback runs on one of
                # Kodi's own player threads, and the 2s retry loop used to
                # stall every other player event behind it.
                seek_target = pending
                kodi_player = self._kodi_player
                gen = self._play_gen

                def _apply_seek() -> None:
                    for _attempt in range(5):
                        if self._play_gen != gen:
                            return
                        try:
                            kodi_player.seekTime(seek_target)
                        except Exception:
                            pass
                        time.sleep(0.4)
                        if self._play_gen != gen:
                            return
                        try:
                            if kodi_player.isPlaying() and abs(kodi_player.getTime() - seek_target) < 2.0:
                                break
                        except Exception:
                            continue

                threading.Thread(target=_apply_seek, name="SeekRetry", daemon=True).start()
            cur_time = int(pending)

        # Re-derive which queue item actually started: the user may have
        # picked a different song through Kodi's own queue view (native
        # auto-advance or manual selection), in which case current_video_id
        # still points at the phone's last cast and the phone desyncs.
        self._sync_current_from_kodi()

        if self.current_video_id:
            for s in self.sessions:
                try:
                    s.report_state_change(PlayerState.PLAYING, cur_time, self.current_duration)
                    s.report_now_playing(
                        self.current_video_id,
                        cur_time,
                        self.current_duration,
                        PlayerState.PLAYING,
                        current_index=self.current_index,
                    )
                    s.report_now_playing_playlist(
                        self.playlist or [self.current_video_id],
                        self.current_video_id,
                        self.current_index,
                        cur_time,
                        self.current_duration,
                        PlayerState.PLAYING,
                    )
                except Exception:
                    pass

    def _sync_current_from_kodi(self) -> bool:
        """Align bridge state with what Kodi is actually playing.

        Returns True if the playing item differs from current_video_id.
        """
        if not (KODI_AVAILABLE and self._kodi_player):
            return False
        try:
            playing_file = self._kodi_player.getPlayingFile()
        except Exception:
            return False
        if not playing_file or "play=" not in playing_file:
            return False
        import urllib.parse
        try:
            qs = urllib.parse.urlparse(playing_file).query
            vid = dict(urllib.parse.parse_qsl(qs)).get("play")
        except Exception:
            return False
        if not vid or vid == self.current_video_id:
            return False
        logger.info("Queue pick via Kodi UI: %s -> %s", self.current_video_id, vid)
        with self._lock:
            self._requested_id = vid
            self.current_video_id = vid
            self.current_duration = 0
            if self.playlist and vid in self.playlist:
                self.current_index = self.playlist.index(vid)
            self._play_gen += 1
            self._active_gen = self._play_gen
        # Duration unknown until resolve; refresh it (and the preload chain)
        # in the background without blocking the state reports below.
        def _refresh() -> None:
            try:
                info = self.resolver.resolve(vid)
            except Exception:
                info = {}
            try:
                cur_vid = None
                cur_duration = 0
                cur_state = PlayerState.PLAYING
                with self._lock:
                    if self.current_video_id == vid:
                        duration = int(info.get("duration", 0) or 0)
                        if duration > 0:
                            self.current_duration = duration
                        elif self.current_duration <= 0:
                            _ = self.current_duration
                        cur_duration = self.current_duration
                        cur_vid = self.current_video_id
                        cur_state = self.state
                if cur_vid:
                    cur_time = self.get_time()
                    for s in self.sessions:
                        try:
                            s.report_now_playing(
                                cur_vid,
                                cur_time,
                                cur_duration,
                                cur_state,
                                current_index=self.current_index,
                            )
                            s.report_now_playing_playlist(
                                self.playlist or [cur_vid],
                                cur_vid,
                                self.current_index,
                                cur_time,
                                cur_duration,
                                cur_state,
                            )
                            s.report_state_change(cur_state, cur_time, cur_duration)
                        except Exception:
                            pass
                self._kick_prefetch()
            except Exception:
                pass
        threading.Thread(target=_refresh, name="QueueSync", daemon=True).start()
        return True

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
        with self._lock:
            self._requested_id = None
            self.current_duration = 0
        for s in self.sessions:
            try:
                s.report_state_change(PlayerState.STOPPED, 0, 0)
            except Exception:
                pass

    def _on_playback_ended(self) -> None:
        self.state = PlayerState.STOPPED
        with self._lock:
            self._requested_id = None
            self.current_duration = 0
        for s in self.sessions:
            try:
                s.report_state_change(PlayerState.STOPPED, 0, 0)
            except Exception:
                pass

        # Advance playlist. Kodi fires Ended for a manual skip too — but in
        # that case it has ALREADY started the next item itself and
        # onPlayBackStarted (with _sync_current_from_kodi) adopts it. The
        # only signal distinguishing natural end from manual skip is whether
        # the bridge already moved off the ended item: natural end leaves
        # current_video_id on the finished track; a manual skip's Started
        # event fires first and syncs it forward. So: advance only if the
        # ended item is still current.
        #
        # NOTE: current_video_id is only trustworthy here because
        # _sync_current_from_kodi() runs on EVERY onPlayBackStarted. If the
        # manual skip's Started event hasn't been processed yet (race), this
        # may still double-start; the generation guard in _play_video drops
        # the stale one.
        with self._lock:
            kodi_queue_mode = self._kodi_queue_mode
            if self.playlist and self.current_index + 1 < len(self.playlist):
                next_id = self.playlist[self.current_index + 1]
                if self.current_video_id == self.playlist[self.current_index]:
                    if kodi_queue_mode:
                        # Music-queue mode: the item played through Kodi's own
                        # playlist, which auto-advances itself (next item's
                        # onPlayBackStarted + _sync_current_from_kodi adopts
                        # it). Spawning our own _play_video here would race
                        # Kodi's advance and restart the track from 0.
                        logger.debug("Ended in Kodi-queue mode; letting Kodi auto-advance to %s", next_id)
                        return
                    self.current_index += 1
                    self._requested_id = next_id
                    logger.info("Auto-advancing to next video: %s", next_id)
                    self._play_gen += 1
                    threading.Thread(target=self._play_video, args=(next_id, self._play_gen, self.current_theme), daemon=True).start()
                else:
                    logger.debug("Ended after manual skip to %s; Kodi already playing it",
                                 self.current_video_id)
