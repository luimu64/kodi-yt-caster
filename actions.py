#!/usr/bin/env python3
"""Handler for user actions invoked from Kodi settings or RunScript."""

from __future__ import annotations

import os
import sys
import time

ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from resources.lib.persistence import SessionStore
from resources.lib.lounge.pairing import (
    generate_screen_id,
    get_lounge_token_batch,
    get_pairing_code,
)
from resources.lib.ui.pairing_dialog import PairingDialog
from resources.lib.ytdlp_downloader import download_ytdlp

try:
    import xbmc
    import xbmcaddon
    import xbmcgui
    KODI_AVAILABLE = True
except ImportError:
    KODI_AVAILABLE = False
    xbmc = None  # type: ignore
    xbmcaddon = None  # type: ignore
    xbmcgui = None  # type: ignore


def get_screen_name() -> str:
    if KODI_AVAILABLE and xbmcaddon:
        try:
            return xbmcaddon.Addon().getSetting("screen_name") or "Kodi"
        except Exception:
            return "Kodi"
    return "Kodi"


def action_show_pairing(reset: bool = False) -> None:
    """Fetch/refresh pairing code and display it on screen."""
    store = SessionStore()
    data = store.load()
    screen_name = get_screen_name()

    if reset:
        store.clear()
        data = store.load()

    screen_id = data.get("screen_id")
    lounge_token = data.get("lounge_token")

    if not screen_id:
        screen_id = generate_screen_id()
        data["screen_id"] = screen_id
        store.save(data)

    if not lounge_token:
        lounge_token, exp = get_lounge_token_batch(screen_id)
        data["lounge_token"] = lounge_token
        data["expiration"] = exp
        store.save(data)

    code = get_pairing_code(screen_id, lounge_token, screen_name)
    data["pairing_code"] = code
    # Ask the running service to re-read the store (it polls this flag).
    data["reload_requested"] = True
    store.save(data)

    dlg = PairingDialog(pairing_code=code, screen_name=screen_name)
    dlg.show()

    # In a RunScript execution context, keep the script alive while dialog is displayed
    while dlg._thread and dlg._thread.is_alive():
        time.sleep(0.5)


def action_update_ytdlp() -> None:
    """Download/update yt-dlp binary."""
    try:
        download_ytdlp(force=True, show_ui=True)
    except Exception as e:
        if KODI_AVAILABLE and xbmcgui:
            xbmcgui.Dialog().notification("YouTube Cast", f"Update error: {e}", xbmcgui.NOTIFICATION_ERROR)
        print(f"Error updating yt-dlp: {e}", file=sys.stderr)


def action_fetch_ffmpeg() -> None:
    """Download the static ffmpeg build used for audio normalization."""
    from resources.lib.audio_norm import fetch_ffmpeg

    def notify(title: str, message: str, error: bool = False) -> None:
        if KODI_AVAILABLE and xbmcgui:
            icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
            xbmcgui.Dialog().notification(title, message, icon, 5000)
        print(f"{title}: {message}", file=sys.stderr)

    profile = _profile_dir()
    existing = os.path.join(profile, "bin", "ffmpeg")
    if os.path.exists(existing):
        notify("YouTube Cast", "ffmpeg is already installed")
        return
    notify("YouTube Cast", "Downloading ffmpeg (about 120 MB)…")
    result = fetch_ffmpeg(os.path.join(profile, "bin"), notify=notify)
    if result:
        notify("YouTube Cast", "Audio normalization is ready")
    elif not KODI_AVAILABLE:
        print("ffmpeg download failed", file=sys.stderr)


def _profile_dir() -> str:
    try:
        import xbmcaddon
        import xbmcvfs
        return xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
    except Exception:
        return ADDON_ROOT


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "show_pairing"

    if action in ("show_pairing", "--show-pairing", "pair"):
        action_show_pairing(reset=False)
    elif action in ("reset_pairing", "--reset-pairing", "relink"):
        action_show_pairing(reset=True)
    elif action in ("update_ytdlp", "--update-ytdlp", "update"):
        action_update_ytdlp()
    else:
        # Default action when triggered without arguments
        action_show_pairing(reset=False)


if __name__ == "__main__":
    main()
