"""Simulated playback clock: position advances with wall time, adjustable rate."""
import time


class SimulatedPlayback:
    def __init__(self, duration: float, rate: float = 1.0):
        self.duration = float(duration)
        self.rate = rate
        self._pos = 0.0
        self._anchor = None  # time.monotonic() anchor while playing
        self.state = "stopped"  # "playing" | "paused" | "stopped"

    def play(self, start_at: float = 0.0) -> None:
        self._pos = max(0.0, min(float(start_at), self.duration))
        self._anchor = time.monotonic()
        self.state = "playing"

    def pause(self) -> None:
        self._freeze()
        if self.state == "playing":
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self._anchor = time.monotonic()
            self.state = "playing"

    def stop(self) -> None:
        self._freeze()
        self.state = "stopped"

    def seek(self, t: float) -> float:
        t = max(0.0, min(float(t), self.duration))
        playing = self.state == "playing"
        self._pos = t
        if playing:
            self._anchor = time.monotonic()
        return t

    def _freeze(self) -> None:
        if self.state == "playing":
            self._pos = self.get_time()
            self._anchor = None

    def get_time(self) -> float:
        if self.state == "playing" and self._anchor is not None:
            return min(self._pos + (time.monotonic() - self._anchor) * self.rate,
                       self.duration)
        return self._pos

    @property
    def finished(self) -> bool:
        return self.state == "playing" and self.get_time() >= self.duration


if __name__ == "__main__":
    # self-check
    c = SimulatedPlayback(10.0, rate=10.0)
    c.play()
    time.sleep(0.3)
    t = c.get_time()
    assert 2.0 <= t <= 4.0, t
    c.pause()
    p1 = c.get_time(); time.sleep(0.15)
    assert c.get_time() == p1, "paused time must not advance"
    assert c.seek(500) == 10.0
    assert c.seek(-3) == 0.0
    c2 = SimulatedPlayback(0.5)
    c2.play(); time.sleep(1.0)
    assert c2.finished
    c2.stop()
    assert c2.state == "stopped" and c2.get_time() == 0.0
    print("clock self-check OK")
