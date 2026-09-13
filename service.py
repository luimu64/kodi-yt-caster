"""YouTube Lounge Cast Receiver Service for Kodi."""

from __future__ import annotations

import logging
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("plugin.service.ytlounge-cast")

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

from resources.lib.persistence import SessionStore
from resources.lib.lounge.pairing import (
    generate_screen_id,
    get_lounge_token_batch,
    get_pairing_code,
)
from resources.lib.lounge.session import LoungeSession
from resources.lib.lounge.listener import CommandDispatcher, LoungeListener
from resources.lib.player_bridge import KodiPlayerBridge
from resources.lib.resolver import VideoResolver
from resources.lib.ytdlp_bridge import YtDlpBridge, find_ytdlp_binary
from resources.lib.ytdlp_downloader import ensure_ytdlp, download_ytdlp
from resources.lib.ui.pairing_dialog import PairingDialog


def get_setting(name: str, default: str = "") -> str:
    if KODI_AVAILABLE and xbmcaddon:
        try:
            return xbmcaddon.Addon().getSetting(name) or default
        except Exception:
            return default
    return default


def get_setting_bool(name: str, default: bool = True) -> bool:
    val = get_setting(name)
    if not val:
        return default
    return val.lower() in ("true", "1", "yes")


def log_kodi(msg: str, level: int = 1) -> None:
    if KODI_AVAILABLE and xbmc:
        xbmc.log(f"[plugin.service.ytlounge-cast] {msg}", level)
    else:
        logger.info(msg)


def run_service() -> None:
    # Check if triggered as script action (e.g. RunScript for manual update)
    if len(sys.argv) > 1 and sys.argv[1] in ("update_ytdlp", "--update-ytdlp"):
        log_kodi("Running manual yt-dlp binary update", 1)
        try:
            download_ytdlp(force=True, show_ui=True)
        except Exception as e:
            log_kodi(f"Manual yt-dlp update failed: {e}", 2)
        return

    log_kodi("Starting YouTube Lounge Cast Receiver service", 1)

    # Ensure yt-dlp binary is installed on first run / service start
    ensure_ytdlp()

    store = SessionStore()
    session_data = store.load()

    enabled = get_setting_bool("enable", True)
    if not enabled:
        log_kodi("Service disabled in addon settings", 1)
        if KODI_AVAILABLE and xbmc:
            monitor = xbmc.Monitor()
            while not monitor.abortRequested():
                monitor.waitForAbort(5)
        return

    screen_name = get_setting("screen_name", "Kodi")
    show_pairing_on_boot = get_setting_bool("show_pairing_on_boot", True)
    custom_ytdlp = get_setting("ytdlp_path", "")
    custom_cookies = get_setting("custom_cookies_file", "")

    # Ensure valid screen_id and lounge_token
    screen_id = session_data.get("screen_id")
    lounge_token = session_data.get("lounge_token")
    device_id = session_data.get("device_id")

    is_first_run = not (screen_id and lounge_token)

    if not screen_id:
        try:
            log_kodi("Requesting new screen_id from YouTube...", 1)
            screen_id = generate_screen_id()
            session_data["screen_id"] = screen_id
            store.save(session_data)
        except Exception as e:
            log_kodi(f"Failed to generate screen_id: {e}", 2)
            return

    if not lounge_token:
        try:
            log_kodi(f"Fetching lounge token batch for screen {screen_id}...", 1)
            lounge_token, expiration = get_lounge_token_batch(screen_id)
            session_data["lounge_token"] = lounge_token
            session_data["expiration"] = expiration
            store.save(session_data)
        except Exception as e:
            log_kodi(f"Failed to fetch lounge token: {e}", 2)
            return

    # Create Lounge Session
    session = LoungeSession(
        screen_id=screen_id,
        lounge_token=lounge_token,
        device_id=device_id,
        screen_name=screen_name,
    )

    # Initialize resolver and player
    ytdlp_bin = find_ytdlp_binary(custom_ytdlp)
    bridge = YtDlpBridge(binary_path=ytdlp_bin, cookies_path=custom_cookies or None)
    resolver = VideoResolver(bridge=bridge)
    player = KodiPlayerBridge(session=session, resolver=resolver)
    player.start_monitor()

    # Show pairing code if needed
    if is_first_run or show_pairing_on_boot:
        try:
            code = get_pairing_code(screen_id, lounge_token, screen_name)
            session_data["pairing_code"] = code
            store.save(session_data)
            PairingDialog(pairing_code=code, screen_name=screen_name).show()
        except Exception as e:
            log_kodi(f"Could not retrieve pairing code: {e}", 2)

    # Dispatcher setup
    dispatcher = CommandDispatcher()

    def on_connected(data: dict) -> None:
        client_name = data.get("name", "Phone")
        log_kodi(f"Device connected: {client_name}", 1)
        PairingDialog("", screen_name).show_notification("YouTube Cast", f"Connected to {client_name}")

    def on_disconnected(data: dict) -> None:
        client_name = data.get("name", "Phone")
        log_kodi(f"Device disconnected: {client_name}", 1)
        PairingDialog("", screen_name).show_notification("YouTube Cast", f"Disconnected from {client_name}")

    dispatcher.on_remote_connected = on_connected
    dispatcher.on_remote_disconnected = on_disconnected
    dispatcher.on_set_playlist = player.set_playlist
    dispatcher.on_update_playlist = player.update_playlist
    dispatcher.on_play = player.resume
    dispatcher.on_pause = player.pause
    dispatcher.on_stop = player.stop
    dispatcher.on_seek = player.seek_to
    dispatcher.on_set_volume = player.set_volume
    dispatcher.on_get_volume = player.get_volume
    dispatcher.on_get_now_playing = lambda: session.report_now_playing(
        player.current_video_id or "", player.get_time(), player.current_duration, player.state
    )

    def on_token_expired() -> None:
        log_kodi("Token expired or revoked, refreshing registration...", 1)
        store.clear()
        try:
            new_sid = generate_screen_id()
            new_tok, exp = get_lounge_token_batch(new_sid)
            session.screen_id = new_sid
            session.lounge_token = new_tok
            session.sid = None
            store.save({"device_id": device_id, "screen_id": new_sid, "lounge_token": new_tok, "expiration": exp})
            new_code = get_pairing_code(new_sid, new_tok, screen_name)
            PairingDialog(new_code, screen_name).show()
        except Exception as ex:
            log_kodi(f"Failed to refresh registration: {ex}", 2)

    listener = LoungeListener(session=session, dispatcher=dispatcher, on_token_expired=on_token_expired)
    listener.start()

    log_kodi(f"YouTube Lounge receiver active for screen '{screen_name}'", 1)

    # Main wait loop
    try:
        if KODI_AVAILABLE and xbmc:
            monitor = xbmc.Monitor()
            while not monitor.abortRequested():
                monitor.waitForAbort(1)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        log_kodi("Shutting down service...", 1)
    finally:
        log_kodi("Stopping listener and player threads...", 1)
        listener.stop()
        player.stop_monitor()
        log_kodi("Service shutdown complete.", 1)


if __name__ == "__main__":
    run_service()
