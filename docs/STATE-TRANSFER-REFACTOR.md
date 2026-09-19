# State-Transfer Refactor Plan (Lounge → Kodi)

**Status:** plan of record for the state-sync refactor. Written after a kanban run in which
ten rule cards were executed against this repo; the board has since been cleaned and
**archived in full** (see *Provenance* at the end). This document is the durable artefact:
it is what should be implemented, in what order, and how each step is proven.

**Design analysis:** `docs/lounge-state-transfer.pdf` (state transfer in the official Lounge
implementation, how this add-on does it, the measured wire behaviour, the five structural
causes of the sync bugs, and the target architecture).

---

## 1. Why

Every recurring cast-sync bug in this add-on is a divergence between the model the phone
holds and the model the receiver holds. They are not typos. They collapse into five
structural causes, and three of those are removed outright by the architecture below
rather than fought bug-by-bug:

| | Cause | Symptom |
|---|---|---|
| **C1** | Identity is carried, not owned — `listId` / `currentIndex` live in RAM and are re-derived ad hoc | TV advances, phone stays frozen on the previous item (bar 0:00, restart glyph); restart mid-queue re-announces a queue the phone cannot match |
| **C2** | No acknowledgement, no idempotency — commands are fire-and-forget, the relay can drop, delay or duplicate them | a play/pause/seek that never lands; duplicate `setPlaylist` triggering two resolves |
| **C3** | The same fact has four homes — phone model, bridge dict, Kodi's own playlist, info labels | queue drift, stale titles, index re-derivation fighting the phone's copy |
| **C4** | Reality is inferred twice a second — Kodi 21 delivers no player callbacks to the service process | pause flapping; a track change missed entirely; window/visualiser thrash |
| **C5** | Fan-out duplicates the model — two Lounge channels, each with its own listener, poster, counter and queue | doubled state traffic; interleaved `ofs` counters (106 descents measured in one capture) |

Measured on the 2026-09-14 device capture: **512 outbound RPC batches, 260 distinct `ofs`
values** (≈2× duplication), median inter-post gap 0.06 s then 2 s of silence, `cpn` constant,
and **4 of the 10** event families official clients emit.

**Target shape:** many event sources → one state owner → one publisher per channel → the
player is a projection, never a source.

```
 Lounge frames ─┐
 Kodi signals  ─┼─► SessionState (single owner, versioned) ─► Publisher ─► channel "cl"
 Resolver      ─┘        ▲                                      └─► channel "m"
                         │
              Kodi player/playlist/windows = PROJECTION (derived, never authoritative)
```

---

## 2. Rules

Ten rules. Each is stated as the invariant to hold, why it exists, and how it is proven.
Rules marked **[architectural]** delete a bug class; **[bounded]** only limit one, because
the protocol or Kodi's behaviour cannot be changed.

### R1 — One in-process owner of session state [architectural → C3]

Nothing else holds a copy. Kodi's playlist, the info labels, the title cache and the
resolver's bookkeeping stop being state and become derived views.

- Extract a `SessionState` data class owning exactly: playlist (ordered), `current_index`,
  `current_video_id`, `list_id`, position, duration, play state, lane/theme, volume, `cpn`
  — plus a monotonic `version` bumped on every accepted change.
- A snapshot accessor returns an **immutable detached copy** (readers must never get a live
  reference they could mutate).
- **Proof:** no assignment to those fields outside the state module (mechanical grep, quoted
  in the handoff); both gates green; behaviour unchanged — assertions may move from report
  strings to snapshots, nothing else.

### R2 — Persist the session record [architectural → C1]

Persist `listId`, playlist, `current_index`, current item, position and `cpn` — not just the
pairing tokens. A report is interpretable by the phone only against the `listId` + index it
last sent for that queue; a restart mid-queue currently destroys that key.

- Extend the persisted settings blob (`resources/lib/persistence.py`) with the session record;
  write-through, debounced; load before the first report is published.
- Generate a real per-playback `cpn` (today it is the constant `"kodi"`), persisted so a
  restart continues the same playback instance.
- Clear only when the queue is replaced by a new `listId`, or the session is unpaired.
- **Proof:** cast a queue → play to mid-item → restart the service with the same profile →
  the first published `nowPlaying`/`nowPlayingPlaylist` carries the **original** `listId`, a
  re-derived index, and the original `cpn`.

### R3 — Every state change is an event reduction [architectural → C3]

There is no direct field write. If it is not an event it cannot change state.

- Explicit event vocabulary covering the intents of the four existing writer paths
  (command dispatch, the 2 s position loop, resolver callbacks, window/queue-title repair).
- The reducer is **pure and idempotent**: replaying the same event twice yields the same
  state — this is what makes a duplicate relay burst harmless instead of a double resolve.
