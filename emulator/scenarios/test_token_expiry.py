#!/usr/bin/env python3
"""Token expiry: Lounge rejecting the token triggers re-registration of ONLY
the affected screen; the other session's tokens survive (the historic
clear()-destroyed-other-session regression).

The full wire path (poll 400 -> 8-failure backoff ~62s -> handshake 400 ->
LoungeTokenExpiredError) is too slow for a deterministic test; the handler
under test is service.make_token_expired(), invoked here directly. The
handshake 400->exception mapping itself is covered by the mock server.
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


def test_expired_token_reregisters_preserving_other_session():
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two sessions bound")
        before = _store(s)
        cl_screen, m_screen = before["screen_id"], before["screen_id_m"]
        cl_tok, m_tok = before["lounge_token"], before["lounge_token_m"]

        # grab the service's handler through the running listener's callback
        # (service builds one per session; find the cl one via its session)
        handler = s.service._last_token_handlers["cl"]  # set by the harness hook below
        handler()

        after = _store(s)
        assert after["screen_id"] != cl_screen, "cl screen must be replaced"
        assert after["screen_id_m"] == m_screen, "music screen id must survive"
        assert after["lounge_token_m"] == m_tok, "music token must survive the cl refresh"
        assert after["lounge_token"] != cl_tok

        # a fresh pairing code was fetched for the NEW cl screen
        gets = [c for c in s.lounge.PAIRING_CALLS if c[0] == "get_pairing_code"]
        assert any(c[1].get("screen_id") == after["screen_id"] for c in gets), \
            "pairing code must be fetched for the new screen"


def test_expired_token_music_session_preserves_cl():
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two sessions bound")
        before = _store(s)
        handler = s.service._last_token_handlers["m"]
        handler()
        after = _store(s)
        assert after["screen_id_m"] != before["screen_id_m"], "music screen replaced"
        assert after["screen_id"] == before["screen_id"], "cl screen must survive"
        assert after["lounge_token"] == before["lounge_token"]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_token_expiry OK")


if __name__ == "__main__":
    main()
