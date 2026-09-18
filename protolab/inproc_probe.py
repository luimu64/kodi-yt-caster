#!/usr/bin/env python3
"""Quantify the in-process yt-dlp path on the device.

The addon's perf pass relies on importing the yt-dlp release file as a zipapp.
On aarch64 the downloader fetches `yt-dlp_linux_aarch64` (a PyInstaller ELF),
which zipimport cannot load — so every resolve pays a subprocess spawn. This
probe imports the PURE-PYTHON release (yt-dlp.tar.gz) and measures what the
inproc path would deliver on the same hardware.
"""
import os
import subprocess
import sys
import time

TARBALL = "/tmp/ytp/yt-dlp.tar.gz"
SRC = "/tmp/ytp/src"
URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.tar.gz"


def fetch():
    os.makedirs("/tmp/ytp", exist_ok=True)
    if os.path.isdir(SRC) and os.path.isdir(os.path.join(SRC, "yt_dlp")):
        return
    if not os.path.exists(TARBALL):
        t0 = time.monotonic()
        import urllib.request
        urllib.request.urlretrieve(URL, TARBALL)
        print(f"PERF Z0_tarball_download_s={time.monotonic()-t0:.2f}s", flush=True)
    os.makedirs(SRC, exist_ok=True)
    subprocess.run(["tar", "xf", TARBALL, "-C", SRC], check=True)
    print(f"PERF Z1_tarball_mb={os.path.getsize(TARBALL)/1048576:.2f}MB", flush=True)


def main():
    fetch()
    pkg = os.path.join(SRC, "yt-dlp")
    sys.path.insert(0, pkg if os.path.isdir(pkg) else SRC)
    t0 = time.monotonic()
    import yt_dlp
    print(f"PERF Z2_import_purepython_s={time.monotonic()-t0:.2f}s  version={yt_dlp.version.__version__}", flush=True)

    t0 = time.monotonic()
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                            "noplaylist": True, "socket_timeout": 30})
    print(f"PERF Z3_ydl_construct_s={time.monotonic()-t0:.2f}s", flush=True)

    ids = ["jNQXAC9IVRw", "aqz-KE-bpKQ", "dQw4w9WgXcQ"]
    for i, vid in enumerate(ids):
        t0 = time.monotonic()
        try:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            print(f"PERF Z4_inproc_resolve_{i}_s={time.monotonic()-t0:.2f}s  formats={len(info.get('formats') or [])}  title={str(info.get('title'))[:30]!r}", flush=True)
        except Exception as e:
            print(f"PERF Z4_inproc_resolve_{i}_s=-1  error={str(e)[:80]}", flush=True)

    with open("/proc/self/status") as f:
        for ln in f:
            if ln.startswith("VmRSS"):
                print(f"PERF Z5_rss_mb={int(ln.split()[1])/1024:.1f}MB", flush=True)


if __name__ == "__main__":
    main()
