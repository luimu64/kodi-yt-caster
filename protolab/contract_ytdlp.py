#!/usr/bin/env python3
"""Contract test for yt-dlp resolution.

Verifies that yt-dlp resolves YouTube video IDs to formats and stream URLs
playable by Kodi (progressive MP4, HLS m3u8, or DASH mpd).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional

SAMPLE_VIDEOS = [
    ("jNQXAC9IVRw", "Me at the zoo (Short video)"),
    ("aqz-KE-bpKQ", "Big Buck Bunny (Animation)"),
]


def find_ytdlp() -> str:
    path = shutil.which("yt-dlp")
    if not path and os.path.exists(".venv/bin/yt-dlp"):
        path = os.path.abspath(".venv/bin/yt-dlp")
    if not path:
        raise RuntimeError("yt-dlp binary not found in PATH or .venv")
    return path


def resolve_video(video_id: str, ytdlp_bin: str) -> Dict[str, Any]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        ytdlp_bin,
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed on {video_id}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)

    # Find playable streams:
    # 1. HLS / DASH manifest
    # 2. Progressive pre-merged (video + audio)
    # 3. Direct video format fallback
    playable_url: Optional[str] = None
    stream_type = "unknown"

    for f in data.get("formats", []):
        proto = f.get("protocol", "")
        f_url = f.get("url", "")
        if "manifest/hls_playlist" in f_url or proto == "m3u8_native":
            playable_url = f_url
            stream_type = "hls"
            break

    if not playable_url:
        for f in data.get("formats", []):
            if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url"):
                playable_url = f["url"]
                stream_type = "progressive_mp4"
                break

    if not playable_url:
        playable_url = data.get("url")
        stream_type = "default"

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "duration": data.get("duration", 0),
        "thumbnail": data.get("thumbnail"),
        "stream_type": stream_type,
        "playable_url": playable_url,
    }


def main() -> None:
    print("==> [Phase 0.2] Testing yt-dlp resolution...")
    ytdlp = find_ytdlp()
    print(f"  Using yt-dlp: {ytdlp}")

    for vid, label in SAMPLE_VIDEOS:
        print(f"\n  Resolving: {label} (ID: {vid})...")
        info = resolve_video(vid, ytdlp)
        print(f"    Title: {info['title']}")
        print(f"    Duration: {info['duration']}s")
        print(f"    Stream type: {info['stream_type']}")
        url_preview = info['playable_url'][:80] + "..." if info['playable_url'] else "NONE"
        print(f"    Playable URL: {url_preview}")
        assert info["playable_url"], f"No playable URL resolved for {vid}"

    print("\n  All resolution checks passed successfully.")


if __name__ == "__main__":
    main()
