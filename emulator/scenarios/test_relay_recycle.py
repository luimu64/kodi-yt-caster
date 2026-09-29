#!/usr/bin/env python3
"""A recycled bind stream must not leave the receiver deaf.

Device evidence (kodi.log, 2026-09-29) — the relay announced `gracefulReconnect`,
the stream died, and after the listener re-handshook it never dispatched another
command on that lounge, while the phone still showed the TV as connected and
casting silently did nothing:

  02:24:02 Lounge command: gracefulReconnect        (cl bind)
  02:28:31 Handshake OK: SID=E759419E428            (cl rebinds, codes restart)
  ...0 commands dispatched ever again (1283 before the recycle)

  05:31:40 Lounge command: gracefulReconnect        (m bind)
  05:36:08 Handshake OK: SID=28AC66FB1B6D0090       (m rebinds)
  ...0 commands dispatched ever again (1708 before the recycle)

Two defects, both on this path:
  1. the rebind inherited the dead session's frame-code high-water mark, and the
     relay numbers a new session from scratch (code ~4 getDiscoveryDeviceId,
     ~8 noop) — so every command of the new session was dropped by the dedup
     guard;
  2. `gracefulReconnect` was ignored, so the receiver rode the dead SID through 8
     failed binds and ~3 minutes of backoff before it re-handshook at all.

This scenario drives the wire path: cast on the boot session, let the relay
recycle the stream, then cast again on the rebound session.
"""
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()


def test_commands_survive_a_relay_initiated_rebind():
    with Scenario() as s:
        s.phone.connect()
        boot_sid = s.wait_for_session("cl")

        # Baseline: the boot session plays what the phone sends.
        s.phone.target(boot_sid)
        s.phone.set_playlist("v1", ["v1", "v2"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""),
                     what="the boot session to play a cast")

        # The relay recycles the bind: it announces the reconnect and then
        # closes the stream (and answers the old SID with 410 Gone from here on).
        s.phone.graceful_reconnect()
        new_sid = s.wait_until(
            lambda: (s.sid_for_theme("cl")
                     if s.sid_for_theme("cl") and s.sid_for_theme("cl") != boot_sid
                     else None),
            what="the listener to rebind on a fresh handshake after gracefulReconnect")

        # The rebind must renumber: a command on the new session has to reach the
        # dispatcher, not be swallowed by the dead session's code high-water mark.
        s.phone.target(new_sid)
        s.phone.set_playlist("v3", ["v3"])
        s.wait_until(lambda: "v3" in (s.playing_file() or ""),
                     what="a cast on the rebound session to be dispatched")


def test_a_recycled_relay_session_does_not_hang_the_phone_out_to_dry():
    """The phone must keep being served after the recycle: the receiver answers
    the new session and still reports state (the field symptom was a phone that
    showed the TV as connected while nothing happened)."""
    with Scenario() as s:
        s.phone.connect()
        boot_sid = s.wait_for_session("cl")
        s.phone.target(boot_sid)
        s.phone.set_playlist("v1", ["v1"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 playing")

        mark = len(s.lounge.REPORTS)
        s.phone.graceful_reconnect()
        new_sid = s.wait_until(
            lambda: (s.sid_for_theme("cl")
                     if s.sid_for_theme("cl") and s.sid_for_theme("cl") != boot_sid
                     else None),
            what="rebind after gracefulReconnect")

        s.phone.target(new_sid)
        s.phone.set_playlist("v4", ["v4"])
        s.wait_until(lambda: "v4" in (s.playing_file() or ""),
                     what="v4 playing after the rebind")
        reports = [r for r in s.lounge.REPORTS[mark:] if r.get("sid") == new_sid]
        assert reports, (
            "the rebound session was never reported to: the phone sees a connected "
            "TV that reports nothing")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_relay_recycle OK")


if __name__ == "__main__":
    main()
