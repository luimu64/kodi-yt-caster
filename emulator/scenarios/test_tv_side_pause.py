#!/usr/bin/env python3
"""TV-side pause with the two broken signals verified on live hardware:
getCondVisibility("Player.Paused") returns False inside the service process
while paused, and onPlayBackPaused/Resumed callbacks never fire. The bridge
must still detect pause (frozen clock) and resume (clock advancing again)
via time-stall polling and report state 2/1 to the phone accordingly.
"""
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def _cast(s, vid="v1", t=0):
    s.phone.set_playlist(vid, [vid], current_time=t)
    s.wait_until(lambda: vid in (s.playing_file() or ""), what=f"{vid} playing")


def test_tv_side_pause_detected_by_time_stall():
    with Scenario() as s:
        _cast(s)
        s.wait_until(lambda: s.lounge.wait_for_report(
            "nowPlaying", lambda r: r.get("state") == "1", 0.1) or True, timeout=3)
        # Simulate the live-device pathology: freeze the clock (TV-side pause)
        # WITHOUT firing the paused callback and while getCondVisibility
        # reports False (return value monkeypatched below).
        orig_cgv = xbmc.getCondVisibility
        xbmc.getCondVisibility = lambda cond: False  # broken signal path
        try:
            engine = xbmc._engine
            engine.clock.pause()  # no events.put -> no onPlayBackPaused
            s.wait_until(
                lambda: s.lounge.wait_for_report(
                    "onStateChange", lambda r: r.get("state") == "2", 0.1) or True,
                timeout=0.1)
            r = s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "2", timeout=15)
            assert r, "PAUSED (state=2) must be reported within ~6s of a TV-side pause"
            np = s.lounge.wait_for_report("nowPlaying", lambda r: r.get("state") == "2", timeout=10)
            assert np, "PAUSED state must also appear in nowPlaying"
        finally:
            xbmc.getCondVisibility = orig_cgv


def test_tv_side_resume_detected_by_time_advance():
    with Scenario() as s:
        _cast(s)
        engine = xbmc._engine
        orig_cgv = xbmc.getCondVisibility
        xbmc.getCondVisibility = lambda cond: False
        try:
            #_bridge_state hack unavailable here; pause via engine only
            engine.clock.pause()
            r = s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "2", timeout=15)
            assert r, "pause must be detected despite broken signals"
            # Resume the engine directly (TV-side), phone never said play.
            engine.clock.resume()
            r2 = s.lounge.wait_for_report("onStateChange", lambda r2: r2.get("state") == "1" and int(float(r2.get("currentTime", 0))) >= 2, timeout=15)
            assert r2, "resume must be detected via time advance and reported as state=1 past the frozen position"
        finally:
            xbmc.getCondVisibility = orig_cgv


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:
                failed += 1
                import traceback
                print(f"FAIL {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
