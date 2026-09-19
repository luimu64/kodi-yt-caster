#!/usr/bin/env python3
"""Scenario: Duration fallback via getTotalTime(), position loop updates, and queue sync duration dispatch."""
import time
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc
import resources.lib.ytdlp_bridge as ytdlp_bridge


def test_duration_fallback_from_kodi():
    """When metadata duration is missing (0), bridge falls back to Kodi Player.getTotalTime()."""
    with Scenario() as s:
        orig_resolve = ytdlp_bridge.YtDlpBridge.resolve

        def _no_duration_resolve(self, video_id):
            res = orig_resolve(self, video_id)
            res["duration"] = 0
            return res

        ytdlp_bridge.YtDlpBridge.resolve = _no_duration_resolve
        try:
            s.phone.set_playlist("v_fallback", ["v_fallback"], current_time=0)
            s.wait_until(lambda: "v_fallback" in (s.playing_file() or ""), what="v_fallback playing")
            # media.example duration is 8.0 in harness; bridge should fall back to 8
            np = s.lounge.wait_for_report(
                "nowPlaying",
                lambda r: r.get("videoId") == "v_fallback" and r.get("duration") == "8",
                timeout=5.0,
            )
            assert np, f"nowPlaying must report Kodi fallback duration 8, reports: {s.lounge.reports()}"
            sc = s.lounge.wait_for_report(
                "onStateChange",
                lambda r: r.get("duration") == "8",
                timeout=5.0,
            )
            assert sc, f"onStateChange must report Kodi fallback duration 8, reports: {s.lounge.reports()}"
        finally:
            ytdlp_bridge.YtDlpBridge.resolve = orig_resolve


def test_position_loop_emits_both_reports():
    """Version-driven position tick emits nowPlaying with resolved duration during active playback."""
    with Scenario() as s:
        s.phone.set_playlist("v_loop", ["v_loop"], current_time=0)
        s.wait_until(lambda: "v_loop" in (s.playing_file() or ""), what="v_loop playing")
        # Clear initial burst of reports
        time.sleep(0.5)
        s.lounge.clear_reports()
        # Wait for position advance
        time.sleep(2.5)
        # Position tick must emit nowPlaying with duration 180 (from fake resolve)
        np = s.lounge.wait_for_report(
            "nowPlaying",
            lambda r: r.get("videoId") == "v_loop" and r.get("duration") == "180",
            timeout=3.0,
        )
        assert np, f"Position tick must emit nowPlaying with duration: {s.lounge.reports()}"


def test_sync_current_from_kodi_refresh_dispatches_duration():
    """When Kodi advances to a new track via plugin URL, _refresh dispatches updated nowPlaying and onStateChange."""
    with Scenario() as s:
        # Start initial video
        s.phone.set_playlist("v1", ["v1", "v2"], current_time=0)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 playing")
        s.lounge.clear_reports()

        # Simulate Kodi playing next queue track via plugin url
        xbmc.MEDIA["plugin://plugin.service.ytlounge-cast/?play=v2"] = {"duration": 180.0}
        xbmc.Player().play("plugin://plugin.service.ytlounge-cast/?play=v2")
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2 playing")

        # Background _refresh should resolve v2 (duration 180) and dispatch reports
        np = s.lounge.wait_for_report(
            "nowPlaying",
            lambda r: r.get("videoId") == "v2" and r.get("duration") == "180",
            timeout=5.0,
        )
        assert np, f"nowPlaying for v2 with duration 180 must be dispatched: {s.lounge.reports()}"
        sc = s.lounge.wait_for_report(
            "onStateChange",
            lambda r: r.get("duration") == "180",
            timeout=5.0,
        )
        assert sc, f"onStateChange with duration 180 must be dispatched: {s.lounge.reports()}"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_duration_reporting OK")


if __name__ == "__main__":
    main()
