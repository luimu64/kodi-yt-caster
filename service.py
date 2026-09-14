"""YouTube Lounge Cast Receiver Service for Kodi."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("plugin.service.ytlounge-cast")

ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

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

from typing import Optional

from resources.lib.persistence import SessionStore
from resources.lib.lounge.pairing import (
    generate_screen_id,
    get_lounge_token_batch,
    get_pairing_code,
    register_pairing_code,
)
from resources.lib.discovery.ssdp import SSDPResponder
from resources.lib.discovery.dial_server import DIALService
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


def write_port_file() -> None:
    """Persist the manifest server port so plugin invocations can reach us."""
    try:
        from resources.lib import manifest_server
        port = manifest_server.server_port()
    except Exception:
        return
    if port is None:
        return
    for base in _profile_dirs():
        try:
            with open(os.path.join(base, "manifest_server.port"), "w", encoding="utf-8") as f:
                f.write(str(port))
        except Exception:
            pass


def _profile_dirs() -> list:
    dirs = []
    try:
        import xbmcaddon
        import xbmcvfs
        dirs.append(xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile")))
    except Exception:
        dirs.append(ADDON_ROOT)
    return dirs


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

    store = SessionStore()

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
    stream_selection = get_setting("stream_selection", "manual-osd")
    max_resolution = get_setting("max_resolution", "auto")
    music_visualizer = get_setting("music_visualizer", "auto")
    enable_discovery = get_setting_bool("enable_discovery", True)
    try:
        dial_port = int(get_setting("dial_port", "8008") or 8008)
    except (TypeError, ValueError):
        dial_port = 8008
    if not (1 <= dial_port <= 65535):
        log_kodi(f"Invalid dial_port {dial_port}, falling back to 8008", 2)
        dial_port = 8008

    # ------------------------------------------------------------------
    # Everything below touches the network (yt-dlp download on first run,
    # screen-id/token registration, pairing-code fetch). Doing it on the
    # service main thread stalled Kodi's boot for the duration — with a
    # slow network or a blocked YouTube that is minutes of a dead box.
    # Bootstrap in a daemon thread; the monitor loop below starts
    # immediately and shuts everything down on abort/reload.
    # ------------------------------------------------------------------
    ready = threading.Event()        # set when bootstrap finished (ok or failed)
    stopping = threading.Event()     # set by main loop -> bootstrap aborts early
    components: dict = {}
    state: dict = {"reload_requested": False, "pairing_dialog": None}

    def _shutdown() -> None:
        if state.get("pairing_dialog"):
            try:
                state["pairing_dialog"].dismiss()
            except Exception:
                pass
        player = components.get("player")
        dial = components.get("dial")
        ssdp = components.get("ssdp")
        listener_cl = components.get("listener_cl")
        listener_m = components.get("listener_m")
        try:
            if dial:
                dial.stop()
            if ssdp:
                ssdp.stop()
            if listener_cl:
                listener_cl.stop()
            if listener_m:
                listener_m.stop()
            if player:
                player.stop_monitor()
        except Exception:
            logger.debug("shutdown error", exc_info=True)

    def _bootstrap() -> None:  # runs in background
        try:
            if stopping.is_set():
                return
            # Ensure yt-dlp binary is installed on first run / service start
            ensure_ytdlp()

            session_data = store.load()
            # Ensure valid screen_id and lounge_token for YouTube video
            screen_id = session_data.get("screen_id")
            lounge_token = session_data.get("lounge_token")
            device_id = session_data.get("device_id")

            # Ensure valid screen_id and lounge_token for YouTube Music
            screen_id_m = session_data.get("screen_id_m")
            lounge_token_m = session_data.get("lounge_token_m")

            if not screen_id:
                log_kodi("Requesting new screen_id from YouTube...", 1)
                screen_id = generate_screen_id()
                session_data["screen_id"] = screen_id
                store.save(session_data)

            if not lounge_token:
                log_kodi(f"Fetching lounge token batch for screen {screen_id}...", 1)
                lounge_token, expiration = get_lounge_token_batch(screen_id)
                session_data["lounge_token"] = lounge_token
                session_data["expiration"] = expiration
                store.save(session_data)

            if not screen_id_m:
                log_kodi("Requesting screen_id for YouTube Music...", 1)
                screen_id_m = generate_screen_id()
                session_data["screen_id_m"] = screen_id_m
                store.save(session_data)

            if screen_id_m and not lounge_token_m:
                log_kodi(f"Fetching lounge token batch for YouTube Music screen {screen_id_m}...", 1)
                lounge_token_m, exp_m = get_lounge_token_batch(screen_id_m)
                session_data["lounge_token_m"] = lounge_token_m
                session_data["expiration_m"] = exp_m
                store.save(session_data)

            if stopping.is_set():
                return

            # Create Lounge Sessions (cl for standard YouTube, m for YouTube Music)
            session_cl = LoungeSession(
                screen_id=screen_id,
                lounge_token=lounge_token,
                device_id=device_id,
                screen_name=screen_name,
                theme="cl",
            )
            sessions = [session_cl]
            session_m = None
            if screen_id_m and lounge_token_m:
                session_m = LoungeSession(
                    screen_id=screen_id_m,
                    lounge_token=lounge_token_m,
                    device_id=device_id,
                    screen_name=screen_name,
                    theme="m",
                )
                sessions.append(session_m)

            # Initialize resolver and player
            ytdlp_bin = find_ytdlp_binary(custom_ytdlp)
            bridge = YtDlpBridge(binary_path=ytdlp_bin, cookies_path=custom_cookies or None)
            resolver = VideoResolver(bridge=bridge)
            player = KodiPlayerBridge(
                session=sessions,
                resolver=resolver,
                stream_selection_type=stream_selection,
                max_resolution=max_resolution,
                music_visualizer=music_visualizer,
            )
            player.start_monitor()

            # Let plugin invocations resolve queue items against our warm caches.
            from resources.lib import manifest_server
            manifest_server.set_resolver(resolver)
            # Boot the manifest server eagerly (it is lazy on first publish) so the
            # port file exists before any plugin invocation tries to resolve a queue
            # item.
            manifest_server.server_url_for("__boot__")
            write_port_file()

            # Show pairing code only when the user opted in via settings (manual
            # linking); discovery handles the default first-time link.
            if show_pairing_on_boot:
                try:
                    code = get_pairing_code(screen_id, lounge_token, screen_name)
                    session_data["pairing_code"] = code
                    store.save(session_data)
                    dlg = PairingDialog(pairing_code=code, screen_name=screen_name)
                    dlg.show()
                    state["pairing_dialog"] = dlg
                except Exception:
                    import traceback
                    log_kodi("Could not retrieve pairing code: " + traceback.format_exc(), 2)

            # Dispatcher setup
            dispatcher = CommandDispatcher()

            def on_connected(data: dict) -> None:
                client_name = data.get("name", "Phone")
                log_kodi(f"Device connected: {client_name}", 1)
                dlg = state.get("pairing_dialog")
                if dlg:
                    try:
                        dlg.dismiss()
                    except Exception:
                        pass
                    state["pairing_dialog"] = None
                PairingDialog("", screen_name).show_notification("YouTube Cast", f"Connected to {client_name}")
                # Announce our playback state right away: the phone will not push its initial
                # video (hasInitialPlayback) until the receiver posts a nowPlaying update.
                for s in sessions:
                    try:
                        s.report_now_playing(
                            player.current_video_id or "",
                            int(player.get_time()),
                            player.current_duration,
                            int(player.state),
                        )
                    except Exception:
                        logger.debug("nowPlaying announcement failed", exc_info=True)

            def on_disconnected(data: dict) -> None:
                client_name = data.get("name", "Phone")
                log_kodi(f"Device disconnected: {client_name}", 1)
                PairingDialog("", screen_name).show_notification("YouTube Cast", f"Disconnected from {client_name}")
                # Cast session ended: drop playback immediately, like a Chromecast does when the sender leaves.
                player.stop()

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
            dispatcher.on_get_now_playing = lambda: [
                s.report_now_playing(player.current_video_id or "", player.get_time(), player.current_duration, player.state)
                for s in sessions
            ]

            store_lock = threading.Lock()

            def make_token_expired(sess: LoungeSession):
                """Build a per-session token refresh handler.

                Each session refreshes only its OWN registration; the other session's
                pairing (and the rest of the store) is preserved via merge, and the
                listener keeps running afterwards.
                """
                def _handler() -> None:
                    log_kodi(f"Token expired for theme={sess.theme}, refreshing registration...", 1)
                    try:
                        new_sid = generate_screen_id()
                        new_tok, exp = get_lounge_token_batch(new_sid)
                        with store_lock:
                            data = store.load()
                            if sess.theme == "cl":
                                data["screen_id"] = new_sid
                                data["lounge_token"] = new_tok
                                data["expiration"] = exp
                            else:
                                data["screen_id_m"] = new_sid
                                data["lounge_token_m"] = new_tok
                                data["expiration_m"] = exp
                            store.save(data)
                        sess.screen_id = new_sid
                        sess.lounge_token = new_tok
                        sess.sid = None
                        sess.gsessionid = None
                        sess.last_code = -1
                        if sess.theme == "cl":
                            new_code = get_pairing_code(new_sid, new_tok, screen_name)
                            dlg = state.get("pairing_dialog")
                            if dlg:
                                try:
                                    dlg.dismiss()
                                except Exception:
                                    pass
                            dlg = PairingDialog(new_code, screen_name)
                            dlg.show()
                            state["pairing_dialog"] = dlg
                    except Exception as ex:
                        log_kodi(f"Failed to refresh registration: {ex}", 2)
                return _handler

            listener_cl = LoungeListener(session=session_cl, dispatcher=dispatcher, on_token_expired=make_token_expired(session_cl))
            listener_cl.start()

            listener_m = None
            if session_m:
                listener_m = LoungeListener(session=session_m, dispatcher=dispatcher, on_token_expired=make_token_expired(session_m))
                listener_m.start()

            # Start SSDP and DIAL discovery if enabled (enables YouTube Music and YouTube local casting)
            dial_service = None
            ssdp_responder = None
            if enable_discovery:
                def on_dial_pairing(code: str, theme: str = "") -> None:
                    log_kodi(f"Registering DIAL pairing code: {code} (theme={theme or 'cl'})", 1)
                    try:
                        # Register both screens: pairing codes are NOT single-use
                        # (verified: same code registers twice with 200), and the
                        # music app sends theme=cl anyway, so routing by theme is
                        # unreliable. Both registrations let either app join either
                        # lounge.
                        register_pairing_code(screen_id, code, screen_name, device_id)
                        if screen_id_m:
                            register_pairing_code(screen_id_m, code, screen_name, device_id)
                        dlg = state.get("pairing_dialog")
                        if dlg:
                            try:
                                dlg.dismiss()
                            except Exception:
                                pass
                            state["pairing_dialog"] = None
                        PairingDialog(code, screen_name).show_notification("YouTube Cast", "Linked device via Wi-Fi")
                    except Exception as err:
                        log_kodi(f"Error registering DIAL pairing code: {err}", 2)

                dial_service = DIALService(
                    port=dial_port,
                    device_uuid=device_id,
                    friendly_name=screen_name,
                    screen_id=screen_id,
                    on_pairing_code=on_dial_pairing,
                )
                dial_service.start()

                ssdp_responder = SSDPResponder(
                    dial_port=dial_port,
                    device_uuid=device_id,
                )
                ssdp_responder.start()

            components.update({
                "player": player,
                "dial": dial_service,
                "ssdp": ssdp_responder,
                "listener_cl": listener_cl,
                "listener_m": listener_m,
            })

            log_kodi(f"YouTube and YouTube Music Cast receiver active for screen '{screen_name}'", 1)
        except Exception as e:
            log_kodi(f"Receiver bootstrap failed: {e}", 2)
            logger.debug("bootstrap traceback", exc_info=True)
        finally:
            ready.set()

    boot_thread = threading.Thread(target=_bootstrap, name="ReceiverBootstrap", daemon=True)
    boot_thread.start()

    # Main wait loop; also watches for a pairing-reload request written by the
    # settings actions (which run in a separate RunScript process).
    try:
        if KODI_AVAILABLE and xbmc:
            monitor = xbmc.Monitor()
            while not monitor.abortRequested():
                if monitor.waitForAbort(2):
                    break
                if store.load().get("reload_requested") and ready.is_set() and not components.get("player"):
                    # reload requested before bootstrap completed; let bootstrap finish first
                    continue
                if store.load().get("reload_requested"):
                    state["reload_requested"] = True
                    data = store.load()
                    data["reload_requested"] = False
                    store.save(data)
                    if not ready.is_set():
                        # stop the still-running bootstrap and wait for it
                        stopping.set()
                        ready.wait(timeout=30)
                    break
        else:
            while True:
                time.sleep(2)
                if store.load().get("reload_requested"):
                    state["reload_requested"] = True
                    data = store.load()
                    data["reload_requested"] = False
                    store.save(data)
                    if not ready.is_set():
                        stopping.set()
                        ready.wait(timeout=30)
                    break
    except KeyboardInterrupt:
        log_kodi("Shutting down service...", 1)
    finally:
        stopping.set()
        ready.wait(timeout=10)
        log_kodi("Stopping listeners, discovery, and player threads...", 1)
        _shutdown()
        log_kodi("Service shutdown complete.", 1)

    if state.get("reload_requested"):
        log_kodi("Pairing reload requested via settings action; restarting receiver...", 1)
        time.sleep(1.0)  # let ports settle before rebinding
        return run_service()


if __name__ == "__main__":
    run_service()
