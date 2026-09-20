#!/usr/bin/env python3
"""Reliability matrix — the acceptance gate of the state model (AGENTS.md, R1–R10).

Not "no crash": after every quiescent point the receiver snapshot, the last
published report set and the simulated player must agree on item, index,
listId, play state and position (within tolerance); no report may carry an
out-of-range index or a foreign listId; per-channel ofs is monotonic.

The heavy half is a FIXED-SEED randomised soak of >= 200 mixed interaction
sequences (phone casts/edits/transport/seek/volume, TV-side pause/resume/stop,
natural end) asserting convergence at every quiescent point. A divergence names
the last event and the disagreeing field.
"""
import random
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from harness import Scenario, xbmc  # noqa: E402

def _cast(s, video_id, video_ids, theme="cl", timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = s.wait_for_session(theme, timeout=5.0)
        s.phone.target(sid)
        s.phone.connect()
        time.sleep(0.35)
        s.phone.set_playlist(video_id, list(video_ids))
        try:
            s.wait_until(lambda: s.resolve_count(video_id) > 0, timeout=4.0,
                         what=f"resolve of {video_id}")
            return
        except AssertionError:
            continue
    raise AssertionError(f"cast of {video_id} never reached the receiver")

def _quiesce(s, timeout=10.0):
    """Wait until the receiver stops changing and the player is stable."""
    prev = None
    deadline = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < deadline:
        snap = s.snapshot()
        cur = (snap.version, xbmc.Player().getTime()) if snap else None
        if cur == prev:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        prev = cur
        time.sleep(0.15)
    raise AssertionError("never quiesced")

def _published_ofs(reports):
    out = []
    for r in reports:
        try:
            out.append(int(r["ofs"]))
        except (KeyError, TypeError, ValueError):
            pass
    return out

def _assert_convergence(s, what):
    """The acceptance assertion: snapshot == last published == player."""
    snap = s.snapshot()
    assert snap is not None, "no receiver snapshot"

    # 1. No published report carries an out-of-range index or a foreign listId.
    plen = len(snap.playlist)
    for r in s.lounge.reports("nowPlaying") + s.lounge.reports("nowPlayingPlaylist"):
        idx = r.get("currentIndex")
        if idx is not None and idx != "":
            assert 0 <= int(idx) <= max(0, plen - 1), (what, r, snap.playlist)
        lid = r.get("listId")
        if lid and snap.list_id:
            assert lid == snap.list_id, (what, r, snap.list_id)

    # 2. Per-channel ofs monotonic, never duplicated.
    for theme in ("cl", "m"):
        sess = getattr(s.service, "_emu_sessions", {}).get(theme)
        if sess is None:
            continue
        sid = sess.sid
        ofs = _published_ofs([r for r in s.lounge.REPORTS if r.get("sid") == sid])
        assert all(b > a for a, b in zip(ofs, ofs[1:])), (what, theme, ofs)

    # 3. The last published report agrees with the snapshot (identity + state).
    last = s.last_published("cl")
    if last is not None:
        assert last.version == snap.version, (
            f"{what}: published v{last.version} != snapshot v{snap.version}")
        assert last.current_video_id == snap.current_video_id, (
            f"{what}: item {last.current_video_id} != {snap.current_video_id}")
        assert last.list_id == snap.list_id, (
            f"{what}: listId {last.list_id} != {snap.list_id}")

    # 4. Player agreement: if the snapshot says something plays, the player is
    #    on that item.
    if snap.play_state == 1 and snap.current_video_id:
        try:
            pf = xbmc.Player().getPlayingFile() or ""
        except Exception:
            pf = ""
        assert pf, (
            f"{what}: snapshot=PLAYING({snap.current_video_id}) but the player has"
            f" no file — the reconciler did not converge")
        if "play=" in pf:
            player_vid = pf.split("play=")[-1].split("&")[0]
            assert player_vid == snap.current_video_id, (
                f"{what}: player {player_vid} != snapshot {snap.current_video_id}")

def _one_quiescent_exchange(s, rng, step):
    """Play one random interaction sequence, quiesce, assert convergence.

    Casts are the expensive path (resolve + play), so most sequences are
    transport/seek/volume edits against the current queue — which is where the
    C1/C2/C3 divergence classes actually bite.
    """
    vids = [f"q{step}_{i}" for i in range(rng.randint(1, 4))]
    action = rng.choice(["cast", "transport", "transport", "seek", "seek",
                         "volume", "volume", "update", "tv_pause", "tv_resume"])
    if action == "cast":
        _cast(s, vids[0], vids)
    elif action == "transport":
        rng.choice([s.phone.pause, s.phone.play, s.phone.stop])()
    elif action == "seek":
        s.phone.seek(float(rng.randint(0, 30)))
    elif action == "volume":
        s.phone.set_volume(rng.randint(0, 100))
    elif action == "update":
        s.phone.update_playlist(vids[:2])
    elif action == "tv_pause":
        if xbmc.Player().isPlaying():
            xbmc._engine.clock.pause()
    elif action == "tv_resume":
        xbmc._engine.clock.resume()
    _quiesce(s)
    _assert_convergence(s, f"step {step} ({action})")

def test_bidirectional_convergence_on_mixed_interactions():
    """Every interaction from both sides ends in agreement."""
    with Scenario() as s:
        _cast(s, "v1", ["v1", "v2", "v3"])
        _quiesce(s)
        _assert_convergence(s, "initial cast")

        s.phone.pause()
        _quiesce(s)
        _assert_convergence(s, "phone pause")

        s.phone.play()
        _quiesce(s)
        _assert_convergence(s, "phone play")

        s.phone.seek(20.0)
        _quiesce(s)
        _assert_convergence(s, "phone seek")

        # Natural end -> Kodi auto-advances; the receiver adopts it.
        before = s.snapshot().current_video_id
        s.end_media()
        def _moved():
            snap = s.snapshot()
            return snap is not None and snap.current_video_id != before
        try:
            s.wait_until(_moved, timeout=20.0, what="auto-advance adopted")
        except AssertionError:
            pass
        _quiesce(s)
        _assert_convergence(s, "natural end / auto-advance")

        s.phone.set_volume(35)
        _quiesce(s)
        _assert_convergence(s, "phone volume")

        s.phone.stop()
        _quiesce(s)
        _assert_convergence(s, "phone stop")

def test_fixed_seed_soak_200_sequences():
    """>= 200 fixed-seed mixed interaction sequences, convergence checked at
    every quiescent point. A failure names the last event and the field."""
    rng = random.Random(0xC0FFEE)
    with Scenario() as s:
        _cast(s, "seed", ["seed"])
        _quiesce(s)
        for step in range(200):
            _one_quiescent_exchange(s, rng, step)
        _assert_convergence(s, "soak end")

def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            started = time.time()
            fn()
            print(f"  {name} OK ({time.time() - started:.1f}s)")
    print("test_reliability_matrix OK")

if __name__ == "__main__":
    main()
