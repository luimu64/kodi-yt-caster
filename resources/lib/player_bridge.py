"""Kodi Player bridge and playback state synchronizer."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from .resolver import VideoResolver
from .lounge.session import LoungeSession
from .session_state import (
    SessionState, StateOwner, SetPlaylistEvent, UpdatePlaylistEvent, PlayEvent,
    PauseEvent, ResumeEvent, PauseDetectedEvent, ResumeDetectedEvent, SeekToEvent,
    SetVolumeEvent, StopVideoEvent, NextEvent, PlaybackStartedEvent, KodiAdvancedEvent,
    PlaybackEndedEvent, ResolverCompletedEvent, KodiStateObservedEvent, SignalUnknownEvent,
    PositionTickEvent
)
from . import preloader

logger = logging.getLogger("ytlounge.player")


class PlayGate:
    """Shared gate ensuring at most ONE stream resolve/start is in flight.

    Module-level singleton: every command source (YouTube `cl`, YouTube
    Music `m`, Kodi-UI queue picks, auto-advance, plugin resolves) funnels
    through the same gate object, so a setPlaylist burst from one app can
    never overlap a resolve from the other. The 2026-09-14 lag incident:
    three concurrent yt-dlp subprocess launches on a 4-core Pi starved the
    service process holding the manifest server's GIL, Kodi's curl timed
    out STATing the localhost master for 20s+ and the player stalled.
    A newer request BUMPS (release-and-preempt) an older one instead of
    queueing blindly: the older work is worthless once a newer gen exists.
    Yields the gate during the actual player.play() handoff so Kodi's own
    demuxer open (which can call back into us) cannot self-deadlock.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._owner_gen = 0       # gen currently inside the gate (0 = idle)
        self._requested_gen = 0   # highest play gen seen (supersede signal)
        self._video_id: Optional[str] = None

    def bump(self, gen: int) -> None:
        """Announce a new play request generation (call before acquiring)."""
        with self._cond:
            if gen > self._requested_gen:
                self._requested_gen = gen
            self._cond.notify_all()

    def acquire(self, gen: int, video_id: str, timeout: float = 90.0) -> bool:
        """Block until this request is the newest one AND the gate is free.

        Returns False if superseded by a newer generation while waiting.
        """
        with self._cond:
            if gen > self._requested_gen:
                self._requested_gen = gen
            deadline = time.monotonic() + timeout
            while self._owner_gen and self._owner_gen != gen:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("PlayGate: wait timeout for gen=%s (%s)", gen, video_id)
                    return False
                self._cond.wait(timeout=min(remaining, 5.0))
                if gen < self._requested_gen:
                    return False  # superseded while waiting
            self._owner_gen = gen
            self._video_id = video_id
            return True

    def release(self, gen: int) -> None:
        with self._cond:
            if self._owner_gen == gen:
                self._owner_gen = 0
                self._video_id = None
            self._cond.notify_all()

PLAY_GATE = PlayGate()

# Preload proxying: serve media segments from this process's localhost server.
# OFF on purpose — see the comment at its use site in _play_video_locked.
PRELOAD_PROXY_ENABLED = False

