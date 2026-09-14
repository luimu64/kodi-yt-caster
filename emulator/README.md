# Kodi Emulator + Phone Casting Simulator

Stdlib-only test rig that runs the **real addon** (`service.py`,
`resources/lib/*` unmodified) outside Kodi against a fake Kodi Python API and
a mock YouTube Lounge backend, plus a fake casting **phone** that drives the
receiver through the real wire protocol.

```bash
python3 emulator/run_all.py            # all scenarios
python3 emulator/scenarios/test_cast_basic.py   # one suite directly
```

## What it is

- `kodi_stub/` — injectable `xbmc`/`xbmcgui`/`xbmcaddon`/`xbmcplugin`/`xbmcvfs`
  fakes. `xbmc.Player` is a *simulated* player: wall-clock playback clock,
  async lifecycle callbacks from a pump thread, and Kodi's real quirks
  (`isPlaying()` true while paused, `pause()` toggles, labels snapshotted at
  `playlist.add()`).
- `lounge_server/` — mock Lounge backend: `/pairing/*`, `/bc/bind`
  handshake + close-delimited long-poll streaming with the exact
  length-delimited frame format (round-trips through the addon's own
  `parse_frames` at import time). Captures every receiver report
  (`REPORTS`) for assertions.
- `lounge_server/phone.py` — the sender: connect/cast/control/seek/volume,
  including relay-duplicate bursts (`burst_set_playlist`).
- `scenarios/` — end-to-end tests asserting both what the fake player did
  and what the receiver reported back to Lounge.
  `scenarios/harness.py` boots the full `service.run_service()` in a thread
  and tears it down.
  The suite names: `test_boot_smoke`, `test_disconnect`, `test_dup_relay_burst`,
  `test_fake_player`, `test_queue_advance`, `test_remote_control`,
  `test_token_expiry`.

## Covered

pair/cast/pending-seek, remote-control truth table (pause-while-paused guard,
toggle semantics, clamped volume), duplicate relay bursts (one resolve), queue
advance (natural end, phone-side edits), disconnect/reconnect, token-expiry
re-registration preserving the other session, teardown thread hygiene.

## NOT covered (on-device only)

demuxer/codecs, IA-vs-native player differences, GUI rendering, `getTime()`
jitter, DIAL/SSDP discovery (multicast), mid-poll token 400 wire path (the
listener needs ~62s of backoff; scenarios drive `on_token_expired` directly).

## Monkeypatch inventory (everything else runs stock)

1. `resources.lib.lounge` `BASE_URL` (client + session + listener re-imports +
   `client.request` default arg) → mock server. The plan's single sanctioned patch.
2. `YtDlpBridge.resolve` → deterministic offline resolver (no yt-dlp/CDN).
3. `KodiPlayerBridge._fetch_title_sync` → instant fake titles (no oEmbed network).
4. `ytdlp_downloader.download_ytdlp` → no-op (fake binary pre-placed in the
   stub profile instead).

## Notes for scenario authors

- Assertions are state-based with deadlines (`wait_until`), never
  ordering-of-threads-based.
- `Scenario.end_media()` lets the simulated clock reach its end and fires the
  Kodi Ended event (media duration is 8s in the harness).
- A sender drives **one** lounge session (like a real phone); the receiver
  still reports to all bound sessions.
- `emulator/` is never packaged: `build.sh` copies an explicit file list
  (verified — zip contains 0 `emulator` entries).
- Thread-leak debugging: `test_boot_smoke.test_teardown_clean` fails if any
  LoungeListener/DIAL/SSDP/PlaybackMonitor thread survives a scenario.
