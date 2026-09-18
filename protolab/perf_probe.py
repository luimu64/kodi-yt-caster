#!/usr/bin/env python3
"""Device-side performance probe for the cast addon. Run ON the target device:

    python3 /storage/perf_probe.py [--skip-norm]

Measures, in isolation or against the LIVE service's localhost manifest server:
  [A] yt-dlp startup: inproc zipapp import vs subprocess spawn
  [B] resolve latency: probe-process inproc cold/warm, bridge cache hit
  [C] live service /resolve: cold and warm (real user-visible resolve time)
  [D] manifest server latency idle, and under a cold resolve saturating it
  [E] HLS preload: early-ready time, segments cached, first segment via proxy
  [F] parallel title fetch (12 ids, 2.5s deadline)
  [G] audio normalization throughput (ebur128 measure + AAC render), x-realtime
  [H] footprint: RSS, load average

Budget commentary lives next to each assertion; failures print FAIL=<metric>
but never abort the run, so one slow subsystem does not hide the rest.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

ADDON_ID = "plugin.service.ytlounge-cast"
PROFILE = f"/storage/.kodi/userdata/addon_data/{ADDON_ID}"
ADDON_DIR = f"/storage/.kodi/addons/{ADDON_ID}"
BIN = f"{PROFILE}/bin"
YTDLP = f"{BIN}/yt-dlp"
FFMPEG = f"{BIN}/ffmpeg"
PORT_FILE = f"{PROFILE}/manifest_server.port"

sys.path.insert(0, ADDON_DIR)

RESULTS = {}
FAILURES = []


def report(key, value, unit="", budget=None, note=""):
    RESULTS[key] = value
    bad = budget is not None and value > budget
    if bad:
        FAILURES.append(f"{key}={value}{unit} (budget {budget}{unit})")
    print(f"PERF {key}={value}{unit}" + (f"  [budget<={budget}{unit}]" + (" FAIL" if bad else " ok") if budget is not None else "") + (f"  {note}" if note else ""), flush=True)


def head_latency(port, path, n=10, gap=0.3, timeout=30):
    lat = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="HEAD")
            urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            pass
        lat.append((time.monotonic() - t0) * 1000)
        time.sleep(gap)
    return lat


def live_port():
    try:
        return int(open(PORT_FILE).read().strip())
    except Exception as e:
        print(f"WARN no live manifest server port: {e}", flush=True)
        return None


# ---------------------------------------------------------------- A: startup
def perf_startup():
    import zipfile
    importable = zipfile.is_zipfile(YTDLP)
    report("A0_ytdlp_zipapp_importable", int(importable), "",
           note=f"inproc path {'available' if importable else 'DEAD: {0} is not a zipapp'.format(os.path.basename(YTDLP))}")
    if importable:
        t0 = time.monotonic()
        code = "import sys; sys.path.insert(0, %r); import yt_dlp" % YTDLP
        subprocess.run([sys.executable, "-c", code], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        report("A1_inproc_import_s", round(time.monotonic() - t0, 2), "s", budget=8.0)
    else:
        report("A1_inproc_import_s", -1, "s", note="skipped (not importable)")

    t0 = time.monotonic()
    subprocess.run([sys.executable, YTDLP, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report("A2_subprocess_spawn_s", round(time.monotonic() - t0, 2), "s", note="per-call cost the inproc path avoids")


# ------------------------------------------------------- B: probe-side resolve
def perf_resolve(vid_cold, vid_warm):
    from resources.lib.ytdlp_bridge import YtDlpBridge

    bridge = YtDlpBridge(binary_path=YTDLP)
    t0 = time.monotonic()
    info = bridge.resolve(vid_cold)
    report("B1_probe_resolve_cold_s", round(time.monotonic() - t0, 2), "s", budget=15.0, note=info["stream_type"])

    t0 = time.monotonic()
    bridge.resolve(vid_warm)
    report("B2_probe_resolve_second_cold_s", round(time.monotonic() - t0, 2), "s", budget=12.0, note="warm HTTP session")

    t0 = time.monotonic()
    info2 = bridge.resolve(vid_cold)
    dt = (time.monotonic() - t0) * 1000
    report("B3_probe_cache_hit_ms", round(dt, 1), "ms", budget=100.0)
    assert info2 == info, "cache returned different payload"
    return bridge, info


# ------------------------------------------------------- C/D: live service
def perf_live_service(vid_cold, vid_warm):
    port = live_port()
    if not port:
        return None

    idle = head_latency(port, "/yt_probe_absent.m3u8", n=8, gap=0.2, timeout=10)
    report("D1_head_idle_p50_ms", round(statistics.median(idle), 1), "ms", budget=20.0)
    report("D2_head_idle_max_ms", round(max(idle), 1), "ms", budget=100.0)

    load = {}

    def saturate():
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/resolve/{vid_warm}", timeout=180) as r:
                json.loads(r.read().decode())
        except Exception as e:
            load["err"] = str(e)
        load["dt"] = time.monotonic() - t0

    th = threading.Thread(target=saturate, daemon=True)
    th.start()
    time.sleep(0.4)
    under = head_latency(port, "/yt_probe_absent.m3u8", n=6, gap=1.0, timeout=60)
    th.join(timeout=200)
    report("D3_head_under_resolve_p50_ms", round(statistics.median(under), 1), "ms", budget=200.0)
    report("D4_head_under_resolve_max_ms", round(max(under), 1), "ms", budget=2000.0, note="Kodi STATs this path before starting the next item")
    report("D5_live_resolve_cold_s", round(load.get("dt", -1), 2), "s", budget=20.0,
           note=load.get("err", ""))

    t0 = time.monotonic()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/resolve/{vid_cold}", timeout=60) as r:
        json.loads(r.read().decode())
    report("D6_live_resolve_second_cold_s", round(time.monotonic() - t0, 2), "s", budget=3.0,
           note="different id, warm HTTP session")
    return port


# ------------------------------------------------------------- E: preload
def perf_preload(bridge, info, vid):
    from resources.lib import manifest_server, preloader

    manifest_server._ensure_server()
    manifest_server.publish("yt_perfprobe.m3u8",
                            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nx.m3u8\n")
    if info.get("stream_type") != "hls_master":
        report("E1_preload_ready_s", -1, "s", note=f"skipped ({info.get('stream_type')})")
        return None

    t0 = time.monotonic()
    preloader.preload(vid, info)
    ready = None
    while time.monotonic() - t0 < 30:
        with preloader._LOCK:
            m = preloader._ITEMS.get(vid)
            if m and m.get("state") == "ready":
                ready = time.monotonic() - t0
                break
        time.sleep(0.05)
    report("E1_preload_ready_s", round(ready, 2) if ready else -1, "s", budget=8.0,
           note="all variant playlist bodies rewritten; segment bytes keep warming")

    prox = preloader.proxy_url(vid, info)
    if not prox:
        report("E2_first_segment_proxy_ms", -1, "ms", note="no proxy url (preload not ready)")
        return None
    port = int(prox.rsplit(":", 1)[1].split("/")[0])
    report("E0_probe_manifest_port", port)
    with urllib.request.urlopen(prox, timeout=10) as r:
        master = r.read().decode()
    line = [l for l in master.splitlines() if l and not l.startswith("#")][0]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{line}", timeout=10) as r:
        media = r.read().decode()
    seg = [l for l in media.splitlines() if l and not l.startswith("#")][0]
    t0 = time.monotonic()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{seg}", timeout=30) as r:
        body = r.read()
    dt = (time.monotonic() - t0) * 1000
    report("E2_first_segment_proxy_ms", round(dt), "ms", budget=1500.0, note=f"{len(body)} B")
    report("E3_first_segment_kbps", round(len(body) * 8 / max(dt, 1)), "kbps")

    # raw CDN reference: untouched variant playlist URL straight from the master
    try:
        with urllib.request.urlopen(info["playable_url"], timeout=10) as r:
            orig = r.read().decode()
        vurl = [l for l in orig.splitlines() if l.startswith("http")][0]
        t0 = time.monotonic()
        req = urllib.request.Request(vurl, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        report("E4_cdn_variant_rtt_ms", round((time.monotonic() - t0) * 1000), "ms", budget=2500.0)
    except Exception as e:
        report("E4_cdn_variant_rtt_ms", -1, "ms", note=str(e)[:60])
    return port


# -------------------------------------------------------------- F: titles
def perf_titles():
    from resources.lib.player_bridge import KodiPlayerBridge

    ids = ["aqz-KE-bpKQ", "jNQXAC9IVRw", "dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk",
           "OPf0YbXqDm0", "JGwWNGJdvx8", "RgKAFK5djSk", "fJ9rUzIMcZQ", "60ItHLz5WEA",
           "CevxZvSJLk8", "hT_nvWreIhg"]
    t0 = time.monotonic()
    titles = KodiPlayerBridge._fetch_titles_parallel(ids, time.monotonic() + 2.5)
    report("F1_titles_parallel_s", round(time.monotonic() - t0, 2), "s", budget=3.5,
           note=f"{len(titles)}/12 titles")


# ------------------------------------------------------- G: audio normalize
def perf_audio_norm():
    src = "/tmp/perf_probe_src.m4a"
    if not os.path.exists(src):
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=180",
                        "-c:a", "aac", "-b:a", "128k", src],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    dur = 180.0

    t0 = time.monotonic()
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", src, "-af", "ebur128=peak=true",
                        "-f", "null", "-"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                       encoding="utf-8", errors="replace")
    dt = time.monotonic() - t0
    report("G1_ebur128_realtime_x", round(dur / dt, 1), "x", note=f"{dt:.1f}s CPU for {dur:.0f}s audio")
    report("G2_ebur128_seconds", round(dt, 1), "s", budget=25.0)

    t0 = time.monotonic()
    p2 = subprocess.run([FFMPEG, "-hide_banner", "-i", src, "-af",
                         "aresample=async=1:first_pts=0,volume=-3dB",
                         "-c:a", "aac", "-b:a", "128k",
                         "-f", "hls", "-hls_time", "6", "-hls_segment_type", "mpegts",
                         "-hls_list_size", "0", "-hls_segment_filename", "/tmp/perf_norm_%03d.ts",
                         "/tmp/perf_norm.m3u8"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                        encoding="utf-8", errors="replace")
    dt2 = time.monotonic() - t0
    report("G3_render_realtime_x", round(dur / dt2, 1), "x", note=f"{dt2:.1f}s CPU for {dur:.0f}s audio")
    report("G4_render_seconds", round(dt2, 1), "s", budget=60.0, note="% of a 3.5min track")


# ------------------------------------------------------------ H: footprint
def perf_footprint(port_self):
    rss = 0
    with open("/proc/self/status") as f:
        for ln in f:
            if ln.startswith("VmRSS"):
                rss = int(ln.split()[1])
    report("H1_probe_rss_mb", round(rss / 1024, 1), "MB")
    report("H2_loadavg_1m", round(os.getloadavg()[0], 2))
    mem = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, v = ln.split(":", 1)
            mem[k] = int(v.split()[0])
    report("H3_mem_available_mb", round(mem["MemAvailable"] / 1024), "MB")

    # Kodi's own footprint: the inproc resolver holds yt-dlp inside kodi.bin.
    try:
        pid = subprocess.run(["pidof", "kodi.bin"], stdout=subprocess.PIPE).stdout.split()[0].decode()
        status = open(f"/proc/{pid}/status").read()
        rss_kb = [int(l.split()[1]) for l in status.splitlines() if l.startswith("VmRSS")][0]
        thr = [int(l.split()[1]) for l in status.splitlines() if l.startswith("Threads")][0]
        report("H5_kodi_rss_mb", round(rss_kb / 1024, 1), "MB", note=f"pid {pid}, {thr} threads")
    except Exception as e:
        report("H5_kodi_rss_mb", -1, "MB", note=str(e)[:50])

    if port_self:
        # keep-alive sanity: two requests on one socket
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port_self, timeout=5)
        conn.request("GET", "/yt_perfprobe.m3u8")
        r1 = conn.getresponse(); b1 = r1.read()
        conn.request("GET", "/yt_perfprobe.m3u8")
        r2 = conn.getresponse(); b2 = r2.read()
        report("H4_keepalive_reuse", int(r1.status == 200 and r2.status == 200 and b1 == b2), "",
               note=f"http/{r1.version} status {r1.status}/{r2.status} len {len(b1)}B")
        conn.close()


def main():
    skip_norm = "--skip-norm" in sys.argv
    vid_cold = "aqz-KE-bpKQ"      # Big Buck Bunny 4K
    vid_warm = "jNQXAC9IVRw"      # first YouTube video ever (small)
    print(f"# device perf probe on {os.uname().machine} python {sys.version.split()[0]}", flush=True)
    perf_startup()
    perf_live_service(vid_cold, vid_warm)
    bridge, info = perf_resolve(vid_cold, vid_warm)
    port_self = perf_preload(bridge, info, vid_cold)
    perf_titles()
    if not skip_norm:
        perf_audio_norm()
    perf_footprint(port_self)

    print("\n=== SUMMARY ===")
    print(json.dumps(RESULTS, indent=1, default=str))
    print("FAILURES: " + (", ".join(FAILURES) if FAILURES else "none"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
