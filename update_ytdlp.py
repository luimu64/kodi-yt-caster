#!/usr/bin/env python3
"""Entry point for updating yt-dlp binary from Kodi settings."""

from __future__ import annotations

import os
import sys

ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from actions import action_update_ytdlp


def main() -> None:
    action_update_ytdlp()


if __name__ == "__main__":
    main()
