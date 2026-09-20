#!/usr/bin/env python3
"""Music lane: the visualisation window must be re-asserted until it sticks,
and a failure must name the observed reason, not guess one."""
import logging
import time

from harness import Scenario
from lounge_server.phone import Phone
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc


def _connect_music_phone(s):
    phone = Phone(s.lounge)
    sid = s.wait_for_session("m", timeout=5.0)
    phone.target(sid)
    phone.connect()
    time.sleep(0.4)
    return phone


def test_window_activated_after_modal_dialog_clears():
    with Scenario(settings={"music_visualizer": "always"}) as s:
        xbmc.set_modal_dialog(True)
        try:
            phone = _connect_music_phone(s)
            phone.set_playlist("v_art", ["v_art"], current_time=0)
            s.wait_until(lambda: "v_art" in (s.playing_file() or ""), what="art track plays")
            time.sleep(1.0)
            assert not xbmc.getCondVisibility("Window.IsActive(visualisation)")
            xbmc.set_modal_dialog(False)
            s.wait_until(lambda: xbmc.getCondVisibility("Window.IsActive(visualisation)"),
                         timeout=15.0, what="visualisation window comes up after the dialog clears")
        finally:
            xbmc.set_modal_dialog(False)


def test_give_up_names_a_reason():
    """A window that never activates must log WHY, and stop at the deadline."""
    import resources.lib.player_bridge as pb
    msgs = []

    class H(logging.Handler):
        def emit(self, record):
            msgs.append(record.getMessage())

    h = H()
    pb.logger.addHandler(h)
    try:
        with Scenario(settings={"music_visualizer": "always"}) as s:
            xbmc.set_modal_dialog(True)
            try:
                phone = _connect_music_phone(s)
                phone.set_playlist("v_art", ["v_art"], current_time=0)
                s.wait_until(lambda: "v_art" in (s.playing_file() or ""), what="art plays")
                time.sleep(3.0)
            finally:
                xbmc.set_modal_dialog(False)
        assert any("did not take" in m for m in msgs), "refusal was never logged"
        assert not any("modal dialog?" in m for m in msgs), \
            "the guess must be replaced by an observed reason"
    finally:
        pb.logger.removeHandler(h)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("test_visualisation_window OK")


if __name__ == "__main__":
    main()
