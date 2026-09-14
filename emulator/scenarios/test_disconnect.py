#!/usr/bin/env python3
"""remoteDisconnected stops playback; sessions survive; reconnect works."""
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def test_disconnect_stops_and_reconnects():
    with Scenario() as s:
        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connect")
        s.phone.set_playlist("v1", ["v1", "v2"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 playing")

        s.phone.disconnect()
        s.wait_until(lambda: s.playing_file() is None, timeout=5, what="playback stopped on disconnect")
        r = s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "0", timeout=10)
        assert r, "stopped state must be reported"
        # sessions still bound (no re-registration happened)
        assert len(s.lounge.sessions) == 2, "lounge sessions survive disconnect"

        # reconnect: casting works again without a service restart
        s.phone.connect()
        s.wait_until(lambda: len(s.notifications("Connected to")) >= 2, what="second connect")
        s.phone.set_playlist("v2", ["v2"])
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2 playing after reconnect")


def test_disconnect_notification():
    with Scenario() as s:
        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connect")
        s.phone.disconnect()
        s.wait_until(lambda: s.notifications("Disconnected from"), what="disconnect notification")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_disconnect OK")


if __name__ == "__main__":
    main()
