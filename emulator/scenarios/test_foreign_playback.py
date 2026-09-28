#!/usr/bin/env python3
"""Playing a movie in Kodi must not disturb anything.

Reported symptom (device, Kodi 21.3 / LibreELEC):

    "The kodi yt addon is interfering with the regular player when playing
     movies and stuff. It should not do anything unless connected to and being
     actively casted to."

Device evidence (kodi.log, 2026-09-28) — a Jellyfin movie, no cast in flight:

    12:54:14 JELLYFIN ... jellycon_play_action     (the user starts a movie)
    12:54:19 Activating window ID: 12005 / Window Init (VideoFullScreen.xml)
    12:54:19 ytlounge.player: pause detected via time-stall (t=0 x2 polls)
    12:54:21 ytlounge.player: Resume detected via time advance (t=1)
    12:54:21 Activating window ID: 12006 / Deinit (VideoFullScreen.xml)
    12:54:24 Window Init (VideoFullScreen.xml)     (Kodi puts the film back)
    12:54:35 Activating window ID: 12006           (and it happens all over again)
    12:54:31 ytlounge.player: Music window engagement window closed (30s)

Mechanism: a persisted session left lane "m" with an item. The tick read the
MOVIE's clock as that item's clock (its buffering/OSD stall → "pause"), each
such event bumped the snapshot version, and the projection — which was gated on
nothing at all — asserted the visualisation window over the film.

This scenario reproduces the device state: our music lane is playing, the user
then starts a movie from another addon, and the film's clock stalls. The
receiver must stay passive: no window builtin, no playlist rewrite, no transport
command, and no position inferred from the film's clock.
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Scenario, xbmc  # noqa: E402
import kodi_stub  # noqa: E402

kodi_stub.install()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())

FOREIGN_URL = "http://jellyfin.example:8096/Videos/abc/stream"
FOREIGN_PREFIX = "http://jellyfin.example:8096/"

# The receiver polls every 2s; the stall detector needs two consecutive polls,
# and the inactivity is only interesting across several ticks.
TICKS_SECONDS = 8.0


def _cast_music(s, video_id, video_ids, timeout=20.0):
    """Cast a music-app queue (theme "m") and wait for our item to play."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session("m", timeout=5.0)
        s.phone.target(sid)
        s.phone.connect()
        time.sleep(0.4)
        s.phone.set_playlist(video_id, list(video_ids))
        try:
            s.wait_until(lambda: s.resolve_count(video_id) > 0, timeout=4.0,
                         what=f"resolve of {video_id}")
            s.wait_until(lambda: "play=" in (s.playing_file() or ""), timeout=20.0,
                         what=f"{video_id} playing on the music lane")
            return
        except AssertionError:
            continue
    raise AssertionError(f"cast of {video_id} never reached the receiver")


def _start_foreign_movie(s):
    """The user plays a movie from another addon (Jellyfin)."""
    xbmc.MEDIA[FOREIGN_PREFIX] = {"duration": 600.0, "audio": False}
    xbmc._engine.play_url(FOREIGN_URL)
    s.wait_until(lambda: s.playing_file() == FOREIGN_URL, timeout=5.0,
                 what="the movie to be the playing file")


def _stall_then_resume(s):
    """The device's stall (buffering/OSD) and the clock picking up again.

    Both phases matter: the stall is republished as a pause of the stale item,
    and the clock moving again is republished as a resume — each one bumped the
    snapshot version, and the resume is what drove the window projection at a
    PLAYING snapshot.
    """
    xbmc._engine.clock.rate = 0.0
    time.sleep(5.0)   # > two 2s polls: the stall detector fires
    xbmc._engine.clock.rate = 1.0


def test_movie_playback_is_left_alone():
    """Our music lane was playing; the user switches to a movie."""
    with Scenario(settings={"music_visualizer": "always"}) as s:
        vids = ["s1", "s2"]
        _cast_music(s, "s1", vids)
        s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                     timeout=15.0, what="our music window is up")

        _start_foreign_movie(s)

        # From here on the receiver owns nothing. Watch the stall, the clock
        # picking up again, and several ticks after that.
        import resources.lib.player_bridge as pb
        cap = _Capture()
        pb.logger.addHandler(cap)
        xbmc.BUILTIN.clear()
        try:
            _stall_then_resume(s)
            time.sleep(TICKS_SECONDS)
        finally:
            pb.logger.removeHandler(cap)
        lines = cap.records

        assert "ActivateWindow(12006)" not in xbmc.BUILTIN, (
            "the receiver asserted the visualisation window over the movie: "
            f"{xbmc.BUILTIN}")
        assert "ActivateWindow(12005)" not in xbmc.BUILTIN, (
            f"the receiver asserted a video window of its own: {xbmc.BUILTIN}")
        assert xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"), (
            f"the movie lost its fullscreen window: {xbmc._windows.history}")
        assert xbmc._engine.url == FOREIGN_URL, (
            f"the receiver changed the playing file: {xbmc._engine.url!r}")
        assert not xbmc._engine.paused, "the receiver paused the movie"
        assert not [e for e, _u in xbmc._engine.event_log if e == "paused"], (
            f"the receiver issued a pause: {xbmc._engine.event_log[-6:]}")

        # No clock inference from the movie: the old code published its stall as
        # a pause of the stale cast item.
        assert not [m for m in lines if "pause detected via time-stall" in m], (
            "the receiver read the movie's clock as its own item's")
        assert [m for m in lines if "did not start" in m], (
            f"the receiver never logged going passive: {lines[-8:]}")


def test_connected_phone_cannot_drive_the_movie():
    """A paired phone's transport commands must not touch foreign playback."""
    with Scenario(settings={"music_visualizer": "always"}) as s:
        vids = ["s1", "s2"]
        _cast_music(s, "s1", vids)
        sid = s.wait_for_session("m", timeout=5.0)
        s.phone.target(sid)

        _start_foreign_movie(s)
        mark = len(xbmc._engine.event_log)

        s.phone.pause()
        time.sleep(0.6)
        s.phone.stop()
        s.phone.seek(300)
        time.sleep(1.5)

        assert xbmc._engine.url == FOREIGN_URL, (
            f"a phone command changed the movie's file: {xbmc._engine.url!r}")
        assert not xbmc._engine.paused, "a phone pause paused the movie"
        stopped = [e for e, _u in xbmc._engine.event_log[mark:] if e == "stopped"]
        assert not stopped, (
            f"a phone command stopped the user's movie: {xbmc._engine.event_log[mark:]}")


def test_own_playback_still_projects():
    """The gate must not make a real cast passive (regression guard)."""
    with Scenario(settings={"music_visualizer": "always"}) as s:
        vids = ["s1", "s2"]
        _cast_music(s, "s1", vids)
        s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                     timeout=15.0, what="our music window is up")
        snap = s.snapshot()
        assert snap is not None and snap.play_state != 0, (
            "our own music playback must still be reported")


if __name__ == "__main__":
    test_movie_playback_is_left_alone()
    print("test_movie_playback_is_left_alone OK")
    test_connected_phone_cannot_drive_the_movie()
    print("test_connected_phone_cannot_drive_the_movie OK")
    test_own_playback_still_projects()
    print("test_own_playback_still_projects OK")
