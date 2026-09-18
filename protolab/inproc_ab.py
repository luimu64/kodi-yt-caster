#!/usr/bin/env python3
"""Zipapp-only A/B run: one process, one binary, fresh inproc state."""
import sys
import time

ADDON_DIR = "/storage/.kodi/addons/plugin.service.ytlounge-cast"
sys.path.insert(0, ADDON_DIR)

mode = sys.argv[1] if len(sys.argv) > 1 else "zipapp"
BIN = "/tmp/ytdlp-zipapp" if mode == "zipapp" else "/storage/.kodi/userdata/addon_data/plugin.service.ytlounge-cast/bin/yt-dlp"

from resources.lib.ytdlp_bridge import YtDlpBridge          # noqa: E402
from resources.lib import ytdlp_inproc                      # noqa: E402

t0 = time.monotonic()
ok = ytdlp_inproc.try_init(BIN)
print(f"PERF {mode}_inproc_ready={ok} init_s={time.monotonic()-t0:.2f}", flush=True)

bridge = YtDlpBridge(binary_path=BIN)
for i, vid in enumerate(["jNQXAC9IVRw", "aqz-KE-bpKQ", "dQw4w9WgXcQ"]):
    t0 = time.monotonic()
    info = bridge.resolve(vid)
    print(f"PERF {mode}_resolve{i}_s={time.monotonic()-t0:.2f} type={info.get('stream_type')}", flush=True)
t0 = time.monotonic()
bridge.resolve("jNQXAC9IVRw")
print(f"PERF {mode}_cache_hit_ms={(time.monotonic()-t0)*1000:.1f}", flush=True)
