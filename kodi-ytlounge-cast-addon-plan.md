# Kodi Addon: `plugin.service.ytlounge-cast` — Android TV YouTube Cast Receiver

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** A Kodi background service that emulates a YouTube-on-TV device: pair with the official Android YouTube app via TV code (Lounge API), then receive play/queue/seek commands from the phone and actually resolve + play all video via yt-dlp inside Kodi.

**Architecture:** Kodi parameterized `xbmc.service` plugin running a background thread with three stages: (1) persistent device registration + pairing-code display, (2) Lounge session listener (long-poll protobuf over `cast.googleapis.com`) translating commands to Kodi actions, (3) yt-dlp resolver subprocess feeding `Player().play()`. No login on the Kodi side; phone supplies all account authority.

**Tech Stack:** Python 3 (Kodi's bundled interpreter), `requests` or `httpx`-style HTTP via Kodi stdlib vendoring (use `requests` from the Kodi python lib or vendor a stdlib-only client), `protobuf` via a bundled `plugin.script.module.protobuf` dependency, yt-dlp shipped as a module addon or as an external binary in the addon's bin dir, SQLite-free persistence via `AddonSettings` JSON blob.

---

## Phase 0 — Protocol ground truth (do FIRST, everything depends on it)

### Task 0.1: Contract-test harness outside Kodi

**Objective:** Prove all Lounge flows with plain-python on a dev box before touching Kodi code, so the addon protocol layer is frozen fact, not guesswork.

**Files:**
- Create: `protolab/contract_lounge.py` (standalone, runs with `uv run`)
- Create: `protolab/README.md` (notes on endpoints, headers, quirks)

**Steps:**
1. `pip install pyytlounge` (https://github.com/FabioGNR/pyytlounge — actively maintained, has docs at readthedocs.io) and `ytlounge` (https://github.com/desvaters/ytlounge — dependency-light, one-file httpx client, good reference for a stdlib-only port). Read both; they agree on the reverse-engineered protocol.
2. Script that:
   - `registerDevice()` with a fixed `deviceId` (UUIDv4, persisted) and `screenName="Kodi"`.
   - Fetch pairing code (`getPairingCode`), print it with expiry estimate.
   - `waitForPairing(code)` → verify a real phone can pair (user must do this for the test).
   - Post-pairing: print `screenId`, `loungeToken`. Save to `protolab/tokens.json`.
   - Akamai test: reconnect in a fresh process using **only** `tokens.json` → prove cold-start session resume works with no re-pair.
3. Test the command channel by queuing a video from the phone and dumping the raw sniffed payload (yawall/CommandContext protobuf) to file for schema reference.
4. Commit everything under `protolab/`.

**Verification:**
- `uv run protolab/contract_lounge.py --acquire` then `--resume` shows pairing once then resume-clean.
- Phone device visible in YouTube app "Watch on TV" list after resume.

**Critical risk this task retires:** undocumented/rotational protocols. If pyytlounge is stale vs Google, fail fast here.

---

### Task 0.2: yt-dlp resolution contract test

**Objective:** Prove yt-dlp reliably resolves YouTube URLs to a stream Kodi can pass to `Player().play()`. This is the other hard dependency.

**Files:**
- Create: `protolab/contract_ytdlp.py`

**Steps:**
1. Test resolution modes on a handful of videos: anonymous, cookieless, age-gated (should be skipped → mobile app won't trust-send such).
2. Output one URL (format selector: `bestvideo[height<=?1080][vcodec!*=vp9.2]+bestaudio/best`) and pass to `xbmc.Player` in a Kodi instance — confirm playback.
3. Compare bundling: vendored yt-dlp folder inside addon (self-update disabled) vs pulled from pip at build time. Choose bundle (YAGNI — user updates addon to update yt-dlp).

**Verification:**
- yt-dlp subprocess produces an m3u8/dash URL that Kodi envelopes into a playable item without a second resolver pass.

---

## Phase 1 — Kodi addon skeleton

### Task 1.1: Addon scaffolding

**Objective:** Minimum viable `xbmc.service` addon structure that loads and runs a background thread.

**Files:**
- Create: `addon.xml` (type `xbmc.service` which means background; use `library="service.py"`), `dae` dependencies: `xbmc.python` version `>=3.0.0`, `script.module.requests` if used.
- Create: `service.py` — `Monitor.waitForAbort(1)` loop bootstrapping.
- Create: `resources/settings.xml` — minimal two booleans (`enable`, `require_pairing_prompt`) for now.
- Create: `resources/language/resource.language.en_gb/strings.po`.
- Create: `icon.png`, `fanart.jpg` placeholders.

**Steps:**
1. Build dir structure in the target repo `plugin.service.ytlounge-cast/`.
2. Watch install: zip, push to Kodi (LibreELEC via SCP or `kodi-send` workaround), test enable via Settings→Services.
3. Confirm `service.py` logs on boot with `xbmc.log("YTLounge starting", xbmc.LOGINFO)` and monitor loop exits cleanly on Kodi shutdown.

**Verification:** Kodi log shows the boot.Info line; monitor loop exits cleanly on Kodi shutdown.

### Task 1.2: STD/Vendored protobuf module

**Objective:** Ship or depend on protobuf python runtime for Lounge command parsing.

**Files:**
- Create or depend: `script.module.protobuf` (Kodi repo has it; verify availability and add to `addon.xml` `<requires>`).

**Steps:**
1. Confirm on the target Kodi install: `plugin://script.module.protobuf` resolves; if not, bundle `google/protobuf` python module in addon's `libs/`.
2. Add compiled Lounge protobuf definitions here (from Phase 0's `yawall/context` payload reverse-engineered in pyytlounge's `_messages.py`; port to `.proto` locally).

**Verification:** `import google.protobuf` works inside a Kodi-launched thread.

### Task 1.3: Session/persistence layer

**Objective:** Persist `deviceId`, `screenId`, `loungeToken`, `screenName`, `pairingCode` across restarts via a single JSON blob.

**Files:**
- Create: `resources/lib/persistence.py`

**Steps:**
1. Wrap `xbmcaddon.Addon().getSetting(...)` string settings (Kodi settings are strings; serialize JSON for token bundle).
2. Expose `load()`, `save()`, `clear()` with test hooks.
3. no-op fallback for non-Kodi dev runs (mock addon).

**Verification:** Restart addon; tokenbundle survives.

---

## Phase 2 — Protocol layer

### Task 2.1: Port Lounge client to addon (stdlib-only)

**Objective:** Zero pip dependency beyond what Kodi ships baked in.

**Files:**
- Create: `resources/lib/lounge/client.py` (port of `desvaters/ytlounge` httpx logic to `urllib.request`/`pickle`-free stdlib, or depend on `script.module.requests` — decide at implementation time based on which Kodi build has it).
- Create: `resources/lib/lounge/pairing.py` (getPairingCode + waitForPairing + registerDevice calls)
- Create: `resources/lib/lounge/session.py` (establishLoungeSession from persisted loungeToken)

**Steps:**
1. Port from `protolab/contract_lounge.py` proven flow; keep concise; map HTTP errors onto one custom `LoungeError`.
2. Reuse fixed `deviceId` from persistence (never regen randomly on restart — that's the "permanent pairing" mechanism).
3. Test with Kodi running against the real phone.

**Verification:** Pairing code shows on screen on first run (via `xbmcgui.Dialog().notification` or an add-on dialog rendering the 12/6-char code); phone links; log shows `establishLoungeSession` OK.

### Task 2.2: Command listener thread

**Objective:** Long-poll Lounge websocket-equivalent session, translate phone commands to local dispatch.

**Files:**
- Create: `resources/lib/lounge/listener.py`
- Modify: `service.py` to spawn thread after session resume.

**Steps:**
1. Subscribe to Lounge session events (it's a chunked long-poll / streaming POST; pyytlounge's `_receiver.py` shows the mechanics).
2. Decode protobuf `ReceiveCommand` envelope; map actions:
   - `playVideo` / `autoplayVideo` → dispatch Resolver+Player
   - `setPlaylist` (autoplay queue) → build local playlist
   - `pause`, `resume`, `seekTo`, `setVolume`, `skipAd` → map to `Player` actions (Kodi native: `Player.pause`, `Player.seekTime`, `Player.setVolume`)
   - `stopVideo` → `Player.stop`
   - heartbeat/reconnect on session idle.
3. Emit Kodi playback state back via Lounge `nowPlaying` / `onStateChange` callbacks so the phone shows correct "Now playing on Kodi" UI including progress.

**Verification:** Phone: play a video → Kodi plays it within ~1s. Same for pause/seek/queue. Phone UI shows "playing on <Kodi screen name>".

### Task 2.3: Reconnect + recovery

**Objective:** Network drops, phone app closes, session resume on rollback.

**Files:**
- Modify: `resources/lib/lounge/session.py`
- Modify: `resources/lib/lounge/listener.py`

**Steps:**
1. Exponential backoff (2/4/8/16s, cap 60s) on HTTP failures; cap consecutive-failure count to 8 before re-registering.
2. On `screenIdle`/session loss: attempt `establishLoungeSession` with saved token once; on token rejection (`404`/`invalid`) → wipe tokens → re-register → show fresh pairing code via Kodi notification.
3. Honor "connected device removed" from phone (phone unlinks) by clearing pairing.

**Verification:** Kill network 60s → auto-resumes. Revoke pairing with phone → new code appears on Kodi within 30s.

---

## Phase 3 — Playback bridge

### Task 3.1: yt-dlp resolver subsystem

**Objective:** Resolve a video ID → Kodi-playable URL/item without blocking the listener thread.

**Files:**
- Create: `resources/lib/resolver.py`
- Create: `resources/lib/ytdlp_bridge.py` (subprocess wrapper around bundled yt-dlp in `resources/bin/yt-dlp`)

**Steps:**
1. Subprocess spawn (never in-thread; Kodi service threads are precious) with `--dump-json --no-playlist -f "<format selector>"`, parse JSON once, return `{url, title, duration, thumbnail, formats, sid}`.
2. Handle livestreams separately (`-f best`, m3u8 falls into native Kodi inputstream handling; register `inputstream.adaptive` dep in `addon.xml` for DASH).
3. Optional cookie file path resolved from settings for edge-case videos — hidden behind a settings toggle, default off (no login by default, per earlier design discussion).
4. Cache snippets of resolution output to avoid repeated yt-dlp for queue items processed rapidly during `setPlaylist` (TTL = video duration).

**Verification:** `plugin://script.ytlounge-cast/resolver/<videoid>` returns a playable stream.

### Task 3.2: Player bridge

**Objective:** Translate a resolver result into a Kodi `ListItem` + `Player().play`.

**Files:**
- Create: `resources/lib/player_bridge.py`

**Steps:**
1. Build `xbmcgui.ListItem(title)` with `setMimeType()` correct for dash (`application/dash+xml`) vs m3u8 (`application/x-mpegURL`); set `setPath`, `setInfo("video", {title, duration, artist...})`.
2. For queue mode, pre-queue into a Kodi Local Playlist so auto-advance works as the phone expects.
3. Respect phone-issued `seekTo` emitted before `playVideo` resolves — handle startup-seek.

**Verification:** Play a video from phone; pause/seek/volume work; playlist advance keeps UI "next" in phone app.

### Task 3.3: Back-plate event notifications

**Objective:** Listener + player state sync so phone sees accurate progress/`skipAd` context.

**Files:**
- Modify: `resources/lib/lounge/listener.py`
- Modify: `resources/lib/player_bridge.py`

**Steps:**
1. Hook `Player` events via `xbmc.Player().onPlayBackStarted/Ended/Stopped/Paused` → push to Lounge webservice session.
2. Position updates polled every 2s while playing; feed `nowPlaying` state back.

**Verification:** Pause seek from Kodi remote → phone app reflects it (UI shows pause icon change).

---

## Phase 4 — Packaging and UX polish

### Task 4.1: Pairing UX

**Objective:** Clean first-run and re-pair UX in Kodi's addon UI.

**Files:**
- Create: `resources/lib/ui/pairing_dialog.py`
- Modify: `resources/settings.xml` (add "Unpair now" button, "Debug")

**Steps:**
1. First run with no tokens and phone in pairing mode → modal dialog with large 6-char code and "Waiting for a device to pair…".
2. Pairing success → dialog auto-dismisses, `xbmc.gui.notification` "Paired with <device name>".
3. Settings "Unpair now" clears tokens, regenerates `deviceId`, forces next run to re-register.
4. On re-pair, phone-side should find the same screen name "Kodi" — keep `screenName` in settings so it's user-editable.

**Verification:** Fresh install on a RPi with LibreELEC pairs under 60 seconds; unpair/re-pair loop works.

### Task 4.2: Final packaging

**Objective:** Distributable zip + docs.

**Files:**
- Create: `build.sh` (bun? no — bash zip pipeline; respect `git-workflow-preferences` for commits).
- Create: `README.md`

**Steps:**
1. Bundle yt-dlp binary snapshot for `x86_64` + note about needing `arm` (aarch64) build for RPi — either fetch from yt-dlp releases at build time into `resources/bin/` for the target arch (parameterized build), or ship source-only and rely on `script.module.yt-dlp` fallback if the binary is missing.
2. Add `addon.xml` requires: `inputstream.adaptive`, `script.module.protobuf`.
3. Third-party notice: Lounge protocol is undocumented & reverse-engineered (credit pyytlounge, ytlounge, ytcast, SmartTube prior art in README).

**Verification:** Install zip on LibreELEC RPi: no missing-deps dialog, clean `xbmc.log`, end-to-end phone→play→yt-dlp→Kodi works.

---

## Test matrix (manual, since Kodi/Lounge can't be unit-faked meaningfully)

| Scenario | Expected |
|---|---|
| Cold install + pair (fresh phone, fresh kodi) | Pair within 60s; play video on request |
| Kodi restart alone | No re-pair; phone reconnects without user action |
| Phone restart alone | Idem |
| Network flake (<60s) | Session auto-resumes |
| Token revocation from phone | Kodi shows fresh code within ~30s |
| Cast a livestream | m3u8 opens, plays natively, no crash |
| Cast a queue of 3 videos | All queue, auto-advance, phone shows progress |
| seekTo before `playVideo` complete | Honored, no race |
| Two phones different accounts | Both can pair; last one controls |

## Risks / tradeoffs / open questions

- **Undocumented protocol** — Google rotates Lounge details occasionally; SmartTube/ytcast survive via community maintenance. Mitigation: isolate protocol in `lib/lounge/` for easy patching; post `protolab/` fixtures for regression testing.
- **yt-dlp binary arch mismatch on RPi** — plan for a `aarch64` yt-dlp fetch during build (yt-dlp publishes `yt-dlp_linux_aarch64`).
- **Kodi bundled python may lack `requests`/`httpx`** — resolve during Task 2.1 by choosing stdlib-only port vs `script.module.requests` dependency.
- **Protobuf version resolution** on Kodi — resolve during Task 1.2 (use Kodi's repo module if present; else vendor).
- **Phone app update breaking pairing shape** — out of our control; ambient risk of the approach, same for SmartTube.
- **Open question** — do we want the addon to also broadcast a mDNS/Bonjour name so the phone's "Watch on TV" auto-list picks it up too (DIAL-style)? Real Android TV app appears there without pairing. Probably yes but look at `ytcast`'s mdns bits or SmartTube's implementation before wiring it; it's a nice-to-have, not a blocker for manual code pairing.

## Sequencing summary

0. Protocol contract tests (pairing + resume + ytdlp) — all else is composable once these are green
1. Addon skeleton + persistence + protobuf plumbing
2. Lounge port + listener + reconnect
3. yt-dlp resolver + player bridge
4. UX + packaging

Recommended execution: one subagent per task via subagent-driven-development, tasks run against a real phone (user in the loop for pairing attempts).
