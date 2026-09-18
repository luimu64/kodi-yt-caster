#!/usr/bin/env python3
"""Unit tests for the background quality climber (no Kodi, no network).

Two distinct surfaces are asserted, and keeping them apart matters:

* ``bodies``  — what the sink actually received (the manifest text Kodi sees).
* ``c.published`` — the widths the climber recorded, i.e. how many renditions
  were exposed at each step. Asserting "1 then 2 then 3" is how the climb order
  is proven without opening a manifest.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from resources.lib.quality import MasterRewriter, QualityLadder  # noqa: E402
from resources.lib.quality_climb import QualityClimber, climb_supported  # noqa: E402


def _f(height, vcodec="avc1.4D400B", tbr=100.0):
    return {
        "height": height, "vcodec": vcodec, "tbr": tbr,
        "format_id": str(height),
        "url": f"http://x/v-{height}.m3u8", "protocol": "m3u8_native", "acodec": "none",
    }


MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=YES,AUTOSELECT=YES,URI="http://x/a.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,CODECS="avc1",AUDIO="audio"
http://x/v-1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1",AUDIO="audio"
http://x/v-720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=256x144,CODECS="avc1",AUDIO="audio"
http://x/v-144.m3u8
"""


class _Clock:
    """Virtual clock the test advances, so no real waiting is needed."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


class _Fixture:
    def __init__(self, climber, bodies, ladder, rewriter, clock):
        self.climber = climber
        self.bodies = bodies          # manifest text handed to the sink
        self.ladder = ladder
        self.rewriter = rewriter
        self.clock = clock


def _make(video_id="v1", *, ladder=None, handoff_free=None, is_current=None,
          playing_ok=None, clock=None, start_index=None, stop_event=None):
    bodies = []
    rw = MasterRewriter(MASTER)
    ld = ladder if ladder is not None else QualityLadder(
        video_id, [_f(144), _f(720), _f(1080)])
    clock = clock or _Clock()
    c = QualityClimber(
        video_id, ld, rw,
        publish=bodies.append,
        handoff_free=handoff_free or (lambda: True),
        is_current=is_current or (lambda: True),
        playing_ok=playing_ok or (lambda: True),
        start_index=start_index,
        first_step_delay=0.0,
        step_gap=0.0,
        clock=clock,
        stop_event=stop_event,
    )
    return _Fixture(c, bodies, ld, rw, clock)


def _drain_clock(fixture, *, tick=0.05, steps=4000):
    """Advance a virtual clock in another thread while a climb blocks."""
    stop = threading.Event()

    def _run():
        for _ in range(steps):
            if stop.is_set():
                return
            fixture.clock.tick(tick)
            time.sleep(0.001)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return stop, t


def _uris(body):
    return [l.strip() for l in body.splitlines() if l.strip().startswith("http://x/v-")]


# ------------------------------------------------------------- initial body
def test_initial_body_is_lowest_rung_only():
    fx = _make()
    body = fx.climber.initial_body()
    assert body is not None
    assert "v-144.m3u8" in body, body
    assert "v-1080.m3u8" not in body, "the first rung must not expose the top"
    assert "v-720.m3u8" not in body, "the first rung must not expose a mid rung"
    assert fx.climber.published == [1], fx.climber.published
    assert len(fx.bodies) == 1, fx.bodies
    assert fx.bodies[0] == body
    print("  test_initial_body_is_lowest_rung_only OK")


def test_initial_body_keeps_audio_group():
    fx = _make()
    body = fx.climber.initial_body()
    assert 'NAME="English"' in body, "audio rendition lost on the first rung"
    print("  test_initial_body_keeps_audio_group OK")


def test_initial_body_none_without_a_ladder():
    fx = _make(ladder=QualityLadder("v1", [_f(720)]))
    assert fx.climber.initial_body() is None, "one rung is not a ladder"
    assert fx.bodies == [], "nothing may be published for a single rendition"
    print("  test_initial_body_none_without_a_ladder OK")


def test_climb_supported_matches_the_ladder_shape():
    assert climb_supported({}) is False
    assert climb_supported({"quality_ladder": None, "master_rewriter": None}) is False
    ld = QualityLadder("v", [_f(144), _f(720)])
    assert climb_supported({"quality_ladder": ld, "master_rewriter": MasterRewriter(MASTER)}) is True
    print("  test_climb_supported_matches_the_ladder_shape OK")


def test_width_formula_is_one_rung_at_a_time():
    """The rung-to-width mapping is the heart of the ladder; pin it directly."""
    assert QualityClimber._width_for(0, 3) == 1, "cheapest rung exposes one stream"
    assert QualityClimber._width_for(1, 3) == 2
    assert QualityClimber._width_for(2, 3) == 3
    # Clamped against a mismatched rung count rather than emitting a broken master.
    assert QualityClimber._width_for(5, 3) == 3
    assert QualityClimber._width_for(0, 1) == 1
    print("  test_width_formula_is_one_rung_at_a_time OK")


# ------------------------------------------------------------------ climbing
def test_climb_walks_every_rung_and_stops_at_top():
    """Each step exposes exactly one more rendition, ending at the full master."""
    fx = _make()
    fx.climber.initial_body()
    while not fx.ladder.at_top():
        if fx.ladder.advance() is None:
            break
        fx.climber._publish_current()
    assert fx.climber.published == [1, 2, 3], fx.climber.published
    last = fx.bodies[-1]
    for uri in ("v-144.m3u8", "v-720.m3u8", "v-1080.m3u8"):
        assert uri in last, f"{uri} missing from the final master"
    assert 'NAME="English"' in last, "audio group lost on the last step"
    print("  test_climb_walks_every_rung_and_stops_at_top OK")


def test_growing_exposure_never_drops_a_cheap_rendition():
    """A superset property: a player mid-climb keeps every playlist it had."""
    fx = _make()
    fx.climber.initial_body()
    seen = [set(_uris(fx.bodies[-1]))]
    while not fx.ladder.at_top():
        if fx.ladder.advance() is None:
            break
        fx.climber._publish_current()
        seen.append(set(_uris(fx.bodies[-1])))
    for prev, cur in zip(seen, seen[1:]):
        assert prev <= cur, f"revision dropped renditions: {prev} -> {cur}"
    print("  test_growing_exposure_never_drops_a_cheap_rendition OK")


def test_climb_waits_while_handoff_is_pending():
    """No climb step may run while a playback handoff is in flight."""
    fx = _make(handoff_free=lambda: False)
    fx.climber.initial_body()
    assert fx.climber.published == [1]
    stop, t = _drain_clock(fx)
    try:
        fx.climber._climb()
    finally:
        stop.set()
        t.join(timeout=5.0)
    assert fx.climber.published == [1], f"climbed during a handoff: {fx.climber.published}"
    assert fx.ladder.at_top() is False
    print("  test_climb_waits_while_handoff_is_pending OK")


def test_climb_stops_when_playback_is_not_ok():
    fx = _make(playing_ok=lambda: False)
    fx.climber.initial_body()
    stop, t = _drain_clock(fx)
    try:
        fx.climber._climb()
    finally:
        stop.set()
        t.join(timeout=5.0)
    assert fx.climber.published == [1], f"climb ran without playback: {fx.climber.published}"
    print("  test_climb_stops_when_playback_is_not_ok OK")


def test_climb_stops_when_superseded():
    """A newer cast must cancel the old climb before it publishes anything."""
    fx = _make(is_current=lambda: False)
    fx.climber.initial_body()
    stop, t = _drain_clock(fx)
    try:
        fx.climber._climb()
    finally:
        stop.set()
        t.join(timeout=5.0)
    assert fx.climber.published == [1], f"superseded climb published: {fx.climber.published}"
    print("  test_climb_stops_when_superseded OK")


def test_stop_event_cancels_the_climb():
    ev = threading.Event()
    ev.set()
    fx = _make(stop_event=ev)
    fx.climber.initial_body()
    stop, t = _drain_clock(fx)
    try:
        fx.climber._climb()
    finally:
        stop.set()
        t.join(timeout=5.0)
    assert fx.climber.published == [1], fx.climber.published
    print("  test_stop_event_cancels_the_climb OK")


def test_start_index_can_begin_higher():
    """A user who pinned a floor still climbs, starting from that rung."""
    fx = _make(start_index=1)
    body = fx.climber.initial_body()
    assert fx.climber.published == [2], fx.climber.published
    assert "v-720.m3u8" in body, body
    assert "v-1080.m3u8" not in body, "the top rung must still be climbed to"
    print("  test_start_index_can_begin_higher OK")


def test_threaded_climb_reaches_the_top():
    """End-to-end threaded run, virtual clock driven by a helper thread."""
    fx = _make()
    fx.climber.initial_body()
    stop, t = _drain_clock(fx)
    fx.climber.start()
    try:
        deadline = time.monotonic() + 15.0
        while not fx.ladder.at_top() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fx.ladder.at_top(), f"climb did not reach the top: {fx.climber.published}"
        assert fx.climber.published[0] == 1, fx.climber.published
        assert fx.climber.published[-1] == 3, fx.climber.published
    finally:
        stop.set()
        fx.climber.stop_event.set()
        if fx.climber._thread:
            fx.climber._thread.join(timeout=5.0)
            assert not fx.climber._thread.is_alive(), "climb thread leaked"
        t.join(timeout=5.0)
    print("  test_threaded_climb_reaches_the_top OK")


def test_finished_climber_is_never_restarted():
    """A completed climb must not be restarted by the monitor loop.

    The bridge re-asserts the climb on every poll, so without the ``finished``
    flag a finished item spawns a fresh dead climb thread every couple of
    seconds for the rest of the video.
    """
    fx = _make()
    fx.climber.initial_body()
    # Drive the ladder to the top by hand, then run the climb to completion.
    while not fx.ladder.at_top():
        if fx.ladder.advance() is None:
            break
        fx.climber._publish_current()
    stop, t = _drain_clock(fx)
    try:
        fx.climber.start()
        deadline = time.monotonic() + 10.0
        while not fx.climber.finished and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fx.climber.finished, "climb never reported completion"
    finally:
        stop.set()
        t.join(timeout=5.0)
    assert fx.climber.running() is False
    # The contract the bridge's start path relies on: exit is recorded, so the
    # monitor loop's every-poll re-assert cannot spawn a dead climb again.
    assert fx.climber.finished is True
    assert fx.ladder.at_top(), "a completed climb leaves the ladder at the top"
    print("  test_finished_climber_is_never_restarted OK")


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
    print("test_quality_climb OK")


if __name__ == "__main__":
    main()
