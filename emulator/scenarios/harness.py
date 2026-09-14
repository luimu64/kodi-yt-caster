"""Scenario harness: boots the real addon service against the fake Kodi API
and the mock Lounge server, in-process, with deterministic offline resolves.

Monkeypatch inventory (deliberately short):
  1. resources.lib.lounge BASE_URL (client + session + listener re-imports)
     -> mock Lounge server. The plan's single sanctioned patch.
  2. YtDlpBridge.resolve -> deterministic offline resolver (no yt-dlp, no CDN).
  3. PlayerBridge._fetch_title_sync -> instant fake titles (no oEmbed network).
  4. ytdlp_downloader.download_ytdlp -> no-op (fake binary pre-placed instead).
"""
import importlib
import os
import shutil
import sys
import threading
import time
import traceback

import _bootstrap  # noqa: F401  (sys.path: repo root + emulator/)

import kodi_stub
kodi_stub.install()

import xbmc  # noqa: E402  (the stub)
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402

from lounge_server.server import MockLoungeServer
from lounge_server.phone import Phone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CALLS = {"resolve": []}


def _fake_bridge_resolve(self, video_id):
    """Deterministic offline resolve: dash type (no preload network), audio+video URLs."""
    _CALLS["resolve"].append(video_id)
    return {
        "id": video_id,
        "title": f"Title of {video_id}",
        "duration": 180,
        "thumbnail": f"http://127.0.0.1:9/thumb-{video_id}.jpg",
        "playable_url": f"http://media.example/{video_id}/video.mpd",
        "stream_type": "dash",
        "audio_url": f"http://media.example/{video_id}/audio.m4a",
        "is_static_art": True,
        "max_video_tbr": 0.0,
        "artist": "Artist",
        "album": "Album",
    }


def _reset_lounge_url(base):
    import resources.lib.lounge.client as client
    import resources.lib.lounge.session as session
    import resources.lib.lounge.listener as listener
    client.BASE_URL = base
    session.BASE_URL = base
    listener.BASE_URL = base
    # client.request binds BASE_URL as a default arg at def time
    client.request.__defaults__ = (None, None, 30.0, base)
    client.BASE_URL = base


class Scenario:
    """with Scenario() as s: s.phone.connect(); ... assertions ..."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def __enter__(self):
        kodi_stub.reset()
        xbmc.MEDIA.update({
            "http://media.example/": {"duration": 8.0, "audio": True},
        })
        base = {
            "enable": "true",
            "screen_name": "KodiEmu",
            "show_pairing_on_boot": "false",
            "enable_discovery": "false",
            "music_visualizer": "never",
            "stream_selection": "manual-osd",
            "max_resolution": "auto",
            "dial_port": "0",
            "ytdlp_path": "",
        }
        base.update(self.settings)
        xbmcaddon.set_settings(base)

        # fresh session store + fake yt-dlp binary so ensure_ytdlp() is a no-op
        import resources.lib.persistence as persistence
        self._store_path = os.path.join(xbmcaddon.profile_dir(), ".session_store.json")
        persistence.FALLBACK_STORE_PATH = self._store_path
        os.makedirs(os.path.join(xbmcaddon.profile_dir(), "bin"), exist_ok=True)
        with open(os.path.join(xbmcaddon.profile_dir(), "bin", "yt-dlp"), "w") as f:
            f.write("#!fake\n")
        os.chmod(os.path.join(xbmcaddon.profile_dir(), "bin", "yt-dlp"), 0o755)

        # the two deterministic patches (see module docstring)
        import resources.lib.ytdlp_bridge as ytdlp_bridge
        ytdlp_bridge.YtDlpBridge.resolve = _fake_bridge_resolve
        import resources.lib.player_bridge as player_bridge
        player_bridge.KodiPlayerBridge._fetch_title_sync = staticmethod(
            lambda vid: f"Title of {vid}")
        import resources.lib.ytdlp_downloader as downloader
        self._orig_download = downloader.download_ytdlp
        downloader.download_ytdlp = lambda *a, **k: None

        # mock Lounge + BASE_URL redirect (import-time resolved constants)
        self.lounge = MockLoungeServer().start()
        _reset_lounge_url(self.lounge.url)

        import service

        # capture per-session token-expired handlers so scenarios can drive
        # the refresh path directly (the wire path needs ~62s of backoff)
        import resources.lib.lounge.listener as listener_mod
        service._last_token_handlers = getattr(service, "_last_token_handlers", {})
        if not getattr(listener_mod, "_emu_wrapped", False):
            listener_mod._emu_wrapped = True
            _orig_init = listener_mod.LoungeListener.__init__

            def _init(self, session=None, dispatcher=None, on_token_expired=None, **kw):
                _orig_init(self, session=session, dispatcher=dispatcher,
                           on_token_expired=on_token_expired, **kw)
                service._last_token_handlers[session.theme] = on_token_expired
            listener_mod.LoungeListener.__init__ = _init

        self.service = service
        _CALLS["resolve"].clear()
        self.service_thread = threading.Thread(target=service.run_service, daemon=True, name="ServiceMain")
        self.service_thread.start()

        self.wait_ready()
        self.phone = Phone(self.lounge)
        return self

    def wait_ready(self, timeout=15.0):
        """Port file written == listeners bound, manifest server up."""
        port_file = os.path.join(xbmcaddon.profile_dir(), "manifest_server.port")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(port_file):
                # give LoungeListener threads a moment to bind sessions
                time.sleep(0.3)
                return
            time.sleep(0.05)
        raise TimeoutError("service did not become ready (no manifest_server.port)")

    def resolve_count(self, video_id=None):
        if video_id is None:
            return len(_CALLS["resolve"])
        return _CALLS["resolve"].count(video_id)

    # --- eventual-consistency helpers ---------------------------------------
    def wait_until(self, pred, timeout=10.0, what="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = pred()
                if last:
                    return last
            except Exception:
                last = traceback.format_exc()
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting for {what}; last value: {last}")

    def playing_file(self):
        try:
            return xbmc.Player().getPlayingFile()
        except Exception:
            return None

    def end_media(self, timeout=40.0):
        """Advance the simulated media to its end and fire the Kodi Ended event."""
        def _go():
            self.wait_until(lambda: xbmc.media_finished(), timeout=timeout,
                            what="media to reach its end")
            xbmc.end_of_media()
        threading.Thread(target=_go, daemon=True).start()

    def notifications(self, *fragments):
        return [n for n in xbmcgui.NOTIFICATIONS if all(f in n[1] for f in fragments)]

    def __exit__(self, exc_type, exc, tb):
        mon = xbmc.Monitor._instance
        if mon:
            mon.request_abort()
        self.service_thread.join(timeout=10)
        if self.service_thread.is_alive():
            raise RuntimeError("service thread did not stop within 10s")
        self.lounge.stop()
        # restore patched symbols
        import resources.lib.ytdlp_downloader as downloader
        downloader.download_ytdlp = self._orig_download
        import resources.lib.persistence as persistence
        # FALLBACK_STORE_PATH intentionally left; SessionStore instances are short-lived
        return False
