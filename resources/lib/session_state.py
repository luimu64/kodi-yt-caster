"""SessionState module: single in-process owner of session state and pure idempotent reducer.

Per design doc /opt/data/misc/lounge-state-sync/lounge-state-transfer.pdf §9 (R1, R3) and §9.2:
1. SessionState: immutable data class representing session state.
2. Snapshot immutability: snapshot() / copy accessors returning detached immutable state.
3. Explicit event vocabulary as first-class values covering §9.2:
   setPlaylist, updatePlaylist, play, pause, seekTo, setVolume, stopVideo,
   next/previous, kodi_advanced, kodi_state_observed, signal_unknown,
   resolver_completed, resolver_superseded.
4. Pure reducer (no side effects, no I/O, no clock reads) taking (state, event) -> new state.
5. Idempotence: applying the same event twice to the same starting state yields the identical
   resulting state, and replaying a duplicate event on the already-reduced state leaves state
   and version unchanged.
6. Monotonically increasing version counter v<N> bumped on each state reduction.
7. Formatted log line per reduction:
   `state v<N> <event> -> <changed field groups> (<source>)`
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import logging
import secrets
import string
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("ytlounge.state")


def generate_cpn() -> str:
    """Generate a random 16-character alphanumeric Client Playback Nonce (CPN)."""
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(secrets.choice(alphabet) for _ in range(16))


class PlayState:
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2
    UNKNOWN = -1


@dataclass(frozen=True)
class SessionState:
    """Immutable data class owning session state facts."""
    playlist: Tuple[str, ...] = ()
    current_index: int = 0
    current_video_id: Optional[str] = None
    list_id: str = ""
    position: float = 0.0
    duration: float = 0.0
    play_state: int = PlayState.STOPPED
    lane: Optional[str] = None
    volume: int = 100
    cpn: str = "kodi"
    version: int = 0

    def snapshot(self) -> SessionState:
        """Return an immutable copy of the state.

        Since SessionState is frozen with immutable fields (tuple playlist),
        deep-copying ensures absolute reference detachment.
        """
        return copy.deepcopy(self)

    @property
    def current_theme(self) -> Optional[str]:
        return self.lane

    @property
    def theme(self) -> Optional[str]:
        return self.lane

    @property
    def state(self) -> int:
        return self.play_state


# =====================================================================
# Event Vocabulary (First-Class Values)
# =====================================================================

@dataclass(frozen=True)
class Event:
    """Base event class."""
    event_id: Optional[str] = None
    source: str = "internal"

    @property
    def event_name(self) -> str:
        name = self.__class__.__name__
        if name.endswith("Event"):
            name = name[:-5]
        # Return lowerCamelCase or name matching the vocabulary
        if name:
            return name[0].lower() + name[1:]
        return name


# --- 1. Protocol / Command events (Phone / Remote Control) ---

@dataclass(frozen=True)
class SetPlaylistEvent(Event):
    video_id: Optional[str] = None
    video_ids: Sequence[str] = ()
    list_id: str = ""
    current_time: float = 0.0
    theme: Optional[str] = None
    cpn: Optional[str] = None
    source: str = "phone"


@dataclass(frozen=True)
class UpdatePlaylistEvent(Event):
    video_ids: Sequence[str] = ()
    source: str = "phone"


@dataclass(frozen=True)
class PlayEvent(Event):
    video_id: Optional[str] = None
    seek_time: Optional[float] = None
    theme: Optional[str] = None
    cpn: Optional[str] = None
    source: str = "phone"


# Backwards compatibility alias
PlayVideoEvent = PlayEvent


@dataclass(frozen=True)
class PauseEvent(Event):
    source: str = "phone"


@dataclass(frozen=True)
class ResumeEvent(Event):
    source: str = "phone"


@dataclass(frozen=True)
class SeekToEvent(Event):
    seconds: float = 0.0
    position: Optional[float] = None
    source: str = "phone"

    def __post_init__(self):
        if self.position is not None:
            object.__setattr__(self, "seconds", float(self.position))
        else:
            object.__setattr__(self, "position", float(self.seconds))


@dataclass(frozen=True)
class SetVolumeEvent(Event):
    volume: int = 100
    source: str = "phone"


@dataclass(frozen=True)
class StopVideoEvent(Event):
    source: str = "phone"


# Backwards compatibility alias
StopEvent = StopVideoEvent


@dataclass(frozen=True)
class NextEvent(Event):
    video_id: Optional[str] = None
    source: str = "phone"


@dataclass(frozen=True)
class PreviousEvent(Event):
    video_id: Optional[str] = None
    source: str = "phone"


# --- 2. Kodi Player & Observation events ---

@dataclass(frozen=True)
class KodiAdvancedEvent(Event):
    video_id: str = ""
    playing_url: Optional[str] = None
    source: str = "player"


@dataclass(frozen=True)
class KodiStateObservedEvent(Event):
    play_state: Optional[int] = None
    position: Optional[float] = None
    duration: Optional[float] = None
    video_id: Optional[str] = None
    volume: Optional[int] = None
    source: str = "player"


@dataclass(frozen=True)
class SignalUnknownEvent(Event):
    source: str = "player"


# Legacy position / player observation aliases
@dataclass(frozen=True)
class PositionTickEvent(Event):
    position: float = 0.0
    duration: Optional[float] = None
    play_state: Optional[int] = None
    source: str = "player-clock"


@dataclass(frozen=True)
class PauseDetectedEvent(Event):
    position: float = 0.0
    source: str = "player-clock"


@dataclass(frozen=True)
class ResumeDetectedEvent(Event):
    position: float = 0.0
    source: str = "player-clock"


@dataclass(frozen=True)
class PlaybackStartedEvent(Event):
    video_id: Optional[str] = None
    position: Optional[float] = None
    duration: Optional[float] = None
    source: str = "player"


@dataclass(frozen=True)
class PlaybackStoppedEvent(Event):
    source: str = "player"


@dataclass(frozen=True)
class PlaybackEndedEvent(Event):
    source: str = "player"


# --- 3. Resolver callbacks ---

@dataclass(frozen=True)
class ResolverCompletedEvent(Event):
    video_id: str = ""
    duration: float = 0.0
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    source: str = "resolver"


# Backwards compatibility alias
ResolverResolvedEvent = ResolverCompletedEvent


@dataclass(frozen=True)
class ResolverFailedEvent(Event):
    video_id: str = ""
    error: str = ""
    source: str = "resolver"


@dataclass(frozen=True)
class ResolverSupersededEvent(Event):
    video_id: str = ""
    source: str = "resolver"


# --- 4. Window & repair-loop events ---

@dataclass(frozen=True)
class WindowLaneRepairedEvent(Event):
    lane: str = ""
    source: str = "repair"


@dataclass(frozen=True)
class QueueTitlePatchedEvent(Event):
    video_id: str = ""
    title: str = ""
    source: str = "repair"


# =====================================================================
# Reducer and Helpers
# =====================================================================

def _derive_index(playlist: Tuple[str, ...], video_id: Optional[str], fallback_index: int = 0) -> int:
    """Derive current_index from current_video_id against the given playlist."""
    if playlist and video_id and video_id in playlist:
        return playlist.index(video_id)
    if playlist:
        return max(0, min(fallback_index, len(playlist) - 1))
    return 0

def published_index(snapshot: "SessionState") -> int:
    """Derive the index to publish (R5) from the stored queue at publication time.

    An index computed against a different list than the phone's is an identity
    mismatch even when ``listId`` is correct, so it is never carried as a field:
    it is recomputed here from the queue the report is actually sent with.

    If the active item is absent from the queue, the fallback is the stored
    index clamped into range — the report must never carry an out-of-range
    index (R5).
    """
    playlist = snapshot.playlist
    if not playlist:
        return 0
    return _derive_index(playlist, snapshot.current_video_id, snapshot.current_index)


def _diff_field_groups(old: SessionState, new: SessionState) -> List[str]:
    """Compute changed field groups between old and new state.

    Groups:
      - identity: list_id, playlist, current_index, current_video_id, cpn
      - playback: play_state, position, duration
      - volume: volume
      - lane: lane
    """
    groups: List[str] = []
    if (
        old.list_id != new.list_id
        or old.playlist != new.playlist
        or old.current_index != new.current_index
        or old.current_video_id != new.current_video_id
        or old.cpn != new.cpn
    ):
        groups.append("identity")

    if (
        old.play_state != new.play_state
        or old.position != new.position
        or old.duration != new.duration
    ):
        groups.append("playback")

    if old.volume != new.volume:
        groups.append("volume")

    if old.lane != new.lane:
        groups.append("lane")

    return groups


def format_reduction_log(version: int, event: Event, changed_groups: Sequence[str], source: str) -> str:
    """Format the mandatory single reduction log line:

    `state v<N> <event> -> <changed field groups> (<source>)`
    """
    groups_str = ", ".join(changed_groups) if changed_groups else "none"
    return f"state v{version} {event.event_name} -> {groups_str} ({source})"


def reduce(state: SessionState, event: Event) -> SessionState:
    """Pure, idempotent state reduction function taking (state, event) -> new SessionState.

    Pure:
      - No side effects, no I/O, no clock reads inside.
      - Given the same (state, event), produces the identical new state.

    Idempotent:
      - Applying the same event twice to the same starting state produces identical states.
      - Replaying an event that has already been applied (or is a duplicate with the same
        event_id or identical effective values) leaves state and version unchanged.

    Logs exactly one line per accepted reduction:
      `state v<N> <event> -> <changed field groups> (<source>)`
    """
    # 1. SetPlaylistEvent
    if isinstance(event, SetPlaylistEvent):
        new_playlist = tuple(event.video_ids) if event.video_ids else ((event.video_id,) if event.video_id else ())
        new_vid = event.video_id or (new_playlist[0] if new_playlist else None)
        new_idx = _derive_index(new_playlist, new_vid, 0)
        new_list_id = event.list_id if event.list_id else state.list_id
        new_theme = event.theme if event.theme is not None else state.lane
        new_pos = max(0.0, float(event.current_time))
        new_play_state = PlayState.PLAYING if new_vid else PlayState.STOPPED

        # Idempotence check: if playlist, list_id, item, index, pos, and state are already identical, no-op
        if (
            new_playlist == state.playlist
            and new_vid == state.current_video_id
            and new_idx == state.current_index
            and new_list_id == state.list_id
            and new_pos == state.position
            and new_play_state == state.play_state
            and (new_theme == state.lane or event.theme is None)
        ):
            return state

        # When explicitly casting a new playlist or re-initiating playback, assign CPN
        # A new item gets a new CPN: the previous item's nonce must never ride
        # along on a report about a different video (device log 2026-09-20:
        # 41/94 onStateChange reports carried the previous video's cpn).
        if event.cpn:
            new_cpn = event.cpn
        elif new_vid and new_vid != state.current_video_id:
            new_cpn = "cpn_" + new_vid
        else:
            new_cpn = state.cpn if state.cpn != "kodi" else "cpn_" + (new_vid or "cast")

        new_state = SessionState(
            playlist=new_playlist,
            current_index=new_idx,
            current_video_id=new_vid,
            list_id=new_list_id,
            position=new_pos,
            duration=0.0,
            play_state=new_play_state,
            lane=new_theme,
            volume=state.volume,
            cpn=new_cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 2. UpdatePlaylistEvent
    if isinstance(event, UpdatePlaylistEvent):
        new_playlist = tuple(event.video_ids)
        new_idx = _derive_index(new_playlist, state.current_video_id, state.current_index)
        if new_playlist == state.playlist and new_idx == state.current_index:
            return state

        new_state = SessionState(
            playlist=new_playlist,
            current_index=new_idx,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=state.position,
            duration=state.duration,
            play_state=state.play_state,
            lane=state.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 3. PlayEvent / PlayVideoEvent
    if isinstance(event, PlayEvent):
        new_vid = event.video_id or state.current_video_id
        new_playlist = state.playlist if state.playlist else ((new_vid,) if new_vid else ())
        new_idx = _derive_index(new_playlist, new_vid, state.current_index)
        new_theme = event.theme if event.theme is not None else state.lane
        new_pos = max(0.0, float(event.seek_time)) if event.seek_time is not None else state.position
        new_dur = 0.0 if (event.video_id and event.video_id != state.current_video_id) else state.duration

        # Idempotence: already playing this video at this position
        if (
            state.play_state == PlayState.PLAYING
            and new_vid == state.current_video_id
            and new_idx == state.current_index
            and new_pos == state.position
            and (new_theme == state.lane or event.theme is None)
        ):
            return state

        if event.cpn:
            new_cpn = event.cpn
        elif new_vid and new_vid != state.current_video_id:
            new_cpn = "cpn_" + new_vid
        else:
            new_cpn = state.cpn if state.cpn != "kodi" else ("cpn_" + (new_vid or "play"))

        new_state = SessionState(
            playlist=new_playlist,
            current_index=new_idx,
            current_video_id=new_vid,
            list_id=state.list_id,
            position=new_pos,
            duration=new_dur,
            play_state=PlayState.PLAYING,
            lane=new_theme,
            volume=state.volume,
            cpn=new_cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 4. PauseEvent / PauseDetectedEvent
    if isinstance(event, (PauseEvent, PauseDetectedEvent)):
        new_pos = max(0.0, float(event.position)) if isinstance(event, PauseDetectedEvent) else state.position
        if state.play_state == PlayState.PAUSED and state.position == new_pos:
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=new_pos,
            duration=state.duration,
            play_state=PlayState.PAUSED,
            lane=state.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 5. ResumeEvent / ResumeDetectedEvent
    if isinstance(event, (ResumeEvent, ResumeDetectedEvent)):
        new_pos = max(0.0, float(event.position)) if isinstance(event, ResumeDetectedEvent) else state.position
        if state.play_state == PlayState.PLAYING and state.position == new_pos:
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=new_pos,
            duration=state.duration,
            play_state=PlayState.PLAYING,
            lane=state.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 6. SeekToEvent
    if isinstance(event, SeekToEvent):
        new_pos = max(0.0, float(event.seconds))
        if new_pos == state.position:
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=new_pos,
            duration=state.duration,
            play_state=state.play_state,
            lane=state.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 7. SetVolumeEvent
    if isinstance(event, SetVolumeEvent):
        new_vol = max(0, min(int(event.volume), 100))
        if new_vol == state.volume:
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=state.position,
            duration=state.duration,
            play_state=state.play_state,
            lane=state.lane,
            volume=new_vol,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 8. StopVideoEvent / PlaybackStoppedEvent
    if isinstance(event, (StopVideoEvent, PlaybackStoppedEvent)):
        if state.play_state == PlayState.STOPPED and state.position == 0.0 and state.duration == 0.0:
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=0.0,
            duration=0.0,
            play_state=PlayState.STOPPED,
            lane=state.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 9. NextEvent
    if isinstance(event, NextEvent):
        if not state.playlist:
            return state
        if event.video_id is not None and event.video_id == state.current_video_id:
            return state
        next_idx = state.current_index + 1
        if next_idx >= len(state.playlist):
            return state
        next_vid = state.playlist[next_idx]
        new_cpn = "cpn_" + next_vid

        new_state = SessionState(
            playlist=state.playlist,
            current_index=next_idx,
            current_video_id=next_vid,
            list_id=state.list_id,
            position=0.0,
            duration=0.0,
            play_state=PlayState.PLAYING,
            lane=state.lane,
            volume=state.volume,
            cpn=new_cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 10. PreviousEvent
    if isinstance(event, PreviousEvent):
        if not state.playlist or state.current_index <= 0:
            return state
        if event.video_id is not None and event.video_id == state.current_video_id:
            return state
        prev_idx = state.current_index - 1
        prev_vid = state.playlist[prev_idx]
        new_cpn = "cpn_" + prev_vid

        new_state = SessionState(
            playlist=state.playlist,
            current_index=prev_idx,
            current_video_id=prev_vid,
            list_id=state.list_id,
            position=0.0,
            duration=0.0,
            play_state=PlayState.PLAYING,
            lane=state.lane,
            volume=state.volume,
            cpn=new_cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 11. KodiAdvancedEvent
    if isinstance(event, KodiAdvancedEvent):
        new_vid = event.video_id
        if not new_vid or new_vid == state.current_video_id:
            return state
        new_idx = _derive_index(state.playlist, new_vid, state.current_index)
        new_cpn = "cpn_" + new_vid

        new_state = SessionState(
            playlist=state.playlist,
            current_index=new_idx,
            current_video_id=new_vid,
            list_id=state.list_id,
            position=0.0,
            duration=0.0,
            play_state=PlayState.PLAYING,
            lane=state.lane,
            volume=state.volume,
            cpn=new_cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 12. KodiStateObservedEvent / PositionTickEvent / PlaybackStartedEvent
    if isinstance(event, (KodiStateObservedEvent, PositionTickEvent, PlaybackStartedEvent)):
        new_pos = max(0.0, float(event.position)) if event.position is not None else state.position
        new_dur = max(0.0, float(event.duration)) if event.duration is not None else state.duration
        if isinstance(event, PlaybackStartedEvent):
            new_state_val = PlayState.PLAYING
        elif isinstance(event, (KodiStateObservedEvent, PositionTickEvent)) and event.play_state is not None:
            new_state_val = event.play_state
        else:
            new_state_val = state.play_state
        new_vid = getattr(event, "video_id", None) or state.current_video_id
        new_idx = _derive_index(state.playlist, new_vid, state.current_index)
        new_vol = getattr(event, "volume", None)
        new_vol_val = max(0, min(int(new_vol), 100)) if new_vol is not None else state.volume

        if (
            new_pos == state.position
            and new_dur == state.duration
            and new_state_val == state.play_state
            and new_vid == state.current_video_id
            and new_idx == state.current_index
            and new_vol_val == state.volume
        ):
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=new_idx,
            current_video_id=new_vid,
            list_id=state.list_id,
            position=new_pos,
            duration=new_dur,
            play_state=new_state_val,
            lane=state.lane,
            volume=new_vol_val,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 13. SignalUnknownEvent
    if isinstance(event, SignalUnknownEvent):
        if state.play_state == PlayState.UNKNOWN:
            return state

        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=state.position,
            duration=state.duration,
            play_state=PlayState.UNKNOWN,
            lane=state.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 14. ResolverCompletedEvent / ResolverResolvedEvent
    if isinstance(event, (ResolverCompletedEvent, ResolverResolvedEvent)):
        if event.video_id == state.current_video_id:
            new_dur = max(0.0, float(event.duration))
            if new_dur != state.duration:
                new_state = SessionState(
                    playlist=state.playlist,
                    current_index=state.current_index,
                    current_video_id=state.current_video_id,
                    list_id=state.list_id,
                    position=state.position,
                    duration=new_dur,
                    play_state=state.play_state,
                    lane=state.lane,
                    volume=state.volume,
                    cpn=state.cpn,
                    version=state.version + 1,
                        )
                changed = _diff_field_groups(state, new_state)
                logger.info(format_reduction_log(new_state.version, event, changed, event.source))
                return new_state
        return state

    # 15. ResolverFailedEvent
    if isinstance(event, ResolverFailedEvent):
        if event.video_id == state.current_video_id:
            new_state = SessionState(
                playlist=state.playlist,
                current_index=state.current_index,
                current_video_id=state.current_video_id,
                list_id=state.list_id,
                position=0.0,
                duration=0.0,
                play_state=PlayState.STOPPED,
                lane=state.lane,
                volume=state.volume,
                cpn=state.cpn,
                version=state.version + 1,
                )
            changed = _diff_field_groups(state, new_state)
            logger.info(format_reduction_log(new_state.version, event, changed, event.source))
            return new_state
        return state

    # 16. ResolverSupersededEvent
    if isinstance(event, ResolverSupersededEvent):
        return state

    # 17. PlaybackEndedEvent
    if isinstance(event, PlaybackEndedEvent):
        if state.playlist and state.current_index + 1 < len(state.playlist):
            next_idx = state.current_index + 1
            next_vid = state.playlist[next_idx]
            new_state = SessionState(
                playlist=state.playlist,
                current_index=next_idx,
                current_video_id=next_vid,
                list_id=state.list_id,
                position=0.0,
                duration=0.0,
                play_state=PlayState.PLAYING,
                lane=state.lane,
                volume=state.volume,
                cpn="cpn_" + next_vid,
                version=state.version + 1,
                )
        else:
            new_state = SessionState(
                playlist=state.playlist,
                current_index=state.current_index,
                current_video_id=state.current_video_id,
                list_id=state.list_id,
                position=0.0,
                duration=0.0,
                play_state=PlayState.STOPPED,
                lane=state.lane,
                volume=state.volume,
                cpn=state.cpn,
                version=state.version + 1,
                )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 18. WindowLaneRepairedEvent
    if isinstance(event, WindowLaneRepairedEvent):
        if event.lane == state.lane:
            return state
        new_state = SessionState(
            playlist=state.playlist,
            current_index=state.current_index,
            current_video_id=state.current_video_id,
            list_id=state.list_id,
            position=state.position,
            duration=state.duration,
            play_state=state.play_state,
            lane=event.lane,
            volume=state.volume,
            cpn=state.cpn,
            version=state.version + 1,
        )
        changed = _diff_field_groups(state, new_state)
        logger.info(format_reduction_log(new_state.version, event, changed, event.source))
        return new_state

    # 19. QueueTitlePatchedEvent
    if isinstance(event, QueueTitlePatchedEvent):
        return state

    # Unknown events are inert, never fatal
    return state

# =====================================================================
# StateOwner: single mutable handle to the immutable SessionState (R1)
# =====================================================================


class StateOwner:
    """Single in-process owner of the session facts (R1).

    Wraps an immutable ``SessionState``. The only write path in the codebase
    is ``apply``, which funnels every change through the pure reducer ``reduce``
    and swaps the stored reference to the new immutable state. All other
    access is via read-only properties, so no caller ever mutates a field
    directly.
    """

    def __init__(self, state: SessionState, on_apply=None) -> None:
        self._state = state
        self._on_apply = on_apply

    def apply(self, event: "Event") -> SessionState:
        """Reduce and store. Returns the new immutable state (idempotent events
        leave version unchanged, per the reducer contract)."""
        new_state = reduce(self._state, event)
        self._state = new_state
        if self._on_apply is not None:
            try:
                self._on_apply(new_state)
            except Exception as exc:
                # never let a persistence or callback failure break state reduction
                logger.warning("Error in StateOwner on_apply callback: %s", exc)
        return new_state

    def snapshot(self) -> SessionState:
        """Return an immutable detached copy of the current state."""
        return self._state.snapshot()

    # --- canonical read-only accessors (the 9 facts + version) ---
    @property
    def playlist(self) -> Tuple[str, ...]:
        return self._state.playlist

    @property
    def current_index(self) -> int:
        return self._state.current_index

    @property
    def current_video_id(self) -> Optional[str]:
        return self._state.current_video_id

    @property
    def list_id(self) -> str:
        return self._state.list_id

    @property
    def position(self) -> float:
        return self._state.position

    @property
    def duration(self) -> float:
        return self._state.duration

    @property
    def play_state(self) -> int:
        return self._state.play_state

    @property
    def lane(self) -> Optional[str]:
        return self._state.lane

    @property
    def volume(self) -> int:
        return self._state.volume

    @property
    def cpn(self) -> str:
        return self._state.cpn

    @property
    def version(self) -> int:
        return self._state.version

    # --- legacy read-only compat alias (existing `self.state` reads) ---
    @property
    def state(self) -> int:
        return self._state.play_state

    @property
    def current_theme(self) -> Optional[str]:
        return self._state.lane

    @property
    def theme(self) -> Optional[str]:
        return self._state.lane
