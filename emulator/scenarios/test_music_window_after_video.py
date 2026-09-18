#!/usr/bin/env python3
"""Music view after a music video: the rest of the queue must be the MUSIC player.

Reported symptom (device, Kodi 21.3 / LibreELEC):

    "when playing song with a video and then non-video song afterwards the
     rest of the content gets played using the video player when it should be
     played using the music player"

Device evidence (live probe, JSONRPC video -> audio switch):
    video playing:      isPlayingAudio=False isPlayingVideo=True  fullscreenvideo=True  visualisation=False
    audio item playing: isPlayingAudio=True  isPlayingVideo=False fullscreenvideo=False visualisation=False home=True

So Kodi closes the fullscreen video window itself on the lane switch and the
audio item activates NO window — and Kodi only ever requests fullscreen for the
FIRST file of a music-playlist session, so every later track (Kodi's own
auto-advance through our plugin) played with no music view at all, behind the
GUI. Device log confirms it: MusicVisualisation.xml initialises once, when the
first cast starts, and never again after a music video — the video window was
popped to Home.xml and the visualiser never returned.

These scenarios model that: player callbacks suppressed (Kodi 21 delivers none
to the service process) and the video window closed by Kodi on the switch.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Scenario, xbmc  # noqa: E402


def _assert_music_window(s, what):
    s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                 timeout=15.0, what=f"music (visualisation) window active — {what}")
    assert not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"), \
        f"fullscreen video window still on screen — {what}: {xbmc._windows.history}"
    # The lane is asserted from the PLAYING FILE, not isPlayingAudio(): in the
    # device state under test Kodi reports our audio-only music item as video
    # (isPlayingAudio() False), so that call cannot identify the lane here.
    playing = xbmc.Player().getPlayingFile()
    assert "plugin://" in playing or xbmc.Player().isPlayingAudio(), \
        f"not playing a music-queue item — {what}: {playing}"


def _cast(s, video_id, video_ids, theme="m", timeout=20.0):
    """Cast a music-app queue, retrying until the receiver resolves it."""
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


def test_music_window_returns_after_a_music_video():
    """Audio track -> music video -> Kodi's own advance to the next audio track.

    The music queue is populated by the first (audio) cast, which is what lets
    Kodi advance on its own; the video cast then takes the video lane, and the
    item after it must come back to the music player.
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        vids = ["s1", "v1", "s2"]

        _cast(s, "s1", vids)
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=20.0,
                     what="first audio track to play")
        _assert_music_window(s, "first cast")

        # A real music video from the same queue: video lane, video window up,
        # music window gone (Kodi closes it).
        _cast(s, "v1", vids)
        s.wait_until(lambda: xbmc.Player().isPlayingVideo(), timeout=20.0,
                     what="music video to play in the video lane")
        s.wait_until(lambda: xbmc.SUPPRESS_PLAYER_CALLBACKS or True, timeout=1.0)

        # Device fidelity: the service process gets NO player callbacks, and
        # Kodi's own window cleanup (not a stale race) is what clears the video
        # window on the lane switch.
        xbmc.SUPPRESS_PLAYER_CALLBACKS = True
        xbmc._engine.stale_video_window_on_lane_switch = False
        # ...and the outgoing video PLAYER lingers: when the next item starts,
        # isPlayingVideo() still reports True for a while (device log: 'DoWork -
        # Saving file state for video item <old>' after the new item started).
        # A receiver that decides the lane once, at that instant, gives up.
        # Sticky for the item's whole duration on the device, so model it long.
        xbmc._engine.video_teardown_lag = 120.0

        # Kodi's music-playlist position tracks the music items it has played;
        # the music video is the queue item it sits on now, so its own advance
        # at end-of-file lands on s2 — exactly what the device log shows: Kodi
        # advances the playlist itself and our code plays nothing at all.
        pl = xbmc.PlayList._instance
        if pl is not None:
            try:
                pl._position = vids.index("v1")
            except ValueError:
                pass

        # The music video ends; Kodi advances its own music playlist to s2.
        s.end_media()

        # Wait on the playing FILE: in this device state isPlayingAudio() is
        # False (Kodi reports the item as video), so it cannot gate the flow.
        s.wait_until(lambda: "play=" in (s.playing_file() or ""), timeout=30.0,
                     what="next audio track to start after the music video")
        # The item must end up on the AUDIO lane, not merely have the music
        # window up — that is what the lane repair exists for (Kodi opened it in
        # the video player's mode and keeps restoring that state).
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=15.0,
                     what="misclassified item re-opened on the audio lane")
        _assert_music_window(s, "track following a music video")

        # Kodi's cleanup for the outgoing video lands LATE and pops the window
        # back to the GUI (device log: PreviousWindow -> Home.xml ~9-12s after
        # the next item started). The music window has to come back from that.
        s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                     timeout=15.0, what="music window before Kodi's late pop")
        xbmc._windows.previous()
        assert not xbmc.getCondVisibility("Window.IsActive(visualisation)"), \
            "window pop did not take (fixture problem)"
        s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                     timeout=20.0,
                     what="music window re-engaged after Kodi's late window pop")
        playing = xbmc.Player().getPlayingFile()
        assert "play=" in playing, f"playback lost while re-engaging the window: {playing}"


def test_music_window_returns_on_a_queue_pick_after_a_video():
    """Same defect on the GUI path: the user picks an audio item in Kodi's own
    queue while a music video is playing.

    Kodi switches to the picked item itself (our code plays nothing), so this
    exercises the same adoption hook as the auto-advance case.
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        vids = ["s1", "v1", "s2"]

        _cast(s, "s1", vids)
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=20.0, what="s1")
        _assert_music_window(s, "first cast")

        _cast(s, "v1", vids)
        s.wait_until(lambda: xbmc.Player().isPlayingVideo(), timeout=20.0, what="v1 video")

        # Device fidelity: no player callbacks reach the service, and Kodi's own
        # cleanup (not the stale race) clears the video window on the switch —
        # with the outgoing video player still lingering.
        xbmc.SUPPRESS_PLAYER_CALLBACKS = True
        xbmc._engine.stale_video_window_on_lane_switch = False
        # Sticky for the item's whole duration on the device, so model it long.
        xbmc._engine.video_teardown_lag = 120.0

        # The user picks the next queue item in Kodi's queue view: Kodi starts
        # it directly and our bridge only sees it through its own poll.
        xbmc.Player().play("plugin://plugin.service.ytlounge-cast/?play=s2", None)

        s.wait_until(lambda: "play=s2" in (s.playing_file() or ""), timeout=30.0,
                     what="picked audio item to play")
        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=15.0,
                     what="picked item re-opened on the audio lane")
        _assert_music_window(s, "queue pick after a music video")


def main():
    failures = []
    for fn in (test_music_window_returns_after_a_music_video,
               test_music_window_returns_on_a_queue_pick_after_a_video):
        started = time.time()
        try:
            fn()
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"    {fn.__name__} FAILED ({time.time() - started:.1f}s): {exc}")
        else:
            print(f"    {fn.__name__} OK ({time.time() - started:.1f}s)")
    if failures:
        raise SystemExit(f"{len(failures)} scenario(s) failed: {[n for n, _ in failures]}")


if __name__ == "__main__":
    main()
