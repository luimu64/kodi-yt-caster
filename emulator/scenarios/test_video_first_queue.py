#!/usr/bin/env python3
"""Queue that STARTS with a music video, then continues with still-art songs.

Reported symptom (device, Kodi 21.3 / LibreELEC):

    "when playing song with a video and then non-video song afterwards the
     rest of the content gets played using the video player when it should be
     played using the music player"

The queue starts on the video lane (item 0 is a real music video, so no Kodi
music playlist is ever built on the audio path) and must switch to the audio
lane from item 1 onwards — and stay there for the rest of the queue.

Lane is observed the way the device sees it: xbmc.Player().isPlayingAudio()
/ isPlayingVideo() plus the active GUI window.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Scenario, xbmc  # noqa: E402


def _cast(s, video_id, video_ids, theme="m", timeout=20.0):
    """Cast a music-app queue and wait until the receiver resolves it."""
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


def test_video_first_queue_switches_to_audio_lane():
    """Queue = [music video, still-art song, still-art song].

    Item 0 must be the video lane; items 1+ must be the audio lane.
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        vids = ["v1", "s1", "s2"]

        _cast(s, "v1", vids)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
        assert xbmc.Player().isPlayingVideo(), \
            f"item 0 (real music video) must use the video player, got {s.playing_file()}"
        assert not xbmc.Player().isPlayingAudio(), "item 0 must not be on the audio lane"

        # The video ends on its own: the next item is a still-art song.
        s.end_media()
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=40.0,
                     what="item 1 (still-art song) to play in the audio lane")
        assert "v1" not in (s.playing_file() or ""), \
            f"video-lane item still playing: {s.playing_file()}"

        # ... and the lane must STAY audio for the rest of the queue.
        s.end_media()
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=40.0,
                     what="item 2 (still-art song) to play in the audio lane")
        assert not xbmc.Player().isPlayingVideo(), \
            f"the rest of the queue fell back to the video player: {s.playing_file()}"
        return "video-first queue switched to the audio lane and stayed there"


def test_audio_lane_from_video_cast_is_not_left_on_video_player():
    """Direct replacement: a still-art song cast over a playing music video.

    Covers the same user path when the phone sends the next item as its own
    cast command (phone-side skip) rather than letting Kodi advance.
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        _cast(s, "v1", ["v1", "s1"])
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
        assert xbmc.Player().isPlayingVideo(), "v1 should be the video lane"

        _cast(s, "s1", ["v1", "s1"])
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=40.0,
                     what="s1 to play in the audio lane")
        assert not xbmc.Player().isPlayingVideo(), \
            f"still-art song played by the video player: {s.playing_file()}"
        return "still-art song cast over a music video used the audio lane"


def main():
    for fn in (test_video_first_queue_switches_to_audio_lane,
               test_audio_lane_from_video_cast_is_not_left_on_video_player):
        started = time.time()
        msg = fn()
        print(f"    {fn.__name__} OK ({time.time() - started:.1f}s) — {msg}")


if __name__ == "__main__":
    main()
