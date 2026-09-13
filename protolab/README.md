# YouTube Lounge Protocol Findings & Ground Truth

## Architecture Overview

YouTube "Watch on TV" (Lounge API) uses HTTP endpoints under `https://www.youtube.com/api/lounge`.
Contrary to initial assumptions, the Lounge API does **not** use protobuf or raw websockets; it uses length-delimited JSON frames over streaming HTTP chunked responses (`/bc/bind`).

## Endpoints

1. **Screen ID Generation:**
   - `GET /api/lounge/pairing/generate_screen_id`
   - Returns a raw screen identifier (e.g. `149l6e6qh09eae0rfo4l304h9p`).

2. **Lounge Token Batch:**
   - `POST /api/lounge/pairing/get_lounge_token_batch`
   - Form data: `screen_ids=<screen_id>`
   - Returns JSON with `loungeToken` and expiration timestamp (~14 days lifespan). Persisting and refreshing this token preserves the TV association indefinitely.

3. **Pairing Code Generation (TV Code):**
   - `POST /api/lounge/pairing/get_pairing_code?ctx=pair`
   - Form data: `access_type=permanent`, `app=kodi-ytcast`, `lounge_token=<token>`, `screen_id=<screen_id>`, `screen_name=<name>`
   - Returns 12-digit string formatted as `xxx-xxx-xxx-xxx` for the user to enter in YouTube Mobile (`Settings -> Watch on TV -> Enter TV Code`).

4. **Pairing Code Registration (Alternative / DIAL):**
   - `POST /api/lounge/pairing/register_pairing_code`
   - Used when phone provides the code directly via local discovery.

5. **Session Handshake (`/bc/bind`):**
   - `POST /api/lounge/bc/bind?<announcement_params>`
   - Form data: `count=0`
   - Returns length-delimited frames containing:
     - `["c", "<SID>", ...]`
     - `["S", "<gsessionid>"]`
     - Initial status / playlist events

6. **Event Listener Stream (`/bc/bind`):**
   - Streaming `GET /api/lounge/bc/bind?<announcement_params>&SID=<SID>&gsessionid=<gsessionid>&RID=rpc&AID=3&CI=0&TYPE=xmlhttp`
   - Kept open; YouTube pushes commands in real time:
     - `remoteConnected`
     - `setPlaylist` / `updatePlaylist` (contains video IDs, list IDs, initial seek offset)
     - `play`, `pause`, `stopVideo`, `seekTo`, `setVolume`, `getNowPlaying`
     - `remoteDisconnected`

7. **Status Reporting Back to YouTube (`/bc/bind`):**
   - `POST /api/lounge/bc/bind?<announcement_params_with_SID>`
   - Parameters prefixed with `req0_`:
     - `nowPlaying`: reports current track, time, playback status
     - `onStateChange`: state transitions (playing=1, paused=2, stopped=0)
     - `onVolumeChanged`: volume level and mute state

## Framing Format

Responses from `/bc/bind` consist of sequential frames:
```
<length_in_bytes>\n
<json_array>
```
Example:
```
584\n
[[0,["c","83802FC134080A01","",8]],[1,["S","kZLqq1wzOM8U_WVZJnh2bfna5xAwmXMJ"]]]
```

## Ground Truth vs Initial Spec

- **Protobuf vs JSON:** Protobuf is used by Google Cast V2 (port 8009 chromecast socket). YouTube Lounge Leanback API is pure JSON over HTTP. No protobuf dependency is required in Kodi.
- **Python Stdlib:** Everything can be done with Python's built-in `urllib.request` and `json` libraries, requiring zero external wheel compilation or complex C-extension dependencies.