- Log one line per reduction: `state v<N> <event> -> <changed field groups> (<source>)`.
  That log is the debugging contract for everything downstream.
- **Proof:** duplicate `setPlaylist` advances the version once and resolves once; replaying the
  archived capture twice yields an identical, stable final snapshot.

### R4 — The player is a projection, never a source [architectural → C3, C4]

Kodi's playlist, windows and titles are rebuilt **on version change**, not polled for truth
and not repaired on a timer.

- One `apply_projection(snapshot)` reconciles playlist contents/order, the active window
  (video full-screen vs music visualiser) and item titles.
- Delete the timer-driven repair loops (`_repair_fullscreen_windows`, `_ensure_music_window`,
  `_music_window_until`, `_repair_music_lane_start`, title patching) or reduce survivors to
  log-only drift reporting.
- Kodi's own auto-advance is reported back as an event and becomes truth **through the
  reducer** — never by patching the projection behind the state's back.
- **Proof:** forced native auto-advance makes the projection follow the snapshot; a tick with
  no version change performs no mutation; existing lane/window scenarios still pass.
  Expect this to be the largest deletion in the codebase.

### R5 — Derive `currentIndex` at publication [architectural → C1]

Compute it from the stored queue at the moment of publishing; never carry it as a field.

- An index computed against a different list than the phone's is an identity mismatch even
  when `listId` is correct — so after any queue edit the phone silently discards reports.
- If the item is absent from the stored queue, publish the queue it *is* in; never a
  mismatched pair.
- **Proof:** `updatePlaylist` removing an earlier item → the reported index tracks the phone's
  list; TV-side pick → one publish with the correct index and the original `listId`; no
  published report ever carries an index outside `0..len(playlist)-1`.

### R6 — Signals carry confidence; `UNKNOWN` is publishable [bounded → C4]

Each fact has one source, and `unknown` is a legal value. Publishing "unknown" is
recoverable; publishing a guess is not.

- Declare per-fact sources: play state ∈ {command, player-clock, unknown};
  item ∈ {command, info-label, unknown}; position ∈ {player clock, none}.
- Retire or re-scope the 2-consecutive-stall and ≥2 s-advance heuristics so they **declare
  uncertainty** instead of asserting a state.
- Log every transition with its source, so a wrong guess is attributable.
- **Proof:** a stalled clock with no pause command does not produce a state the phone would
  contradict (no flap across the following ticks); resume from a real pause returns to
  `PLAYING` exactly once.

### R7 — Publish by snapshot version, not by RPC tag [architectural → C5]

- Wake on `version` change; emit **one batch per channel per changed field group**, computed
  by diffing against the last successfully published snapshot.
- Superseded versions are never posted — the newest state already contains everything the
  older one did. **No per-tag priority table, no `nowPlaying` exemption** (that exception
  exists only because the reports were independent fragments).
- One monotonic `ofs` sequence per channel, incremented under a single lock shared with the
  handshake path.
- A failed post leaves the version **dirty** so the next tick republishes the same state —
  the only retry this protocol permits, since reports are absolute.
- Keep a ≤1 Hz full-snapshot heartbeat as the phone's crash-recovery path, logged distinctly
  from change-driven posts.
- **Proof:** assertions on **counts**, not strings — one `setPlaylist` yields at most one
  batch per channel per changed group; per-channel `ofs` strictly monotonic, never
  duplicated; duplicate-relay burst still produces a single effect. Report the before/after
  post count (expect roughly half).

### R8 — Reconcile once per tick, through the reducer [bounded → C2, C4]

The tick stops being a writer and becomes a checker.

- Compare the snapshot against the player (playing file / `Player.FileNameAndPath` for the
  `?play=<id>` item, `getTime()`, duration, volume).
- Disagreements are emitted as events (`kodi_advanced`, `kodi_state_observed`, `kodi_stopped`)
  and resolved by the reducer. Log both sides:
  `reconcile: snapshot=<x> player=<y> -> event=<z>`.
- One reconciler only — a second poller is the failure mode this rule exists to prevent.
  Unresolvable disagreement publishes `UNKNOWN` (R6), never a guess.
- **Proof:** a deliberate drift produces exactly one corrective event and one publish
  carrying the adopted item with a re-derived index and the stored `listId`; a quiet tick
  produces zero versions and zero publishes.

### R9 — One model, N subscribers [architectural → C5]

A channel is a transport detail, never a second state.

- Keep one shared snapshot and one publisher **per channel**, each rendering that snapshot
  into its own `ofs` sequence and socket. Per-channel state (screenId, token, `ofs`,
  connection) may differ; playback state may not.
- Theme/lane (`cl` video vs `m` music) becomes a field of the snapshot, derived from the
  command's originating app — not a property that selects a separate state.
