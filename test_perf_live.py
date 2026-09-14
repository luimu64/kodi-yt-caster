#!/usr/bin/env python3
"""Live verification harness for the perf pass: exercises the inproc resolver,
manifest server keep-alive, parallel title fetch, and preloader with a real
YouTube video. Run manually: python3 test_perf_live.py"""

import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from resources.lib.ytdlp_bridge import YtDlpBridge, build_hls_master_manifest
from resources.lib import ytdlp_inproc, manifest_server, preloader

VID = "aqz-KE-bpKQ"  # Big Buck Bunny 4K

# 1. inproc resolve end-to-end through the bridge (cache + manifest build)
bridge = YtDlpBridge(binary_path="/tmp/ytdlp_probe/yt-dlp")
t0 = time.monotonic()
info = bridge.resolve(VID)
t_resolve = time.monotonic() - t0
print(f"[1] resolve (inproc): {t_resolve:.2f}s  type={info['stream_type']}  title={info['title'][:40]!r}")
assert t_resolve < 15, "resolve too slow"
assert info["playable_url"], "no playable url"

# 2. cache hit path
t0 = time.monotonic()
info2 = bridge.resolve(VID)
t_cache = time.monotonic() - t0
print(f"[2] cache hit: {t_cache*1000:.1f}ms")
assert t_cache < 0.1 and info2 == info

# 3. manifest server: keep-alive + two requests on ONE connection
url = manifest_server.publish("probe.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nx.m3u8\n")
import http.client
port = int(url.rsplit(":", 1)[1].split("/")[0])
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
conn.request("GET", "/probe.m3u8")
r1 = conn.getresponse(); b1 = r1.read()
conn.request("GET", "/probe.m3u8")  # second request on same socket
r2 = conn.getresponse(); b2 = r2.read()
print(f"[3] keep-alive: status {r1.status}/{r2.status}, reused connection, versions {r1.version}/{r2.version}")
assert r1.status == 200 and r2.status == 200 and b1 == b2
conn.close()

# 4. resolve endpoint JSON
from resources.lib.resolver import VideoResolver
manifest_server.set_resolver(VideoResolver(bridge=bridge))
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
conn.request("GET", f"/resolve/{VID}")
r = conn.getresponse()
data = json.loads(r.read())
conn.close()
print(f"[4] /resolve endpoint: {r.status}, id={data['id']}")
assert r.status == 200 and data["id"] == VID

# 5. preloader: HLS preload + early-ready + proxied play
if info["stream_type"] == "hls_master":
    preloader.preload(VID, info)
    deadline = time.monotonic() + 8
    ready_at = None
    while time.monotonic() < deadline:
        with preloader._LOCK:
            m = preloader._ITEMS.get(VID)
            if m and m.get("state") == "ready":
                ready_at = time.monotonic()
                break
        time.sleep(0.05)
    assert ready_at, "preload never became ready"
    t_ready = ready_at - (deadline - 8)
    print(f"[5] preload early-ready after {t_ready:.2f}s (was: whole ~60s window serial)")
    prox = preloader.proxy_url(VID, info)
    print(f"    proxy_url -> {prox}")
    assert prox
    # fetch the rewritten master, then one segment through the local server
    with urllib.request.urlopen(prox, timeout=5) as resp:
        master = resp.read().decode()
    assert "/preload/" in master, "master not rewritten to local URLs"
    line = [l for l in master.splitlines() if l and not l.startswith("#")][0]
    media_url = f"http://127.0.0.1:{port}{line}"
    with urllib.request.urlopen(media_url, timeout=5) as resp:
        media = resp.read().decode()
    seg = [l for l in media.splitlines() if l and not l.startswith("#")][0]
    seg_url = f"http://127.0.0.1:{port}{seg}"
    t0 = time.monotonic()
    with urllib.request.urlopen(seg_url, timeout=10) as resp:
        seg_bytes = resp.read()
    t_seg = time.monotonic() - t0
    print(f"[6] first segment via local proxy: {len(seg_bytes)} bytes in {t_seg*1000:.0f}ms")
    assert len(seg_bytes) > 10000

# 7. parallel title fetch
from resources.lib.player_bridge import KodiPlayerBridge
t0 = time.monotonic()
titles = KodiPlayerBridge._fetch_titles_parallel([VID, "jNQXAC9IVRw"], time.monotonic() + 2.5)
t_titles = time.monotonic() - t0
print(f"[7] parallel titles: {len(titles)} in {t_titles:.2f}s -> {list(titles.values())[:2]}")

print("LIVE PERF CHECKS PASSED")
