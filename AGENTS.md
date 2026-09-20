# AGENTS.md

## The rule

**Never claim addon behavior is "working" or "done" without running tests that
exercise it.** Unit tests alone are not sufficient evidence for anything that
touches casting, playback, queueing, Lounge protocol, or lifecycle.

The emulator boots the real `service.py` + `resources/lib/*` unmodified
against a fake Kodi API and a mock Lounge server with a scripted phone — for
the subsystem under test it is the closest thing to "actually works" that can
run offline and deterministically.

## Test scope: run targeted, not the whole suite

**Run the targeted tests for what you changed — that is what backs a commit.**
Name the scenarios that cover your subsystem and run them directly:

```bash
python3 test_addon.py                                  # fast unit checks: always
python3 emulator/scenarios/test_remote_control.py      # + the scenarios your change touches
python3 emulator/scenarios/test_user_navigation_during_music.py
python3 emulator/scenarios/test_lane_switch_windows.py
```

Every scenario file is a plain script (`python3 <file>`; the harness lives in
`emulator/scenarios/harness.py`) and runs its own `test_*` functions, so a
two-scenario run is seconds-to-a-minute against the ten-minute full suite.

**Run the whole suite only before a version bump** (`emulator/run_all.py`) —
its job is finding bugs *you were not looking for*, across subsystems you did
not name:

```bash
python3 emulator/run_all.py   # pre-release sweep, exit 0 or you don't bump
```

Do not burn the full suite on every commit or iteration; do not skip it at
release time. CI runs both gates on every push
(`.github/workflows/release.yml`), so a red cross-cutting scenario still gets
caught.

## When you fix a bug

Add or extend a scenario that fails before the fix and passes after
(`emulator/scenarios/test_*.py`, discovered automatically by `run_all.py`).
A bug fixed without a regression scenario is not fixed. Assertions are
state-based with deadlines (`harness.wait_until`), never thread-ordering.
Demonstrate it both ways: the scenario failing on the pre-fix tree (stash the
fix, or `git checkout` the file) and passing after — that is the proof.

## Workspace discipline (kanban workers)

- **Work in the checkout you were given.** Do not clone the repo somewhere else and do not edit any other copy of it — another profile's `home/work/…`, a `/tmp` clone, or a stale snapshot elsewhere on the box. Edits made outside the tree the board handed you are invisible to the board and are treated as **not done**, no matter what the tests said in that other copy.
- **Commit on the current branch and report the commit sha.** Leave no uncommitted work behind; a card that ends with a dirty tree has not handed anything over.
- **A "done" claim must be reproducible here.** Run the targeted tests for your change (see *Test scope*) in this checkout and paste their tails. Never report a test result from a different copy; the full suite is the pre-release sweep, not a per-card gate.
- **One card, one concern.** Touch only the files your card names plus their tests; unrelated edits collide with sibling cards working in the same tree.

## Hard constraints

- **Do not modify addon runtime code (`service.py`, `resources/lib/*`) to
  make tests pass.** The emulator must exercise the addon as-is. If a
  scenario can't pass against stock addon code, either the emulator is wrong
  or you found a real bug — fix the right one.
- The monkeypatch inventory is deliberately short and listed in
  `emulator/README.md` (mock Lounge `BASE_URL`, deterministic resolve,
  fake titles, no-op downloader). Don't add more without strong reason.
- Keep scenarios deterministic: no real network, no sleeps > 5 s except
  documented retry windows. Live-network checks belong in `test_perf_live.py`
  (dev box) and `protolab/perf_probe.py` (on the target device — see
  `protolab/PERF.md`), and are never a gate.
- `emulator/` is never packaged (build.sh copies an explicit file list;
  verified: 0 entries in the zip).

## State model invariants (R1–R10)

Code comments, test names (`test_r5_*`) and old card specs refer to these by number, so they
are kept as vocabulary. Any change to `session_state.py`, `player_bridge.py`,
`lounge/session.py` or `service.py` has to keep them true.

| | Invariant |
|---|---|
| **R1** | One in-process owner of session state (`SessionState`): playlist, `current_index`, `current_video_id`, `list_id`, position, duration, play state, lane, volume, `cpn` + a monotonic `version`. Snapshots are immutable detached copies; Kodi's playlist, info labels and the title cache are derived views, not state. |
| **R2** | The session record is persisted (listId, playlist, index, item, position, `cpn`) so a restart mid-queue keeps an identity the phone can match. |
| **R3** | No direct field writes: every change is an `Event` through `StateOwner.apply` → the pure, idempotent reducer (replaying an event must not double-resolve), one log line per reduction. |
| **R4** | The Kodi player/playlist/windows/titles are a **projection** of the snapshot via `apply_projection`, never a source of truth. Version-driven for content — but see the bound below. |
| **R5** | `currentIndex` is derived at publication from the queue the report carries (`published_index()`), never carried as a field. |
| **R6** | Signals carry confidence: play state ∈ {command, player-clock, unknown}, item ∈ {command, info-label, unknown}. `UNKNOWN` is publishable; a guess is not. |
| **R7** | Publish on `version` change: one batch per channel per changed field group, one monotonic `ofs` per channel under one lock, superseded versions dropped, failed posts stay dirty, ≤1 Hz snapshot heartbeat. |
| **R8** | The 2 s tick is a **checker, not a writer**: `_reconcile_tick()` compares snapshot vs player and emits events (`reconcile: snapshot=<x> player=<y> -> event=<z>`). A quiet tick emits nothing. One reconciler only. |
| **R9** | One model, N subscribers: a channel is transport (its own socket/token/`ofs`); playback state may not diverge between channels. |
| **R10** | The official event vocabulary is declared with each family's payload and implemented/gap status, logged per session (`lounge/vocabulary.py`). |

### The music-window bound is load-bearing — do not delete it

`MUSIC_WINDOW_ENGAGE_SECONDS` / `_music_window_until` is not a leftover repair loop. It is
what stops the visualisation-window (12006) assertion from fighting the person holding the
remote:

- The snapshot version bumps on **every 2 s position poll**, so a version-driven window
  assertion runs forever.
- Kodi's `ActivateWindow` pops the duplicate out of the window history, so a forever-assertion
  closes whatever the user opens while music plays (home, the playlist view) — that shipped as
  "the receiver prevents opening the main menu and any other menu while music is playing".
- The deadline is armed **only** when a play is handed to Kodi on the music lane and when the
  music lane is entered. It exists to outlive Kodi's own late window pop (9–12 s after a
  video→audio switch). Outside it, the projection must issue no window builtin at all.
- Regression scenario: `emulator/scenarios/test_user_navigation_during_music.py`.

### What not to do

- **Do not add reports to fix a phone that ignores reports.** If the phone discards a state it
  cannot map, more state changes nothing — that is identity (R2/R5), not volume.
- **Do not add a second poller.** The 2 s tick is already the one reconciler; two guesses
  disagree.
- **Do not store another copy of a fact to paper over a disagreement** — that is how the
  four-homes drift started.
- **Do not treat HTTP 200 as delivery.** Verify against the phone's rendering or a snapshot
  assertion in the emulator.

## Maps

- Emulator design, coverage, and what it deliberately does NOT cover
  (demuxer, codecs, GUI, DIAL/SSDP multicast): `emulator/README.md`
- Full implementation plan: `emulator/PLAN.md`
- Fake Kodi API + Kodi quirks reproduced (`isPlaying()` while paused,
  `pause()` toggle, label snapshot at `add()`): `emulator/kodi_stub/`
- Mock Lounge wire format + phone sender: `emulator/lounge_server/`
