# AGENTS.md

## The rule

**Never claim addon behavior is "working" or "done" without running the
emulator end-to-end suite first.** Unit tests alone are not sufficient
evidence for anything that touches casting, playback, queueing, Lounge
protocol, or lifecycle.

The emulator boots the real `service.py` + `resources/lib/*` unmodified
against a fake Kodi API and a mock Lounge server with a scripted phone —
it is the closest thing to "actually works" that can run offline and
deterministically.

## Gates before any "it works" claim

```bash
python3 test_addon.py        # fast unit checks
python3 emulator/run_all.py  # REQUIRED for behavior claims — exit 0 or it's not working
```

Both must pass. For work on one subsystem, the relevant scenario can be run
directly and iterated on:

```bash
python3 emulator/scenarios/test_remote_control.py   # any test_*.py, plain python3
```

CI runs both gates on every push (`.github/workflows/release.yml`).

## When you fix a bug

Add or extend a scenario that fails before the fix and passes after
(`emulator/scenarios/test_*.py`, discovered automatically by `run_all.py`).
A bug fixed without a regression scenario is not fixed. Assertions are
state-based with deadlines (`harness.wait_until`), never thread-ordering.

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

## Maps

- Emulator design, coverage, and what it deliberately does NOT cover
  (demuxer, codecs, GUI, DIAL/SSDP multicast): `emulator/README.md`
- Full implementation plan: `emulator/PLAN.md`
- Fake Kodi API + Kodi quirks reproduced (`isPlaying()` while paused,
  `pause()` toggle, label snapshot at `add()`): `emulator/kodi_stub/`
- Mock Lounge wire format + phone sender: `emulator/lounge_server/`
