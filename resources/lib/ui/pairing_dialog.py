"""Pairing UI dialog for Kodi."""

from __future__ import annotations

import logging
import threading
import time
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
        self._dp = None
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def show(self) -> None:
        """Display pairing code non-blockingly."""
        self._closed.clear()
        self._thread = threading.Thread(name="PairingDialogThread", target=self._dialog_worker, daemon=True)
        self._thread.start()

    def dismiss(self) -> None:
        """Programmatically close the dialog once connected."""
        self._closed.set()
        if self._dp:
            try:
                self._dp.close()
            except Exception:
                pass

    def _dialog_worker(self) -> None:
        heading = "YouTube TV Pairing Code"
        message = (
            f"Pairing Code:  {self.pairing_code}\n\n"
            f"1. Open YouTube on your phone -> Settings -> Watch on TV\n"
            f"2. Tap 'Link with TV code' and enter the digits above\n"
            f"Waiting for connection to '{self.screen_name}'..."
        )

        if KODI_AVAILABLE and xbmcgui:
            try:
                self._dp = xbmcgui.DialogProgress()
                self._dp.create(heading, message)
                while not self._closed.is_set():
                    if self._dp.iscanceled():
                        break
                    time.sleep(0.5)
            except Exception as e:
                logger.warning("Error displaying pairing dialog: %s", e)
            finally:
                if self._dp:
                    try:
                        self._dp.close()
                    except Exception:
                        pass
        else:
            print("\n" + "=" * 55)
            print(f"  YOUTUBE TV PAIRING CODE: {self.pairing_code}")
            print(f"  Device Name: {self.screen_name}")
            print("  Enter this code in YouTube app -> Watch on TV")
            print("=" * 55 + "\n")

    def show_notification(self, title: str, message: str) -> None:
        if KODI_AVAILABLE and xbmcgui:
            try:
                xbmcgui.Dialog().notification(title, message, xbmcgui.NOTIFICATION_INFO, 5000)
            except Exception:
                pass
        else:
            print(f"[{title}] {message}")
