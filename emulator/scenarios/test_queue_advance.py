#!/usr/bin/env python3
"""Queue advance: natural end advances once; Kodi-queue Ended does not
double-start; phone-side queue edits resync the index."""
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def _cast_queue(s, vids=("v1", "v2", "v3")):
    s.phone.set_playlist(vids[0], list(vids), current_time=0)
    s.wait_until(lambda: vids[0] in (s.playing_file() or ""), what=f"{vids[0]} starts")


def test_natural_advance():
    with Scenario() as s:
        _cast_queue(s)
        # finish v1 naturally -> Kodi advances the queue itself
        s.end_media()
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2 starts after natural end")
        assert s.resolve_count("v2") == 1, "v2 resolved exactly once (warm cache from prefetch)"
        s.end_media()
        s.wait_until(lambda: "v3" in (s.playing_file() or ""), what="v3 starts")
        # end of last item: playback stops
        s.end_media()
        s.wait_until(lambda: s.playing_file() is None, what="queue end stops playback")


def test_now_playing_follows_queue():
    with Scenario() as s:
        _cast_queue(s)
        s.end_media()
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2")
        r = s.lounge.wait_for_report("nowPlaying", lambda r: r.get("videoId") == "v2", timeout=10)
        assert r, "phone must see v2 as now playing"
        s.end_media()
        s.wait_until(lambda: "v3" in (s.playing_file() or ""), what="v3")
        r = s.lounge.wait_for_report("nowPlaying", lambda r: r.get("videoId") == "v3", timeout=10)
        assert r, "phone must see v3 as now playing"


def test_phone_queue_edit_resyncs():
    """Phone removes upcoming v2 mid-play; advance must land on v3."""
    with Scenario() as s:
        _cast_queue(s, ("v1", "v2", "v3"))
        s.phone.update_playlist(["v1", "v3"])   # v2 removed while v1 plays
        s.end_media()
        s.wait_until(lambda: "v3" in (s.playing_file() or ""), what="v3 after v2 removed")
        import time
        time.sleep(1.0)
        assert "v2" not in (s.playing_file() or ""), "v2 must be skipped"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_queue_advance OK")


if __name__ == "__main__":
    main()
