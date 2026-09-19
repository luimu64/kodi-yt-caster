#!/usr/bin/env python3
"""R10 — vocabulary coverage.

The declared vocabulary is logged at session start, and the two families the
receiver genuinely knows are on the wire:

  * onAdStateChange / onAdPlaying — "no ad" is a known fact (we resolve locally
    with yt-dlp and never inject ads), so a skip control is backed by a real
    family instead of a phone-side default.
  * autoplayUpNext — derived from the stored queue, and OMITTED when there is no
    next item (R6: never guess).
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Scenario  # noqa: E402

def _cast(s, video_id, video_ids, theme="cl", timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session(theme, timeout=5.0)
        s.phone.target(sid)
        s.phone.connect()
        time.sleep(0.4)
        s.phone.set_playlist(video_id, list(video_ids))
        try:
            s.wait_until(lambda: s.resolve_count(video_id) > 0, timeout=4.0,
                         what=f"resolve of {video_id}")
            return
        except AssertionError:
            continue
    raise AssertionError(f"cast of {video_id} never reached the receiver")

def test_coverage_line_logged_at_session_start():
    import logging

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Cap()
    slog = logging.getLogger("ytlounge.session")
    slog.addHandler(handler)
    try:
        with Scenario() as s:
            s.wait_until(lambda: len(s.lounge.sessions) == 2, what="two channels bound")
    finally:
        slog.removeHandler(handler)
    hit = [m for m in records if m.startswith("vocabulary: ")]
    assert hit, records[-20:]
    assert "vocabulary: 7/10" in hit[0], hit[0]
    assert "autoplayModeChanged" in hit[0], hit[0]

def test_ad_state_and_up_next_on_a_queue_cast():
    with Scenario() as s:
        _cast(s, "v1", ["v1", "v2", "v3"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 plays")

        # onAdStateChange: a known "no ad" rides a playback/identity change.
        r = s.lounge.wait_for_report("onAdStateChange", timeout=10)
        assert r, "onAdStateChange must be emitted"
        assert r.get("adState") == "0", r
        assert r.get("isSkippable") == "false", r

        # onAdPlaying accompanies a new item.
        r2 = s.lounge.wait_for_report("onAdPlaying", timeout=10)
        assert r2, "onAdPlaying must be emitted on a new item"
        assert r2.get("adState") == "0", r2

        # autoplayUpNext, derived from the stored queue: v1 -> v2.
        up = s.lounge.wait_for_report("autoplayUpNext", timeout=10)
        assert up, "autoplayUpNext must be emitted while a next item exists"
        assert up.get("videoId") == "v2", up
        assert up.get("listId") == s.phone.list_id, up

def test_up_next_omitted_when_last_item():
    """R6/R10: with the last item playing there is nothing to advertise, so the
    family is omitted rather than guessed."""
    with Scenario() as s:
        _cast(s, "v1", ["v1"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 plays")
        s.lounge.clear_reports()
        # A discrete change (pause) re-publishes; up-next must stay absent.
        s.phone.pause()
        s.lounge.wait_for_report("onStateChange", lambda r: r.get("state") == "2", timeout=10)
        time.sleep(1.0)
        ups = s.lounge.reports("autoplayUpNext")
        assert not ups, f"up-next must be omitted for the last item: {ups}"

def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_vocabulary_coverage OK")

if __name__ == "__main__":
    main()
