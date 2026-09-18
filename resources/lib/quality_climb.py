"""Background quality climber: walks the ladder up while playback runs.

Split out of the player bridge so the climbing rules are testable without Kodi
and so the timing constraints are stated in one place.

Timing constraints, all learned on the device:

* **Never climb during a playback handoff.** The climb republishes the master
  manifest; Kodi STATs the outgoing item against the same localhost server
  before it processes the stop, and heavy work in this process delays the
  handoff (see ``references/localhost-server-responsiveness.md``). The climber
  therefore waits for ``handoff_free()`` to report true before each step.
* **Climb only from the rung that is actually playing.** Republishing with more
  renditions while the first is still being opened makes inputstream.adaptive
  re-select before the picture is up, which is the stutter the ladder exists to
  avoid. A step waits for playback to be confirmed and for the clock to be
  moving.
* **One step at a time, with a settle gap.** The rungs are cheap to publish but
  each republish costs a manifest re-read, so the ladder is walked in steps
  separated by ``settle`` seconds rather than all at once.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("ytlounge.quality")

# Seconds of confirmed, advancing playback before the first upgrade. Short
# enough that a 30s music video still reaches full quality, long enough that the
# initial buffering burst is over.
FIRST_STEP_DELAY = 2.0
# Gap between subsequent rungs.
STEP_GAP = 3.0
# Never spend longer than this climbing one item.
MAX_CLIMB_SECONDS = 90.0
# How often to re-check whether the handoff has settled.
POLL = 0.25
# Hard wall-clock ceiling for any single wait, independent of the injected
# clock: with a virtual clock the deadline can stand still forever, so a stuck
# predicate would spin indefinitely without this guard.
MAX_WAIT_SECONDS = 20.0


class QualityClimber:
    """Drives one item's ladder upward in a daemon thread.

    ``publish(body)`` is the sink that actually republishes the master manifest;
    injecting it keeps this class free of manifest-server knowledge and lets the
    tests assert the exact sequence of published bodies.
    """

    def __init__(
        self,
        video_id: str,
        ladder: Any,
        rewriter: Any,
        publish: Callable[[str], None],
        handoff_free: Callable[[], bool],
        is_current: Callable[[], bool],
        playing_ok: Callable[[], bool],
        start_index: Optional[int] = None,
        first_step_delay: float = FIRST_STEP_DELAY,
        step_gap: float = STEP_GAP,
        clock: Callable[[], float] = time.monotonic,
        stop_event: Optional[threading.Event] = None,
    ):
        self.video_id = video_id
        self.ladder = ladder
        self.rewriter = rewriter
        self.publish = publish
        self.handoff_free = handoff_free
        self.is_current = is_current
        self.playing_ok = playing_ok
        self.first_step_delay = first_step_delay
        self.step_gap = step_gap
        self.clock = clock
        self.stop_event = stop_event or threading.Event()
        self._start_index = start_index
        self._thread: Optional[threading.Thread] = None
        # Set once the climb has exited for ANY reason (reached the top, was
        # superseded, playback stalled, budget exhausted). Callers must not
        # restart a finished climber: the monitor loop re-asserts the climb on
        # every poll, so without this a completed item would spawn a dead climb
        # thread every couple of seconds for the rest of the video.
        self.finished = False
        # Observable for tests and logs.
        self.published: list = []

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"QualityClimb-{self.video_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ work
    def initial_body(self) -> Optional[str]:
        """The master the FIRST play should use: the lowest rung only.

        Falls back to None when the ladder cannot be narrowed, in which case the
        caller plays the full master (never a broken manifest).
        """
        if self.ladder is None or self.rewriter is None:
            return None
        if self.ladder.rung_count() < 2:
            return None
        total = self.rewriter.stream_count
        if total < 2:
            return None
        index = self.ladder.start_index() if self._start_index is None else self._start_index
        self.ladder.set_index(index)
        width = self._width_for(index, total)
        body = self.rewriter.body_with(width)
        self.publish(body)
        self.published.append(width)
        logger.info("Auto quality %s: starting at %sp with %d/%d renditions exposed",
                    self.video_id, (self.ladder.current() or {}).get("height"), width, total)
        return body

    @staticmethod
    def _width_for(index: int, total: int) -> int:
        """How many renditions (counted from the TOP of the master) to expose.

        The generator emits best-first; rung ``index`` counts from the bottom of
        the quality range. So rung 0 (the cheapest) exposes exactly ONE stream —
        the last line of the master, which is the lowest rendition — and the top
        rung exposes all ``total`` of them. Exposing a superset on each step is
        what lets the ladder climb without invalidating a playlist the player
        has already fetched.
        """
        return max(1, min(total, index + 1))

    def _wait_for(self, pred: Callable[[], bool], deadline: float) -> bool:
        """Poll ``pred`` until it holds or ``deadline`` passes.

        The deadline is compared against ``self.clock()``, but the sleep is a
        REAL sleep: a caller that injects a virtual clock (tests) still makes
        progress, and a caller using the monotonic clock behaves normally. The
        wall-clock guard is what stops a stuck predicate spinning forever.
        """
        wall_deadline = time.monotonic() + MAX_WAIT_SECONDS
        while self.clock() < deadline and time.monotonic() < wall_deadline:
            if self.stop_event.is_set():
                return False
            if not self.is_current():
                return False
            try:
                if pred():
                    return True
            except Exception:
                return False
            time.sleep(POLL)
        return False

    def _sleep(self, seconds: float, deadline: float) -> bool:
        """Sleep, honouring the stop event and the climb budget.

        Returns False when the sleep was cut short because the item is no longer
        current, the climber was stopped, or the budget ran out.
        """
        if seconds <= 0:
            return not self.stop_event.is_set() and self.is_current()
        end = min(deadline, self.clock() + seconds)
        wall_end = time.monotonic() + min(seconds, MAX_WAIT_SECONDS)
        while self.clock() < end and time.monotonic() < wall_end:
            if self.stop_event.is_set() or not self.is_current():
                return False
            time.sleep(min(POLL, max(0.0, end - self.clock())))
        return not self.stop_event.is_set()

    def _run(self) -> None:
        try:
            self._climb()
        except Exception:
            logger.debug("quality climb for %s failed", self.video_id, exc_info=True)
        finally:
            self.finished = True

    def _climb(self) -> None:
        if self.ladder is None or self.rewriter is None:
            return
        if self.ladder.rung_count() < 2:
            logger.info("Auto quality %s: single rendition, nothing to climb", self.video_id)
            return

        deadline = self.clock() + MAX_CLIMB_SECONDS
        # Phase 1: playback confirmed and the handoff settled. Until the first
        # rung is actually on screen, publishing more renditions only makes
        # inputstream.adaptive re-select before the picture is up.
        if not self._wait_for(
            lambda: self.handoff_free() and self.playing_ok(),
            min(deadline, self.clock() + self.first_step_delay + 20.0),
        ):
            logger.info("Auto quality %s: playback never settled; leaving %s",
                        self.video_id, self.ladder.heights())
            return
        if not self._sleep(self.first_step_delay, deadline):
            return

        # Phase 2: walk up one rung per step.
        while not self.stop_event.is_set() and self.clock() < deadline:
            if not self.is_current():
                logger.info("Auto quality %s: superseded, stopping climb", self.video_id)
                return
            if self.ladder.at_top():
                logger.info("Auto quality %s: climbed to top %sp", self.video_id,
                            self.ladder.heights()[-1])
                return
            if not self._wait_for(lambda: self.handoff_free() and self.playing_ok(),
                                  min(deadline, self.clock() + 15.0)):
                logger.info("Auto quality %s: playback stalled, stopping climb at %sp",
                            self.video_id, (self.ladder.current() or {}).get("height"))
                return
            if self.ladder.advance() is None:
                return
            self._publish_current()
            if not self._sleep(self.step_gap, deadline):
                return
        logger.info("Auto quality %s: climb budget exhausted at %s rungs",
                    self.video_id, self.ladder.summary())

    def _publish_current(self) -> None:
        """Republish the master exposing every rung up to the ladder's cursor."""
        total = self.rewriter.stream_count
        index = self._index_of_current()
        width = self._width_for(index, total)
        body = self.rewriter.body_with(width)
        self.publish(body)
        self.published.append(width)
        logger.info("Auto quality %s: upgraded to %sp (%d/%d renditions exposed)",
                    self.video_id, (self.ladder.current() or {}).get("height"),
                    width, total)

    def _index_of_current(self) -> int:
        """Index of the ladder's current rung within its full rung list."""
        cur = self.ladder.current()
        if cur is None:
            return 0
        try:
            return self.ladder.rungs.index(cur)
        except ValueError:
            return 0


def climb_supported(info: Dict[str, Any]) -> bool:
    """True when this resolve carries a usable ladder to climb."""
    ladder = info.get("quality_ladder")
    rewriter = info.get("master_rewriter")
    if ladder is None or rewriter is None:
        return False
    try:
        return ladder.rung_count() >= 2 and rewriter.stream_count >= 2
    except Exception:
        return False
