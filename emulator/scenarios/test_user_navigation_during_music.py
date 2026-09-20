"""The GUI belongs to the user while music plays (device-reported bug).

Reported symptom (device):

    "There is a bug in the kodi plugin that prevents opening the main menu and
     any other menu like playlist whenever music is playing."

Cause: the receiver asserted window 12006 from the *state projection*, and the
projection runs on every snapshot version — which bumps on every 2s position
poll. So 12006 was re-asserted for as long as music played, and Kodi's
ActivateWindow pops the duplicate out of the window history: whatever the user
had just opened (home, the playlist view) was closed again within a tick.

The assertion itself is not optional: Kodi's own PlaybackCleanup pops the
window back to the GUI ~9-12s after a video->audio switch (device-verified),
so it has to be held across that. It is the *bound* that was lost —
``MUSIC_WINDOW_ENGAGE_SECONDS`` is armed when a play is handed to Kodi on the
music lane and when the music lane is entered, and nothing else may arm it.
Outside it, the receiver must issue no window builtin at all, whatever window
the user picked.

Scenarios here drive the real addon against the fake Kodi API, wait out the
engagement window, then model the user navigating (home, then the playlist
view) and assert the receiver leaves the GUI alone from then on.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Scenario, xbmc  # noqa: E402

# Long enough that playback outlives the engagement window, the two user
# navigations and their observation windows (the fake player's items are 8s).
QUEUE = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"]


def _cast(s, phone, video_id, video_ids, timeout=20.0):
    """Send a cast on the music lounge, retrying if the listener rebinds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session("m", timeout=5.0)
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


def _wait_out_engagement_window(s):
    """Wait until the receiver's bounded music-window repair has expired.

    Inside that window the receiver legitimately re-asserts the visualisation
    window (Kodi's own late window pop has to be outlived); the bug under test
    is that it used to keep doing so forever. Read through getattr because a
    receiver without the bound at all (the regression) has no deadline: that
    counts as "never expires", and the assertion below is what catches it.
    """
    bridge = s.service._emu_player
    s.wait_until(lambda: time.monotonic() > getattr(bridge, "_music_window_until", 0.0),
                 timeout=45.0, what="the music-window engagement window to expire")
    assert getattr(bridge, "_projected_lane", "m") == "m", \
        f"receiver should still be on the music lane, it is on {bridge._projected_lane}"


def _assert_user_keeps(window_name, win_id, hold=8.0):
    """The user opens a window; the receiver must not touch the GUI afterwards.

    ``hold`` covers several 2s monitor polls, each of which used to bump the
    snapshot version and re-assert 12006.
    """
    before = len(xbmc.BUILTIN)
    xbmc.user_opens_window(win_id)
    assert xbmc.getCondVisibility("Window.IsActive(visualisation)") is False, \
        f"fixture problem: {window_name} did not replace the visualisation window"
    time.sleep(hold)
    assert xbmc._windows.active == win_id, (
        f"{window_name} was closed by the receiver {hold:.0f}s after the user opened it: "
        f"history={xbmc._windows.history}")
    stomps = [c for c in xbmc.BUILTIN[before:] if "12006" in c]
    assert not stomps, (
        f"receiver re-asserted the visualisation window while the user was in "
        f"{window_name}: {stomps}")


def test_user_menus_survive_music_playback():
    """Home and the playlist view stay open while music plays."""
    with Scenario(settings={"music_visualizer": "always"}) as s:
        _cast(s, s.phone, "s1", QUEUE)
        s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                     timeout=20.0, what="visualisation window after the music cast")

        _wait_out_engagement_window(s)

        # 1. The main menu.
        _assert_user_keeps("the main menu", xbmc.WINDOW_HOME)

        # 2. Another menu: the music playlist view. (The receiver cannot tell
        #    one window from another here — every window it did not ask for is
        #    the user's.)
        _assert_user_keeps("the music playlist", xbmc.WINDOW_MUSIC_PLAYLIST)

        # 3. ...and the takeover still works: a cast is not supposed to be
        #    silenced by the fix, it takes the screen again.
        assert xbmc.Player().isPlaying(), "playback stopped during the assertions"
        _cast(s, s.phone, "s2", QUEUE)
        s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                     timeout=20.0,
                     what="visualisation window after a new cast from the playlist view")

        return "home and playlist stayed open while music played; a new cast still took the screen"


def main():
    for fn in (test_user_menus_survive_music_playback,):
        started = time.time()
        msg = fn()
        print(f"    {fn.__name__} OK ({time.time() - started:.1f}s) — {msg}")


if __name__ == "__main__":
    main()