# How long after a music-queue item change the monitor loop keeps re-asserting
# the music (visualisation) window. Sized from device behaviour: Kodi's
# PlaybackCleanup for the outgoing video popped the window back to the GUI
# ~9-12s after the next item had already started, so the window has to be
# re-asserted across that window; after it the loop stops, so a deliberate GUI
# browse is not fought.
MUSIC_WINDOW_ENGAGE_SECONDS = 30.0

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
        initial_state: Optional["SessionState"] = None,
        store=None,
        on_apply=None,
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
        # Single in-process owner of the session facts (R1/R3). Reads go through
        # the read-only properties below; writes go through self.owner.apply().
        self.pending_seek: Optional[float] = None
        self.store = store
        self._save_hook = on_apply
        self._last_projected_version: Optional[int] = None
        self._owner = StateOwner(
            initial_state if initial_state is not None else SessionState(),
            on_apply=self._handle_state_applied,
        )
        for s in self.sessions:
            s.attach_state_owner(self._owner)
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
        # Time-stall pause detection: getCondVisibility("Player.Paused") is
        # unreliable inside the service process (verified returning False while
        # the audio player was paused), and onPlayBackPaused/Resumed callbacks
        # do not fire reliably. The one signal that never lies: a live player
        # whose getTime() stops advancing.
        self._last_poll_time: Optional[int] = None
        self._stall_polls = 0
        self._last_stall_time: Optional[int] = None
        # Playback-transition quiet window (monotonic deadline): while a play is
        # being handed to Kodi — the outgoing item's post-stop STAT, the video
        # window teardown, the new player opening — this process must keep the
        # localhost manifest server responsive, so background resolve/preload
        # work is held back. A starved server makes Kodi's STAT time out and
        # defers OnPlayBackStopped/PlaybackCleanup with it.
        self._transition_until: float = 0.0
        self._viz_inflight = False
        # Last item re-opened because Kodi started it in the video player's mode.
        self._lane_repaired_id: Optional[str] = None

        if KODI_AVAILABLE:
            self._kodi_player = self._create_kodi_player()
        else:
            self._kodi_player = None

    @property
    def owner(self) -> StateOwner:
        return self._owner

    @owner.setter
    def owner(self, new_owner: StateOwner) -> None:
        self._owner = new_owner
        if self._owner._on_apply is None:
            self._owner._on_apply = self._handle_state_applied
        for s in self.sessions:
            s.attach_state_owner(new_owner)

    def _handle_state_applied(self, new_state: SessionState) -> None:
        self.apply_projection(new_state)
        for s in self.sessions:
            s.notify_state_changed(new_state)
        if self._save_hook is not None:
            try:
                self._save_hook(new_state)
            except Exception:
                pass

    def announce_state(self) -> None:
        """Ask every channel to re-publish the shared snapshot now (R9).

        The phone needs an immediate report on connect / getNowPlaying, but that
        is a transport concern: no channel invents its own report, each just
        re-sends the one in-process state.
        """
        for s in self.sessions:
            try:
                s.force_publish()
            except Exception:
                logger.debug("announce_state failed", exc_info=True)

    def apply_projection(self, snapshot: Optional[SessionState] = None) -> None:
        """Sole reconciler for Kodi playlist contents, track ordering, item titles,
        and active GUI window (12005 vs 12006).

        Executes strictly when snapshot.version changes (Rule R4). Consecutive calls
        with identical version perform zero mutations.
        """
        if snapshot is None:
            snapshot = self.owner.snapshot()

        with self._lock:
            if snapshot.version == self._last_projected_version:
                return
            self._last_projected_version = snapshot.version

            if not (KODI_AVAILABLE and xbmc):
                return

            # 1. Reconcile playlist contents, ordering, and pre-cached titles
            #    Kodi's music playlist is a projection of the snapshot queue. A
            #    music-app cast (lane "m") may start on a music video yet still
            #    need the playlist for Kodi's own auto-advance, so a non-music
            #    projection must not destroy it. Only clear the music playlist for
            #    a genuine video-lane snapshot (a video queue advances through the
            #    bridge, not Kodi's music playlist).
            if self._kodi_queue_mode or (snapshot.lane or "") == "m":
                try:
                    playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
                    self._reconcile_playlist(playlist, snapshot)
                    # Keep Kodi's own playlist cursor on the snapshot's active item
                    # so its native auto-advance lands on the next queue entry even
                    # when the active item played on the video lane.
                    if hasattr(playlist, "_position") and snapshot.current_index is not None:
                        playlist._position = snapshot.current_index
                except Exception as exc:
                    logger.debug("Playlist reconciliation error: %s", exc)
            elif (snapshot.lane or "") != "m":
                try:
                    playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
                    if playlist.size() > 0:
                        playlist.clear()
                except Exception:
                    pass

            # 2. Check for player mode misclassification on music lane
            is_music_lane = (snapshot.lane or "") == "m"
            if is_music_lane and self._kodi_player and self._music_lane_playing():
                try:
                    if self._kodi_player.isPlayingVideo():
                        vid = snapshot.current_video_id
                        if vid and vid != self._lane_repaired_id:
                            self._lane_repaired_id = vid
                            position = self.get_time()
                            try:
                                info = self.resolver.resolve(vid)
                            except Exception:
                                info = {}
                            wants_audio = bool(info.get("audio_url")) and (
                                self.music_visualizer == "always"
                                or (self.music_visualizer != "never" and info.get("is_static_art"))
                            )
                            if wants_audio:
                                logger.info(
                                    "Kodi opened music item %s in video player mode — re-opening on music lane at %ss",
                                    vid, position)
                                self.play_video_id(vid, position)
                                return
                except Exception as exc:
                    logger.debug("Music lane check error: %s", exc)

            # 3. Reconcile active GUI windows (12005 vs 12006)
            if snapshot.play_state == PlayerState.PLAYING:
                if is_music_lane:
                    self._project_music_window()
                else:
                    self._project_video_window()

    def _reconcile_playlist(self, playlist, snapshot: SessionState) -> None:
        """Reconcile Kodi's music playlist with snapshot.playlist and pre-cached titles."""
        expected_vids = list(snapshot.playlist)
        expected_urls = [f"plugin://plugin.service.ytlounge-cast/?play={vid}" for vid in expected_vids]
        expected_labels = [self._queue_titles.get(vid) or vid for vid in expected_vids]

        if not expected_vids:
            if playlist.size() > 0:
                playlist.clear()
            return

        def _make_item(vid: str, label: str):
            if not (KODI_AVAILABLE and xbmcgui):
                return None
            li = xbmcgui.ListItem(label=label)
            li.setArt({
                "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
                "icon": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            })
            return li

        if playlist.size() == 0:
            for vid, url, label in zip(expected_vids, expected_urls, expected_labels):
                playlist.add(url, _make_item(vid, label))
            return

        if hasattr(playlist, "get_entry"):
            current_entries = [playlist.get_entry(i) for i in range(playlist.size())]
            current_urls = [e.url for e in current_entries]
            current_labels = [e.label for e in current_entries]

            if current_urls == expected_urls and current_labels == expected_labels:
                return

            pos = playlist.getposition()

            if current_urls == expected_urls:
                for idx, (vid, url, exp_lbl, cur_lbl) in enumerate(zip(expected_vids, expected_urls, expected_labels, current_labels)):
                    if exp_lbl != cur_lbl and idx != pos:
                        playlist.remove(url)
                        playlist.add(url, _make_item(vid, exp_lbl), idx)
                return

            # Remove items that no longer belong in playlist
            for u in list(current_urls):
                if u not in expected_urls:
                    playlist.remove(u)

            # Reconcile ordering and labels
            for idx, (vid, url, label) in enumerate(zip(expected_vids, expected_urls, expected_labels)):
                if idx >= playlist.size():
                    playlist.add(url, _make_item(vid, label))
                elif playlist.get_entry(idx).url != url:
                    if idx != pos:
                        if url in [playlist.get_entry(i).url for i in range(playlist.size())]:
                            playlist.remove(url)
                        playlist.add(url, _make_item(vid, label), idx)
                elif playlist.get_entry(idx).label != label and idx != pos:
                    playlist.remove(url)
                    playlist.add(url, _make_item(vid, label), idx)

    def _project_music_window(self) -> None:
        """Project music visualization window (12006)."""
        if not (KODI_AVAILABLE and xbmc):
            return
        if not self._visualisation_is_active():
            try:
                xbmc.executebuiltin("ActivateWindow(12006)")
            except Exception:
                pass
        self._activate_visualizer()

    def _project_video_window(self) -> None:
        """Project fullscreen video window (12005)."""
        if not (KODI_AVAILABLE and xbmc):
            return
        if self._visualisation_is_active():
            try:
                xbmc.executebuiltin("ActivateWindow(12005)")
            except Exception:
                pass

    # --- Session facts: read-only views over the single owner (R1) ---
    # Every read routes to the owner; the only write path is owner.apply().
    @property
    def state(self) -> int:
        return self.owner.play_state

    @property
    def playlist(self) -> List[str]:
        return list(self.owner.playlist)

    @property
    def current_index(self) -> int:
        return self.owner.current_index

    @property
    def current_video_id(self) -> Optional[str]:
        return self.owner.current_video_id

    @property
    def list_id(self) -> str:
        return self.owner.list_id

    @property
    def current_theme(self) -> Optional[str]:
        return self.owner.lane

    @property
    def volume(self) -> int:
        return self.owner.volume

    @property
    def position(self) -> float:
        return self.owner.position

    @property
    def current_duration(self) -> int:
        d = self.owner.duration
        if d <= 0:
            player = getattr(self, "_kodi_player", None)
            if KODI_AVAILABLE and player:
                try:
                    if player.isPlaying():
                        total = player.getTotalTime()
                        if total is not None and total > 0:
                            # Keep the owner the single source: fold the
                            # live total into it, not a parallel field.
                            self.owner.apply(KodiStateObservedEvent(duration=float(total)))
                except Exception:
                    pass
        return max(0, int(self.owner.duration))

    def snapshot(self) -> SessionState:
        """Return an immutable detached SessionState snapshot from the owner."""
        return self.owner.snapshot()

    def get_duration(self) -> int:
        """Return total duration in seconds, querying Kodi player if unknown."""
        return self.current_duration

    def _create_kodi_player(self):
        parent = self

        class SubclassPlayer(xbmc.Player):
            def __init__(self):
                super().__init__()

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
        for s in self.sessions:
            try:
                s.close()
            except Exception:
                pass

    def _position_loop(self) -> None:
        last_wake = time.monotonic()
        while not self._monitor_stop.is_set():
            time.sleep(2.0)
            drift = time.monotonic() - last_wake - 2.0
            last_wake = time.monotonic()
            if drift > 5.0:
                self._dump_thread_stacks(drift)
            self._poll_pause_state()
            if KODI_AVAILABLE and self._kodi_player:
                # R8: the tick is a checker, not a writer. It compares the
                # snapshot against the player and emits disagreements as
                # events; the reducer resolves them.
                self._reconcile_tick()

    def _reconcile_tick(self) -> None:
        """Compare the snapshot against the Kodi player once per tick (R8).

        One reconciler only. Every disagreement is emitted as an event and
        resolved by the reducer; a quiet tick emits nothing. Never guesses:
        a state the player cannot explain is published as UNKNOWN (R6).

        Logs both sides for every corrective event:
        ``reconcile: snapshot=<x> player=<y> -> event=<z>``
        """
        if not (KODI_AVAILABLE and self._kodi_player):
            return
        try:
            playing = bool(self._kodi_player.isPlaying())
        except Exception:
            return

        # 1. Item identity: which item is Kodi actually playing? On Kodi 21
        #    getPlayingFile() returns the final googlevideo URL (no video id),
        #    so read the info label carrying the plugin:// ?play=<id> URL.
        kodi_vid = None
        try:
            playing_url = xbmc.getInfoLabel("Player.FileNameAndPath")  # type: ignore[union-attr]
        except Exception:
            playing_url = None
        if playing_url and "play=" in playing_url:
            kodi_vid = playing_url.split("play=")[-1].split("&")[0] or None

        if not playing:
            # Player is gone: if we still believe something plays, that is a
            # contradiction. One corrective event, resolved by the reducer.
            if self.state != PlayerState.STOPPED:
                logger.info(
                    "reconcile: snapshot=playing(%s) player=stopped -> event=playbackStopped",
                    self.current_video_id)
                self._on_playback_stopped()
            return

        if not kodi_vid:
            # Alive player, no readable plugin URL. A momentary label gap is not
            # a reason to contradict the phone (that would flap); the stored
            # item stays authoritative until the label is readable again. Only
            # when we have NO item at all is the item fact genuinely unknown.
            # (R6: unknown is publishable, a guess is not.)
            if self.state == PlayerState.PLAYING and not self.current_video_id:
                logger.info(
                    "reconcile: snapshot=playing(no item) player=alive/unidentified"
                    " -> event=signalUnknown")
                self.owner.apply(SignalUnknownEvent(source="player-clock"))
            return

        # 2. Item differs -> adopt the player's item through the reducer, then
        #    fall through: the newly adopted item's clock is folded in this
        #    same tick, exactly as the pre-R8 watchdog loop did.
        if kodi_vid != self.current_video_id:
            logger.info(
                "reconcile: snapshot=%s player=%s -> event=kodiAdvanced",
                self.current_video_id, kodi_vid)
            self._sync_current_from_kodi(playing_url)

        # 3. Play state: broken signals (getCondVisibility returns False while
        #    paused) mean we cannot assert PLAYING from the live player alone.
        #    If we believe PAUSED and the clock has advanced, that is a resume.
        #    If we believe an unpaused state and the player is alive, nothing
        #    to correct. The stall detector owns the pause direction.
        try:
            cur_time = float(self.get_time())
            cur_duration = float(self.current_duration)
        except Exception:
            return

        # 4. Position/duration: one event carrying the observed clock (R6:
        #    source=player-clock), only when it actually differs.
        if (self.state != PlayerState.PAUSED
                and (cur_time != self.owner.position or cur_duration != self.owner.duration)):
            self.owner.apply(PositionTickEvent(
                position=cur_time,
                duration=cur_duration,
                play_state=self.state,
                source="player-clock",
            ))

        # 5. GUI drift is log-only (R4: ticks perform zero mutations).
        self._report_drift_log_only()

    def _poll_pause_state(self) -> None:
        """Detect pause via time-stall: a live Kodi player whose getTime()
        stops advancing across consecutive 2s polls is paused. Covers both
        broken-signal cases verified live: getCondVisibility("Player.Paused")
        returning False inside the service process, and
        onPlayBackPaused/Resumed callbacks never firing (TV-side JSONRPC pause).

        Requires duration loaded (skip buffering stalls) and two consecutive
        zero-advance polls (hysteresis against demuxer hiccups).
        """
        if not (KODI_AVAILABLE and self._kodi_player) or not self.current_video_id:
            self._last_poll_time = None
            self._stall_polls = 0
            return
        try:
            if not self._kodi_player.isPlaying():
                self._last_poll_time = None
                self._stall_polls = 0
                return
            cur = int(self._kodi_player.getTime())
        except Exception:
            return
        dur = self.current_duration
        if not dur or cur >= dur - 2:
            # No trustworthy clock yet, or track finished.
            self._last_poll_time = None
            self._stall_polls = 0
            return
        if self._last_poll_time is not None and cur == self._last_poll_time:
            self._stall_polls += 1
        else:
            self._stall_polls = 0
            self._last_stall_time = None
        self._last_poll_time = cur
        if self._stall_polls >= 2:
            self._last_stall_time = cur
            if self.state != PlayerState.PAUSED:
                # R6: the clock stopped while the player claims to be playing and
                # the duration is loaded — the only remaining explanation is a
                # pause. Source recorded so a wrong inference is attributable.
                logger.info(
                    "pause detected via time-stall (t=%s x%s polls, source=player-clock)",
                    cur, self._stall_polls)
                self._on_playback_paused()
        elif self._stall_polls == 0 and self.state == PlayerState.PAUSED:
            # Clock moving again while we believe paused: TV-side resume.
            # Require real progress (>= 2s past the stall timestamp): right
            # after a pause command the Kodi audio engine is still draining
            # its buffer and the clock can advance a beat before the pause
            # bites — treating that as "resumed" flipped the bridge back to
            # PLAYING and the phone spammed pause again (field flap).
            base = self._last_stall_time
            if base is None or cur >= base + 2:
                logger.info("Resume detected via time advance (t=%s)", cur)
                self._on_playback_resumed()

    def _resync_index(self) -> None:
        """Index is owned by the reducer (SetPlaylistEvent / UpdatePlaylistEvent
        re-derive it), so there is nothing to re-derive here. Kept for call-site
        compatibility."""
        pass

    def set_playlist(self, data: Dict[str, Any]) -> None:
        """Handle setPlaylist command from YouTube mobile app."""
        with self._lock:
            video_id = data.get("videoId")
            video_ids_str = data.get("videoIds", "")
            current_time = float(data.get("currentTime", 0.0) or 0.0)
            list_id = str(data.get("listId") or "")
            if list_id:
                if self.list_id and self.list_id != list_id:
                    logger.info("Lounge listId changed: %s -> %s", self.list_id, list_id)
            # R1: the owner owns list_id, playlist, index and lane. Feed it the
            # queue; the reducer derives current_index and keeps lane.
            self.owner.apply(
                SetPlaylistEvent(
                    video_id=video_id or None,
                    video_ids=[v for v in video_ids_str.split(",") if v] or None,
                    list_id=list_id or None,
                    current_time=current_time,
                    theme=data.get("_theme") or None,
                )
            )
            target_id = self.owner.playlist[self.owner.current_index] if self.owner.playlist else video_id
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
                PLAY_GATE.bump(self._play_gen)  # wake/hold supersede for the shared stream gate
                threading.Thread(target=self._play_video, args=(target_id, self._play_gen, self.current_theme), daemon=True).start()

    def update_playlist(self, data: Dict[str, Any]) -> None:
        """Handle queue modifications (add, remove, reorder)."""
        with self._lock:
            video_ids_str = data.get("videoIds", "")
            if video_ids_str:
                self.owner.apply(
                    UpdatePlaylistEvent(video_ids=[v for v in video_ids_str.split(",") if v])
                )

    def play_video_id(self, video_id: str, seek_time: float = 0.0, theme: Optional[str] = None) -> None:
        with self._lock:
            self._requested_id = video_id
            self.owner.apply(PlayEvent(video_id=video_id, seek_time=seek_time, theme=theme))
            self.pending_seek = seek_time if seek_time > 0 else None
            self._play_gen += 1
            PLAY_GATE.bump(self._play_gen)  # wake/hold supersede for the shared stream gate
            threading.Thread(target=self._play_video, args=(video_id, self._play_gen, theme), daemon=True).start()

    def _resync_index_locked(self) -> None:
        # caller holds self._lock
        # No-op: the reducer (SetPlaylistEvent / PlayEvent) re-derives
        # current_index; there is no separate index field to patch here.
        pass

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

    def _begin_transition(self, seconds: float = 8.0) -> None:
        """Hold background resolve/preload work back during a playback handoff.

        Kodi STATs the outgoing file (``DoWork - Saving file state``) right after
        the player stops and before it processes the stop, and it opens the
        incoming file right after; both need this process to answer localhost
        HTTP. Saturating the interpreter here stalls playback itself.
        """
        self._transition_until = max(self._transition_until, time.monotonic() + seconds)

    def _transition_remaining(self) -> float:
        return max(0.0, self._transition_until - time.monotonic())

    def handoff_pending(self) -> bool:
        """True while a playback handoff is in flight.

        Public because the audio normalizer holds its ffmpeg child while this
        is set: heavy work in this process starves the localhost server Kodi
        STATs the outgoing file against, which stalls the handoff itself.
        """
        return self._transition_remaining() > 0.0

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
        if next_id == self.current_video_id:
            # Never background-resolve the item that is already playing: the
            # play path resolves it, and a deferred prefetch can land after it.
            return
        self._prefetch_id = next_id

        def _run() -> None:
            try:
                delay = self._transition_remaining()
                if delay > 0:
                    logger.info("Prefetch of %s held back %.1fs (playback transition)",
                                next_id, delay)
                    time.sleep(min(delay, 12.0))
                logger.info("Prefetching next video: %s", next_id)
                if self._play_gen != gen:
                    return
                info = self.resolver.resolve(next_id, prefetch=True)
                if self._play_gen != gen:
                    return
                # Preload the first ~60s of media itself: track change then
                # starts from warm disk instead of a cold CDN round-trip.
                # Never while a handoff is still settling — 72 parallel segment
                # fetches saturate this process and starve the manifest server.
                delay = self._transition_remaining()
                if delay > 0:
                    time.sleep(min(delay, 12.0))
                if not PRELOAD_PROXY_ENABLED:
                    # Proxy disabled: nothing will serve these segments, so
                    # don't burn the CPU (and the interpreter) fetching them.
                    return
                preloader.preload(next_id, info)
            except Exception:
                logger.debug("Prefetch of %s failed", next_id, exc_info=True)

        threading.Thread(target=_run, name="Prefetch", daemon=True).start()

    def _play_video(self, video_id: str, gen: int, theme: Optional[str] = None) -> None:
        logger.info("TIMING %s: _play_video start (gen=%s)", video_id, gen)
        # Shared gate: announce, then wait until we are the newest request AND
        # no other stream is resolving/starting (covers YT + YT Music together).
        PLAY_GATE.bump(gen)
        if not PLAY_GATE.acquire(gen, video_id):
            logger.info("Play request for %s superseded pre-resolve (gen=%s), dropping", video_id, gen)
            with self._lock:
                if gen == self._play_gen:
                    self._requested_id = None
            return
        logger.info("TIMING %s: play gate acquired (gen=%s)", video_id, gen)
        try:
            self._play_video_locked(video_id, gen, theme)
        finally:
            PLAY_GATE.release(gen)

    def _play_video_locked(self, video_id: str, gen: int, theme: Optional[str] = None) -> None:
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
            # Preload proxying is deliberately OFF: serving segment traffic from
            # this process let our HTTP server block on a slow remote fetch while
            # Kodi's post-stop STAT (a HEAD to the same server) waited behind it.
            # A stalled STAT delays OnPlayBackStopped/PlaybackCleanup, and that is
            # what left a stopped video's last frame on screen with the next item
            # never starting. The video lane now plays the localhost master
            # manifest (a few hundred bytes, static) and Kodi fetches the media
            # straight from the CDN.
            proxied = preloader.proxy_url(video_id, info) if PRELOAD_PROXY_ENABLED else None
        except Exception:
            proxied = None
        if proxied:
            playable_url = proxied

        with self._lock:
            if gen != self._play_gen:
                logger.info("Play request for %s superseded, dropping", video_id)
                return
            self.owner.apply(
                ResolverCompletedEvent(video_id=video_id, duration=int(info.get("duration", 0)))
            )
            self._active_gen = gen

        title = info.get("title") or "YouTube Video"
        logger.info("TIMING %s: resolved -> now calling player.play", video_id)
        self._notify("YouTube Cast", f"Now playing: {title}")

        # Prefetch of the next queue item is kicked per lane below: immediately
        # for a video-lane play (fast auto-advance), and only once the
        # audio-lane playback is actually running for a lane switch — a
        # background resolve/preload running through the handoff starves the
        # localhost manifest server, and Kodi's post-stop STAT then times out
        # (the stop is delayed with it and the video frame stays on screen).
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
                # Lane switch video -> audio: close the video player ourselves
                # before handing Kodi the audio play. Kodi's own cleanup only
                # leaves fullscreen video when the video player is already gone
                # (PlaybackCleanup), so a video still closing at this instant
                # leaves the VideoFullScreen window rendering its last frame on
                # top of the GUI while the audio plays underneath.
                self._stop_video_player_for_audio()

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
                # Lane switch / audio start: keep this process quiet across the
                # handoff (Kodi STATs the outgoing file and opens the incoming
                # one back-to-back) and only warm the next track once playback
                # is actually up — see _activate_visualizer.
                self._begin_transition()
                self._play_music_queue(video_id, info, list_item)
            else:
                self._kodi_queue_mode = False
                # A video owns the screen: stop re-asserting the music window.
                self._lane_repaired_id = None
                player = self._kodi_player if self._kodi_player is not None else (xbmc.Player() if xbmc else None)
                if player:
                    player.play(playable_url, list_item)
                # Warm the cache for the next track (auto-advance then starts
                # without paying a cold resolve). Video lane only: the handoff is
                # a plain replace, so nothing needs to stay quiet here.
                self._kick_prefetch()
        else:
            self._kodi_queue_mode = False
            self.owner.apply(PlaybackStartedEvent(video_id=self.current_video_id, duration=float(self.current_duration)))

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
            kodi_player = self._kodi_player if self._kodi_player is not None else (xbmc.Player() if xbmc else None)
            if kodi_player:
                kodi_player.play(playlist, list_item, False, position)
            self._activate_visualizer()
        except Exception:
            logger.debug("music queue playback failed; direct play fallback", exc_info=True)
            kodi_player = self._kodi_player
            if kodi_player:
                kodi_player.play(info.get("audio_url") or info.get("playable_url"), list_item)
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

            def _run() -> None:
                try:
                    for vid in rest:
                        title = self._fetch_title_sync(vid)
                        if not title:
                            continue
                        self._store_queue_title(vid, title)
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
        """Passive stub (Rule R4: titles are projected on version change)."""
        pass

    def _stop_video_player_for_audio(self) -> None:
        """Stop the video player before handing Kodi an audio-lane play.

        Kodi only leaves the fullscreen video window when its own
        PlaybackCleanup runs with the video player already gone; when the video
        is still closing as the audio starts, that cleanup is skipped and the
        VideoFullScreen window keeps rendering the video's last frame on top of
        the GUI while the audio plays underneath. Stopping the video player
        first makes the lane switch deterministic.
        """
        if not (KODI_AVAILABLE and self._kodi_player):
            return
        try:
            if not self._kodi_player.isPlayingVideo():
                return
        except Exception:
            return
        logger.info("Lane switch to audio: stopping the video player first")
        try:
            self._kodi_player.stop()
        except Exception:
            logger.debug("video stop before audio failed", exc_info=True)
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if not self._kodi_player.isPlayingVideo():
                    break
            except Exception:
                break
            time.sleep(0.1)

    @staticmethod
    def _visualisation_is_active() -> bool:
        try:
            return bool(xbmc.getCondVisibility("Window.IsActive(visualisation)"))
        except Exception:
            return False

    @staticmethod
    def _fullscreen_video_window_active() -> bool:
        try:
            return bool(xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"))
        except Exception:
            return False

    def _music_lane_playing(self) -> bool:
        """True when the playing item is one of OUR music-queue items.

        Authoritative over ``isPlayingAudio()`` on this device. Device-verified:
        when Kodi advances to one of these items while the outgoing video player
        is still closing, the item is opened in the video player's mode and stays
        there for its whole duration — kodi.log shows the player opened, only
        ``CVideoPlayerAudio`` running (no video stream at all), yet
        ``isPlayingVideo()`` is True, ``isPlayingAudio()`` is False and window
        12005 is on screen with no music view. The playing file being our own
        ``plugin://…?play=`` URL is the reliable signal for the audio/visualiser
        lane: plugin.py hands Kodi the audio URL whenever the item has one, so a
        real music video never plays through the plugin path.
        """
        if not (KODI_AVAILABLE and self._kodi_player):
            return False
        try:
            if not self._kodi_player.isPlaying():
                return False
        except Exception:
            return False
        try:
            url = xbmc.getInfoLabel("Player.FileNameAndPath") or ""
        except Exception:
            return False
        return "plugin://" in url and "play=" in url

    def _activate_visualizer(self) -> None:
        """Route the GUI to the music/visualisation window (12006).

        ActivateWindow is REFUSED while a modal dialog is up — and Kodi shows
        its Busy dialog exactly during playback start, which is when this runs.
        Spawns a bounded retry worker that retries window activation until the
        dialog dismisses and the window sticks.
        """
        if not (KODI_AVAILABLE and xbmc) or self._viz_inflight:
            return
        self._viz_inflight = True

        def _run() -> None:
            deadline = time.monotonic() + 15.0
            prefetched = False
            try:
                while time.monotonic() < deadline and not self._monitor_stop.is_set():
                    try:
                        if (self.owner.lane or "") != "m" or self.owner.play_state != PlayerState.PLAYING:
                            break
                        if self._kodi_player and (self._kodi_player.isPlayingAudio()
                                                  or self._music_lane_playing()):
                            if not self._visualisation_is_active():
                                xbmc.executebuiltin("ActivateWindow(12006)")
                            if self._visualisation_is_active():
                                if not prefetched:
                                    logger.info("Visualisation window active")
                                    self._kick_prefetch()
                                    prefetched = True
                            else:
                                logger.info("ActivateWindow(12006) did not take (modal dialog?) — retrying")
                        time.sleep(0.5)
                    except Exception:
                        break
                if not self._visualisation_is_active():
                    logger.warning("Visualisation window never became active")
            finally:
                self._viz_inflight = False

        threading.Thread(target=_run, daemon=True, name="VizActivator").start()

    def _report_drift_log_only(self) -> None:
        """Passive log-only drift reporting (R4: timer ticks perform zero mutations)."""
        if not (KODI_AVAILABLE and self._kodi_player):
            return
        try:
            if (self._kodi_player.isPlayingAudio()
                    and not self._kodi_player.isPlayingVideo()
                    and self._fullscreen_video_window_active()):
                logger.debug("Drift detected: fullscreen video window active while audio playing")
            if (self._music_lane_playing()
                    and not self._visualisation_is_active()):
                logger.debug("Drift detected: music window not active while music lane playing")
        except Exception:
            pass

    def _repair_music_lane_start(self) -> None:
        """Passive log-only check (R4)."""
        self._report_drift_log_only()

    def _ensure_music_window(self) -> None:
        """Passive log-only check (R4)."""
        self._report_drift_log_only()

    @staticmethod
    def _dump_thread_stacks(drift: float) -> None:
        """Log every thread stack when this process was starved of the GIL.

        A starved interpreter makes the localhost manifest server stop
        answering; Kodi's post-stop STAT then times out, the video player never
        finishes closing, PlaybackCleanup never starts the pending item, and the
        stopped video's last frame stays on screen. This dump names the hog.
        """
        try:
            import sys
            import traceback
            logger.warning("Monitor loop starved %.1fs - thread stacks:", drift)
            for tid, frame in sys._current_frames().items():
                logger.warning("thread %s:\n%s", tid,
                               "".join(traceback.format_stack(frame)[-5:]))
        except Exception:
            pass

    def _repair_fullscreen_windows(self) -> None:
        """Passive log-only check (R4)."""
        self._report_drift_log_only()

    def _is_paused(self) -> bool:
        # xbmc.Player.isPlaying() returns True WHILE PAUSED, so it cannot
        # distinguish pause from play. Ask Kodi's GUI conditions and internal state.
        if self.state == PlayerState.PAUSED:
            return True
        if KODI_AVAILABLE and xbmc:
            try:
                return bool(xbmc.getCondVisibility("Player.Paused"))
            except Exception:
                pass
        return False

    def pause(self) -> None:
        # Nothing loaded (stopped/never started) -> pausing is a no-op. A
        # fire-and-forget pause applied here would claim PAUSED on an empty
        # player, a state the phone cannot map and the reconciler would have to
        # undo (R6/R8: no asserting a state no source supports).
        if not self.current_video_id or self.owner.play_state == PlayerState.STOPPED:
            logger.info("pause ignored: nothing loaded (state=%s)", self.owner.play_state)
            return
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            # pause() TOGGLES: guard so pause-while-paused does not resume.
            if not self._is_paused():
                self._kodi_player.pause()
        self.owner.apply(PauseEvent())

    def resume(self) -> None:
        if KODI_AVAILABLE and self._kodi_player:
            if self._is_paused():
                # Kodi's pause() toggles pause/resume.
                self._kodi_player.pause()
            elif self._kodi_player.isPlaying():
                # Actively playing: nothing to do.
                return
            elif self.current_video_id:
                # Stopped/idle: restart the current item where we left off.
                self.play_video_id(self.current_video_id, self.get_time())
                return
        self.owner.apply(ResumeEvent())

    def stop(self) -> None:
        with self._lock:
            self._play_gen += 1
            PLAY_GATE.bump(self._play_gen)  # wake/hold supersede for the shared stream gate
            self._active_gen = self._play_gen
            self._requested_id = None
            self.owner.apply(StopVideoEvent())
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            self._kodi_player.stop()

    def seek_to(self, seconds: float) -> None:
        if KODI_AVAILABLE and self._kodi_player and self._kodi_player.isPlaying():
            self._kodi_player.seekTime(seconds)
        else:
            with self._lock:
                self.pending_seek = seconds
        self.owner.apply(SeekToEvent(position=seconds))

    def get_time(self) -> int:
        if KODI_AVAILABLE and self._kodi_player:
            try:
                # isPlaying() is True while paused too, so getTime() works in
                # both states (gating unconditionally returned 0 for paused
                # audio and froze the phone's position display). But in the
                # not-yet-loaded / just-stopped window getTime() raises
                # "Kodi is not playing any media file" (visible in kodi.log
                # as EXCEPTION lines), so only gate on the cover-state here.
                if not self._kodi_player.isPlaying():
                    return 0
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
                    # JSON-RPC error: report the last known volume (single source
                    # is the owner now) rather than a fabricated value.
                    return max(0, min(self.owner.volume, 100))
                vol = max(0, min(int(vol), 100))
                self.owner.apply(SetVolumeEvent(volume=vol))
                return vol
            except Exception:
                return max(0, min(self.owner.volume, 100))
        return max(0, min(self.owner.volume, 100))

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
        self.owner.apply(SetVolumeEvent(volume=volume))

    # Callbacks from Kodi player
    def _on_playback_started(self) -> None:
        logger.info("TIMING %s: Kodi onPlayBackStarted fired (video visible)", self.current_video_id)
        # Kodi told us playback is visible; fold that observation into the owner
        # (state-only: duration/video were already set by the play thread).
        self.owner.apply(KodiStateObservedEvent(play_state=PlayerState.PLAYING))
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

    def _sync_current_from_kodi(self, playing_url: Optional[str] = None) -> bool:
        """Align bridge state with what Kodi is actually playing.

        Returns True if the playing item differs from current_video_id.

        playing_url: optional pre-fetched URL (probe callers pass the
        info-label value here — on Kodi 21 getPlayingFile() returns the final
        googlevideo URL without the video id, so it cannot parse a vid).
        """
        if not (KODI_AVAILABLE and self._kodi_player):
            return False
        if playing_url is None:
            try:
                playing_url = xbmc.getInfoLabel("Player.FileNameAndPath")
            except Exception:
                playing_url = None
            if not playing_url:
                try:
                    playing_url = self._kodi_player.getPlayingFile()
                except Exception:
                    return False
        if not playing_url or "play=" not in playing_url:
            return False
        import urllib.parse
        try:
            qs = urllib.parse.urlparse(playing_url).query
            vid = dict(urllib.parse.parse_qsl(qs)).get("play")
        except Exception:
            return False
        if not vid or vid == self.current_video_id:
            return False
        logger.info("Queue pick via Kodi UI: %s -> %s", self.current_video_id, vid)
        with self._lock:
            self._requested_id = vid
            # Owner owns video id / index / duration: fold the queue-pick
            # adoption into it (PLAYING, position 0, index re-derived).
            self.owner.apply(KodiAdvancedEvent(video_id=vid))
            self._play_gen += 1
            PLAY_GATE.bump(self._play_gen)  # wake/hold supersede for the shared stream gate
            self._active_gen = self._play_gen

        if "plugin://" in playing_url:
            self._lane_repaired_id = None

        # Duration unknown until resolve; refresh it (and the preload chain)
        # in the background without blocking the state reports below.
        def _refresh() -> None:
            try:
                info = self.resolver.resolve(vid)
            except Exception:
                info = {}
            try:
                with self._lock:
                    if self.current_video_id == vid:
                        duration = int(info.get("duration", 0) or 0)
                        if duration > 0:
                            self.owner.apply(ResolverCompletedEvent(video_id=vid, duration=duration))
                self._kick_prefetch()
            except Exception:
                pass
        threading.Thread(target=_refresh, name="QueueSync", daemon=True).start()
        return True

    def _on_playback_paused(self) -> None:
        # TV-side pause (player clock): fold the observation into the owner.
        cur_time = self.get_time()
        self.owner.apply(PauseDetectedEvent(position=cur_time))

    def _on_playback_resumed(self) -> None:
        # TV-side resume (player clock): fold the observation into the owner.
        cur_time = self.get_time()
        self.owner.apply(ResumeDetectedEvent(position=cur_time))

    def _on_playback_stopped(self) -> None:
        # Kodi fires Stopped after Ended for a naturally-finished item too;
        # if a newer play generation is already active, this stop is stale.
        with self._lock:
            if self._active_gen != self._play_gen:
                logger.debug("Ignoring stale onPlayBackStopped (active=%s, latest=%s)",
                             self._active_gen, self._play_gen)
                return
        self.owner.apply(StopVideoEvent())
        with self._lock:
            self._requested_id = None

    def _on_playback_ended(self) -> None:
        # Advance playlist. Kodi fires Ended for a manual skip too — but in
        # that case it has ALREADY started the next item itself and
        # onPlayBackStarted (with _sync_current_from_kodi) adopts it. The
        # only signal distinguishing natural end from manual skip is whether
        # the bridge already moved off the ended item: natural end leaves
        # current_video_id on the finished track; a manual skip's Started
        # event fires first and syncs it forward. So: advance only if the
        # ended item is still current.
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
                    self.owner.apply(KodiAdvancedEvent(video_id=next_id))
                    self._requested_id = next_id
                    logger.info("Auto-advancing to next video: %s", next_id)
                    self._play_gen += 1
                    PLAY_GATE.bump(self._play_gen)  # wake/hold supersede for the shared stream gate
                    threading.Thread(target=self._play_video, args=(next_id, self._play_gen, self.current_theme), daemon=True).start()
                    return
                else:
                    logger.debug("Ended after manual skip to %s; Kodi already playing it",
                                 self.current_video_id)
                    return
            self.owner.apply(StopVideoEvent())
            self._requested_id = None
