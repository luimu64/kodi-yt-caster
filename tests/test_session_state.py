"""Unit tests for resources/lib/session_state.py covering R1 and R3 requirements.

Acceptance criteria for R3:
- Unit coverage showing purity (same inputs, same output) for every event in the vocabulary.
- Unit coverage showing idempotence (replay of a duplicated event leaves state and version unchanged)
  for every event in the vocabulary.
- Monotonic version counter v<N> bumped on accepted reductions.
- Log line formatted as `state v<N> <event> -> <changed field groups> (<source>)`.
- No event type may be reachable that mutates state outside the reducer.
"""

from dataclasses import FrozenInstanceError
import logging
import unittest

from resources.lib.session_state import (
    Event,
    KodiAdvancedEvent,
    KodiStateObservedEvent,
    NextEvent,
    PauseDetectedEvent,
    PauseEvent,
    PlaybackEndedEvent,
    PlaybackStartedEvent,
    PlaybackStoppedEvent,
    PlayEvent,
    PlayState,
    PlayVideoEvent,
    PositionTickEvent,
    PreviousEvent,
    QueueTitlePatchedEvent,
    reduce,
    ResolverCompletedEvent,
    ResolverFailedEvent,
    ResolverResolvedEvent,
    ResolverSupersededEvent,
    ResumeDetectedEvent,
    ResumeEvent,
    SeekToEvent,
    SessionState,
    SetPlaylistEvent,
    SetVolumeEvent,
    SignalUnknownEvent,
    StateOwner,
    StopEvent,
    StopVideoEvent,
    UpdatePlaylistEvent,
    WindowLaneRepairedEvent,
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


class TestReducerPurityAndIdempotence(unittest.TestCase):
    """Rigorous purity and idempotence tests for EVERY event in the vocabulary (§9 R3)."""

    def setUp(self):
        self.base_state = SessionState(
            playlist=("v1", "v2", "v3"),
            current_index=0,
            current_video_id="v1",
            list_id="PL_TEST_123",
            position=10.0,
            duration=180.0,
            play_state=PlayState.PLAYING,
            lane="cl",
            volume=75,
            cpn="cpn_initial",
            version=1,
        )

    def _get_vocabulary_events(self):
        """Return a mapping of vocabulary name to an event instance that alters base_state."""
        return {
            "setPlaylist": SetPlaylistEvent(
                video_id="v2",
                video_ids=["v1", "v2", "v3", "v4"],
                list_id="PL_NEW",
                current_time=5.0,
                theme="cl",
            ),
            "updatePlaylist": UpdatePlaylistEvent(video_ids=["v1", "v3"]),
            "play": PlayEvent(video_id="v2", seek_time=0.0),
            "pause": PauseEvent(),
            "seekTo": SeekToEvent(seconds=45.0),
            "setVolume": SetVolumeEvent(volume=50),
            "stopVideo": StopVideoEvent(),
            "next": NextEvent(video_id="v2"),
            "previous": PreviousEvent(video_id="v1"),
            "kodi_advanced": KodiAdvancedEvent(video_id="v2"),
            "kodi_state_observed": KodiStateObservedEvent(position=25.0, duration=180.0, play_state=PlayState.PLAYING),
            "signal_unknown": SignalUnknownEvent(),
            "resolver_completed": ResolverCompletedEvent(video_id="v1", duration=210.0),
            "resolver_superseded": ResolverSupersededEvent(video_id="v1"),
        }

    def test_purity_for_all_vocabulary_events(self):
        """Purity: given identical inputs (state, event), reduce must return identical outputs

        with zero mutation to input objects and no side effects.
        """
        events = self._get_vocabulary_events()
        for name, ev in events.items():
            # For 'previous', adjust starting index to > 0 so it produces a state transition
            start_state = SessionState(
                playlist=("v1", "v2", "v3"),
                current_index=1,
                current_video_id="v2",
                list_id="PL_TEST_123",
                position=10.0,
                duration=180.0,
                play_state=PlayState.PLAYING,
                lane="cl",
                volume=75,
                cpn="cpn_initial",
                version=1,
            ) if name == "previous" else self.base_state

            snap1 = start_state.snapshot()
            res1 = reduce(start_state, ev)
            self.assertEqual(start_state, snap1, f"reduce mutated start_state in-place for {name}")

            snap2 = start_state.snapshot()
            res2 = reduce(start_state, ev)
            self.assertEqual(start_state, snap2, f"reduce mutated start_state in-place for second call {name}")

            self.assertEqual(res1, res2, f"reduce was not pure for {name}: outputs differ for identical input")

    def test_idempotence_and_replay_for_all_vocabulary_events(self):
        """Idempotence: applying the same event twice to the same starting state yields

        identical resulting states, AND replaying a duplicate event on the reduced state
        leaves state and version unchanged.
        """
        events = self._get_vocabulary_events()
        for name, ev in events.items():
            start_state = SessionState(
                playlist=("v1", "v2", "v3"),
                current_index=1,
                current_video_id="v2",
                list_id="PL_TEST_123",
                position=10.0,
                duration=180.0,
                play_state=PlayState.PLAYING,
                lane="cl",
                volume=75,
                cpn="cpn_initial",
                version=1,
            ) if name == "previous" else self.base_state

            # First reduction
            res1 = reduce(start_state, ev)

            # Replaying the same event on res1 (duplicate burst / replay)
            res_replay = reduce(res1, ev)
            self.assertEqual(
                res_replay,
                res1,
                f"Replaying duplicate event {name} failed idempotence: state changed",
            )
            self.assertEqual(
                res_replay.version,
                res1.version,
                f"Replaying duplicate event {name} bumped version from {res1.version} to {res_replay.version}",
            )

    def test_duplicate_relay_burst_protection(self):
        """Simulate a rapid duplicate relay burst of 4 identical setPlaylist commands.

        State version must advance exactly once.
        """
        s = SessionState()
        ev = SetPlaylistEvent(
            video_id="v1",
            video_ids=["v1", "v2", "v3"],
            list_id="PL_BURST",
            current_time=0.0,
            theme="cl",
        )
        s1 = reduce(s, ev)
        self.assertEqual(s1.version, 1)

        # 3 duplicate burst arrivals
        s2 = reduce(s1, ev)
        s3 = reduce(s2, ev)
        s4 = reduce(s3, ev)

        self.assertEqual(s4.version, 1)
        self.assertIs(s4, s1)

    def test_reduction_log_line_format(self):
        """Verify that exactly one log line is emitted per reduction in the required format:

        `state v<N> <event> -> <changed field groups> (<source>)`
        """
        logger = logging.getLogger("ytlounge.state")
        logs = []

        class LogHandler(logging.Handler):
            def emit(self, record):
                logs.append(self.format(record))

        handler = LogHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            s0 = SessionState()
            ev1 = SetPlaylistEvent(video_id="v1", video_ids=["v1", "v2"], list_id="PL1", source="phone")
            s1 = reduce(s0, ev1)
            self.assertEqual(len(logs), 1)
            self.assertTrue(
                logs[0].startswith("state v1 setPlaylist -> identity, playback (phone)"),
                f"Unexpected log line: {logs[0]}",
            )

            # Redundant setPlaylist: should emit no log
            logs.clear()
            s1_dup = reduce(s1, ev1)
            self.assertEqual(len(logs), 0)

            # Volume reduction
            ev_vol = SetVolumeEvent(volume=60, source="remote")
            s2 = reduce(s1, ev_vol)
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0], "state v2 setVolume -> volume (remote)")

            # Player clock observation
            logs.clear()
            ev_obs = KodiStateObservedEvent(position=12.0, source="player-clock")
            s3 = reduce(s2, ev_obs)
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0], "state v3 kodiStateObserved -> playback (player-clock)")

        finally:
            logger.removeHandler(handler)

    def test_state_owner_snapshot_immutability_and_detachment(self):
        """Verify StateOwner.snapshot() returns an immutable detached SessionState snapshot."""
        owner = StateOwner(SessionState(playlist=("v1", "v2"), current_video_id="v1", version=1))
        snap = owner.snapshot()
        self.assertIsInstance(snap, SessionState)
        self.assertEqual(snap.playlist, ("v1", "v2"))
        self.assertEqual(snap.current_video_id, "v1")
        self.assertEqual(snap.version, 1)

        # Snapshot is frozen
        with self.assertRaises(FrozenInstanceError):
            snap.version = 2  # type: ignore

        # Mutating through StateOwner advances version, snapshot unchanged
        owner.apply(SetVolumeEvent(volume=80))
        self.assertEqual(owner.volume, 80)
        self.assertEqual(owner.version, 2)
        self.assertEqual(snap.volume, 100)
        self.assertEqual(snap.version, 1)

    def test_backward_compatibility_properties(self):
        """Verify backward-compatibility properties current_theme and theme on SessionState and StateOwner."""
        state = SessionState(lane="cl")
        self.assertEqual(state.lane, "cl")
        self.assertEqual(state.current_theme, "cl")
        self.assertEqual(state.theme, "cl")
        self.assertEqual(state.state, PlayState.STOPPED)

        owner = StateOwner(state)
        self.assertEqual(owner.lane, "cl")
        self.assertEqual(owner.current_theme, "cl")
        self.assertEqual(owner.theme, "cl")
        self.assertEqual(owner.state, PlayState.STOPPED)

    def test_zero_field_assignments_outside_session_state(self):
        """Mechanical check: zero assignments to the 11 session state attributes outside session_state.py."""
        import ast
        import os

        lib_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "resources", "lib")
        )
        forbidden = {
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
        violations = []
        for root, _, files in os.walk(lib_dir):
            for file in files:
                if not file.endswith(".py") or file == "session_state.py":
                    continue
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target, ast.Attribute) and target.attr in forbidden:
                                violations.append(f"{path}:{target.lineno} assigned to attribute '{target.attr}'")
        self.assertEqual(violations, [], f"Found forbidden field assignments: {violations}")


if __name__ == "__main__":
    unittest.main()
