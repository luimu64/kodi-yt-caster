#!/usr/bin/env python3
"""Progress reporting: our own cast must advance the clock it publishes.

Reported symptom (device, 2026-09-28, "progress tracking is broken on both yt
and music" + "pause menu doesn't show any info when playing music"):

    16:36:43 ytlounge.session: REPORT nowPlaying vid=y9GCetHctO0 idx=0 t=2 dur=501 state=1
    16:36:57 ytlounge.session: REPORT nowPlaying vid=y9GCetHctO0 idx=0 t=2 dur=501 state=1
    ... t frozen at 2 for minutes while the phone believed it was playing

Mechanism: a resolved cast plays the loopback master manifest
(``http://127.0.0.1:<port>/yt_<id>.m3u8``), so ``Player.FileNameAndPath``
carries NO ``?play=`` id. ``_reconcile_tick`` parsed the id from that label and
returned early when it found none — the "stored item is authoritative" branch —
which skipped the position/duration tick. No PositionTickEvent was ever emitted
for the manifest lane, so the snapshot kept whatever the phone last asserted:
the progress bar froze and the nowPlaying overlay (which renders the same
payload) had nothing current to show.

The clock must advance for BOTH lanes: the video lane (manifest, no play= id)
and the audio/music lane.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Scenario, xbmc  # noqa: E402


def _cast(s, video_id, video_ids, theme="cl", timeout=20.0):
    """Cast a queue and wait until the receiver resolves the first item."""
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


def _assert_clock_advances(s, theme, video_id, expect_manifest=True):
    s.wait_until(lambda: video_id in (s.playing_file() or ""),
                 what=f"{video_id} starts playing")
    if expect_manifest:
        label = xbmc.getInfoLabel("Player.FileNameAndPath")
        assert "play=" not in label, (
            f"this lane must exercise the manifest/no-id path, label={label!r}")

    start = s.snapshot().position
    s.wait_until(lambda: s.snapshot().position > start + 1,
                 timeout=15.0,
                 what=f"snapshot position to advance past {start} ({theme})")

    # The phone only sees what the channel publishes: the published snapshot and
    # the nowPlaying report must carry the same advanced clock.
    pub = s.last_published(theme)
    assert pub is not None and pub.position > start + 1, (
        f"published position frozen: {getattr(pub, 'position', None)} vs start {start}")
    report = s.lounge.wait_for_report(
        "nowPlaying", pred=lambda r: int(r.get("currentTime") or 0) > start + 1,
        timeout=15.0)
    assert report is not None, (
        "no nowPlaying report carried an advanced currentTime — the phone's "
        "progress bar and overlay info come from exactly this field")
    assert int(report.get("duration") or 0) > 0, "duration must be reported"

    # Cadence: the phone only sees what we post, so a slow publish IS visible
    # lag ("phone lags behind around 2 seconds"). At 1 Hz the reported clock must
    # never be more than a tick or so behind the player's own clock.
    s.lounge.clear_reports()
    time.sleep(3.0)
    fresh = s.lounge.reports("nowPlaying")
    assert len(fresh) >= 2, (
        f"expected >= 2 nowPlaying reports in 3s at 1 Hz, got {len(fresh)}")
    player_clock = float(xbmc.Player().getTime())
    reported = max(int(r.get("currentTime") or 0) for r in fresh)
    assert player_clock - reported <= 2.0, (
        f"reported clock lags the player by {player_clock - reported:.1f}s "
        f"(player={player_clock}, reported={reported})")



def test_music_lane_reports_a_moving_clock():
    """A music-app queue whose first item is a real music video.

    Device case: the phone cast audioOnly=true and Kodi opened the loopback
    manifest in the VideoPlayer (no ?play= id in the label), so the music
    channel needs the same manifest-lane clock as the video channel.
    """
    with Scenario(settings={"music_visualizer": "auto"}) as s:
        _cast(s, "v1", ["v1", "s1"], theme="m")
        _assert_clock_advances(s, "m", "v1")


def test_video_lane_reports_a_moving_clock():
    with Scenario() as s:
        _cast(s, "v1", ["v1", "v2"], theme="cl")
        _assert_clock_advances(s, "cl", "v1")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_progress_reporting OK")


if __name__ == "__main__":
    main()
