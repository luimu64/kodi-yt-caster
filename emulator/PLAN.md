# Full Kodi Emulator + Phone Casting Simulator — Implementation Plan

> **For Hermes:** Implement task-by-task, TDD where a behavior is testable. Do not modify addon runtime code (`service.py`, `resources/lib/*`) — the emulator must exercise the addon **as-is**, exactly like real Kodi does.

**Goal:** A stdlib-only, in-repo test rig that (a) fakes the Kodi Python API well enough to run the full addon service outside Kodi, and (b) fakes a casting **phone** (YouTube app sender) that speaks the real Lounge wire protocol against a mock Lounge server — so every cast scenario (pair, play, pause, seek, queue, dup-relay bursts, disconnect) is a deterministic scripted test.

**Architecture:** Three layers, all importable/runnable with plain `python3`, zero new dependencies:

1. `emulator/kodi_stub/` — a fake `xbmc`/`xbmcgui`/`xbmcaddon`/`xbmcplugin`/`xbmcvfs` package injected via `sys.path`. `xbmc.Player` is a *simulated* player: it has a playback clock thread, fires real callbacks (`onPlayBackStarted/Paused/Ended/...`), and pulls media from a file:// URL mapper so "playback" actually progresses.
2. `emulator/lounge_server/` — a mock YouTube Lounge backend (`/pairing/*`, `/bc/bind` long-poll with real length-delimited frame encoding) plus a `Phone` sender class that drives the receiver the way the real app does (connect, `setPlaylist`, `seekTo`, dup-relay bursts).
3. `emulator/scenarios/` — scripted end-to-end tests asserting on (i) what the fake player did and (ii) what the receiver reported *back* to Lounge (nowPlaying/onStateChange streams, ofs monotonicity).

**Tech Stack:** Python 3.9+ stdlib only (`http.server`, `threading`, `json`, `unittest`). Matches the addon's own constraint (Kodi bundles no third-party libs) and the repo convention of runnable scripts (`python3 test_x.py`, no pytest).

---

## Non-negotiable design constraints

These come from verified past bugs — the emulator is worthless if it gets them wrong:

- **`Player` and `executebuiltin` live on `xbmc`, NOT `xbmcgui`.** Fake `xbmcgui.Dialog/ListItem` only; put `Player`, `Monitor`, `PlayList`, `executebuiltin`, `getCondVisibility`, `log` on `xbmc`. A harness that gets this wrong dies on an unrelated `AttributeError` mid-path.
- **`Player.isPlaying()` returns True while paused.** The fake player MUST reproduce this Kodi quirk, or the pause/resume toggle-guard tests prove nothing.
- **`pause()` toggles.** Fake `Player.pause()` flips the paused state; pause-while-paused resumes.
- **Callbacks fire asynchronously** (Kodi invokes them from its own threads). The fake fires them from a timer thread, not inline in `play()` — inline firing hides races (e.g. the Ended-vs-Started manual-skip race).
- **`playlist.clear()` then immediate `add()` needs the ~100ms settle** in the real player API. The fake PlayList does NOT need to reproduce the quirk, but scenario authors must not write tests that depend on real-time quirks; those stay on-device.
- **`ListItem` labels snapshot at `playlist.add()` time** — the fake PlayList must copy the label at `add()`, not reference the live ListItem, or the queue-title patching tests prove nothing.
- **Long-poll framing:** mock `/bc/bind` responses use the verified length-delimited format (`<len>\n<json>\n`, parsed by the addon's own `parse_frames`). Reuse `resources.lib.lounge.session.parse_frames` in the *server* to self-check every frame it emits (round-trip guarantee).

## What the emulator deliberately does NOT cover

No demuxer, no codecs, no IA-vs-native-player difference, no GUI rendering, no real timing of `getTime()` jitter. Those are on-device. The emulator's contract: **logic and protocol correctness only.**

---

## Directory layout

```
emulator/
  PLAN.md                      <- this file
  README.md                    <- how to run, what it covers (Task 15)
  kodi_stub/
    __init__.py                <- install(path) / uninstall() sys.path hooks
    xbmc.py                    <- log, Monitor, Player, PlayList, executebuiltin, getCondVisibility, PLAYLIST_MUSIC, ...
    xbmcgui.py                 <- ListItem, Dialog, DialogProgress, NOTIFICATION_*
    xbmcaddon.py               <- Addon(settings dict + profile dir)
    xbmcplugin.py              <- setResolvedUrl/endOfDirectory recorders (plugin.py entry)
    xbmcvfs.py                 <- translatePath
    clock.py                   <- SimulatedPlaybackClock (thread-driven, adjustable rate)
  lounge_server/
    __init__.py
    server.py                  <- MockLoungeServer: pairing + /bc/bind + report capture
    frames.py                  <- frame encoder (inverse of parse_frames)
    phone.py                   <- Phone sender: high-level cast actions
  scenarios/
    __init__.py
    harness.py                 <- boots service.py inside kodi_stub, wires mock Lounge, exposes asserts
    test_cast_basic.py         <- pair -> setPlaylist -> playback starts, reports flow
    test_remote_control.py     <- pause/resume/seek/stop truth table incl. paused-isPlaying quirk
    test_dup_relay_burst.py    <- 2-5x duplicate setPlaylist => exactly one resolve/play
    test_queue_advance.py      <- natural end advances once; queue-mode Ended does not double-start
    test_disconnect.py         <- remoteDisconnected stops playback, sessions survive
    test_token_expiry.py       <- Lounge 400/404 => re-registration, listener survives
  run_all.py                   <- discover + run scenarios sequentially, exit code
```

Nothing here touches `build.sh` or ships in the addon zip (build copies an explicit file list — emulator/ is never included; verify in Task 15).

---

### Task 1: `kodi_stub` skeleton + `xbmc.log`/settings plumbing

**Objective:** injectable fake modules that the addon's guarded imports pick up.

**Files:**
- Create: `emulator/kodi_stub/__init__.py`, `xbmc.py`, `xbmcgui.py`, `xbmcaddon.py`, `xbmcvfs.py`, `xbmcplugin.py`

**Step 1 (failing probe):** from repo root run

```bash
python3 -c "
import sys; sys.path.insert(0, 'emulator/kodi_stub'); sys.path.insert(0, '.')
import service   # must import without ImportError and with KODI_AVAILABLE=True
"
```

Expected now: `KODI_AVAILABLE` is False (no stub yet) — that's the failure.

**Step 2 (implement):**

`kodi_stub/__init__.py`:
```python
import sys, os

def install():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

def uninstall():
    here = os.path.dirname(os.path.abspath(__file__))
    while here in sys.path:
        sys.path.remove(here)
    for m in ("xbmc", "xbmcgui", "xbmcaddon", "xbmcplugin", "xbmcvfs"):
        sys.modules.pop(m, None)
```

`xbmc.py` (minimal for this task):
```python
LOG_DEBUG, LOG_INFO, LOG_ERROR = 0, 1, 2
PLAYLIST_MUSIC = 0
LOG = []          # list of (level, message)

def log(msg, level=LOG_INFO):
    LOG.append((level, msg))

class Monitor:
    def __init__(self): self._abort = __import__("threading").Event()
    def abortRequested(self): return self._abort.is_set()
    def waitForAbort(self, timeout=None):
        return self._abort.wait(timeout or 1.0)
    def request_abort(self): self._abort.set()

def executebuiltin(cmd):
    BUILTIN.append(cmd)
BUILTIN = []

def getCondVisibility(cond):
    return _CONDITIONS.get(cond, False)
_CONDITIONS = {}
```

`xbmcaddon.py`:
```python
_SETTINGS = {}
_PROFILE = "/tmp/ytc-emulator-profile/"

class Addon:
    def __init__(self, addon_id=None): pass
    def getSetting(self, name): return _SETTINGS.get(name, "")
    def setSetting(self, name, value): _SETTINGS[name] = str(value)
    def getAddonInfo(self, key):
        return {"profile": _PROFILE}.get(key, "")
```

`xbmcgui.py`: `ListItem`/`Dialog`/`DialogProgress` stubs that record calls (full versions in Task 4). `xbmcvfs.py`: `translatePath = lambda p: p`. `xbmcplugin.py`: recorders for `setResolvedUrl`/`endOfDirectory`.

**Step 3 (verify):** re-run the Step 1 probe → imports clean. Also `python3 test_addon.py` still passes **without** the stub installed (no import leakage).

**Step 4 (commit):** `git commit -m "emulator: kodi_stub skeleton with settings/log plumbing"`

### Task 2: Simulated playback clock

**Objective:** a fake player core whose `getTime()` advances in real time (adjustable rate, seek/pause support) — the thing everything else builds on.

**Files:**
- Create: `emulator/kodi_stub/clock.py`
- Test: `emulator/scenarios/test_clock.py` (plain script with asserts)

**Behavior:**
```python
class SimulatedPlayback:
    def __init__(self, duration): ...
    def play(self, start_at=0.0)   # starts advancing position
    def pause(self)                # freezes position (is_playing stays True)
    def resume(self)
    def seek(self, t)              # jump, clamps [0, duration]
    def stop(self)                 # position frozen at 0, is_playing False
    @property state                # "playing" | "paused" | "stopped"
    def get_time() -> float        # advanced by wall clock * rate while playing
    rate = 1.0                     # crank to 50.0 to simulate a full 3-min track in 4s
```

**Tests (write first, run to see them fail):**
- playing 0.3s at rate=10 advances position ~3s ± tolerance 1s
- paused time does not advance; `get_time()` still returns last position
- seek clamps; stop resets is_playing

**Commit:** `emulator: simulated playback clock`

### Task 3: Fake `xbmc.Player` with async callbacks

**Objective:** the keystone — a Player subclass surface the addon's `SubclassPlayer` can extend, firing Kodi-lifecycle callbacks from a worker thread.

**Files:**
- Modify: `emulator/kodi_stub/xbmc.py`
- Test: `emulator/scenarios/test_fake_player.py`

**Design:**

```python
class Player:
    def __init__(self):
        self._media = None           # (url, listitem)
        self._clock = None
        self._listeners = []         # the addon's SubclassPlayer registers itself
        self.audio_only = False

    # --- API surface ---
    def play(self, url=None, listitem=None, windowed=False, playlist=None, startpos=-1):
        # playlist-mode: resolve synchronously like Kodi does by invoking the
        # addon's plugin entry? NO (Task 8 handles queue mode); here: direct URL.
        self._clock = SimulatedPlayback(duration=self._lookup_duration(url))
        self._clock.play()
        self._fire_async("onPlayBackStarted")

    def pause(self):                 # TOGGLE semantics
        if self._clock and self._clock.state == "playing":
            self._clock.pause(); self._fire_async("onPlayBackPaused")
        elif self._clock and self._clock.state == "paused":
            self._clock.resume(); self._fire_async("onPlayBackResumed")

    def stop(self):
        self._clock = None; self._fire_async("onPlayBackStopped")

    def seekTime(self, t):
        self._clock.seek(t)          # drop the seek when clock is "opening"
        # (simulate demuxer-not-ready by first-call no-op if configured)

    def isPlaying(self):  return self._clock is not None      # True WHILE PAUSED (quirk!)
    def isPlayingAudio(self): return self.isPlaying() and self.audio_only
    def getTime(self):    return self._clock.get_time() if self._clock else 0.0
    def getPlayingFile(self): return self._url

    # --- callback plumbing ---
    def _fire_async(self, event):
        # threading.Thread(target=..., daemon=True) -> listener.onPlayBackXxx()
        # subscribe via module registry: the addon's SubclassPlayer instance
        # is auto-discovered through xbmc.Player.__init__ registration.
```

Auto-advancement: the clock thread watches for position >= duration and fires `onPlayBackEnded` then resets — that is what drives natural queue advance.

`_lookup_duration(url)`: Task 5 wires the URL mapper; for now a dict `{url: seconds}` settable by the harness.

**Mandatory quirk tests (write first):**
1. `isPlaying()` is True after `pause()` — assert explicitly.
2. `pause(); pause()` resumes.
3. Callbacks arrive on a different thread than the caller (assert `threading.get_ident()` differs at least with a generous wait).
4. Clock reaching duration fires `onPlayBackEnded` exactly once.

**Commit:** `emulator: fake xbmc.Player with async lifecycle + Kodi quirks`

### Task 4: ListItem / Dialog / getCondVisibility completeness

**Objective:** enough GUI surface that `service.py` + `player_bridge.py` run their full paths (notifications, pairing dialog, visualizer activation).

**Files:**
- Modify: `emulator/kodi_stub/xbmcgui.py`, `xbmc.py`
- Test: `emulator/scenarios/test_gui_stub.py`

**Behavior:**
- `ListItem(label)` stores label + `setInfo(type, dict)` + `setArt(dict)` + `setPath` + `setProperty`/`setMimeType` — all gettable for assertions.
- `Dialog().notification(title, msg, icon, ms)` and `DialogProgress().create/update/iscanceled/close` append to `NOTIFICATIONS` list; `DialogProgress` has a settable `canceled` flag.
- `xbmc.getCondVisibility("Player.Paused")` is **derived from the active fake Player clock state** (not a manual dict): this is what makes the pause-guard tests meaningful.
- `xbmc.executebuiltin("ActivateWindow(12006)")` records to `BUILTIN` — scenario asserts visualizer activation from there.

**Tests:** pairing dialog show/dismiss non-blocking; notification recorded; `Player.Paused` condition tracks clock.

**Commit:** `emulator: GUI stubs + derived Player.Paused condition`

### Task 5: URL mapper — playable URLs resolve to durations

**Objective:** the addon plays localhost manifest URLs (`/yt_<id>.m3u8`) and plugin URLs; the fake player must map any URL it's handed to a duration so playback "runs".

**Files:**
- Modify: `emulator/kodi_stub/xbmc.py` (`_lookup_duration`)
- Test: extend `test_fake_player.py`

**Design:** module-level registry `MEDIA: dict[url_or_prefix, float]`. `_lookup_duration` does longest-prefix match; default 180s with a warning in `LOG`. The scenario harness registers the manifest-server URLs it expects the addon to publish (it can even query the real running `manifest_server` port file to build exact URLs).

**Commit:** `emulator: URL-to-duration media registry for fake playback`

### Task 6: Mock Lounge server — pairing + handshake + long-poll

**Objective:** a local HTTP server that reproduces the Lounge receiver-side contract, so `LoungeSession.handshake()` and `LoungeListener._listen_stream()` run unmodified against it.

**Files:**
- Create: `emulator/lounge_server/frames.py`, `server.py`
- Test: `emulator/scenarios/test_lounge_server.py`

**`frames.py` — encoder as inverse of the addon's parser:**
```python
def encode_frame(items: list) -> bytes:
    payload = json.dumps(items)
    return f"{len(payload)}\n{payload}".encode()
```
Self-check in test: `parse_frames(encode_frame(x))` round-trips `x` for handshake frames, commands, and reports.

**`MockLoungeServer` (ThreadingHTTPServer, 127.0.0.1:0):**
- `GET/POST /pairing/generate_screen_id` → fixed 26-char id (counter)
- `POST /pairing/get_lounge_token_batch` → JSON `{"screens":[{"loungeToken": ..., "expiration": <future>}]}` keyed by screen_id
- `POST /pairing/get_pairing_code`, `register_pairing_code` → 200, recorded (assert pairing happened)
- `POST /bc/bind` (handshake RID=1337): returns frame `[0,["c",SID]]` + `[1,["S",GSESSIONID]]`, stores session
- `GET /bc/bind` (long-poll RID=rpc): holds the connection open (chunked writes, `read1`-friendly), delivers queued command frames; between commands keeps the socket open — the handler must NOT return/close to mimic the real relay (otherwise the blocking-`read` bug class is invisible)
- **Report capture:** every POST body with `req0__sc` is parsed into `REPORTS: list[dict]` (action, ofs, videoId, state, currentTime...) — the assertion surface for receiver→phone sync
- `LoungeTokenExpiredError` simulation: respond 400 with "token" in body on demand (`expire_next = True`)

**Where does the addon point at it?** `resources/lib/lounge/client.py` uses module constant `BASE_URL`. The harness monkeypatches `resources.lib.lounge.client.BASE_URL = "http://127.0.0.1:<port>/api/lounge"` **before** importing `service` — no addon edits. Note in plan: this is the single accepted monkeypatch; everything else runs stock.

**Tests:** handshake yields SID/gsessionid; long-poll stays open with zero commands for 2s (connection alive); a queued command comes back parsed; ofs duplicates are observable in REPORTS (groundwork for Task 10).

**Commit:** `emulator: mock Lounge server with real frame encoding + report capture`

### Task 7: `Phone` — the sender

**Objective:** a fake YouTube app that drives the receiver through the mock server: connect, cast, control — including the *duplicate relay* behavior of the real backend.

**Files:**
- Create: `emulator/lounge_server/phone.py`
- Test: `emulator/scenarios/test_phone.py`

**API:**
```python
class Phone:
    def connect(self, name="Pixel 8")                    # -> server queues remoteConnected
    def disconnect(self)                                 # remoteDisconnected
    def set_playlist(self, video_id, video_ids, current_time=0, theme="cl")   # -> setPlaylist
    def update_playlist(self, video_ids)                 # updatePlaylist
    def play(self); def pause(self); def stop(self)
    def seek(self, new_time)
    def set_volume(self, v)
    def burst_set_playlist(self, n=3)                    # relay-duplicate simulation:
                                                         # same setPlaylist queued n times
                                                         # within ~100ms (the 2-5x real behavior)
    def get_now_playing(self)
```

Implementation: each method writes the properly-formed command frame into every bound session's queue via the server. The server can also *itself* relay duplicates (configurable) — cover both ends of the duplication spectrum.

**Test:** frames the phone emits parse through the addon's `parse_frames` and carry `_theme`-eligible dict payloads (theme stamping happens receiver-side; phone must NOT pre-stamp).

**Commit:** `emulator: Phone sender with dup-burst simulation`

### Task 8: Queue mode — plugin:// resolution

**Objective:** make music-queue playback (`plugin://...?play=<id>` URLs) work: the fake PlayList must invoke the addon's real `plugin.py` in a subprocess-like way when a playlist item starts.

**Files:**
- Modify: `emulator/kodi_stub/xbmc.py` (`PlayList`, `Player.play(playlist=...)`)
- Test: `emulator/scenarios/test_queue_mode.py`

**Design:** Kodi runs plugins in a separate process. Emulation choice (simple + faithful enough): run `plugin.py` via `subprocess.run([sys.executable, "plugin.py", "0", f"?play={vid}"])` with env pointing at the same port file, parse the emitted `setResolvedUrl` result from a small shim the stub injects through argv/exit-file. If the subprocess dance proves flaky in a day, fallback: call `plugin.main()` in-process with `sys.argv` patched and `xbmcplugin.setResolvedUrl` captured (document the divergence: no cross-process realism, but the localhost `/resolve/<id>` round-trip is still exercised for real).

- Fake `PlayList`: `add(url, listitem, pos)` **deep-copies the label at add time** (snapshot quirk), `remove(url)`, `size()`, `getposition()`, `clear()`; `Player.play(playlist, item, ..., startpos)` starts item at `startpos` and auto-fires `onPlayBackEnded` → next item Started, matching native auto-advance.

**Tests:** playlist add/clear snapshot semantics; auto-advance chain of 3 items fires Started×3 Ended×3 in order; label patch via remove+add visible in `PlayList.get_entry(idx).label`.

**Commit:** `emulator: fake PlayList + plugin resolution path`

### Task 9: Scenario harness — boot the whole service

**Objective:** one `harness.Scenario` that assembles kodi_stub + mock Lounge + real `service.run_service()` in a background thread and tears it down cleanly.

**Files:**
- Create: `emulator/scenarios/harness.py`
- Test: smoke — service boots, listeners bind, port file written, clean shutdown.

**Design:**

```python
class Scenario:
    def __enter__(self):
        kodi_stub.install(); populate settings (enable, screen_name, discovery ON with ephemeral dial port...)
        self.lounge = MockLoungeServer(); monkeypatch BASE_URL
        self.service_thread = Thread(target=service.run_service, daemon=True)
        start; wait until port file exists OR xbmc.LOG contains "receiver active" (poll, 15s cap)
        self.player_registry / self.phone = Phone(self.lounge)
    def __exit__(self):
        Monitor.request_abort(); join service thread (10s cap); kodi_stub.uninstall()
```

Discovery: DIAL binds a real port — use `dial_port=0`-style ephemeral via settings (`dial_port` setting accepts any int; server binds it; use a free port found by the harness). SSDP responder would try multicast — settings flag `enable_discovery=true` still starts SSDP; on CI containers multicast join usually succeeds harmlessly; if it proves flaky, set discovery false in harness default and test DIAL separately with discovery on. **Decide by behavior, not preference:** first implementation boots with discovery on; if CI shows failures, gate it behind `SCENARIO_DISCOVERY=1`.

Resolve speed: point the bridge at a **FakeYtDlpBridge equivalent** by pre-seeding the resolver cache? No — better: the harness registers tiny `.m3u8` bodies in the real manifest server via monkeypatched `publish`, and swaps `YtDlpBridge.resolve` at the class level with a deterministic fake producing those URLs (fast, offline, still exercises manifest/proxy code). Live yt-dlp resolves stay in `test_perf_live.py`, not here.

**Smoke test:** boot → assert port file, listener handshake observed on mock server, `Phone.connect` produces `remoteConnected` handling (notification in xbmcgui.NOTIFICATIONS) → teardown leaves no threads named LoungeListener/DIALService alive.

**Commit:** `emulator: scenario harness booting full service against mock Lounge`

### Task 10: Scenario — basic cast

**Files:** `emulator/scenarios/test_cast_basic.py`

**Script:** boot → phone.connect → phone.set_playlist("v1", ["v1","v2","v3"], current_time=15) → assert:
- fake player eventually `getPlayingFile()` contains `v1`'s URL
- REPORTS contain nowPlaying with videoId=v1 (the phone-withholds-until-nowPlaying contract)
- pending_seek applied: player clock within 2s of 15 (allow the retry loop to run — assert after ~3s wall)
- resolve happened exactly once (bridge call counter == 1)

**Commit:** `emulator: basic cast scenario`

### Task 11: Scenario — remote-control truth table

**Files:** `emulator/scenarios/test_remote_control.py`

Full matrix as separate test functions (the skill's truth-table rule — one run per combination):
- pause while playing → paused, onStateChange(PAUSED) reported
- pause while paused → still paused (no resume!)
- resume while paused → playing
- resume while stopped with current_video_id → restarts at last position
- seek while stopped → pending_seek consumed on next start
- stop → onPlayBackStopped + state STOPPED reported
- setVolume → onVolumeChanged report with clamped value

**Commit:** `emulator: remote control truth-table scenario`

### Task 12: Scenario — duplicate relay burst + queue races

**Files:** `emulator/scenarios/test_dup_relay_burst.py`, `test_queue_advance.py`

**dup burst:** `phone.burst_set_playlist(4)` → bridge call counter == 1, one play, no restart flicker (play-generation stays 1).
**queue advance:** cast 3-track queue at clock rate 50 (tracks end in ~4s):
- natural end: exactly 3 Started events, current advances v1→v2→v3, REPORTS' nowPlaying videoIds follow, no duplicate resolve of same id
- manual skip: harness fakes a Kodi-side pick (set player to v2 directly + fire Started) → Ended of v1 does NOT spawn extra play (the `_kodi_queue_mode`/manual-skip logic)
- phone removes upcoming track mid-play (update_playlist) → index resyncs, advance lands on the right id

**Commit:** `emulator: dup-burst + queue advance scenarios`

### Task 13: Scenarios — disconnect & token expiry

**Files:** `test_disconnect.py`, `test_token_expiry.py`

**disconnect:** phone.disconnect → player stopped within 2s, playback stopped report sent, sessions still bound; phone.connect again → casting works again (no restart of service).
**token expiry:** `lounge.expire_next_bind(400, "token")` → listener re-handshakes; for full expiry: force token invalid → assert re-registration POSTs to pairing endpoints, listener thread alive, **other session's tokens untouched** (the historic clear()-destroyed-other-session regression).

**Commit:** `emulator: disconnect + token expiry scenarios`

### Task 14: Runner + CI

**Files:** `emulator/run_all.py`; Modify: `.github/workflows/release.yml` (or a new `ci.yml`)

`run_all.py`: sequential scenario discovery (import module, run its `test_*` functions) with per-scenario fresh `Scenario()`; nonzero exit on failure; `--list` flag.

CI: add a job step `python3 emulator/run_all.py` after the build step in the existing workflow (keep it in the same job — no new permissions needed). Also run `python3 test_addon.py` there (currently CI only builds — free win).

**Commit:** `emulator: scenario runner + CI wiring`

### Task 15: README + packaging exclusion check

**Files:** `emulator/README.md`; verify build.

README: what's covered / not covered (the "no demuxer" disclaimer), how to run locally, how to write a scenario, the monkeypatch inventory (exactly one: `BASE_URL`), thread-leak debugging tip (`Scenario.__exit__` asserts no LoungeListener threads survive).

Packaging check:
```bash
./build.sh && python3 -m zipfile -l dist/plugin.service.ytlounge-cast/plugin.service.ytlounge-cast-<ver>.zip | grep -c emulator
```
Expected: `0`. (build.sh copies an explicit file list, so this should hold by construction — verify anyway.)

**Commit:** `emulator: README + packaging verification`

---

## Test / validation summary

- Every task ships its own runnable check (`python3 emulator/scenarios/test_x.py`), no pytest.
- Global gates: `python3 emulator/run_all.py` exit 0; `python3 test_addon.py` still green; zip contains no `emulator/`.
- Flakiness budget: scenarios must be deterministic — no real network (mock Lounge only), no sleeps >5s except where a documented retry loop requires ~3s (seek apply).

## Risks / open questions

- **Subprocess plugin emulation (Task 8)** is the one genuinely fiddly piece; in-process fallback specified. Decide by testing, not preference.
- **SSDP multicast in CI** may be blocked; plan gates it behind env flag if it flakes.
- **`service.py` reload loop** (`run_service` recursion on reload_requested) — scenarios should trigger reload at most once, or the teardown logic must handle nested monitors. Harness asserts thread-count baseline instead of chasing every path.
- Callback-timing races in the fake Player could *create* false positives the real Kodi never shows; mitigation: callbacks always async + scenario asserts are state-based (eventually-style with deadline), never ordering-of-threads-based.

## Estimate

~1 focused day for Tasks 1–9 (the rig), ~half day for 10–15 (scenarios + CI + docs). Individually committable; nothing blocks the addon's release train.
