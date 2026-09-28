#!/usr/bin/env python3
"""Token expiry: a rejected lounge token is refreshed IN PLACE.

Root cause of "neither app can connect at all" (device 2026-09-28): the lounge
token has a 14-day lifespan and both stored tokens had expired two days earlier.
The handshake answered HTTP 401 — and 401 was classified as a GENERIC listener
error, so the listener retried the same dead token forever with backoff and the
refresh path never ran:

    16:19:03 ytlounge.listener: Listener error (4 consecutive): Handshake failed: HTTP 401

Two defects are covered here:
  1. 401 must surface as a token expiry (not "transient network trouble").
  2. The refresh must KEEP the screen id: a new screen id orphans the TV entry in
     the YouTube app and forces a fresh TV-code pairing. Only a revoked screen
     (the batch endpoint refuses to reissue) falls back to a new screen.

The full wire path (poll 400 -> 8-failure backoff ~62s -> handshake -> expiry) is
too slow for a deterministic test, so the service's handler is driven directly,
plus a boot-path test for the proactive refresh with a pre-seeded expired store.
"""
import json
import time

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def _store(s):
    import xbmcaddon
    raw = xbmcaddon.Addon().getSetting("session_data")
    return json.loads(raw) if raw else {}


def test_expired_token_refreshes_in_place_preserving_pairing():
    """The common case: the 14-day token lapsed, the screen is still paired."""
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two sessions bound")
        before = _store(s)
        handler = s.service._last_token_handlers["cl"]
        handler()
        after = _store(s)

        assert after["screen_id"] == before["screen_id"], (
            "the screen id must be KEPT — a new one orphans the phone's pairing")
        assert after["lounge_token"] != before["lounge_token"], (
            "the token must be reissued")
        assert after["expiration"] > before["expiration"]
        # ...and no pairing code was fetched: nothing for the user to re-enter.
        codes = [c for c in s.lounge.PAIRING_CALLS if c[0] == "get_pairing_code"]
        assert not codes, f"no TV-code pairing should be needed: {codes}"

        # The other session (YouTube Music) is untouched.
        assert after["screen_id_m"] == before["screen_id_m"]
        assert after["lounge_token_m"] == before["lounge_token_m"]


def test_expired_token_music_session_preserves_cl():
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two sessions bound")
        before = _store(s)
        handler = s.service._last_token_handlers["m"]
        handler()
        after = _store(s)
        assert after["screen_id_m"] == before["screen_id_m"], "music screen kept"
        assert after["lounge_token_m"] != before["lounge_token_m"], "music token reissued"
        assert after["screen_id"] == before["screen_id"], "cl screen must survive"
        assert after["lounge_token"] == before["lounge_token"]


def test_revoked_screen_falls_back_to_new_screen_and_pairing_code():
    """A revoked screen cannot be refreshed: re-pair with a fresh TV code."""
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two sessions bound")
        before = _store(s)
        s.lounge.bad_screen_ids.add(before["screen_id"])
        handler = s.service._last_token_handlers["cl"]
        handler()
        after = _store(s)

        assert after["screen_id"] != before["screen_id"], (
            "a revoked screen must be replaced")
        codes = [c for c in s.lounge.PAIRING_CALLS if c[0] == "get_pairing_code"]
        assert any(c[1].get("screen_id") == after["screen_id"] for c in codes), (
            "a pairing code must be fetched for the new screen")
        assert after["screen_id_m"] == before["screen_id_m"], "music session survives"


def test_boot_refreshes_an_expired_token_in_place():
    """The outage path: the stored token lapsed while Kodi was off/disabled.

    Boot must refresh it (same screen id) BEFORE the handshake, so the listener
    never enters the 401 loop at all.
    """
    expired = {
        "device_id": "dev-boot",
        "screen_id": "bootscreen1",
        "lounge_token": "tok-expired-cl",
        "expiration": int((time.time() - 86400) * 1000),
        "screen_id_m": "bootscreen2",
        "lounge_token_m": "tok-expired-m",
        "expiration_m": int((time.time() - 86400) * 1000),
    }
    with Scenario(settings={"session_data": json.dumps(expired)}) as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two sessions bound")
        after = _store(s)
        assert after["screen_id"] == "bootscreen1", (
            f"boot must keep the paired screen: {after['screen_id']}")
        assert after["screen_id_m"] == "bootscreen2"
        assert after["lounge_token"] != "tok-expired-cl", "cl token refreshed at boot"
        assert after["lounge_token_m"] != "tok-expired-m", "music token refreshed at boot"
        assert after["expiration"] > time.time() * 1000
        # Both sessions bound on the refreshed tokens (no 401 loop).
        assert len(s.lounge.sessions) == 2


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_token_expiry OK")


if __name__ == "__main__":
    main()
