#!/usr/bin/env python3
"""Scenario: a cold cast publishes a LOAD, not a lie.

Device 2026-09-29 (kodi.log 10:09:5x), one song change in YouTube Music:

  10:09:55.483  Lounge command: setPlaylist idx=25 vid=BVvvUGP0MFw ct=0
  10:09:55.517  REPORT nowPlaying vid=BVvvUGP0MFw t=0 dur=0 state=1   <- "playing", zero length
  10:09:55.559  Lounge command: setPlaylist idx=25 vid=BVvvUGP0MFw ct=0  <- the phone re-sent the cast
  10:09:56.567  REPORT nowPlaying vid=BVvvUGP0MFw t=111 dur=156 state=1 <- the OLD item's clock under the new id
  10:09:57.633  REPORT nowPlaying vid=BVvvUGP0MFw t=0 dur=254 state=1
  10:10:28.673  pause detected via time-stall (t=20 x3 polls, source=player-clock)
  10:10:28.674  PUBLISH v522 playback -> nowPlaying ... state=2            <- false pause, mid-load
  10:10:30.674  Resume detected via time advance (t=0) -> v523 state=1

Three lies in the first two seconds, each one rendered by the phone (which is a
slave of these reports) — that is the "song flips back on the next second"
behaviour, repeated per change.

The contract asserted here, for a cold cast:

  1. before the requested item's own clock advances, the report is state=3
     (buffering) at t=0 — never state=1 with duration 0;
  2. the duration appears while still loading (as soon as the resolver knows it);
  3. state=1 only once that item is in the player and its clock moves;
  4. no pause (state=2) is ever inferred from the load window;
  5. the load window never publishes the previous item's clock (t stays 0).
"""
import time

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import resources.lib.ytdlp_bridge as ytdlp_bridge


def _cast(s, video_id, video_ids, theme="m", timeout=20.0):
    """Send a cast on the theme's lounge, retrying until it is delivered.

    The listener re-registers its screen shortly after boot (new mock sid) and
    the mock relay can drop a command between two long-polls, so a sid captured
    too early delivers nothing: re-read the sid and resend until the receiver
    actually starts resolving. The subject here is the report sequence, never
    thread ordering.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session(theme, timeout=5.0)
        s.phone.target(sid)
        s.phone.connect()
        time.sleep(0.4)
        s.phone.set_playlist(video_id, video_ids)
        try:
            s.wait_until(lambda: s.resolve_count(video_id) > 0, timeout=4.0,
                         what=f"resolve of {video_id}")
            return
        except AssertionError:
            continue
    raise AssertionError(f"cast of {video_id} never reached the receiver")


def test_cold_cast_is_buffering_then_playing():
    with Scenario() as s:
        # Make the load window deterministic: hold the resolve ~1.2s so the
        # reports that describe "not started yet" cannot race the handoff.
        orig_resolve = ytdlp_bridge.YtDlpBridge.resolve

        def _slow_resolve(self, video_id):
            time.sleep(1.2)
            return orig_resolve(self, video_id)

        ytdlp_bridge.YtDlpBridge.resolve = _slow_resolve
        try:
            # A previous item is playing, so a stale clock exists to leak.
            _cast(s, "v_prev", ["v_prev"])
            s.wait_until(lambda: "v_prev" in (s.playing_file() or ""), what="v_prev playing")
            s.lounge.clear_reports()

            # The phone changes the song.
            _cast(s, "v_cold", ["v_cold", "v_prev"])

            # 1. The load window is reported as buffering, at t=0.
            np = s.lounge.wait_for_report(
                "nowPlaying",
                lambda r: r.get("videoId") == "v_cold" and r.get("state") == "3",
                timeout=6.0,
            )
            assert np, (
                "a cold cast must be reported as state=3 (buffering): "
                f"{s.lounge.reports('nowPlaying')}"
            )
            assert np.get("currentTime") == "0", f"the load must not carry a clock: {np}"

            # 2. Nothing may claim the new item is PLAYING while it is loading,
            #    and no pause may be inferred from the old item's clock.
            during_load = [r for r in s.lounge.reports("nowPlaying") if r.get("videoId") == "v_cold"]
            assert not any(r.get("state") == "1" for r in during_load), (
                f"PLAYING published before the item started: {during_load}")
            assert not any(r.get("state") == "2" for r in during_load), (
                f"a pause inferred from the load window: {during_load}")

            # 3. Once its clock moves, the state becomes PLAYING with the real
            #    duration, and position/duration/id agree from then on.
            playing = s.lounge.wait_for_report(
                "nowPlaying",
                lambda r: (r.get("videoId") == "v_cold" and r.get("state") == "1"
                           and r.get("duration") == "180"),
                timeout=10.0,
            )
            assert playing, (
                f"playback never reported PLAYING with the resolved duration: "
                f"{s.lounge.reports('nowPlaying')}"
            )
        finally:
            ytdlp_bridge.YtDlpBridge.resolve = orig_resolve


def test_a_song_change_never_publishes_the_previous_items_clock():
    """The new id may never be paired with the clock of the item still playing.

    Split source of truth (identity from the snapshot, clock from a player that
    has not switched yet) is what made the phone show the new song at the old
    song's progress and then jump — device `t=111 dur=156` under the new id.
    """
    with Scenario() as s:
        orig_resolve = ytdlp_bridge.YtDlpBridge.resolve

        def _slow_resolve(self, video_id):
            time.sleep(1.5)
            return orig_resolve(self, video_id)

        ytdlp_bridge.YtDlpBridge.resolve = _slow_resolve
        try:
            _cast(s, "v_a", ["v_a"])
            s.wait_until(lambda: "v_a" in (s.playing_file() or ""), what="v_a playing")
            # Let v_a's own clock get well past zero so a leak would be visible.
            time.sleep(3.0)
            s.lounge.clear_reports()

            _cast(s, "v_b", ["v_b", "v_a"])
            s.lounge.wait_for_report(
                "nowPlaying",
                lambda r: r.get("videoId") == "v_b",
                timeout=6.0,
            )

            # Everything reported about v_b before it actually started must have
            # been reported at t=0.
            s.wait_until(lambda: "v_b" in (s.playing_file() or ""), what="v_b playing")
            early = [
                r for r in s.lounge.reports("nowPlaying")
                if r.get("videoId") == "v_b" and r.get("state") == "3"
            ]
            assert early, f"no buffering report for v_b: {s.lounge.reports('nowPlaying')}"
            for r in early:
                assert r.get("currentTime") == "0", (
                    f"the load window carried another item's clock: {r}")
        finally:
            ytdlp_bridge.YtDlpBridge.resolve = orig_resolve


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_cold_cast_states OK")


if __name__ == "__main__":
    main()
