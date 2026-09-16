#!/usr/bin/env python3
"""Device-side harness: video lane -> audio lane transition probe.

Plays a real video item through the video player (so the fullscreen video
window 12005 becomes active), then starts an audio item through Kodi's music
playlist (same mechanism the addon uses for the visualiser lane), and reports
which window is left active + what the player did.
"""
import base64
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8080/jsonrpc"
AUTH = "Basic " + base64.b64encode(b"kodi:amogus").decode()
LOG = "/storage/.kodi/temp/kodi.log"


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(BASE, data=body, headers={
        "Content-Type": "application/json", "Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def rpc_ok(method, params=None):
    try:
        return rpc(method, params)
    except Exception as e:
        return {"error": str(e)}


def state():
    win = rpc("GUI.GetProperties", {"properties": ["currentwindow", "fullscreen"]})["result"]
    act = rpc("Player.GetActivePlayers")["result"]
    item0 = "-"
    for p in act:
        r = rpc_ok("Player.GetItem", {"playerid": p["playerid"], "properties": ["title", "file"]})
        if "result" in r:
            item0 = "%s:%s" % (p["type"], (r["result"].get("item", {}).get("file") or "")[:60])
            break
    return {"win": win["currentwindow"]["id"], "wname": win["currentwindow"]["label"],
            "fullscreen": win["fullscreen"], "players": [p["type"] for p in act],
            "playing": item0}


def logmark():
    try:
        with open(LOG, "r", errors="replace") as f:
            return len(f.readlines())
    except Exception:
        return 0


def logslice(mark, pats=("Window Init", "Window Deinit", "Activating window ID",
                         "PAPlayer::Process - Playback started", "CVideoPlayer::CloseFile",
                         "OnPlayBackStarted: CApplication", "OnPlayBackStopped: CApplication",
                         "ytlounge")):
    with open(LOG, "r", errors="replace") as f:
        lines = f.readlines()[mark:]
    out = []
    for ln in lines:
        if any(p in ln for p in pats):
            out.append(ln.rstrip()[:150])
    return out


def resolve(vid):
    port = int(open("/storage/.kodi/userdata/addon_data/plugin.service.ytlounge-cast/manifest_server.port").read().strip())
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/resolve/{vid}", timeout=90) as r:
        return json.loads(r.read().decode())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    rpc("Application.SetVolume", {"volume": 20})
    mark = logmark()
    if mode == "state":
        print("state:", state())
        return
    if mode != "audio":
        print("== resolve video id")
        info = resolve("pCsEe3Ja5p8")
        url = info.get("playable_url") or ""
        print("   title=%r url=%s" % (info.get("title"), url[:100]))
        if not url:
            print("FAIL: no playable_url")
            return
        print("== play VIDEO lane item (direct url, video info)")
        print("   ", rpc("Player.Open", {"item": {"file": url}}))
        time.sleep(12)
        print("   state:", state())
    print("== switch to AUDIO lane item (music playlist, plugin:// url)")
    print("   ", rpc("Playlist.Clear", {"playlistid": 0}))
    print("   ", rpc("Playlist.Add", {"playlistid": 0, "item": {
        "file": "plugin://plugin.service.ytlounge-cast/?play=2XA22mZg6SA"}}))
    print("   ", rpc("Player.Open", {"item": {"playlistid": 0, "position": 0}}))
    for i in range(6):
        time.sleep(2)
        print("   t+%ss state: %s" % (2 * (i + 1), state()))
    print("== kodi.log during the transition")
    for ln in logslice(mark):
        print("   " + ln)
    rpc("Application.SetVolume", {"volume": 40})


if __name__ == "__main__":
    main()
