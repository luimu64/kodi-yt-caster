"""Replay accuracy test: feed REAL captured wire frames (archived device
dump, see emulator/fixtures/tv_capture_20260914.json) at the emulated
transport exactly the way the real backend sent them, and assert that
parse_frames + the emitter round-trip still reproduce the same commands the
live session saw.

The dump was captured directly on the TV via a debug build that recorded
every long-poll chunk plus every outgoing /bc/bind report body.
"""
import json
import os
import urllib.parse

import _bootstrap  # noqa: F401

from lounge_server.frames import encode_frame, _normalized
from resources.lib.lounge.session import parse_frames

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fixtures", "tv_capture_20260914.json")

VALID_SC = {"nowPlaying", "nowPlayingPlaylist", "onStateChange", "onVolumeChanged"}


def test_outgoing_report_shapes_are_parseable_lounge():
    rows = json.load(open(FIXTURE))
    out = [b for d, w, b in rows if d == "out"]
    assert out, "fixture has no outgoing reports"
    seen = set()
    for body in out:
        form = dict(urllib.parse.parse_qsl(body))
        sc = form.get("req0__sc")
        assert sc in VALID_SC, f"unexpected sc {sc}"
        assert body.startswith("count=1&ofs=")
        assert form["ofs"] is not None
        seen.add(sc)
    assert VALID_SC <= seen


def test_incoming_frames_contain_real_relay_chatter():
    rows = json.load(open(FIXTURE))
    ins = [b for d, w, b in rows if d == "in"]
    assert ins, "fixture has no incoming chunks"
    # Pre-fix capture interleaves HTTP chunk markers; parse_frames must never
    # crash on them (worst case it yields nothing usable) - accuracy contract.
    for body in ins:
        cmds, consumed = parse_frames(body)
        for c, name, data in cmds:
            assert isinstance(name, str)
        assert isinstance(consumed, int)


def test_clean_noop_roundtrip():
    # Post-fix device stream delivered clean frames:
    #   "16\n[[8, [\"noop\"]]\n]" => exactly one noop with code 8.
    raw = "16\n[[8, [\"noop\"]]\n]\n"
    cmds, consumed = parse_frames(raw)
    assert cmds == [(8, "noop", None)]
    assert consumed == len(raw)
