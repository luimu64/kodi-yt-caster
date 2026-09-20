#!/usr/bin/env python3
"""A sender disconnect is not a stop: playback continues; sessions survive.

Device evidence for the old behaviour (kodi.log, 2026-09-20):
  19:12:50.549 Lounge command: remoteDisconnected (GOOGLE Pixel 9)
  19:12:50.550 state v58 stopVideo -> playback (phone)
  19:12:50.559 ------ Window Deinit (VideoFullScreen.xml) ------
A phone dropping off (screen off, wifi blip, app swiped away) stopped whatever
Kodi was playing, including media the cast session never started.
"""
import time

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc

# Settle window: how long the old implementation needed to hand the stop to
# Kodi (it called player.stop() synchronously inside the disconnect handler,
# so this is an upper bound with slack).
STOP_SETTLE_SECONDS = 1.5


def _stopped_events(since):
    return [e for e, _u in xbmc._engine.event_log[since:] if e == "stopped"]


def test_disconnect_keeps_cast_playback_running():
    with Scenario() as s:
        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connect")
        s.phone.set_playlist("v1", ["v1", "v2"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 playing")

        mark = len(xbmc._engine.event_log)
        s.phone.disconnect()
        s.wait_until(lambda: s.notifications("Disconnected from"), what="disconnect notification")
        time.sleep(STOP_SETTLE_SECONDS)

        assert "v1" in (s.playing_file() or ""), (
            f"disconnect stopped the cast item: {s.playing_file()!r}")
        assert not _stopped_events(mark), (
            f"disconnect issued a stop to the player: {xbmc._engine.event_log[mark:]}")
        # The phone is told the item is still playing, not that it stopped.
        snap = s.snapshot()
        assert snap is not None and snap.play_state != 0, (
            f"receiver reported a stopped state after disconnect: {snap.play_state}")
        # sessions still bound (no re-registration happened)
        assert len(s.lounge.sessions) == 2, "lounge sessions survive disconnect"

        # reconnect: casting works again without a service restart
        s.phone.connect()
        s.wait_until(lambda: len(s.notifications("Connected to")) >= 2, what="second connect")
        s.phone.set_playlist("v2", ["v2"])
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2 playing after reconnect")


def test_disconnect_leaves_unrelated_playback_alone():
    """The user switched to their own content; a phone blip must not touch it."""
    with Scenario() as s:
        s.phone.connect()
        s.wait_until(lambda: s.notifications("Connected to"), what="connect")
        s.phone.set_playlist("v1", ["v1"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="cast playing")

        # Unrelated playback, started by the user (Jellyfin via JellyCon) —
        # not a queue item of this cast session.
        foreign = "plugin://plugin.video.jellycon/?mode=play&id=abc123"
        xbmc._engine.play_url(foreign)
        s.wait_until(lambda: (s.playing_file() or "") == foreign, what="unrelated item playing")

        mark = len(xbmc._engine.event_log)
        s.phone.disconnect()
        s.wait_until(lambda: s.notifications("Disconnected from"), what="disconnect notification")
        time.sleep(STOP_SETTLE_SECONDS)

        assert (s.playing_file() or "") == foreign, (
            f"disconnect stopped unrelated playback: {s.playing_file()!r}")
        assert not _stopped_events(mark), (
            f"disconnect issued a stop to the player: {xbmc._engine.event_log[mark:]}")


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
