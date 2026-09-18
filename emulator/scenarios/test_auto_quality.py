#!/usr/bin/env python3
"""End-to-end auto quality: a cast starts on the cheapest rendition and the
master is widened until every rendition is published.

Auto quality works by REPUBLISHING a master under the same name, so the only
faithful observation is the publication sequence (`manifest_server.publish_log`)
plus the body Kodi would fetch at each moment. Reading the current body alone
cannot tell a narrowed revision from a full master that was never narrowed —
which is exactly how an earlier version of these tests passed against code with
the feature disabled.

Isolation note: the resolve-time FULL master is the generator's own output, so
every assertion below scopes itself to the NARROWED revision onward. Each
scenario also settles its climb before exiting — the climber is a daemon thread
that republishes through a module-level manifest server, so a leftover one
writes into the next scenario's log and makes unrelated assertions flap.
"""
import time

import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()

from harness import Scenario  # noqa: E402

RENDITION_PREFIX = "http://x/h-"


def _revisions(video_id):
    """Every master body published for this video id, oldest first."""
    from resources.lib.manifest_server import publish_log
    name = f"yt_{video_id}.m3u8"
    return [body for _ts, n, body in publish_log() if n == name]


def _renditions(body):
    return sorted(
        line.strip() for line in body.splitlines()
        if line.strip().startswith(RENDITION_PREFIX)
    )


def _counts(video_id):
    return [len(_renditions(b)) for b in _revisions(video_id)]


def _climb_from_narrowed(video_id):
    """The rendition-count sequence starting at the first NARROWED revision.

    Returns [] when no narrowed revision was ever published, which is the
    signal that auto quality did not run at all.
    """
    counts = _counts(video_id)
    try:
        start = counts.index(1)
    except ValueError:
        return []
    return counts[start:]


def _wait(pred, timeout, what):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}; last={last!r}")


def _settle(video_id, timeout=30.0):
    """Wait for the climb to finish so nothing leaks into the next scenario."""
    try:
        _wait(lambda: _counts(video_id)[-1:] == [3], timeout, f"{video_id} to reach full quality")
    except AssertionError:
        pass  # an unscaled scenario (manual/adaptive) legitimately never climbs


# --------------------------------------------------------------------- tests
def test_playback_starts_on_a_narrowed_master():
    """Auto quality publishes a one-rendition master; disabled, none appears."""
    with Scenario() as s:
        s.phone.set_playlist("h1", ["h1"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        climb = _wait(lambda: _climb_from_narrowed("h1"), 15.0, "a narrowed revision")
        assert climb[0] == 1, f"climb did not start narrowed: {climb}"
        narrowed = [b for b in _revisions("h1") if len(_renditions(b)) == 1]
        assert "h-144" in _renditions(narrowed[0])[0], (
            f"the narrowed revision must expose the cheapest rendition: {narrowed[0]}")
        assert 'NAME="English"' in narrowed[0], "audio group missing from the narrowed master"
        _settle("h1")
        print("  test_playback_starts_on_a_narrowed_master OK")


def test_climb_steps_one_rendition_at_a_time_to_full_quality():
    with Scenario() as s:
        s.phone.set_playlist("h1", ["h1"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        _wait(lambda: _climb_from_narrowed("h1")[-1:] == [3], 25.0, "full quality")
        climb = _climb_from_narrowed("h1")
        assert climb == [1, 2, 3], f"climb did not step one rung at a time: {climb}"
        for body in _revisions("h1"):
            assert 'NAME="English"' in body, "audio group lost in a revision"
        print("  test_climb_steps_one_rendition_at_a_time_to_full_quality OK")


def test_a_new_cast_narrows_its_own_master():
    """Every new item gets its own narrowed revision, not the previous one."""
    with Scenario() as s:
        s.phone.set_playlist("h1", ["h1", "h2"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        _wait(lambda: _climb_from_narrowed("h1"), 15.0, "h1 narrowed")
        _settle("h1")
        s.phone.set_playlist("h2", ["h1", "h2"], current_time=0)
        s.wait_until(lambda: "h2" in (s.playing_file() or ""), what="h2 playing")
        _wait(lambda: _climb_from_narrowed("h2"), 15.0, "h2 narrowed")
        _settle("h2")
        print("  test_a_new_cast_narrows_its_own_master OK")


def test_manual_mode_publishes_no_narrowed_revision():
    """quality_mode=manual leaves the master at full quality from the start."""
    with Scenario(settings={"quality_mode": "manual"}) as s:
        s.phone.set_playlist("h1", ["h1"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        time.sleep(3.0)  # ample time for a climb that must never happen
        counts = _counts("h1")
        assert counts, "no master was published at all"
        assert all(c == 3 for c in counts), f"manual mode narrowed the master: {counts}"
        print("  test_manual_mode_publishes_no_narrowed_revision OK")


def test_adaptive_mode_publishes_no_narrowed_revision():
    with Scenario(settings={"quality_mode": "adaptive"}) as s:
        s.phone.set_playlist("h1", ["h1"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        time.sleep(3.0)
        counts = _counts("h1")
        assert all(c == 3 for c in counts), f"adaptive mode narrowed the master: {counts}"
        print("  test_adaptive_mode_publishes_no_narrowed_revision OK")


def test_non_hls_item_publishes_no_master():
    """A dash/audio cast has no ladder and must be untouched by auto quality."""
    with Scenario() as s:
        s.phone.set_playlist("s1", ["s1"], current_time=0)
        s.wait_until(lambda: "s1" in (s.playing_file() or ""), what="s1 playing")
        time.sleep(1.5)
        assert _revisions("s1") == [], "a non-HLS cast must not publish a master"
        print("  test_non_hls_item_publishes_no_master OK")


def test_superseded_climb_stops_publishing():
    """A cast that is replaced mid-climb must stop republishing."""
    with Scenario() as s:
        s.phone.set_playlist("h1", ["h1", "h2"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        _wait(lambda: _climb_from_narrowed("h1"), 15.0, "h1 narrowed")
        # Replace the cast while h1 is still climbing.
        s.phone.set_playlist("h2", ["h1", "h2"], current_time=0)
        s.wait_until(lambda: "h2" in (s.playing_file() or ""), what="h2 playing")
        time.sleep(1.0)
        frozen = len(_revisions("h1"))
        time.sleep(3.0)
        assert len(_revisions("h1")) == frozen, (
            "the superseded item's climb kept republishing: "
            f"{len(_revisions('h1'))} vs {frozen}")
        _settle("h2")
        print("  test_superseded_climb_stops_publishing OK")


def test_completed_climb_is_not_restarted_by_the_monitor_loop():
    """After the climb finishes, no further revisions may appear.

    The monitor loop re-asserts the climb on EVERY poll, so a climber that is
    not marked finished would be restarted every couple of seconds for the rest
    of the video. Asserting the revision count is frozen after settling is what
    catches that respawn churn.
    """
    with Scenario() as s:
        s.phone.set_playlist("h1", ["h1"], current_time=0)
        s.wait_until(lambda: "h1" in (s.playing_file() or ""), what="h1 playing")
        _wait(lambda: _climb_from_narrowed("h1")[-1:] == [3], 25.0, "full quality")
        frozen = len(_revisions("h1"))
        time.sleep(6.0)  # several monitor-loop polls
        assert len(_revisions("h1")) == frozen, (
            f"the monitor loop kept republishing after the climb finished: "
            f"{len(_revisions('h1'))} vs {frozen}")
        print("  test_completed_climb_is_not_restarted_by_the_monitor_loop OK")


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
    print("test_auto_quality OK")


if __name__ == "__main__":
    main()
