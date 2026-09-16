#!/usr/bin/env python3
"""Measure manifest-server latency while the service is saturated by a resolve.

Kodi STATs the playing file against this server right after the player stops; if
the answer is slow, the stop (and PlaybackCleanup, which starts the pending
audio item) is delayed with it. Run on the device.
"""
import threading
import time
import urllib.request

PROFILE = "/storage/.kodi/userdata/addon_data/plugin.service.ytlounge-cast/manifest_server.port"
port = int(open(PROFILE).read().strip())
print("manifest server port:", port)
result = {}


def heavy():
    t0 = time.monotonic()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/resolve/dQw4w9WgXcQ", timeout=180).read()
        print("cold resolve finished in %.1fs" % (time.monotonic() - t0))
    except Exception as e:
        print("cold resolve error after %.1fs: %s" % (time.monotonic() - t0, e))
    result["done"] = True


t = threading.Thread(target=heavy, daemon=True)
t.start()
time.sleep(0.5)
worst = 0.0
for i in range(10):
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/yt_probe.m3u8", method="HEAD")
        urllib.request.urlopen(req, timeout=30).read()
        dt = time.monotonic() - t0
        print("HEAD while resolving: %.3fs" % dt)
    except Exception as e:
        dt = time.monotonic() - t0
        print("HEAD while resolving: %.3fs (status %s)" % (dt, getattr(e, "code", e)))
    worst = max(worst, dt)
    time.sleep(1)
print("worst HEAD latency under load: %.3fs" % worst)
