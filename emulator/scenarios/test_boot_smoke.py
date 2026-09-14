#!/usr/bin/env python3
"""Smoke: full service boots inside the harness, listener binds, phone connects,
casting starts playback, teardown leaves no addon threads alive."""
import threading

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def test_boot_and_cast():
    with Scenario() as s:
        # listeners performed real handshakes against the mock Lounge
        assert len(s.lounge.sessions) >= 2, "cl + music sessions must bind"

        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connected notification")
        # connect announces nowPlaying on every bound session
        assert s.lounge.wait_for_report("nowPlaying", timeout=5)

        s.phone.set_playlist("v1", ["v1", "v2", "v3"], current_time=5)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 playback start")

        np = s.lounge.wait_for_report("nowPlaying", pred=lambda r: r.get("videoId") == "v1", timeout=8)
        assert np, "nowPlaying for v1 must reach the phone"

        # pending_seek applied: bridge waits for onPlayBackStarted then seeks
        s.wait_until(lambda: abs(xbmc.Player().getTime() - 5) < 2, timeout=8,
                     what="pending seek to 5s")
        assert s.resolve_count("v1") == 1, "exactly one resolve for v1"


def test_teardown_clean():
    before = threading.active_count()
    with Scenario() as s:
        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connect")
    # LoungeListener / DIAL / SSDP / PlaybackMonitor / ReceiverBootstrap gone
    time_deadline_wait = 2.0
    import time
    deadline = time.time() + time_deadline_wait
    while time.time() < deadline:
        names = [t.name for t in threading.enumerate() if t.name in
                 ("LoungeListener", "DIALService", "SSDPResponder", "PlaybackMonitor", "ReceiverBootstrap")]
        if not names:
            break
        time.sleep(0.05)
    names = [t.name for t in threading.enumerate() if t.name in
             ("LoungeListener", "DIALService", "SSDPResponder", "PlaybackMonitor", "ReceiverBootstrap")]
    assert not names, f"leaked threads: {names}"


def main():
    test_boot_and_cast()
    print("  boot_and_cast OK")
    test_teardown_clean()
    print("  teardown_clean OK")
    print("test_boot_smoke OK")


if __name__ == "__main__":
    main()
