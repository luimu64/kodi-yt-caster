#!/usr/bin/env python3
"""Plugin entry point: resolve a music-queue item for Kodi's playlist player.

Kodi invokes this per playlist item when playback reaches it. We resolve the
video ID against the running service's resolver (warm yt-dlp cache + preload
cache) over localhost HTTP and hand Kodi the playable audio URL, so queue
auto-advance is native and instant.

URL: plugin://plugin.service.ytlounge-cast/?play=<video_id>
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)


def _log(msg: str) -> None:
    try:
        import xbmc
        xbmc.log(f"[plugin.service.ytlounge-cast] {msg}", 1)
    except ImportError:
        print(msg, file=sys.stderr)


def _service_port() -> int | None:
    """Port of the service's manifest server, from the addon profile port file."""
    candidates = []
    try:
        import xbmcaddon
        import xbmcvfs
        profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
        candidates.append(os.path.join(profile, "manifest_server.port"))
    except Exception:
        pass
    candidates.append(os.path.join(ADDON_ROOT, "manifest_server.port"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            continue
    return None


def resolve_from_service(video_id: str, timeout: float = 60.0):
    port = _service_port()
    if port is None:
        _log("service port file not found; is the service running?")
        return None
    url = f"http://127.0.0.1:{port}/resolve/{video_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _log(f"resolve_from_service failed: {e}")
        return None


def main() -> None:
    import xbmcgui
    import xbmcplugin

    handle = int(sys.argv[1])
    qs = sys.argv[2] if len(sys.argv) > 2 else ""
    params = dict(urllib.parse.parse_qsl(qs.lstrip("?")))
    video_id = params.get("play")
    if not video_id:
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    info = resolve_from_service(video_id)
    li = xbmcgui.ListItem(info and info.get("title") or video_id)
    if info:
        li.setInfo("music", {
            "title": info.get("title") or "",
            "duration": int(info.get("duration") or 0),
            "artist": info.get("artist") or "",
            "album": info.get("album") or "",
        })
        if info.get("thumbnail"):
            li.setArt({"thumb": info["thumbnail"], "icon": info["thumbnail"]})
        li.setPath(info.get("audio_url") or info.get("playable_url"))
        xbmcplugin.setResolvedUrl(handle, True, li)
        _log(f"queue item {video_id} resolved")
    else:
        li.setPath("")
        xbmcplugin.setResolvedUrl(handle, False, li)
        _log(f"queue item {video_id} could not be resolved")


if __name__ == "__main__":
    main()
