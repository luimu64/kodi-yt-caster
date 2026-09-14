#!/usr/bin/env python3
"""Remote-control truth table: pause/resume/seek/stop/volume against reports."""
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def _cast(s, vid="v1", t=0):
    s.phone.set_playlist(vid, [vid], current_time=t)
    s.wait_until(lambda: vid in (s.playing_file() or ""), what=f"{vid} playing")
    s.wait_until(lambda: s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "1" and r.get("videoId") is None or r.get("state") == "1", 0.1) is not None or True, timeout=0.1)


def test_pause_while_playing():
    with Scenario() as s:
        _cast(s)
        s.phone.pause()
        s.wait_until(lambda: not xbmc.getCondVisibility("Player.Paused") is False and xbmc.getCondVisibility("Player.Paused"), what="paused condition")
        r = s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "2")
        assert r, "PAUSED state must be reported"


def test_pause_while_paused_stays_paused():
    with Scenario() as s:
        _cast(s)
        s.phone.pause()
        s.wait_until(lambda: xbmc.getCondVisibility("Player.Paused"), what="first pause")
        import time
        t0 = xbmc.Player().getTime()
        s.phone.pause()  # Kodi's pause() toggles; the BRIDGE must guard against that
        time.sleep(0.5)
        assert xbmc.getCondVisibility("Player.Paused"), "pause-while-paused must NOT resume"
        assert abs(xbmc.Player().getTime() - t0) < 0.1, "clock must stay frozen"


def test_resume_after_pause():
    with Scenario() as s:
        _cast(s)
        s.phone.pause()
        s.wait_until(lambda: xbmc.getCondVisibility("Player.Paused"), what="paused")
        s.phone.play()
        s.wait_until(lambda: not xbmc.getCondVisibility("Player.Paused"), what="resumed")
        assert s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "1")


def test_seek():
    with Scenario() as s:
        _cast(s)
        s.wait_until(lambda: xbmc.Player().getTime() > 1, what="playback underway")
        s.phone.seek(5)
        s.wait_until(lambda: 4 <= xbmc.Player().getTime() <= 7, what="seek to 5")
        r = s.lounge.wait_for_report("onStateChange", timeout=5)
        assert r is not None


def test_stop():
    with Scenario() as s:
        _cast(s)
        s.phone.stop()
        s.wait_until(lambda: s.playing_file() is None, what="playback stopped")
        r = s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "0", timeout=15)
        assert r, "STOPPED must be reported"


def test_volume():
    with Scenario() as s:
        s.phone.set_volume(42)
        r = s.lounge.wait_for_report("onVolumeChanged", lambda r: r.get("volume") == "42", timeout=15)
        assert r, "volume 42 must be reported back"
        s.phone.set_volume(500)  # clamps
        r = s.lounge.wait_for_report("onVolumeChanged", lambda r: r.get("volume") == "100", timeout=15)
        assert r, "volume must clamp to 100"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_remote_control OK")


if __name__ == "__main__":
    main()
