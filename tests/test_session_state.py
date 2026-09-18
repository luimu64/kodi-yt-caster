"""Unit tests for resources/lib/session_state.py."""

import unittest
from dataclasses import FrozenInstanceError

from resources.lib.session_state import (
    SessionState,
    PlayState,
    reduce,
    SetPlaylistEvent,
    UpdatePlaylistEvent,
    PlayVideoEvent,
    PauseEvent,
    ResumeEvent,
    StopEvent,
    SeekToEvent,
    SetVolumeEvent,
    PositionTickEvent,
    PauseDetectedEvent,
    ResumeDetectedEvent,
    KodiAdvancedEvent,
    PlaybackStartedEvent,
    PlaybackStoppedEvent,
    PlaybackEndedEvent,
    SignalUnknownEvent,
    ResolverResolvedEvent,
    ResolverFailedEvent,
    WindowLaneRepairedEvent,
    Event,
)


class TestSessionState(unittest.TestCase):
    def test_field_specification(self):
        """Verify SessionState owns exactly the specified fields."""
        state = SessionState()
        expected_fields = {
            "playlist",
            "current_index",
            "current_video_id",
            "list_id",
            "position",
            "duration",
            "play_state",
            "lane",
            "volume",
            "cpn",
            "version",
        }
        actual_fields = set(state.__dataclass_fields__.keys())
        self.assertEqual(actual_fields, expected_fields)
        self.assertEqual(state.version, 0)
        self.assertEqual(state.playlist, ())
        self.assertEqual(state.current_index, 0)
        self.assertIsNone(state.current_video_id)
        self.assertEqual(state.list_id, "")
        self.assertEqual(state.position, 0.0)
        self.assertEqual(state.duration, 0.0)
        self.assertEqual(state.play_state, PlayState.STOPPED)
        self.assertIsNone(state.lane)
        self.assertEqual(state.volume, 100)
        self.assertEqual(state.cpn, "kodi")

    def test_immutability(self):
        """Verify that direct field mutation on SessionState is prohibited."""
        state = SessionState()
        with self.assertRaises(FrozenInstanceError):
            state.version = 1  # type: ignore
        with self.assertRaises(FrozenInstanceError):
            state.position = 10.0  # type: ignore
        with self.assertRaises(FrozenInstanceError):
            state.current_video_id = "abc"  # type: ignore

    def test_snapshot_immutability_and_detachment(self):
        """Verify snapshot accessor returns an immutable, detached copy."""
        state = SessionState(playlist=("v1", "v2"), current_video_id="v1", version=5)
        snap = state.snapshot()
        self.assertEqual(snap, state)
        self.assertIsNot(snap, state)

        with self.assertRaises(FrozenInstanceError):
            snap.version = 10  # type: ignore

        # Mutating through reduce creates new state, leaving snapshot untouched
        new_state = reduce(state, SetVolumeEvent(volume=50))
        self.assertEqual(new_state.volume, 50)
        self.assertEqual(snap.volume, 100)
        self.assertEqual(snap.version, 5)

    def test_version_monotonicity(self):
        """Verify that version increases monotonically on accepted state changes."""
        s0 = SessionState()
        self.assertEqual(s0.version, 0)

        s1 = reduce(s0, SetPlaylistEvent(video_id="v1", video_ids=["v1", "v2"], list_id="PL1", current_time=0.0))
        self.assertEqual(s1.version, 1)
        self.assertEqual(s1.playlist, ("v1", "v2"))
        self.assertEqual(s1.current_video_id, "v1")
        self.assertEqual(s1.current_index, 0)
        self.assertEqual(s1.list_id, "PL1")
        self.assertEqual(s1.play_state, PlayState.PLAYING)

        # Pause
        s2 = reduce(s1, PauseEvent())
        self.assertEqual(s2.version, 2)
        self.assertEqual(s2.play_state, PlayState.PAUSED)

        # Redundant pause should be a no-op: version does not advance
        s2_dup = reduce(s2, PauseEvent())
        self.assertIs(s2_dup, s2)
        self.assertEqual(s2_dup.version, 2)

        # Resume
        s3 = reduce(s2, ResumeEvent())
        self.assertEqual(s3.version, 3)
        self.assertEqual(s3.play_state, PlayState.PLAYING)

        # Seek
        s4 = reduce(s3, SeekToEvent(seconds=42.0))
        self.assertEqual(s4.version, 4)
        self.assertEqual(s4.position, 42.0)

        # Redundant seek is no-op
        s4_dup = reduce(s4, SeekToEvent(seconds=42.0))
        self.assertIs(s4_dup, s4)
        self.assertEqual(s4_dup.version, 4)

        # Volume
        s5 = reduce(s4, SetVolumeEvent(volume=80))
        self.assertEqual(s5.version, 5)
        self.assertEqual(s5.volume, 80)

        # Resolver duration
        s6 = reduce(s5, ResolverResolvedEvent(video_id="v1", duration=180.0))
        self.assertEqual(s6.version, 6)
        self.assertEqual(s6.duration, 180.0)

        # Stop
        s7 = reduce(s6, StopEvent())
        self.assertEqual(s7.version, 7)
        self.assertEqual(s7.play_state, PlayState.STOPPED)
        self.assertEqual(s7.position, 0.0)
        self.assertEqual(s7.duration, 0.0)

        # Unknown event is inert
        class UnknownEvent(Event):
            pass

        s7_unknown = reduce(s7, UnknownEvent())
        self.assertIs(s7_unknown, s7)
        self.assertEqual(s7_unknown.version, 7)

    def test_writer_paths_reduction(self):
        """Verify reduce correctly handles events from all four writer paths."""
        # 1. Command-dispatch: setPlaylist, updatePlaylist, playVideo
        s = SessionState()
        s = reduce(s, SetPlaylistEvent(video_id="v1", video_ids=["v1", "v2", "v3"], list_id="PL1", theme="cl"))
        self.assertEqual(s.current_video_id, "v1")
        self.assertEqual(s.current_index, 0)
        self.assertEqual(s.lane, "cl")
        self.assertEqual(len(s.playlist), 3)

        s = reduce(s, UpdatePlaylistEvent(video_ids=["v2", "v1"]))
        self.assertEqual(s.playlist, ("v2", "v1"))
        self.assertEqual(s.current_index, 1)  # Re-derived index for v1

        # 2. Position loop ticks & pause/resume detection
        s = reduce(s, PositionTickEvent(position=15.0, duration=200.0))
        self.assertEqual(s.position, 15.0)
        self.assertEqual(s.duration, 200.0)

        s = reduce(s, PauseDetectedEvent(position=15.0))
        self.assertEqual(s.play_state, PlayState.PAUSED)

        s = reduce(s, ResumeDetectedEvent(position=17.0))
        self.assertEqual(s.play_state, PlayState.PLAYING)
        self.assertEqual(s.position, 17.0)

        s = reduce(s, KodiAdvancedEvent(video_id="v2"))
        self.assertEqual(s.current_video_id, "v2")
        self.assertEqual(s.current_index, 0)
        self.assertNotEqual(s.cpn, "kodi")

        # 3. Resolver callbacks
        s = reduce(s, ResolverResolvedEvent(video_id="v2", duration=300.0))
        self.assertEqual(s.duration, 300.0)

        s = reduce(s, ResolverFailedEvent(video_id="v2"))
        self.assertEqual(s.play_state, PlayState.STOPPED)

        # 4. Window / lane repair
        s = reduce(s, WindowLaneRepairedEvent(lane="m"))
        self.assertEqual(s.lane, "m")


if __name__ == "__main__":
    unittest.main()
