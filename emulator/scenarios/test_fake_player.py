#!/usr/bin/env python3
"""Smoke: clock math, frame encoding, Player quirks, PlayList snapshot semantics."""
import _bootstrap  # noqa: F401
import kodi_stub

kodi_stub.install()
import sys
import time
import threading

import xbmc
import xbmcgui


def test_clock():
    from kodi_stub.clock import SimulatedPlayback
    c = SimulatedPlayback(30.0, rate=10.0)
    c.play()
    time.sleep(0.3)
    t = c.get_time()
    assert 2.0 <= t <= 4.0, t
    c.pause()
    p = c.get_time()
    time.sleep(0.1)
    assert c.get_time() == p, "paused must not advance"
    assert c.seek(999) == 30.0 and c.seek(-5) == 0.0
    assert c.state == "paused"
    c2 = SimulatedPlayback(0.3)
    c2.play()
    time.sleep(0.6)
    assert c2.get_time() >= c2.duration


def test_frame_roundtrip():
    from lounge_server.frames import encode_frame, _normalized
    from resources.lib.lounge.session import parse_frames
    samples = [
        [[0, ["c", "SID"]], [1, ["S", "GSID"]]],
        [[3, ["setPlaylist", {"videoId": "v1", "videoIds": "v1,v2", "currentTime": "7"}]]],
        [[5, ["pause"]]],
    ]
    for s in samples:
        cmds, _ = parse_frames(encode_frame(s).decode())
        assert [(c[0], c[1], c[2]) for c in cmds] == _normalized(s), s


def test_player_quirks():
    kodi_stub.reset()
    xbmc.MEDIA["http://m/"] = {"duration": 60.0}
    events = []
    callsites = []

    class P(xbmc.Player):
        def onPlayBackStarted(self): events.append("started"); callsites.append(threading.get_ident())
        def onPlayBackPaused(self): events.append("paused")
        def onPlayBackResumed(self): events.append("resumed")
        def onPlayBackStopped(self): events.append("stopped")
        def onPlayBackEnded(self): events.append("ended")

    p = P()
    caller = threading.get_ident()
    p.play("http://m/x")
    deadline = time.time() + 2
    while not events and time.time() < deadline:
        time.sleep(0.01)
    assert events == ["started"], events
    assert callsites[0] != caller, "callback must be async"
    assert p.isPlaying()
    assert p.getTotalTime() == 60.0
    # pause keeps isPlaying True, pause() toggles
    p.pause()
    time.sleep(0.2)
    assert events == ["started", "paused"], events
    assert p.isPlaying(), "isPlaying() must stay True while paused"
    assert xbmc.getCondVisibility("Player.Paused")
    t0 = p.getTime()
    time.sleep(0.3)
    assert abs(p.getTime() - t0) < 0.05, "clock must freeze while paused"
    p.pause()  # toggle -> resume
    time.sleep(0.2)
    assert events == ["started", "paused", "resumed"], events
    p.seekTime(30)
    time.sleep(0.15)
    assert 29.5 <= p.getTime() <= 31.5
    p.stop()
    time.sleep(0.2)
    assert events[-1] == "stopped" and not p.isPlaying()
    assert p.getTime() == 0.0
    assert p.getTotalTime() == 0.0


def test_playlist_snapshot():
    kodi_stub.reset()
    pl = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
    li = xbmcgui.ListItem("original")
    pl.add("plugin://x/?play=a", li)
    li.setLabel("changed-later")  # must NOT affect the snapshotted label
    assert pl.size() == 1
    assert pl.get_entry(0).label == "original"
    pl.remove("plugin://x/?play=a")
    assert pl.size() == 0


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_fake_player OK")


if __name__ == "__main__":
    main()
