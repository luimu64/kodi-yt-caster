"""Lane-switch window hygiene: a video song followed by a song without a video.

Reported symptom (device, Kodi 21.3 / LibreELEC): when a song WITH a music
video is followed by one WITHOUT, the fullscreen video window (12005) keeps
showing the video's last frame on top while the audio player starts underneath.

Two device-verified Kodi behaviours make that happen:

  * ``CGUIWindowManager::ActivateWindow_Internal`` REFUSES an activation while a
    modal dialog is up, and Kodi shows its Busy dialog exactly during playback
    start — so a single ``ActivateWindow(12006)`` can be dropped silently;
  * Kodi leaves the fullscreen video window only when its own PlaybackCleanup
    runs with the video player already gone, and the audio player never
    activates the visualisation window by itself (only the FIRST file of a
    music-playlist session requests fullscreen).

These scenarios drive the real addon against the fake Kodi API and assert the
end state the user sees: the visualisation window active, no stale video window.
"""
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Scenario, xbmc  # noqa: E402


def _assert_visualisation_on_top(s, what):
    """The visualisation window is active and no stale video window is left."""
    s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                 timeout=12.0, what=f"visualisation window active ({what})")
    assert not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"), \
        f"fullscreen video window still active after {what}: {xbmc._windows.history}"
    assert xbmc.Player().isPlayingAudio(), f"audio player not running after {what}"


def _cast(s, phone, video_id, video_ids, theme="m", timeout=20.0):
    """Send a cast on the theme's lounge, retrying if the listener rebinds.

    The listener re-registers its screen shortly after boot (new mock sid), so
    a sid captured too early delivers nothing: re-read the sid and resend until
    the receiver actually starts resolving.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session(theme, timeout=5.0)
        phone.target(sid)
        phone.connect()
        # A command queued in the same instant as remoteConnected is not
        # delivered on the mock relay (it arrives between two long-polls);
        # give the connect a beat before the cast, like a real app.
        time.sleep(0.4)
        phone.set_playlist(video_id, video_ids)
        try:
            s.wait_until(lambda: s.resolve_count(video_id) > 0, timeout=4.0,
                         what=f"resolve of {video_id}")
            return
        except AssertionError:
            continue
    raise AssertionError(f"cast of {video_id} never reached the receiver")


def _cast_until(s, phone, video_id, video_ids, pred, timeout=45.0, what="playback"):
    """Cast until the receiver actually reaches the expected playback state.

    The mock relay can drop a command that lands between two long-polls, so the
    cast is re-sent until the state under test is reached (never a thread-order
    assertion) — the test's real subject is the window state, not delivery.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _cast(s, phone, video_id, video_ids)
        try:
            s.wait_until(pred, timeout=6.0, what=what)
            return
        except AssertionError:
            continue
    raise AssertionError(f"never reached {what} for {video_id}")


def test_visualiser_activation_survives_busy_dialog():
    """The first ActivateWindow(12006) is refused (Busy dialog) and must retry.

    Kodi's Busy dialog is up during playback start and ActivateWindow is
    refused while any modal dialog is active. A fire-once activation therefore
    loses the race and the visualiser never appears.
    """
    with Scenario(settings={"music_visualizer": "always"}) as s:
        # Busy dialog up through the start of audio playback, as on the device.
        xbmc.set_modal_dialog(True)

        def _clear_busy():
            s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=10.0,
                         what="audio playback to start")
            time.sleep(1.0)   # the busy dialog outlives playback start
            xbmc.set_modal_dialog(False)
        threading.Thread(target=_clear_busy, daemon=True).start()

        try:
            _cast(s, s.phone, "s1", ["s1"])
            _assert_visualisation_on_top(s, "audio cast during busy dialog")
        finally:
            xbmc.set_modal_dialog(False)

        return "visualiser activation retried past the busy dialog"


def test_visualisation_repair_after_video_song_ends():
    """Video song -> (Kodi's own queue advance) -> song without a video.

    Models the device: the service process gets no player callbacks, so the
    next track is started by Kodi's music-playlist advance while the video
    player is still the active one — the fullscreen video window is left
    behind and has to be repaired.
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        # An audio-lane track first: it populates Kodi's music playlist with
        # the queue (that is what makes Kodi able to advance on its own).
        _cast_until(s, s.phone, "s1", ["s1", "v1", "s2"],
                    lambda: xbmc.Player().isPlayingAudio(),
                    what="first audio-lane track to play")

        # Then a real music video: video lane, fullscreen video window on top.
        _cast_until(s, s.phone, "v1", ["s1", "v1", "s2"],
                    lambda: xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"),
                    what="fullscreen video window for the music video")
        assert xbmc.Player().isPlayingVideo(), "music video should play in the video lane"

        # Device behaviour: no player callbacks reach the service.
        xbmc.SUPPRESS_PLAYER_CALLBACKS = True

        # The video ends; Kodi advances its own music playlist to the next
        # queue item (a still-art song => audio player).
        def _end_video():
            s.wait_until(lambda: xbmc.media_finished(), timeout=40.0,
                         what="music video to reach its end")
            xbmc.end_of_media()
        threading.Thread(target=_end_video, daemon=True).start()

        s.wait_until(lambda: xbmc.Player().isPlayingAudio(), timeout=40.0,
                     what="next (non-video) song to start playing")
        _assert_visualisation_on_top(s, "video song followed by a non-video song")

        return "stale fullscreen video window repaired after the lane switch"


def test_lane_switch_stops_video_player_before_audio():
    """Invariant: a receiver-driven lane switch stops the video player first.

    The receiver stops the video player explicitly instead of leaving it to
    Kodi's cleanup, so the fullscreen video window is gone before the audio
    play is handed over. (This ordering is not what the reported symptom needs
    — the stale-window repair in the Kodi-driven path is covered by
    test_visualisation_repair_after_video_song_ends — it guards that the
    receiver keeps owning the transition.)
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        _cast_until(s, s.phone, "v1", ["v1", "s1"],
                    lambda: xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"),
                    what="fullscreen video window for the music video")

        events_before = len(xbmc._engine.event_log)
        _cast_until(s, s.phone, "s1", ["v1", "s1"],
                    lambda: xbmc.Player().isPlayingAudio(),
                    what="audio-lane track to replace the video")

        audio_start = None
        for idx, (event, url) in enumerate(xbmc._engine.event_log):
            if event == "started" and url and ("play=s1" in url or "audio" in url):
                audio_start = idx
                break
        assert audio_start is not None, \
            f"no audio-lane playback started: {xbmc._engine.event_log[events_before:]}"
        stopped = [i for i, (event, _u) in enumerate(xbmc._engine.event_log[:audio_start])
                   if event == "stopped"]
        assert stopped, ("video player was not stopped before the audio play — the stale "
                         f"fullscreen video window race is back: {xbmc._engine.event_log}")

        _assert_visualisation_on_top(s, "receiver-driven lane switch")
        return "video player stopped before the audio-lane play"


def main():
    for fn in (test_visualiser_activation_survives_busy_dialog,
               test_visualisation_repair_after_video_song_ends,
               test_lane_switch_stops_video_player_before_audio):
        started = time.time()
        msg = fn()
        print(f"    {fn.__name__} OK ({time.time() - started:.1f}s) — {msg}")


if __name__ == "__main__":
    main()
