"""Pairing UI dialog for Kodi."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("ytlounge.ui")

try:
    import xbmcgui
    KODI_AVAILABLE = True
except ImportError:
    KODI_AVAILABLE = False
    xbmcgui = None  # type: ignore


class PairingDialog:
    def __init__(self, pairing_code: str, screen_name: str = "Kodi"):
        self.pairing_code = pairing_code
        self.screen_name = screen_name
        self._dialog = None

    def show(self, timeout_ms: int = 15000) -> None:
        """Display pairing code to user."""
        message = (
            f"TV Code: {self.pairing_code}\n\n"
            f"1. Open YouTube on your phone or tablet\n"
            f"2. Go to Settings -> Watch on TV -> Link with TV code\n"
            f"3. Enter the code above to link with '{self.screen_name}'"
        )
        if KODI_AVAILABLE:
            try:
                self._dialog = xbmcgui.Dialog()
                self._dialog.ok("YouTube TV Pairing Code", message)
            except Exception as e:
                logger.warning("Could not show Kodi dialog: %s", e)
        else:
            print("\n" + "=" * 55)
            print(f"  YOUTUBE TV PAIRING CODE: {self.pairing_code}")
            print(f"  Device Name: {self.screen_name}")
            print("  Enter this code in YouTube app -> Watch on TV")
            print("=" * 55 + "\n")

    def show_notification(self, title: str, message: str) -> None:
        if KODI_AVAILABLE:
            try:
                xbmcgui.Dialog().notification(title, message, xbmcgui.NOTIFICATION_INFO, 5000)
            except Exception:
                pass
        else:
            print(f"[{title}] {message}")
