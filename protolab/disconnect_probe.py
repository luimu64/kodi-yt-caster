#!/usr/bin/env python3
"""Device-side disconnect driver: bind as a remote, then let the socket drop.

Google's Lounge sends `remoteDisconnected` to the screen when a bound remote's
connection ends, which is exactly the event that used to stop playback.
Run on the LibreELEC box (it needs the addon's own session_data).
"""
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request

SETTINGS = "/storage/.kodi/userdata/addon_data/plugin.service.ytlounge-cast/settings.xml"
BASE = "https://www.youtube.com/api/lounge"
NAME = sys.argv[1] if len(sys.argv) > 1 else "probe-phone"
HOLD = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

raw = open(SETTINGS).read()
d = json.loads(html.unescape(re.search(r'<setting id="session_data">(.*?)</setting>', raw, re.S).group(1)))

sys.path.insert(0, "/storage/.kodi/addons/plugin.service.ytlounge-cast")
from resources.lib.lounge.pairing import get_lounge_token_batch  # noqa: E402

tok, _exp = get_lounge_token_batch(d["screen_id"])
print(f"screen_id={d['screen_id']} token_len={len(tok)}")

params = {
    "app": "web", "mdx-version": "3", "name": NAME, "device": "REMOTE_CONTROL",
    "capabilities": "que,dsdtr,atp,vsp", "magnaKey": "cloudPairedDevice", "ui": "false",
    "deviceContext": "user_agent=dunno&os_name=android&ms=",
    "theme": "cl", "loungeIdToken": tok, "VER": "8", "v": "2", "t": "1", "CVER": "1",
    "id": d["screen_id"], "RID": "1", "AID": "0", "zx": "probedisconnect",
}
req = urllib.request.Request(
    f"{BASE}/bc/bind?" + urllib.parse.urlencode(params),
    data=urllib.parse.urlencode({"count": "0"}).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded",
             "Origin": "https://www.youtube.com", "User-Agent": "Mozilla/5.0"})
resp = urllib.request.urlopen(req, timeout=20).read().decode()
print("bind ok, sid=", (re.search(r'\["c","([^"]+)"', resp) or [None, "?"])[1])

print(f"holding {HOLD}s, then dropping the connection (remoteDisconnected)")
time.sleep(HOLD)
print("dropping")