- Adding a channel adds a socket and an offset: no queue, no listener logic, no fan-out loop.
- **Proof:** with two channels bound, one state change produces exactly one batch per channel,
  both carrying the same `listId`/index/item, each channel's `ofs` independently monotonic
  (no interleaving descents); token expiry on one channel leaves the other session's pairing
  intact.

### R10 — Declare the vocabulary, log the gaps [bounded → UI staleness]

- A declared table of the ten official event families: name, payload fields, implemented?,
  and if not, why not.
- A coverage line at session start and on change:
  `vocabulary: 4/10 (missing: onAdStateChange, autoplayUpNext, …)`.
- Implement the two the receiver genuinely knows: `onAdStateChange`/`onAdPlaying` (ad state
  and skip availability — a working skip control needs it) and `autoplayUpNext` (derivable
  from the stored queue, since we resolve locally).
- **Proof:** emulator assertions that the new families fire at the right moments, and that
  up-next is omitted (per R6) when the receiver does not know. Mark which parts are
  guesswork about the phone's behaviour and need on-device confirmation.

---

## 3. Order of work

R1 and R3 are the foundation; the rest compose on them.

```
R1 ──► R3 ──┬─► R4 ──► R8 ──┐
            ├─► R7 ──► R9   │
            └─► R5         ├─► Reliability matrix (final gate)
R2 ─────────────► R6 ──────┘
R10 (independent; schedule when a quick win is wanted)
```

1. **R1** — behaviour-preserving refactor, no protocol change. Land first. ✅ `c28bebb`
2. **R3** — the event vocabulary + pure reducer + reduction log. ✅ `c28bebb`
3. **R4** — projections replace repair loops (biggest deletion; verify against the lane/window
   scenarios *before* removing anything). ✅ `a2da223`
4. **R7 → R9** — publisher by version, then one model with N channels. This is the change that
   halves the wire traffic. R7 ✅ `06a1f7a`; R9 ✅ (one snapshot, one publisher per channel; the
   connect/getNowPlaying fan-out loops in `service.py` and the direct `report_volume` reply in
   `lounge/listener.py` are gone — a handshake now re-sends the shared snapshot on each channel
   via `force_publish()`).
5. **R2, R5** — identity: persist the session record, derive the index at publication. R2 ✅
   `c28bebb`; R5 ✅ (publication derives the index from the queue it sends: `published_index()`
   in `session_state.py`, used by the `nowPlaying`/`nowPlayingPlaylist` builders, so a stale
   carried field or an out-of-range index can never reach the wire).
6. **R6 → R8** — confidence and reconciliation.
7. **R10** — vocabulary coverage, independently schedulable.
8. **Reliability matrix** — the acceptance gate, only meaningful after the rest.

### What not to do

- **Do not add reports to fix a phone that ignores reports.** If the phone discards a state it
  cannot map, more state changes nothing — the problem is identity (R2/R5), not volume.
- **Do not add a second poller.** The tick is already a guess; two guesses disagree.
- **Do not store another copy to paper over a disagreement** — that is how C3 grew.
- **Do not treat HTTP 200 as delivery.** Verify against the phone's rendering, or against a
  snapshot assertion in the emulator.

---

## 4. Acceptance: the reliability matrix

The final gate exercises **every user interaction available from both sides** and asserts
bidirectional convergence — not "no crash".

**Phone side (Lounge sender):** pair via TV code and via DIAL/SSDP; re-pair after unpair;
screenId mismatch must fail visibly. Attach/detach/reopen/switch device. `setPlaylist`
(single, music queue, 27-item, non-zero start index, cast-over-cast, startup seek).
`updatePlaylist` (append, remove before/current, reorder, clear, new `listId`). Transport
idempotence (`play`/`pause`/`pause`-while-paused/resume/`stopVideo`/`next`/`previous` at both
ends). Seek (forward, backward, past end → clamp, pre-playback, twice in one tick). Volume
(steps, mute/unmute, `getVolume`, while stopped). `getNowPlaying` in every state. Lane/theme
`cl`↔`m`, `skipAd`. Adversarial: duplicate relay bursts, back-to-back commands, split frames,
a dropped command (the reconciler must re-converge). Lifecycle: one-channel token expiry,
network flap, service restart mid-queue and mid-item.

**Kodi side:** remote/GUI play/pause (including `isPlaying()`-true-while-paused)/stop/seek/
next/prev/volume; queue edits in the Kodi UI; playback started from Kodi itself
(`?play=<id>`) → adopt and publish with the stored `listId` and a re-derived index; natural
end (auto-advance once, never twice; no `state=0` while loading; no phantom next); window/lane
switching with no mutation on an unchanged tick; standby and kill-mid-item.

