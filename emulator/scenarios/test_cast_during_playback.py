#!/usr/bin/env python3
"""Cast during playback: a cast that lands while another item is still playing
must PLAY, not be cancelled by the reconciler adopting Kodi's lagging info
label (device log 2026-09-20: 4/4 drops had such a tick in front of them)."""
import logging
import time

from harness import Scenario
from lounge_server.phone import Phone
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _capture_player_log():
    import resources.lib.player_bridge as pb
    h = _Capture()
    pb.logger.addHandler(h)
    return h


def _slow_resolve(seconds):
    """Wrap VideoResolver.resolve so the resolve straddles >=1 reconcile tick."""
    from resources.lib.resolver import VideoResolver
    orig = VideoResolver.resolve

    def slow(self, vid, prefetch=False):
        time.sleep(seconds)
        return orig(self, vid, prefetch=prefetch)

    VideoResolver.resolve = slow
    return orig


def _connect_music_phone(s):
    phone = Phone(s.lounge)
    sid = s.wait_for_session("m", timeout=5.0)
    phone.target(sid)
    phone.connect()
    time.sleep(0.4)
    return phone


def test_cast_during_playback_is_not_dropped():
    orig = _slow_resolve(4.0)          # > 2 ticks: deterministic window
    try:
        with Scenario(settings={"music_visualizer": "always"}) as s:
            phone = _connect_music_phone(s)
            cap = _capture_player_log()
            phone.set_playlist("m1", ["m1", "m2"], current_time=0)
            s.wait_until(lambda: "m1" in (s.playing_file() or ""), what="m1 plays")

            phone.set_playlist("m2", ["m1", "m2"], current_time=0)
            s.wait_until(lambda: "m2" in (s.playing_file() or ""),
                         timeout=20.0, what="m2 plays after a mid-playback cast")

            dropped = [m for m in cap.records if "superseded, dropping" in m]
            assert not dropped, f"cast was cancelled by the reconciler: {dropped}"
            assert s.resolve_count("m2") == 1, "m2 must be resolved exactly once"
    finally:
        from resources.lib.resolver import VideoResolver
        VideoResolver.resolve = orig


def test_identity_does_not_flap_after_handoff():
    """The info label also lags ~2s AFTER the handoff; the snapshot must not
    flip back to the outgoing item once the new cast has started."""
    with Scenario(settings={"music_visualizer": "always"}) as s:
        phone = _connect_music_phone(s)
        phone.set_playlist("m1", ["m1", "m2"], current_time=0)
        s.wait_until(lambda: "m1" in (s.playing_file() or ""), what="m1 plays")
        phone.set_playlist("m2", ["m1", "m2"], current_time=0)
        s.wait_until(lambda: "m2" in (s.playing_file() or ""),
                     timeout=20.0, what="m2 plays")
        seen = []
        for _ in range(8):                     # ~4s of samples
            seen.append(s.snapshot().current_video_id)
            time.sleep(0.5)
        assert all(v == "m2" for v in seen), f"identity flapped: {seen}"
        r = s.lounge.wait_for_report("nowPlaying", lambda r: r.get("videoId") == "m2",
                                     timeout=10)
        assert r, "phone must be told m2 is the current item"


def test_published_cpn_matches_reported_item():
    """Every onStateChange must carry the cpn of the item nowPlaying names."""
    with Scenario(settings={"music_visualizer": "always"}) as s:
        phone = _connect_music_phone(s)
        phone.set_playlist("m1", ["m1", "m2"], current_time=0)
        s.wait_until(lambda: "m1" in (s.playing_file() or ""), what="m1 plays")
        phone.set_playlist("m2", ["m1", "m2"], current_time=0)
        s.wait_until(lambda: "m2" in (s.playing_file() or ""),
                     timeout=20.0, what="m2 plays")
        s.lounge.clear_reports()
        time.sleep(4.0)
        bad = []
        for r in s.lounge.reports("onStateChange"):
            cpn = str(r.get("cpn") or "")
            if cpn.startswith("cpn_") and cpn != "cpn_m2":
                bad.append(cpn)
        assert not bad, f"onStateChange published a foreign cpn: {bad}"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_cast_during_playback OK")


if __name__ == "__main__":
    main()
