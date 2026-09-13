# YouTube Lounge Cast Receiver (`plugin.service.ytlounge-cast`)

A Kodi background service addon that turns your Kodi device into a YouTube "Watch on TV" receiver. Pair directly from the official YouTube mobile app on Android or iOS using a TV pairing code, and cast videos/playlists with full playback control.

Under the hood, stream URLs are resolved locally via `yt-dlp` and passed directly into Kodi's native player (with `inputstream.adaptive` support for DASH/HLS).

## Features

- **No login required on Kodi:** Your phone provides all account context and video selection.
- **Permanent pairing:** Device identity and lounge tokens are persisted; re-pair is not required across restarts.
- **Full remote control:** Play, pause, seek, stop, queue/playlist navigation, and volume control from the phone app.
- **Native Kodi playback:** Video streams are extracted via yt-dlp and fed directly to Kodi's player.
- **In-player resolution & audio switching:** Dynamically compiles a multi-rendition HLS Master Playlist containing all video resolutions (from 4K/1080p down to 144p) and audio streams. Open the Kodi OSD menu during playback -> Video Settings -> "Video stream" to switch resolution live without stopping the video.
- **Automatic yt-dlp binary management:** Automatically detects system architecture (x86_64, aarch64, armv7l, Windows, macOS) and downloads the matching yt-dlp binary upon install/startup.
- **Manual update button:** Update yt-dlp on demand at any time directly in Addon Settings -> Playback.
- **Zero non-stdlib dependencies in Kodi:** Operates using Python's standard library (`urllib.request`, `json`, `threading`, `subprocess`).

## Installation

1. Download or build `plugin.service.ytlounge-cast-1.0.0.zip`.
2. In Kodi, navigate to **Settings -> Add-ons -> Install from zip file**.
3. Select the zip file.
4. Ensure `yt-dlp` is available on the system PATH or configure its path in Addon Settings. (Optional: download arch-specific yt-dlp using `./build.sh <arch>`).

## How to Pair

1. Upon addon startup (or via Addon Settings), a 12-digit TV Code is shown (e.g. `123-456-789-012`).
2. Open the **YouTube app** on your phone or tablet.
3. Tap your profile icon -> **Settings** -> **General** -> **Watch on TV**.
4. Select **Link with TV code** and enter the 12 digits.
5. Once linked, tap the Cast icon in YouTube and select **Kodi**.

## Building & Packaging

To package the addon into a distributable zip:
```bash
./build.sh
```

To automatically bundle a `yt-dlp` binary for ARM / Raspberry Pi or x86_64:
```bash
./build.sh aarch64   # For 64-bit ARM (LibreELEC RPi4/5)
./build.sh x86_64    # For x86_64 PC
```

## Protocol Testing (`protolab/`)

The `protolab/` folder contains standalone contract test harnesses verifying the Lounge API and yt-dlp resolver outside Kodi:
```bash
# Acquire a new screen registration and TV pairing code
python3 protolab/contract_lounge.py --acquire

# Test session resume from saved tokens.json
python3 protolab/contract_lounge.py --resume

# Test yt-dlp stream resolution
python3 protolab/contract_ytdlp.py
```

## Acknowledgments & Prior Art

The YouTube Lounge (Leanback) protocol reverse engineering draws upon insights from:
- [desvaters/ytlounge](https://github.com/desvaters/ytlounge)
- [FabioGNR/pyytlounge](https://github.com/FabioGNR/pyytlounge)
- [enen92/script.tubecast](https://github.com/enen92/script.tubecast)
- [MarcoLucidi01/ytcast](https://github.com/MarcoLucidi01/ytcast)
