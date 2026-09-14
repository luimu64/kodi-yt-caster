"""Lounge traffic recorder: capture a REAL phone session into a replayable
fixture, so the emulator is built from ground truth instead of guesses.

WHY: the emulator previously sent a hand-written approximation of the app's
commands and never modelled `listId`, so it could not reproduce the
app-stuck-on-old-track desync. Capturing real traffic closes that gap.

HOW IT WORKS: this installs thin wrappers around the two protocol boundaries
the addon already owns —
  * inbound:  LoungeListener._handle_command   (every command from the phone)
  * outbound: LoungeSession.post_action        (every report we send back)
Each call is appended to a JSONL event stream with a timestamp, so the
interleaving (and the causal order that matters for desync bugs) is preserved.

Zero-dependency, no TLS interception, no changes to the wire format: the data
seen here is exactly what the addon parses and emits after decryption.

USAGE (on the Kodi box, from the addon directory):
    python3 tools/capture_traffic.py start     # arm recording
    # ... cast from the phone, let a few songs auto-advance, scrub, pause ...
    python3 tools/capture_traffic.py stop      # write the fixture
    python3 tools/capture_traffic.py status

Then pull the file back and replay it:
    python3 tools/capture_traffic.py replay <fixture.jsonl>
"""
import json
import os
import sys
import time

# Capture artefacts live beside the addon so they survive a service restart
# (the recorder is re-armed by a marker file, not by staying resident).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ADDON_ROOT = os.path.dirname(_HERE)
_CAPTURE_DIR = os.environ.get("YTLOUNGE_CAPTURE_DIR") or os.path.join(_ADDON_ROOT, "captures")
_ARMED = os.path.join(_CAPTURE_DIR, ".armed")
_EVENTS = os.path.join(_CAPTURE_DIR, "session.jsonl")


def _log(msg):
    sys.stderr.write(f"[capture] {msg}\n")
    sys.stderr.flush()


def _now():
    return time.time()


def install(*, active_flag=True):
    """Hook the protocol boundaries. Import-guarded: safe to call from the
    addon service on every boot, cheap and idempotent."""
    try:
        from resources.lib.lounge import listener as listener_mod
        from resources.lib.lounge import session as session_mod
    except Exception as exc:  # pragma: no cover - only when imported outside Kodi
        _log(f"cannot import lounge modules: {exc}")
        return False

    if getattr(listener_mod.LoungeListener, "_ytl_capture_hooked", False):
        return True

    os.makedirs(_CAPTURE_DIR, exist_ok=True)

    def _emit(kind, payload):
        if not os.path.exists(_ARMED):
            return
        rec = {"t": round(_now(), 4), "kind": kind}
        rec.update(payload)
        try:
            with open(_EVENTS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        except Exception as exc:
            _log(f"write failed: {exc}")

    # --- inbound: commands from the phone ---------------------------------
    orig_handle = listener_mod.LoungeListener._handle_command

    def _handle_command(self, name, data):
        _emit("in", {
            "cmd": name,
            "data": data,
            "sid": getattr(self.session, "sid", None),
            "theme": getattr(self.session, "theme", None),
            "code": getattr(self.session, "last_code", None),
        })
        return orig_handle(self, name, data)

    listener_mod.LoungeListener._handle_command = _handle_command

    # --- outbound: reports we send back -----------------------------------
    orig_post = session_mod.LoungeSession.post_action

    def _post_action(self, sc, data):
        _emit("out", {
            "sc": sc,
            "data": dict(data or {}),
            "sid": getattr(self, "sid", None),
            "theme": getattr(self, "theme", None),
            # ofs is the Lounge sequence number: gaps here mean a report was
            # coalesced/dropped, which is itself a desync suspect.
            "ofs": getattr(self, "ofs", None),
        })
        return orig_post(self, sc, data)

    session_mod.LoungeSession.post_action = _post_action

    listener_mod.LoungeListener._ytl_capture_hooked = True

    # Register a playback-state sampler: the phone's view also depends on
    # what Kodi actually did, so record the bridge's transitions too.
    try:
        from resources.lib import player_bridge as pb

        if not getattr(pb.KodiPlayerBridge, "_ytl_capture_hooked", False):
            orig_started = pb.KodiPlayerBridge._on_playback_started
            orig_ended = pb.KodiPlayerBridge._on_playback_ended

            def _started(self):
                _emit("bridge", {
                    "event": "onPlayBackStarted",
                    "video_id": getattr(self, "current_video_id", None),
                    "index": getattr(self, "current_index", None),
                    "list_id": getattr(self, "list_id", None),
                })
                return orig_started(self)

            def _ended(self):
                _emit("bridge", {
                    "event": "onPlayBackEnded",
                    "video_id": getattr(self, "current_video_id", None),
                    "index": getattr(self, "current_index", None),
                    "list_id": getattr(self, "list_id", None),
                })
                return orig_ended(self)

            pb.KodiPlayerBridge._on_playback_started = _started
            pb.KodiPlayerBridge._on_playback_ended = _ended
            pb.KodiPlayerBridge._ytl_capture_hooked = True
    except Exception as exc:
        _log(f"bridge sampler not installed: {exc}")

    _log(f"hooked; dir={_CAPTURE_DIR}")
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_start():
    os.makedirs(_CAPTURE_DIR, exist_ok=True)
    if os.path.exists(_EVENTS):
        os.remove(_EVENTS)
    with open(_ARMED, "w") as fh:
        fh.write(str(_now()))
    _log("armed — cast from the phone now")


def cmd_stop():
    if os.path.exists(_ARMED):
        os.remove(_ARMED)
    if not os.path.exists(_EVENTS):
        _log("no events captured")
        return
    n = sum(1 for _ in open(_EVENTS))
    _log(f"disarmed; {n} events in {_EVENTS}")


def cmd_status():
    armed = os.path.exists(_ARMED)
    n = sum(1 for _ in open(_EVENTS)) if os.path.exists(_EVENTS) else 0
    _log(f"armed={armed} events={n} file={_EVENTS}")


def cmd_replay(path):
    """Summarise a fixture: the command/report skeleton the emulator must
    reproduce. Prints command names and report types in causal order."""
    if not os.path.exists(path):
        _log(f"not found: {path}")
        return 1
    seen = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec["kind"] == "in":
            key = f"IN  {rec['cmd']}"
        elif rec["kind"] == "out":
            key = f"OUT {rec['sc']}"
        else:
            key = f"BRG {rec.get('event')}"
        seen[key] = seen.get(key, 0) + 1
    for k in sorted(seen):
        print(f"{seen[k]:5d}  {k}")
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    action = argv[1]
    if action == "start":
        cmd_start()
    elif action == "stop":
        cmd_stop()
    elif action == "status":
        cmd_status()
    elif action == "replay":
        return cmd_replay(argv[2] if len(argv) > 2 else _EVENTS)
    elif action == "hook":
        install()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
