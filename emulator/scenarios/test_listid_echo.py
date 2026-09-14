#!/usr/bin/env python3
"""listId echo: the mobile client keys its player model on the listId it
sent in setPlaylist. A nowPlaying report whose videoId is not attached to a
listId the client already knows is silently rejected, so after a TV-side
advance the app keeps rendering the OLD track (0:00 + replay glyph) instead
of moving to the next song.

Regression guard: every report the bridge emits must carry the listId the
phone handed us, including on autonomous advance and on queue-pick sync.
"""
from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def test_initial_cast_reports_listid():
    """The first nowPlaying after a cast must echo the phone's listId."""
    with Scenario() as s:
        s.phone.set_playlist("v1", ["v1", "v2"], current_time=0)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
        r = s.lounge.wait_for_report(
            "nowPlaying",
            lambda r: r.get("videoId") == "v1" and r.get("listId"),
            timeout=10,
        )
        assert r, "nowPlaying for v1 must carry listId"
        assert r["listId"] == s.phone.list_id, (
            f"listId echoed must match the phone's: got {r.get('listId')!r}, "
            f"want {s.phone.list_id!r}"
        )
        assert r.get("currentIndex") == "0", f"index must be 0, got {r.get('currentIndex')!r}"


def test_tv_advance_reports_new_video_with_listid_and_index():
    """THE BUG: on TV-initiated advance the app never progressed because the
    new videoId was reported without the queue identity that binds it."""
    with Scenario() as s:
        s.phone.set_playlist("v1", ["v1", "v2", "v3"], current_time=0)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")

        # Let the bridge report v1 first, so the advance is a pure delta.
        s.lounge.wait_for_report("nowPlaying", lambda r: r.get("videoId") == "v1", timeout=10)

        # TV-side advance: Kodi finishes v1 on its own and moves to v2.
        s.end_media()
        s.wait_until(lambda: "v2" in (s.playing_file() or ""), what="v2 after TV advance")

        r = s.lounge.wait_for_report(
            "nowPlaying",
            lambda r: r.get("videoId") == "v2",
            timeout=10,
        )
        assert r, "phone must be told v2 is now playing"
        assert r.get("listId") == s.phone.list_id, (
            "TV-advance report MUST echo listId, else the app keeps showing the "
            f"old track; got {r.get('listId')!r}"
        )
        assert r.get("currentIndex") == "1", (
            f"TV-advance report must carry the new queue index; got {r.get('currentIndex')!r}"
        )


def test_playlist_report_carries_listid():
    """nowPlayingPlaylist is what lets the client bind the queue; it needs
    the listId too, plus the full ordered videoIds."""
    with Scenario() as s:
        s.phone.set_playlist("v1", ["v1", "v2"], current_time=0)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
        r = s.lounge.wait_for_report(
            "nowPlayingPlaylist",
            lambda r: r.get("videoId") == "v1" and r.get("listId"),
            timeout=10,
        )
        assert r, "nowPlayingPlaylist must carry listId"
        assert r["listId"] == s.phone.list_id
        assert r.get("videoIds") == "v1,v2", f"full queue expected, got {r.get('videoIds')!r}"


def test_queue_pick_from_kodi_echoes_listid():
    """Picking a different item in Kodi's own queue view must resync the
    phone onto that item, still inside the phone's listId."""
    with Scenario() as s:
        s.phone.set_playlist("v1", ["v1", "v2", "v3"], current_time=0)
        s.wait_until(lambda: "v1" in (s.playing_file() or ""), what="v1 starts")
        s.lounge.wait_for_report("nowPlaying", lambda r: r.get("videoId") == "v1", timeout=10)

        # Simulate a Kodi-native queue jump: Kodi starts the next queue item by
        # plugin URL and fires onPlayBackStarted, which is exactly what the
        # bridge's _sync_current_from_kodi path has to reconcile.
        xbmc.MEDIA["plugin://plugin.service.ytlounge-cast/?play=v3"] = {"duration": 180.0}
        xbmc.Player().play("plugin://plugin.service.ytlounge-cast/?play=v3")
        s.wait_until(lambda: "v3" in (s.playing_file() or ""), what="v3 via Kodi UI")

        r = s.lounge.wait_for_report(
            "nowPlaying",
            lambda r: r.get("videoId") == "v3",
            timeout=10,
        )
        assert r, "phone must learn about the Kodi-side queue pick"
        assert r.get("listId") == s.phone.list_id, "Kodi-side pick report must echo listId"
        assert r.get("currentIndex") == "2", (
            f"index must follow the queue position; got {r.get('currentIndex')!r}"
        )


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_listid_echo OK")


if __name__ == "__main__":
    main()
