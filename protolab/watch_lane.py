#!/usr/bin/env python3
"""Watch kodi.log for a lane transition and print the window/player events.

Usage: watch_lane.py [seconds] [start_line]
"""
import sys
import time

LOG = "/storage/.kodi/temp/kodi.log"
PATS = ("Window Init (VideoFullScreen", "Window Deinit (VideoFullScreen",
        "Window Init (MusicVisualisation", "Window Deinit (MusicVisualisation",
        "Activating window ID: 12005", "Activating window ID: 12006",
        "PAPlayer::Process - Playback started", "CVideoPlayer::CloseFile",
        "OnPlayBackStarted: CApplication", "OnPlayBackStopped: CApplication",
        "OnPlayBackEnded: CApplication", "PreviousWindow", "FreeVisualisation",
        "ytlounge.player: ", "ytlounge.session: REPORT nowPlaying vid=",
        "Saving file state for")


def read_all():
    with open(LOG, "r", errors="replace") as f:
        return f.readlines()


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    pos = int(sys.argv[2]) if len(sys.argv) > 2 else len(read_all())
    print("watching from line %d for %ds" % (pos, secs))
    end = time.time() + secs
    seen = 0
    while time.time() < end:
        time.sleep(5)
        lines = read_all()
        for ln in lines[pos:]:
            if any(p in ln for p in PATS):
                print(ln.rstrip()[:150])
                seen += 1
        pos = len(lines)
    print("done, %d matching lines" % seen)


if __name__ == "__main__":
    main()
