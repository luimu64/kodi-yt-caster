# YouTube & YouTube Music Cast Receiver (`plugin.service.ytlounge-cast`)

A Kodi background service addon that turns your Kodi device into a YouTube and **YouTube Music** Cast receiver. Cast videos, music, albums, and playlists directly from the official YouTube and YouTube Music apps on Android or iOS.

Under the hood, stream URLs are resolved locally via `yt-dlp` and passed directly into Kodi's native player (with `inputstream.adaptive` support for DASH/HLS and multi-resolution switching).

## Features

- **YouTube Music Casting:** Full support for casting tracks, playlists, and albums from the YouTube Music mobile app.
- **Automatic Wi-Fi Discovery (SSDP / DIAL):** Kodi appears automatically under the Cast button in YouTube and YouTube Music when on the same Wi-Fi network.
- **No login required on Kodi:** Your phone provides all account context and music/video selection.
- **Permanent pairing:** Device identity and lounge tokens are persisted across restarts. Manual 12-digit TV code pairing is also supported.
- **Full remote control:** Play, pause, seek, stop, track skipping, queue navigation, and volume control from the phone app.
- **Native Kodi playback:** Video and audio streams are extracted via yt-dlp and fed directly to Kodi's player.
- **In-player resolution & audio switching:** Dynamically compiles a multi-rendition HLS Master Playlist containing all video resolutions (from 4K/1080p down to 144p) and audio streams. Open the Kodi OSD menu during playback -> Video Settings -> "Video stream" to switch resolution live without stopping the video.
- **Automatic yt-dlp binary management:** Automatically detects system architecture (x86_64, aarch64, armv7l, Windows, macOS) and downloads the matching yt-dlp binary upon install/startup.
- **Manual update button:** Update yt-dlp on demand at any time directly in Addon Settings -> Playback.
- **Zero non-stdlib dependencies in Kodi:** Operates using Python's standard library (`urllib.request`, `socket`, `http.server`, `json`, `threading`, `subprocess`).

## How to Cast

### Option 1: Direct Wi-Fi Cast (YouTube & YouTube Music)
1. Ensure your phone and Kodi device are on the same Wi-Fi / local network.
2. In **YouTube** or **YouTube Music**, tap the **Cast** icon in the top bar or player.
3. Select your Kodi device (e.g. `Kodi`). Playback starts automatically.

### Option 2: TV Code Pairing (YouTube App)
1. Upon addon startup (or via Addon Settings), a 12-digit TV Code is shown (e.g. `123-456-789-012`).
2. In the YouTube app on your phone, tap your profile icon -> **Settings** -> **General** -> **Watch on TV** -> **Link with TV code**.
3. Enter the 12 digits. Once linked, the device is associated with your Google account.

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