**Assertions that make it a reliability test:** after quiescence, the receiver snapshot, the
last published report set and the simulated player state must agree on item, index, `listId`,
play state and position (within tolerance); per-channel `ofs` monotonic and batch budgets
asserted as counts; no out-of-range index or foreign `listId`; no report asserting a state the
player contradicts within the same tick; no surviving listener/poster/reconciler/DIAL threads;
plus a **fixed-seed randomised soak** of ≥200 mixed interaction sequences asserting convergence
at every quiescent point (a divergence must name the last event and the disagreeing field).

**Emulator cannot cover** (device checklist, not a gate): demuxer/codec behaviour, IA-vs-native
player differences, GUI rendering, `getTime()` jitter, multicast discovery. Drive the Kodi
side over **JSONRPC** (basic auth from `services.webserver*` in `guisettings.xml`).

---

## 5. Working agreement (what went wrong, so it does not repeat)

A previous execution of these rules produced work that had to be thrown away. The lessons are
binding for the next attempt and are mirrored in `AGENTS.md`:

- **Work in the checkout you are given.** Do not clone the repo elsewhere and do not edit any
  other copy. One card in that run reported success after doing its work in a second copy
  (`/tmp/zx`, plus a stale snapshot at `/opt/data/kodi-yt-caster` missing `audio_norm.py`), and
  the change never reached this repo. **Verify a claim against the tree, not the report.**
- **Commit on the branch and report the sha.** One card's publisher work sat uncommitted and was
  reported as done; a later card then blocked itself because the "missing" work was already in
  the tree, uncommitted.
- **One card, one concern.** Parallel cards edited the same files (`session.py`,
  `player_bridge.py`) with no coordination; the combined state failed both gates.
- **A gate result is only valid for the tree it was run in.** Paste the tails, and never report
  a gate from a different copy.

### Known-good state

Rules land on `feat/state-single-owner`, each with both gates green at its commit:

- `c28bebb` — **R1** (SessionState single owner, immutable snapshot) + **R2** (durable session
  record) + **R3** (event vocabulary + pure idempotent reducer + reduction log).
- `a2da223` — **R4** (player/window state derived from the projection; timer repair loops
  reduced to log-only). Both gates green.
- `06a1f7a` — **R7** (version-driven publication: wake on `SessionState.version`, diff against
  the last successfully published snapshot, one batch per channel per changed field group,
  monotonic `ofs`, dirty-on-failure, ≤1 Hz heartbeat). The legacy per-tag coalescing queue and
  its `nowPlaying` exemption are gone; `player_bridge` no longer runs naive report loops.
  Both gates green.
- **R9** (one model, N subscribers) — the two channels already shared one `StateOwner`; the
  remaining duplication was the per-channel fan-out loops: `on_connected` and
  `_handle_get_now_playing` in `service.py` built a `nowPlaying`/`nowPlayingPlaylist` report per
  session from the player, and `lounge/listener.py` answered `getVolume` with a hand-rolled
  `report_volume`. All three now go through the single publisher (`LoungeSession.force_publish()`
  + `KodiPlayerBridge.announce_state()`), so a handshake re-sends the one shared snapshot on each
  channel. `lane` is already a snapshot field, so no second state exists per channel. Tests:
  `test_r9_*` unit assertions (one batch per channel, identical identity, independent monotonic
  `ofs`) and `emulator/scenarios/test_one_model_n_channels.py`. Both gates green.

**R7 landed with two regression fixes that are part of it**, both in the projection path:

1. `apply_projection` used to clear the music playlist on any video-lane projection. R7 makes
   projections run on every version change (not just at play start), so the clear destroyed a
   music-app queue that Kodi was about to auto-advance. The clear is now limited to genuine
   video-lane snapshots (`lane != "m"`); a music-lane snapshot keeps its playlist and its
   cursor (`playlist._position = current_index`) so Kodi's native advance lands on the next
   queue item.
2. An earlier draft of the R7 card had also reworked the window-selection and visualiser-
   activation heuristics to key off `isPlayingAudio()`/`isPlayingVideo()`. That contradicts the
   device-verified quirk documented in `_music_lane_playing()` (the lingering outgoing video
   player reports the music item as video) and has been reverted to the R4 lane-based logic.

The remaining work is **R5, R6, R8, R9, R10** (`session.py`, `player_bridge.py` are the shared
files — schedule those cards one at a time, not in parallel).

---

## 6. Provenance

The ten rules and the reliability matrix were executed as kanban cards on the
`kodi-casting-plugin` board. That board has been **archived in full** (103 tasks) and exported
to `kanban-consolidate-20260918-*.tar.gz` under the operator's backups; this document is the
durable form of those specs. Nothing in this plan depends on the board existing.
