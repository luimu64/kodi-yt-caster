#!/usr/bin/env python3
"""Entry point for updating yt-dlp binary from Kodi settings."""

from __future__ import annotations

import os
import sys

# Ensure addon root and libraries are in sys.path
ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from resources.lib.ytdlp_downloader import download_ytdlp


def main() -> None:
    try:
        download_ytdlp(force=True, show_ui=True)
    except Exception as e:
        print(f"Error updating yt-dlp: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
