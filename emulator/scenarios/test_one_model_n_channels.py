#!/usr/bin/env python3
"""R9 — one model, N subscribers.

A channel is a transport detail, never a second state: two Lounge channels
bound to the one in-process `StateOwner` must publish the SAME playback state
(item, index, listId) for one state change, each rendering it into its own
independently monotonic `ofs` sequence.

Before R9 the connect/getNowPlaying handshakes ran a per-channel fan-out loop
that built each report from the player directly (a second writer, duplicated
model). This scenario asserts the wire result: one change, one batch per
channel, identical identity, no interleaved `ofs` descents.
"""
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Scenario  # noqa: E402

def _reports_for(s, theme):
    """Reports the mock Lounge received on the channel serving `theme`."""
    sid = s.sid_for_theme(theme)
    assert sid, f"no bound session for theme={theme}"
    return [r for r in s.lounge.REPORTS if r.get("sid") == sid]

def _cast(s, phone, video_id, video_ids, theme="cl", timeout=20.0):
    """Cast on a theme's lounge, retrying if the listener rebinds its sid."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session(theme, timeout=5.0)
        phone.target(sid)
        phone.connect()
        time.sleep(0.4)
        phone.set_playlist(video_id, video_ids)
        try:
            s.wait_until(lambda: s.resolve_count(video_id) > 0, timeout=4.0,
                         what=f"resolve of {video_id}")
            return
        except AssertionError:
            continue
    raise AssertionError(f"cast of {video_id} never reached the receiver")

def test_one_change_publishes_identically_on_both_channels():
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two channels bound")

        # Cast a two-item queue on the video channel through the real path.
        _cast(s, s.phone, "v1", ["v1", "v2"], theme="cl")
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 plays")

        # Give both publishers a beat to render the same change.
        s.wait_until(lambda: _reports_for(s, "cl") and _reports_for(s, "m"),
                     what="both channels published")

        cl = _reports_for(s, "cl")
        m = _reports_for(s, "m")

        # Same identity on both channels: listId, current item, index.
        def _ident(rep):
            return (rep.get("listId"), rep.get("videoId"), rep.get("currentIndex"))

        cl_np = [r for r in cl if r["sc"] == "nowPlaying"]
        m_np = [r for r in m if r["sc"] == "nowPlaying"]
        assert cl_np and m_np, (cl, m)
        assert _ident(cl_np[-1]) == _ident(m_np[-1]), (cl_np[-1], m_np[-1])
        assert cl_np[-1].get("listId") == s.phone.list_id
        assert cl_np[-1].get("videoId") == "v1"
        assert cl_np[-1].get("currentIndex") == "0"

        # Per-channel ofs strictly monotonic, never duplicated (R9: each
        # channel renders the shared snapshot into its OWN ofs sequence; the
        # handshake and listener frames consume values too, so it need not be
        # contiguous, but it must never descend or repeat).
        for theme, reps in (("cl", cl), ("m", m)):
            ofs = [int(r["ofs"]) for r in reps]
            assert all(b > a for a, b in zip(ofs, ofs[1:])), (theme, ofs)

        # One discrete change (pause) -> at most one nowPlaying batch per
        # channel; no second writer duplicating the model through a fan-out.
        cl_before = len(_reports_for(s, "cl"))
        m_before = len(_reports_for(s, "m"))
        s.phone.pause()
        s.wait_until(
            lambda: any(r["sc"] == "nowPlaying" and r.get("state") == "2"
                        for r in _reports_for(s, "cl")[cl_before:]),
            what="cl reports paused")
        s.wait_until(
            lambda: any(r["sc"] == "nowPlaying" and r.get("state") == "2"
                        for r in _reports_for(s, "m")[m_before:]),
            what="m reports paused")
        for theme, before in (("cl", cl_before), ("m", m_before)):
            new_np = [r for r in _reports_for(s, theme)[before:] if r["sc"] == "nowPlaying"]
            assert len(new_np) == 1, f"{theme} published {len(new_np)} nowPlaying for one pause"

def test_get_now_playing_resends_shared_state():
    """getNowPlaying is a transport request: each channel re-sends the ONE
    shared snapshot instead of building its own report."""
    with Scenario() as s:
        s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two channels bound")
        _cast(s, s.phone, "v1", ["v1"], theme="cl")
        s.wait_until(lambda: _reports_for(s, "cl"), what="initial publish")

        before_cl = len(_reports_for(s, "cl"))
        s.phone.get_now_playing()
        s.wait_until(lambda: len(_reports_for(s, "cl")) > before_cl,
                     what="getNowPlaying answered")
        # The reply is a re-send of the shared snapshot: a nowPlaying batch
        # carrying the known item, not a channel-built report.
        s.wait_until(
            lambda: any(r["sc"] == "nowPlaying" and r.get("videoId") == "v1"
                        for r in _reports_for(s, "cl")[before_cl:]),
            what="getNowPlaying ack carries shared item")

def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_one_model_n_channels OK")

if __name__ == "__main__":
    main()
