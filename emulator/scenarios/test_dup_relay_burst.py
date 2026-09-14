#!/usr/bin/env python3
"""Duplicate relay burst: 4x setPlaylist within ~100ms => one resolve, one play."""
import time

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def test_burst_four():
    with Scenario() as s:
        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connect")
        s.phone.burst_set_playlist("v1", ["v1", "v2", "v3"], current_time=0, n=4)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
        import time
        time.sleep(2.0)  # let any redundant plays surface
        assert s.resolve_count("v1") == 1, f"resolve count {s.resolve_count('v1')} != 1"
        # no restart: playback still v1 and no second Started generation
        assert "v1" in (s.playing_file() or "")
        gen1 = s.lounge.reports("nowPlaying")
        assert gen1, "nowPlaying reports exist"


def test_burst_then_different_video():
    with Scenario() as s:
        s.phone.set_playlist("v1", ["v1"], current_time=0)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1")
        s.phone.burst_set_playlist("v2", ["v2"], current_time=0, n=3)
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2")
        import time
        time.sleep(1.0)
        assert s.resolve_count("v2") == 1
        assert "v2" in (s.playing_file() or "")


def test_dup_during_resolve_window():
    """Regression (fix: _requested_id dedup): a duplicate setPlaylist arriving
    between play() and the async onPlayBackStarted — hundreds of ms on real
    Kodi while the demuxer opens, state still STOPPED, so the old
    current_video_id+state dedup missed it — bumped _play_gen: a second
    _play_video restart the track from 0, and armed the stale-gen guard in
    _on_playback_stopped to eat the real STOPPED report."""
    from resources.lib.resolver import VideoResolver
    orig = VideoResolver.resolve

    def slow_resolve(self, vid):
        time.sleep(0.3)  # resolve takes a beat, like a real crawl
        return orig(self, vid)

    VideoResolver.resolve = slow_resolve
    try:
        with Scenario() as s:
            import xbmc
            xbmc._engine.start_latency = 0.4  # Started fires 400ms after play()
            s.phone.set_playlist("v1", ["v1"])
            time.sleep(0.45)                   # play() issued; Started pending or just fired
            before = s.playing_file()
            s.phone.set_playlist("v1", ["v1"])  # relay dup inside the window
            s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
            time.sleep(1.5)
            # THE bug: the dup spawned a second _play_video -> playback restarted
            # from 0. Count Started firings for v1 from the engine's event log.
            starts = [e for e, u in xbmc._engine.event_log if e == "started" and u and "v1" in u]
            assert len(starts) == 1, f"duplicate restart: {len(starts)} playbacks started"
            s.phone.stop()
            r = s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "0", timeout=10)
            assert r, "STOPPED report must not be eaten by the stale-gen guard"
    finally:
        VideoResolver.resolve = orig


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_dup_relay_burst OK")


if __name__ == "__main__":
    main()
