#!/usr/bin/env python3
"""One runnable check for YouTube Lounge Cast Receiver."""

import os
import shutil
import time
import unittest
from typing import Any, Dict
from resources.lib.lounge.session import parse_frames, LoungeSession
from resources.lib.lounge.client import BASE_URL
from resources.lib.persistence import SessionStore
from resources.lib.resolver import VideoResolver
from resources.lib.player_bridge import KodiPlayerBridge, PlayerState
from resources.lib.lounge.listener import CommandDispatcher
from resources.lib.session_state import SessionState, StateOwner
from dataclasses import replace


def _set_state(player, **kw):
    """Test fixture: replace the owner's session state with the given fields
    (no direct field write; mirrors how R2/R3 load/restore a snapshot)."""
    player.owner = StateOwner(replace(player.owner._state, **kw))


class MockYtDlpBridge:
    def resolve(self, video_id: str):
        return {
            "id": video_id,
            "title": f"Test Video {video_id}",
            "duration": 120,
            "thumbnail": "http://example.com/thumb.jpg",
            "playable_url": f"http://example.com/stream/{video_id}.mp4",
            "stream_type": "progressive",
        }


def test_frame_parsing():
    payload = '[[0,["c","TEST_SID","",8]],[1,["S","TEST_GSESSIONID"]]]'
    raw_payload = f"{len(payload)}\n{payload}\n"
    frames, consumed = parse_frames(raw_payload)
    assert len(frames) == 2
    assert frames[0] == (0, "c", "TEST_SID")
    assert frames[1] == (1, "S", "TEST_GSESSIONID")
    assert consumed == len(raw_payload)


def test_frame_parsing_chunked():
    """A frame split across reads must not lose its tail (regression: the
    listener used to discard partial trailing frames)."""
    import json as _json
    f1 = _json.dumps([[0, ["c", "SID"]]])
    f2 = _json.dumps([[1, ["S", "GS"]]])
    stream = f"{len(f1)}\n{f1}{len(f2)}\n{f2}"

    # simulate listener reads: buffer grows, consume only what's safe
    got = []
    buf = ""
    for i in range(len(stream)):
        buf += stream[i]
        commands, consumed = parse_frames(buf)
        if consumed:
            buf = buf[consumed:]
        got.extend(commands)
    assert got == [(0, "c", "SID"), (1, "S", "GS")], got


def test_frame_parsing_incomplete_tail():
    """A truncated trailing frame stays buffered, unconsumed."""
    import json as _json
    f1 = _json.dumps([[0, ["c", "SID"]]])
    partial = f"{len(f1)}\n{f1}12\n[[1,["
    commands, consumed = parse_frames(partial)
    assert commands == [(0, "c", "SID")]
    assert partial[consumed:] == "12\n[[1,[", partial[consumed:]


def test_persistence():
    test_file = ".test_session.json"
    if os.path.exists(test_file):
        os.remove(test_file)
    store = SessionStore(fallback_path=test_file)
    data = store.load()
    assert "device_id" in data
    dev_id = data["device_id"]
    store.save({"device_id": dev_id, "screen_id": "s123", "lounge_token": "tok456"})
    reloaded = store.load()
    assert reloaded["screen_id"] == "s123"
    assert reloaded["lounge_token"] == "tok456"

    # Session record tests: back-compat loading old blob with no session fields yields nulls
    empty_sess = store.load_session()
    for field in ("list_id", "playlist", "current_index", "current_video_id", "position", "cpn", "theme"):
        assert empty_sess[field] is None, f"Expected {field} to be None"

    # Save and round-trip populated session record
    record = {
        "list_id": "PLtest123",
        "playlist": ["v1", "v2"],
        "current_index": 0,
        "current_video_id": "v1",
        "position": 12.34,
        "cpn": "cpn_test",
        "theme": "cl",
    }
    store.save_session(record, debounce=False)
    loaded_sess = store.load_session()
    for field in ("list_id", "playlist", "current_index", "current_video_id", "position", "cpn", "theme"):
        assert loaded_sess[field] == record[field]

    # Debounce verification
    writes = 0
    orig_save = store._save_raw_locked

    def counted_save(data: Dict[str, Any]) -> None:
        nonlocal writes
        writes += 1
        orig_save(data)

    store._save_raw_locked = counted_save
    store.debounce_interval = 0.1
    for i in range(10):
        rec = dict(record)
        rec["position"] = float(i)
        store.save_session(rec, debounce=True)
    assert writes == 0
    time.sleep(0.2)
    assert writes == 1
    assert store.load_session()["position"] == 9.0
    store._save_raw_locked = orig_save

    store.clear()
    cleared = store.load()
    assert "screen_id" not in cleared
    assert cleared["device_id"] == dev_id
    cleared_sess = store.load_session()
    assert cleared_sess["list_id"] is None
    if os.path.exists(test_file):
        os.remove(test_file)


def test_session_state_module():
    """Run all unit tests in tests/test_session_state.py as part of test_addon.py."""
    import unittest
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_session_state")
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    assert result.wasSuccessful(), f"test_session_state failed: {result.errors + result.failures}"



def test_resolver_cache():
    mock_bridge = MockYtDlpBridge()
    resolver = VideoResolver(bridge=mock_bridge)
    info1 = resolver.resolve("vid1")
    assert info1["title"] == "Test Video vid1"
    # Should serve from cache
    info2 = resolver.resolve("vid1")
    assert info1 == info2


def test_player_bridge_queue():
    session = LoungeSession("s1", "t1", "d1")
    mock_bridge = MockYtDlpBridge()
    resolver = VideoResolver(bridge=mock_bridge)
    player = KodiPlayerBridge(session=session, resolver=resolver)

    player.set_playlist({"videoId": "v1", "videoIds": "v1,v2,v3", "currentTime": 15})
    assert player.playlist == ["v1", "v2", "v3"]
    assert player.current_index == 0

    player.update_playlist({"videoIds": "v1,v3"})
    assert player.playlist == ["v1", "v3"]


def test_player_bridge_index_resync():
    """Removing items before the current position must not desync the index
    (regression: queue used to die early)."""
    session = LoungeSession("s1", "t1", "d1")
    resolver = VideoResolver(bridge=MockYtDlpBridge())
    player = KodiPlayerBridge(session=session, resolver=resolver)

    player.set_playlist({"videoId": "v4", "videoIds": "v1,v2,v3,v4", "currentTime": 0})
    assert player.current_index == 3
    # phone removes v1 and v2 from the queue while v4 plays
    player.update_playlist({"videoIds": "v3,v4"})
    assert player.current_index == 1  # v4 is now position 1 of [v3, v4]

    # and after v4 ends, no advance past the end
    player._on_playback_ended()
    time.sleep(0.2)  # allow spawned play threads to settle


def test_play_generation_supersedes():
    """A stale resolve completing late must not overwrite a newer request."""
    import threading
    session = LoungeSession("s1", "t1", "d1")
    resolver = VideoResolver(bridge=MockYtDlpBridge())
    player = KodiPlayerBridge(session=session, resolver=resolver)

    player.set_playlist({"videoId": "v1", "videoIds": "v1", "currentTime": 0})
    player.set_playlist({"videoId": "v2", "videoIds": "v2", "currentTime": 0})
    time.sleep(0.3)
    assert player.current_video_id == "v2"


def test_ytdlp_downloader_metadata():
    from resources.lib.ytdlp_downloader import get_platform_asset_name, get_binary_destination
    asset, fn = get_platform_asset_name()
    assert "yt-dlp" in asset
    assert "yt-dlp" in fn
    dest = get_binary_destination()
    assert dest.endswith(fn)


def test_ytdlp_asset_prefers_importable_zipapp():
    """Regression: the aarch64 asset used to be the 38MB PyInstaller build, which
    resources/lib/ytdlp_inproc cannot import (zipimport needs a zipapp) — so the
    in-process resolver never engaged and every resolve paid a subprocess spawn
    (measured on a Pi 4: 4.7s vs 1.9s cold, 1.4s warm)."""
    from resources.lib import ytdlp_downloader as dl

    real_system, real_machine = dl.platform.system, dl.platform.machine
    real_which, real_exe = dl.shutil.which, dl.sys.executable
    try:
        dl.platform.system = lambda: "Linux"
        dl.platform.machine = lambda: "aarch64"
        dl.shutil.which = lambda name: "/usr/bin/python3" if name == "python3" else None
        assert dl.get_platform_asset_name() == ("yt-dlp", "yt-dlp"), "zipapp must be preferred where an interpreter exists"
        assert dl.preferred_asset_is_zipapp()

        # No interpreter: the native build is the only thing that can run.
        dl.shutil.which = lambda name: None
        dl.sys.executable = "/usr/bin/kodi"
        assert dl.get_platform_asset_name() == ("yt-dlp_linux_aarch64", "yt-dlp")
        assert not dl.preferred_asset_is_zipapp()
    finally:
        dl.platform.system, dl.platform.machine = real_system, real_machine
        dl.shutil.which, dl.sys.executable = real_which, real_exe


def test_ytdlp_reinstall_detects_non_importable_binary():
    """A pre-zipapp install (ELF) must be recognised as stale and replaced once."""
    import tempfile
    import zipfile as _zipfile
    from resources.lib import ytdlp_downloader as dl

    with tempfile.TemporaryDirectory() as d:
        elf = os.path.join(d, "yt-dlp")
        with open(elf, "wb") as f:
            f.write(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 4096)   # not a zip
        zipapp = os.path.join(d, "yt-dlp-zip")
        with _zipfile.ZipFile(zipapp, "w") as z:
            z.writestr("yt_dlp/__init__.py", "")

        real_pref = dl.preferred_asset_is_zipapp
        try:
            dl.preferred_asset_is_zipapp = lambda: True
            assert dl.needs_reinstall(elf), "ELF install must be upgraded to the zipapp"
            assert not dl.needs_reinstall(zipapp), "an existing zipapp is current"
            assert not dl.needs_reinstall(os.path.join(d, "absent")), "missing file is not a reinstall"

            # When the native build is the preferred asset, an ELF is current.
            dl.preferred_asset_is_zipapp = lambda: False
            assert not dl.needs_reinstall(elf)
        finally:
            dl.preferred_asset_is_zipapp = real_pref


def test_ytdlp_download_keeps_old_binary_when_probe_fails():
    """A downloaded binary that does not run (e.g. zipapp with no interpreter)
    must be rejected, never installed over a working yt-dlp."""
    import tempfile
    import urllib.request as _urlrequest
    from resources.lib import ytdlp_downloader as dl

    class _Resp:
        headers = {"Content-Length": str(2 * 1024 * 1024)}

        def __init__(self):
            self._left = 2 * 1024 * 1024

        def read(self, n=-1):
            if self._left <= 0:
                return b""
            chunk = b"\x00" * min(n, self._left)
            self._left -= len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "yt-dlp")
        with open(dest, "wb") as f:
            f.write(b"OLD-WORKING-BINARY")

        real_urlopen = _urlrequest.urlopen
        real_dest = dl.get_binary_destination
        try:
            _urlrequest.urlopen = lambda *a, **k: _Resp()
            dl.get_binary_destination = lambda: dest
            try:
                dl.download_ytdlp(force=True, show_ui=False)
                raise AssertionError("probe failure must raise")
            except AssertionError:
                raise
            except Exception:
                pass
        finally:
            _urlrequest.urlopen = real_urlopen
            dl.get_binary_destination = real_dest

        with open(dest, "rb") as f:
            assert f.read() == b"OLD-WORKING-BINARY", "failed candidate must not replace the working binary"
        assert os.listdir(d) == ["yt-dlp"], f"temp download left behind: {os.listdir(d)}"


def test_hls_master_generation():
    from resources.lib.ytdlp_bridge import build_hls_master_manifest
    mock_formats = [
        {"format_id": "234", "url": "https://example.com/audio.m3u8", "vcodec": "none", "acodec": "mp4a", "format_note": "Audio High"},
        {"format_id": "312", "url": "https://example.com/1080p.m3u8", "vcodec": "avc1.640028", "height": 1080, "width": 1920, "fps": 60, "tbr": 4500},
        {"format_id": "230", "url": "https://example.com/360p.m3u8", "vcodec": "avc1.4D401E", "height": 360, "width": 640, "fps": 30, "tbr": 600},
    ]
    manifest_path = build_hls_master_manifest(mock_formats, "test_vid")
    assert manifest_path is not None
    # Served over the localhost manifest server, not a file path.
    assert manifest_path.startswith("http://127.0.0.1:")
    import urllib.request
    with urllib.request.urlopen(manifest_path, timeout=5) as resp:
        content = resp.read().decode("utf-8")
    assert "#EXT-X-STREAM-INF" in content
    assert "RESOLUTION=1920x1080" in content
    assert "RESOLUTION=640x360" in content
    assert "#EXT-X-MEDIA:TYPE=AUDIO" in content



def test_youtube_music_session():
    session_m = LoungeSession("s_music", "token_m", "dev_123", "Kodi Music", theme="m")
    params = session_m._base_params()
    assert params["theme"] == "m"
    assert "mus" in params["capabilities"]
    assert "que" in params["capabilities"]


def test_dial_and_ssdp_discovery():
    from resources.lib.discovery.ssdp import get_local_ip
    from resources.lib.discovery.dial_server import DIALServer
    import urllib.request

    ip = get_local_ip()
    assert ip and len(ip.split(".")) == 4

    paired_codes = []
    server = DIALServer(
        port=0,  # bind ephemeral free port
        device_uuid="test-uuid",
        friendly_name="Kodi Test Discovery",
        screen_id="screen_123",
        on_pairing_code=lambda code, theme="": paired_codes.append((code, theme)),
    )
    port = server.server_address[1]

    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        # 1. Test /ssdp/device-desc.xml
        desc_url = f"http://127.0.0.1:{port}/ssdp/device-desc.xml"
        with urllib.request.urlopen(desc_url, timeout=5) as resp:
            data = resp.read().decode("utf-8")
            assert "Kodi Test Discovery" in data
            assert "urn:dial-multiscreen-org:device:dial:1" in data

        # 2. Test /apps/YouTube GET
        app_url = f"http://127.0.0.1:{port}/apps/YouTube"
        with urllib.request.urlopen(app_url, timeout=5) as resp:
            app_data = resp.read().decode("utf-8")
            assert "<screenId>screen_123</screenId>" in app_data

        # 3. Test /apps/YouTube POST pairing code
        post_data = urllib.parse.urlencode({"pairingCode": "123-456-789-000", "theme": "cl"}).encode("utf-8")
        req = urllib.request.Request(app_url, data=post_data)
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
            loc = resp.headers.get("Location")
            assert "/apps/YouTube/run" in loc
            assert paired_codes == [("123-456-789-000", "cl")]
    finally:
        server.shutdown()
        server.server_close()


def test_pairing_dialog_non_blocking():
    from resources.lib.ui.pairing_dialog import PairingDialog
    import time
    dlg = PairingDialog("123-456-789-000", "Kodi Test")
    t0 = time.time()
    dlg.show()
    elapsed = time.time() - t0
    # show() must return immediately (< 0.1s) without blocking
    assert elapsed < 0.2
    assert dlg._thread is not None
    dlg.dismiss()
    dlg._thread.join(timeout=2.0)
    assert not dlg._thread.is_alive()


def test_actions_module():
    import actions
    assert hasattr(actions, "action_show_pairing")
    assert hasattr(actions, "action_update_ytdlp")


def test_kodi_queue_mode_ended_no_self_advance():
    """In Kodi-queue (music playlist) mode, Ended must NOT spawn our own
    play for the next item: Kodi's playlist auto-advances natively, and a
    second _play_video races it into a restart-from-0 (regression test)."""
    session = LoungeSession("s1", "t1", "d1")
    player = KodiPlayerBridge(session=session, resolver=VideoResolver(bridge=MockYtDlpBridge()))
    player.set_playlist({"videoId": "v1", "videoIds": "v1,v2", "currentTime": 0})
    time.sleep(0.3)
    with player._lock:
        player._kodi_queue_mode = True
        before = player._play_gen
    player._on_playback_ended()
    time.sleep(0.2)
    with player._lock:
        assert player._play_gen == before, "queue mode Ended must not advance itself"
        assert player.current_index == 0
    # Non-queue mode still advances
    _set_state(player, play_state=PlayerState.PLAYING)
    with player._lock:
        player._kodi_queue_mode = False
    player._on_playback_ended()
    time.sleep(0.2)
    with player._lock:
        assert player.current_index == 1


def test_ofs_increment_thread_safe():
    """Concurrent listener-style and post-worker increments must never
    produce duplicate offsets (Lounge drops duplicate-ofs reports)."""
    session = LoungeSession("s1", "t1", "d1")
    import threading
    def _bump():
        for _ in range(200):
            with session._ofs_lock:
                session.ofs += 1
    ts = [threading.Thread(target=_bump) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert session.ofs == 800, session.ofs


def test_r7_publish_one_batch_per_changed_field_group():
    """R7: publication diffs against the last published snapshot and emits at
    most one batch per channel per changed field group."""
    from resources.lib.session_state import SessionState, PlayState

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"  # post_action only needs a bound sid
    posted = []
    session.post_action = lambda sc, data, heartbeat=False: posted.append(sc) or True

    base = SessionState(version=1, playlist=("a", "b"), current_video_id="a",
                        current_index=0, list_id="L1", play_state=PlayState.PLAYING,
                        position=0.0, duration=180.0, volume=100, lane="cl")
    assert session.publish_snapshot(base) is True
    posted.clear()

    # volume-only change -> exactly one batch, onVolumeChanged only
    vol = base.__class__(**{**base.__dict__, "volume": 42, "version": 2})
    assert session.publish_snapshot(vol) is True
    assert posted == ["onVolumeChanged"], posted
    posted.clear()

    # playback-only change (same play_state) -> nowPlaying, no onStateChange;
    # R10 adds the ad-state family to a playback change.
    tick = vol.__class__(**{**vol.__dict__, "position": 5.0, "version": 3})
    assert session.publish_snapshot(tick) is True
    assert sorted(posted) == sorted(["nowPlaying", "onAdStateChange"]), posted
    posted.clear()

    # play_state change -> nowPlaying + onStateChange (+ ad state)
    paused = tick.__class__(**{**tick.__dict__, "play_state": PlayState.PAUSED, "version": 4})
    assert session.publish_snapshot(paused) is True
    assert sorted(posted) == sorted(["nowPlaying", "onStateChange", "onAdStateChange"]), posted
    posted.clear()

    # identity change with a playlist -> nowPlaying + nowPlayingPlaylist
    # (+ ad families; up-next omitted because 'b' is the last item: R6/R10)
    ident = paused.__class__(**{**paused.__dict__, "current_video_id": "b",
                                "current_index": 1, "version": 5})
    assert session.publish_snapshot(ident) is True
    assert sorted(posted) == sorted(["nowPlaying", "nowPlayingPlaylist",
                                     "onAdStateChange", "onAdPlaying"]), posted
    posted.clear()

    # identical snapshot -> no batch at all
    assert session.publish_snapshot(ident) is True
    assert posted == [], posted


def test_r7_superseded_versions_never_posted_and_ofs_monotonic():
    """R7: only the newest snapshot is posted (intermediate versions are
    superseded), and per-channel ofs increments strictly monotonically."""
    from resources.lib.session_state import SessionState, PlayState
    import urllib.parse as _up

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"
    session.gsessionid = "gs-test"
    seen_ofs = []

    class _Resp:
        def read(self):
            return b""

    class _FakeConn:
        def request(self, method, path, body=None, headers=None):
            # ofs travels in the urlencoded body (req0 params include ofs)
            params = _up.parse_qs(body.decode("utf-8") if isinstance(body, bytes) else body)
            seen_ofs.append(int(params["ofs"][0]))

        def getresponse(self):
            return _Resp()

    session._conn = _FakeConn()
    session._conn_host = _up.urlparse(BASE_URL).netloc
    session._conn_scheme = _up.urlparse(BASE_URL).scheme or "https"

    a = SessionState(version=1, current_video_id="a", play_state=PlayState.PLAYING, duration=10.0)
    c = a.__class__(**{**a.__dict__, "position": 2.0, "version": 3})

    # Simulate a burst: publish a, then c. The diff is always against the last
    # *published* state, so nothing stale is posted; c carries the newest state.
    assert session.publish_snapshot(a) is True
    assert session.publish_snapshot(c) is True
    assert session._last_published.version == 3
    # ofs strictly monotonic, never duplicated
    assert seen_ofs == list(range(1, len(seen_ofs) + 1)), seen_ofs


def test_r7_failed_post_leaves_state_dirty():
    """R7: a failed post keeps last-published behind, so the next cycle
    republishes the same absolute state."""
    from resources.lib.session_state import SessionState, PlayState

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"
    attempts = []

    def _flaky(sc, data, heartbeat=False):
        attempts.append(sc)
        return False  # simulate HTTP failure
    session.post_action = _flaky

    snap = SessionState(version=1, current_video_id="a", play_state=PlayState.PLAYING)
    assert session.publish_snapshot(snap) is False
    assert session._last_published is None  # still dirty

    # Next cycle with the same snapshot republishes it (dirty, not superseded).
    attempts.clear()
    assert session.publish_snapshot(snap) is False
    assert attempts, "dirty state must be republished on the next cycle"
    assert session._last_published is None


def test_r7_heartbeat_emits_full_snapshot():
    """R7: the <=1 Hz crash-recovery heartbeat sends the full snapshot
    (nowPlaying, nowPlayingPlaylist when a playlist exists, onVolumeChanged)."""
    from resources.lib.session_state import SessionState, PlayState

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"
    posted = []
    session.post_action = lambda sc, data, heartbeat=False: posted.append((sc, heartbeat)) or True

    snap = SessionState(version=7, playlist=("a", "b"), current_video_id="a",
                        current_index=0, list_id="L1", play_state=PlayState.PLAYING,
                        duration=180.0, volume=100, lane="cl")
    assert session.publish_snapshot(snap, heartbeat=True) is True
    names = sorted(sc for sc, _ in posted)
    # R10: the crash-recovery heartbeat carries the full vocabulary we know.
    assert names == sorted(["nowPlaying", "nowPlayingPlaylist", "autoplayUpNext",
                            "onAdStateChange", "onVolumeChanged"]), posted
    assert all(hb is True for _, hb in posted), posted
    # "<= 1 Hz" means no more than one heartbeat per second: period >= 1s.
    assert session._heartbeat_interval >= 1.0, session._heartbeat_interval


def test_r9_one_model_n_channels_same_state_independent_ofs():
    """R9: a channel is transport, never a second state. Two channels bound to one
    StateOwner publish exactly one batch each per change, carrying the same
    listId/index/item, with independently monotonic per-channel ofs."""
    from resources.lib.session_state import (
        SessionState, PlayState, StateOwner, SetPlaylistEvent, SetVolumeEvent,
        PositionTickEvent,
    )
    import urllib.parse as _up

    class _Resp:
        def read(self):
            return b""

    class _FakeConn:
        def __init__(self):
            self.ofs = []

        def request(self, method, path, body=None, headers=None):
            params = _up.parse_qs(body.decode("utf-8") if isinstance(body, bytes) else body)
            self.ofs.append(int(params["ofs"][0]))

        def getresponse(self):
            return _Resp()
        def close(self):
            pass

    def _wire(sess):
        sess.sid = "sid-%s" % sess.theme
        sess.gsessionid = "gs"
        conn = _FakeConn()
        sess._conn = conn
        sess._conn_host = _up.urlparse(BASE_URL).netloc
        sess._conn_scheme = _up.urlparse(BASE_URL).scheme or "https"
        # capture emitted action names per channel without touching the wire
        posted = []
        orig_post = sess.post_action
        def _post(sc, data, heartbeat=False):
            posted.append((sc, dict(data)))
            return orig_post(sc, data, heartbeat=heartbeat)
        sess.post_action = _post
        return posted, conn

    cl = LoungeSession("sc", "tc", "d1", theme="cl")
    m = LoungeSession("sm", "tm", "d1", theme="m")
    owner = StateOwner(SessionState())
    for s in (cl, m):
        s.attach_state_owner(owner)
    cl_posted, cl_conn = _wire(cl)
    m_posted, m_conn = _wire(m)

    # One shared cast through the single owner wakes both channels.
    owner.apply(SetPlaylistEvent(video_ids=("a", "b"), video_id="a", list_id="L1",
                                 theme="cl", source="phone"))
    snap = owner.snapshot()
    # First publish is a full snapshot (diff against the version=-1 sentinel).
    assert cl.publish_snapshot(snap) is True
    assert m.publish_snapshot(snap) is True
    cl_posted.clear(); m_posted.clear()

    # A playback-only change -> exactly one batch per channel, identical
    # item/index/listId on both (one model, N subscribers).
    owner.apply(PositionTickEvent(position=5.0, duration=180.0, source="clock"))
    snap = owner.snapshot()
    assert cl.publish_snapshot(snap) is True
    assert m.publish_snapshot(snap) is True

    for posted in (cl_posted, m_posted):
        assert "nowPlaying" in [sc for sc, _ in posted], posted
        np = next(d for sc, d in posted if sc == "nowPlaying")
        assert np["listId"] == "L1" and np["videoId"] == "a"
        assert np["currentIndex"] == "0"
    # Both channels emitted the identical action set for the same change.
    assert [sc for sc, _ in cl_posted] == [sc for sc, _ in m_posted]

    # Independent monotonic ofs per channel (no interleaving descents).
    assert cl_conn.ofs == list(range(1, len(cl_conn.ofs) + 1)), cl_conn.ofs
    assert m_conn.ofs == list(range(1, len(m_conn.ofs) + 1)), m_conn.ofs
    assert cl.ofs == m.ofs  # same batch count, own counters

    # A volume-only change -> one batch per channel again.
    cl_posted.clear(); m_posted.clear()
    owner.apply(SetVolumeEvent(volume=33, source="phone"))
    snap = owner.snapshot()
    assert cl.publish_snapshot(snap) is True
    assert m.publish_snapshot(snap) is True
    assert [sc for sc, _ in cl_posted] == ["onVolumeChanged"]
    assert [sc for sc, _ in m_posted] == ["onVolumeChanged"]
    cl.close(); m.close()

def test_r9_force_publish_resends_shared_snapshot_without_second_writer():
    """R9: the connect/getNowPlaying handshakes re-send the one shared snapshot on
    each channel instead of each channel building its own report."""
    from resources.lib.session_state import SessionState, StateOwner, PlayState

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"
    posted = []
    session.post_action = lambda sc, data, heartbeat=False: posted.append(sc) or True

    owner = StateOwner(SessionState(version=3, current_video_id="a",
                                    play_state=PlayState.PLAYING, duration=10.0))
    session.attach_state_owner(owner)
    # Publish once so the channel is clean.
    assert session.publish_snapshot(owner.snapshot()) is True
    posted.clear()

    # A handshake must force a republish of the same snapshot (dirty).
    session.force_publish()
    assert session._last_published is None
    assert session.publish_snapshot(owner.snapshot()) is True
    assert "nowPlaying" in posted, posted
    session.close()

def test_r5_index_derived_at_publication_tracks_queue_edits():
    """R5: currentIndex is derived from the stored queue at publication, so a
    queue edit that removes an earlier item moves the reported index with it."""
    from resources.lib.session_state import (
        SessionState, PlayState, SetPlaylistEvent, UpdatePlaylistEvent, published_index,
    )

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"
    posted = []
    session.post_action = lambda sc, data, heartbeat=False: posted.append((sc, dict(data))) or True

    # Queue [a,b,c], playing b -> index 1.
    base = SessionState(version=1, playlist=("a", "b", "c"), current_video_id="b",
                        current_index=1, list_id="L1", play_state=PlayState.PLAYING)
    npp = session._build_now_playing_playlist(base)
    assert npp["currentIndex"] == "1"

    # Phone removes 'a' -> [b,c]; b is now index 0 and the report must say 0,
    # not the stale carried 1.
    edited = SessionState(version=2, playlist=("b", "c"), current_video_id="b",
                          current_index=0, list_id="L1", play_state=PlayState.PLAYING)
    assert session._build_now_playing_playlist(edited)["currentIndex"] == "0"

    # A snapshot whose stale index was never recomputed is still reported from
    # the queue (derived at publication), not from the carried field.
    stale = SessionState(version=3, playlist=("b", "c"), current_video_id="c",
                         current_index=0, list_id="L1", play_state=PlayState.PLAYING)
    assert published_index(stale) == 1
    assert session._build_now_playing_playlist(stale)["currentIndex"] == "1"

    # Never an out-of-range index, even when the active item is not in the queue.
    foreign = SessionState(version=4, playlist=("x", "y"), current_video_id="zz",
                           current_index=99, list_id="L1", play_state=PlayState.PLAYING)
    idx = int(session._build_now_playing_playlist(foreign)["currentIndex"])
    assert 0 <= idx <= 1, idx
    assert session._build_now_playing(foreign)["currentIndex"] == str(idx)
    # Empty queue -> 0, not a carried value.
    empty = SessionState(version=5, playlist=(), current_video_id="a",
                         current_index=7, play_state=PlayState.PLAYING)
    assert published_index(empty) == 0
    session.close()

def test_r5_tv_side_pick_publishes_derived_index_with_original_listid():
    """R5: a TV-side pick publishes one report with the re-derived index and the
    original listId — the pair is never mismatched."""
    from resources.lib.session_state import (
        SessionState, PlayState, StateOwner, SetPlaylistEvent, KodiAdvancedEvent,
    )

    owner = StateOwner(SessionState())
    owner.apply(SetPlaylistEvent(video_ids=("a", "b", "c"), video_id="a", list_id="L9"))
    owner.apply(KodiAdvancedEvent(video_id="c", source="player-clock"))
    snap = owner.snapshot()

    session = LoungeSession("s1", "t1", "d1")
    np = session._build_now_playing(snap)
    npp = session._build_now_playing_playlist(snap)
    assert np["listId"] == "L9" and npp["listId"] == "L9"
    assert np["videoId"] == "c"
    assert np["currentIndex"] == "2" and npp["currentIndex"] == "2"
    session.close()

def test_r8_reconcile_emits_one_corrective_event_for_a_drift():
    """R8: a deliberate drift produces exactly one corrective event and, when the
    index moved, one publish carrying the adopted item with the stored listId."""
    import resources.lib.player_bridge as pb
    from resources.lib.session_state import (
        SessionState, PlayState, StateOwner, SetPlaylistEvent,
    )

    session = LoungeSession("s1", "t1", "d1")
    player = KodiPlayerBridge(session=session, resolver=VideoResolver(bridge=MockYtDlpBridge()))
    owner = player.owner
    owner.apply(SetPlaylistEvent(video_ids=("a", "b", "c"), video_id="a", list_id="L7"))

    class _FakePlayer:
        def isPlaying(self):
            return True
        def isPlayingVideo(self):
            return False
        def isPlayingAudio(self):
            return False
        def getTime(self):
            return 12.0
        def getTotalTime(self):
            return 300.0

    class _FakeXbmc:
        def __init__(self, label):
            self._label = label
        def getInfoLabel(self, name):
            return self._label
        def getCondVisibility(self, cond):
            return False

    orig_kodi = pb.KODI_AVAILABLE
    orig_xbmc = pb.xbmc
    pb.KODI_AVAILABLE = True
    player._kodi_player = _FakePlayer()
    # Kodi is playing 'c' while the snapshot still says 'a'.
    pb.xbmc = _FakeXbmc("plugin://plugin.service.ytlounge-cast/?play=c")
    try:
        before = owner.version
        player._reconcile_tick()
        # The item drift produces one kodiAdvanced adoption; the clock fold that
        # follows is a separate, legitimate observation. What matters (R8):
        # the adopted item is published with a re-derived index and the stored
        # listId, and the queue is unchanged.
        assert owner.current_video_id == "c"
        assert owner.list_id == "L7"
        assert owner.current_index == 2  # re-derived against the stored queue
        assert owner.playlist == ("a", "b", "c")
        assert owner.version > before

        # Quiet tick: same player, same state -> zero versions.
        v = owner.version
        player._reconcile_tick()
        assert owner.version == v, f"quiet tick advanced to {owner.version}"
    finally:
        pb.KODI_AVAILABLE = orig_kodi
        pb.xbmc = orig_xbmc
        session.close()

def test_r8_vanished_player_is_a_corrective_stop():
    """R8: the player disappearing while we believe something plays is one
    corrective STOPPED event, not a silent stale state."""
    import resources.lib.player_bridge as pb
    from resources.lib.session_state import SetPlaylistEvent

    session = LoungeSession("s1", "t1", "d1")
    player = KodiPlayerBridge(session=session, resolver=VideoResolver(bridge=MockYtDlpBridge()))
    player.owner.apply(SetPlaylistEvent(video_ids=("a",), video_id="a", list_id="L1"))

    class _GonePlayer:
        def isPlaying(self):
            return False

    orig_kodi = pb.KODI_AVAILABLE
    pb.KODI_AVAILABLE = True
    player._kodi_player = _GonePlayer()
    try:
        before = player.owner.version
        player._reconcile_tick()
        assert player.owner.version == before + 1
        assert player.state == PlayerState.STOPPED
    finally:
        pb.KODI_AVAILABLE = orig_kodi
        session.close()

def test_r6_unknown_published_when_item_fact_has_no_source():
    """R6: with an alive player but no item at all, the item fact is genuinely
    unknown and UNKNOWN is publishable (state=-1), never a guess."""
    import resources.lib.player_bridge as pb
    from resources.lib.session_state import PlayState

    session = LoungeSession("s1", "t1", "d1")
    player = KodiPlayerBridge(session=session, resolver=VideoResolver(bridge=MockYtDlpBridge()))
    player.owner.apply(pb.PlaybackStartedEvent())

    class _AliveNoItem:
        def isPlaying(self):
            return True
        def isPlayingVideo(self):
            return False
        def isPlayingAudio(self):
            return False

    class _FakeXbmc:
        def getInfoLabel(self, name):
            return ""
        def getCondVisibility(self, cond):
            return False

    orig_kodi = pb.KODI_AVAILABLE
    orig_xbmc = pb.xbmc
    pb.KODI_AVAILABLE = True
    player._kodi_player = _AliveNoItem()
    pb.xbmc = _FakeXbmc()  # no plugin URL readable
    try:
        player._reconcile_tick()
        assert player.owner.play_state == PlayState.UNKNOWN, player.owner.play_state
    finally:
        pb.KODI_AVAILABLE = orig_kodi
        pb.xbmc = orig_xbmc
        session.close()

def test_r10_vocabulary_declared_and_coverage_logged():
    """R10: the ten official families are declared, the implemented set is
    logged with the gaps, and the two newly implemented ones are real."""
    from resources.lib.lounge import vocabulary

    assert len(vocabulary.VOCABULARY) == 10, vocabulary.VOCABULARY
    assert len({f.name for f in vocabulary.VOCABULARY}) == 10
    line = vocabulary.coverage_line()
    assert line.startswith("vocabulary: "), line
    assert "missing:" in line and "autoplayModeChanged" in line
    # Every declared name has a payload spec and, when unimplemented, a reason.
    for f in vocabulary.VOCABULARY:
        assert f.payload, f
        if not f.implemented:
            assert f.why_not, f

    # The newly implemented families are emittable through the one path.
    assert vocabulary.is_implemented("onAdStateChange")
    assert vocabulary.is_implemented("onAdPlaying")
    assert vocabulary.is_implemented("autoplayUpNext")

def test_r10_up_next_emitted_when_known_omitted_when_not():
    """R10/R6: autoplayUpNext is derived from the stored queue and omitted when
    the receiver does not know what comes next (no guess)."""
    from resources.lib.session_state import SessionState, PlayState

    session = LoungeSession("s1", "t1", "d1")
    session.sid = "sid-test"

    mid = SessionState(version=1, playlist=("a", "b", "c"), current_video_id="b",
                       current_index=1, list_id="L1", play_state=PlayState.PLAYING)
    assert session._build_up_next(mid) == {"videoId": "c", "listId": "L1"}

    last = SessionState(version=2, playlist=("a", "b", "c"), current_video_id="c",
                        current_index=2, list_id="L1", play_state=PlayState.PLAYING)
    assert session._build_up_next(last) is None

    # Single item / foreign item at the end -> nothing to advertise.
    single = SessionState(version=3, playlist=("a",), current_video_id="a",
                          current_index=0, list_id="L1", play_state=PlayState.PLAYING)
    assert session._build_up_next(single) is None

    # The ad family is a known "no ad", and the emitted payload is stable.
    ads = session._build_ad_state()
    assert ads["adState"] == "0" and ads["isSkippable"] == "false"
    session.close()

def test_duration_reporting_and_fallback():
    session = LoungeSession("s1", "t1", "d1")
    actions = []
    session.post_action = lambda sc, data: actions.append((sc, data))

    player = KodiPlayerBridge(session=session, resolver=VideoResolver(bridge=MockYtDlpBridge()))

    # Non-negative int check in report_now_playing and report_state_change
    session.report_now_playing("v1", -5, -10, 1)
    assert actions[-1] == ("nowPlaying", {
        "videoId": "v1", "currentTime": "0", "duration": "0", "state": "1", "cpn": "kodi"
    })
    session.report_now_playing("v1", 10, 120, 1, current_index=2, list_id="PL123")
    assert actions[-1] == ("nowPlaying", {
        "videoId": "v1", "currentTime": "10", "duration": "120", "state": "1", "cpn": "kodi",
        "seekableStartTime": "0", "seekableEndTime": "120", "loadedTime": "120",
        "currentIndex": "2", "listId": "PL123"
    })
    session.report_now_playing_playlist(["v1", "v2"], "v1", 0, 10, 120, 1)
    assert actions[-1] == ("nowPlayingPlaylist", {
        "videoIds": "v1,v2", "videoId": "v1", "currentIndex": "0",
        "currentTime": "10", "duration": "120", "state": "1",
        "seekableStartTime": "0", "seekableEndTime": "120"
    })
    session.report_state_change(1, 15.6, 120.4)
    assert actions[-1] == ("onStateChange", {
        "state": "1",
        "currentTime": "15",
        "duration": "120",
        "cpn": "kodi",
        "seekableStartTime": "0",
        "seekableEndTime": "120",
        "loadedTime": "120",
    })

    # Test fallback to getTotalTime when current_duration <= 0 and media is active
    class FakeKodiPlayer:
        def __init__(self):
            self._playing = True
            self._time = 10.0
            self._total_time = 240.0
        def isPlaying(self):
            return self._playing
        def getTime(self):
            return self._time
        def getTotalTime(self):
            return self._total_time

    fake_kp = FakeKodiPlayer()
    player._kodi_player = fake_kp
    import resources.lib.player_bridge as pb
    orig_kodi_avail = pb.KODI_AVAILABLE
    pb.KODI_AVAILABLE = True

    try:
        _set_state(player, duration=0)
        assert player.current_duration == 240
        assert player.get_duration() == 240

        # When not playing, fallback is not used
        fake_kp._playing = False
        _set_state(player, duration=0)
        assert player.current_duration == 0

        # Position loop emits both report_now_playing and report_state_change
        fake_kp._playing = True
        _set_state(player, play_state=PlayerState.PLAYING, current_video_id="v_loop", duration=180)
        actions.clear()

        # Execute monitoring block logic directly
        cur_time = player.get_time()
        cur_duration = player.current_duration
        for s in player.sessions:
            s.report_now_playing(player.current_video_id, int(cur_time), cur_duration, player.state)
            s.report_state_change(player.state, int(cur_time), cur_duration)

        assert any(a[0] == "nowPlaying" and a[1]["videoId"] == "v_loop" and a[1]["duration"] == "180" for a in actions)
        assert any(a[0] == "onStateChange" and a[1]["duration"] == "180" for a in actions)
    finally:
        pb.KODI_AVAILABLE = orig_kodi_avail


def test_sync_current_from_kodi_refresh_dispatches_duration():
    session = LoungeSession("s1", "t1", "d1")
    actions = []
    session.post_action = lambda sc, data: actions.append((sc, data))

    class MockResolveBridge:
        def resolve(self, video_id):
            return {
                "id": video_id,
                "title": f"Title {video_id}",
                "duration": 315,
                "playable_url": f"http://example.com/{video_id}",
            }

    resolver = VideoResolver(bridge=MockResolveBridge())
    player = KodiPlayerBridge(session=session, resolver=resolver)

    class FakeKodiPlayer:
        def __init__(self):
            self._file = "plugin://plugin.service.ytlounge-cast/?play=v_synced"
            self._playing = True
        def getPlayingFile(self):
            return self._file
        def isPlaying(self):
            return self._playing
        def getTime(self):
            return 5.0
        def getTotalTime(self):
            return 315.0

    player._kodi_player = FakeKodiPlayer()
    import resources.lib.player_bridge as pb
    orig_kodi_avail = pb.KODI_AVAILABLE
    pb.KODI_AVAILABLE = True

    try:
        _set_state(player, current_video_id="v_old", play_state=PlayerState.PLAYING)
        actions.clear()

        changed = player._sync_current_from_kodi()
        assert changed is True
        assert player.current_video_id == "v_synced"

        # Wait for background _refresh to finish
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(a[0] == "nowPlaying" and a[1].get("videoId") == "v_synced" and a[1].get("duration") == "315" for a in actions):
                break
            time.sleep(0.05)

        assert any(a[0] == "nowPlaying" and a[1]["videoId"] == "v_synced" and a[1]["duration"] == "315" for a in actions)
        assert any(a[0] == "onStateChange" and a[1]["duration"] == "315" for a in actions)
    finally:
        pb.KODI_AVAILABLE = orig_kodi_avail


def test_track_change_watchdog_adopts_on_kodi_native_advance():
    """Regression: on LibreELEC Kodi 21 xbmc.Player callbacks never fire in the
    service process, and during a Kodi-native auto-advance the bridge state
    stays PLAYING — so the pre-existing adoption path (state != PLAYING)
    never ran and the phone kept reporting the old video forever.

    Signal verified on device (YTCAST-PROBE2): getPlayingFile() returns the
    resolved googlevideo URL (no video id); the info label
    "Player.FileNameAndPath" carries plugin://...?play=<video_id>. The
    watchdog must read that label and adopt."""
    session = LoungeSession("s1", "t1", "d1")
    actions = []
    session.post_action = lambda sc, data: actions.append((sc, data))

    class MockResolveBridge:
        def resolve(self, video_id):
            return {
                "id": video_id,
                "title": f"Title {video_id}",
                "duration": 315,
                "playable_url": f"http://example.com/{video_id}",
            }

    import resources.lib.player_bridge as pb
    scraper = {"plugin_url": "plugin://plugin.service.ytlounge-cast/?play=v_old"}

    class FakeXbmc:
        @staticmethod
        def getInfoLabel(key):
            if key == "Player.FileNameAndPath":
                return scraper["plugin_url"]
            return ""

    class FakeKodiPlayer:
        """Real-device model: no callbacks, getPlayingFile returns googlevideo URL."""
        def isPlaying(self):
            return True
        def getPlayingFile(self):
            return "https://rr3---sn.googlevideo.com/videoplayback?id=o-ABC"
        def getTime(self):
            return 3.0
        def getTotalTime(self):
            return 315.0

    resolver = VideoResolver(bridge=MockResolveBridge())  # type: ignore[arg-type]
    player = KodiPlayerBridge(session=session, resolver=resolver)
    player._kodi_player = FakeKodiPlayer()  # type: ignore[assignment]
    _set_state(player,
               current_video_id="v_old", duration=315,
               playlist=("v_old", "v_new"), current_index=0,
               play_state=PlayerState.PLAYING)  # stale: stays PLAYING across native advance

    orig_avail = pb.KODI_AVAILABLE
    orig_xbmc = pb.xbmc
    pb.KODI_AVAILABLE = True
    pb.xbmc = FakeXbmc
    try:
        # Kodi auto-advanced natively: info label now shows the next item.
        scraper["plugin_url"] = "plugin://plugin.service.ytlounge-cast/?play=v_new"

        class OnePass(Exception):
            pass

        orig_time_sleep = pb.time.sleep
        first = [True]

        def sleeper(_s):
            if first[0]:
                first[0] = False
                return
            raise OnePass()

        pb.time.sleep = sleeper
        try:
            try:
                player._position_loop()
            except OnePass:
                pass
        finally:
            pb.time.sleep = orig_time_sleep

        assert player.current_video_id == "v_new", player.current_video_id
        assert player.current_index == 1
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(a[0] == "nowPlaying" and a[1].get("videoId") == "v_new" for a in actions):
                break
            time.sleep(0.05)
        assert any(a[0] == "nowPlaying" and a[1]["videoId"] == "v_new" for a in actions), actions
    finally:
        pb.KODI_AVAILABLE = orig_avail
        pb.xbmc = orig_xbmc


def test_pause_and_resume_reporting():
    session = LoungeSession("s1", "t1", "d1")
    actions = []
    session.post_action = lambda sc, data: actions.append((sc, data))

    player = KodiPlayerBridge(session=session)
    _set_state(player, current_video_id="v1", duration=120, play_state=PlayerState.PLAYING)

    actions.clear()
    player.pause()
    session.flush()
    assert player.state == PlayerState.PAUSED
    assert any(a[0] == "onStateChange" and a[1]["state"] == "2" for a in actions)
    assert any(a[0] == "nowPlaying" and a[1]["state"] == "2" for a in actions)

    actions.clear()
    player.resume()
    session.flush()
    assert player.state == PlayerState.PLAYING
    assert any(a[0] == "onStateChange" and a[1]["state"] == "1" for a in actions)
    assert any(a[0] == "nowPlaying" and a[1]["state"] == "1" for a in actions)


def test_audio_normalization_ebur128_parsing():
    """Integrated loudness + true peak come out of ffmpeg's ebur128 summary."""
    from resources.lib.audio_norm import parse_ebur128

    sample = """
[Parsed_ebur128_0 @ 0x55a8138280] Summary:

  Integrated loudness:
    I:         -13.7 LUFS
    Threshold: -23.7 LUFS

  Loudness range:
    LRA:         5.0 LU
    Threshold: -33.2 LUFS
    LRA low:   -15.8 LUFS
    LRA high:  -10.8 LUFS

  True peak:
    Peak:       -0.4 dBFS
"""
    assert parse_ebur128(sample) == (-13.7, -0.4)
    # No summary at all (filter never ran, ffmpeg died): skip, do not guess.
    assert parse_ebur128("") is None
    assert parse_ebur128("[hls] Opening 'https://x/y.m3u8' for reading") is None


def test_audio_normalization_gain_math():
    """Gain is target-relative, peak-guarded and boost-capped."""
    from resources.lib.audio_norm import compute_gain_db

    # Ample headroom: the cut is applied in full.
    assert compute_gain_db(-10.0, -3.0, target_lufs=-14.0) == -4.0
    # Modern YouTube master (-13.7 LUFS): a small trim, but its true peak is
    # already at -0.4 dBTP, so the guard binds first (-1.0 - (-0.4) = -0.6).
    assert compute_gain_db(-13.7, -0.4, target_lufs=-14.0) == -0.6
    # A quiet upload is boosted only as far as its headroom allows
    # (-1.0 - (-6.0) = 5 dB), not all the way to the target.
    assert compute_gain_db(-24.0, -6.0, target_lufs=-14.0, max_gain_db=12.0) == 5.0
    # Very quiet with lots of headroom: the boost cap binds (no noise lift).
    assert compute_gain_db(-40.0, -20.0, target_lufs=-14.0, max_gain_db=12.0) == 12.0
    # Garbage measurement is not a reason to change anything.
    assert compute_gain_db(None, None) == 0.0


def test_audio_normalization_artifact_paths():
    """Only the generated artifact names are servable — no path traversal."""
    import tempfile
    from resources.lib.audio_norm import AudioNormalizer

    with tempfile.TemporaryDirectory() as cache:
        norm = AudioNormalizer(cache_dir=cache)
        os.makedirs(os.path.join(cache, "abc"), exist_ok=True)
        with open(os.path.join(cache, "abc", "audio.m3u8"), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\nseg0000.ts\n")
        assert norm.file_path("abc", "audio.m3u8") is not None
        assert norm.file_path("abc", "../../etc/passwd") is None
        assert norm.file_path("abc", "..%2fpasswd") is None
        assert norm.file_path("..", "audio.m3u8") is None
        # meta.json is not a playlist: not in the allowlist of servable names.
        assert norm.file_path("abc", "meta.json") is None
        # Playlist without the progressive track is not a usable artifact.
        assert norm.has_artifact("abc") is False

        # Playlist segment URLs are absolute on the origin Kodi talks to.
        body = norm.playlist_body("abc", "http://127.0.0.1:1234/")
        assert "http://127.0.0.1:1234/audio_norm/abc/seg0000.ts" in body


def test_audio_normalization_range_parsing():
    from resources.lib.audio_norm import _parse_range

    assert _parse_range("bytes=0-99", 1000) == (0, 99)
    assert _parse_range("bytes=100-", 1000) == (100, 999)
    assert _parse_range("bytes=-100", 1000) == (900, 999)
    assert _parse_range("bytes=4000-", 1000) is None
    assert _parse_range("nonsense", 1000) is None


def test_audio_normalization_master_retarget():
    """A video-lane master hands its DEFAULT audio rendition to the normalized
    track once it exists; alternate (dub) renditions stay on YouTube."""
    from resources.lib import manifest_server as manifest_server_mod
    from resources.lib.resolver import VideoResolver

    class _Norm:
        enabled = True

        def has_artifact(self, video_id):
            return True

        def progressive_url(self, video_id):
            return f"http://127.0.0.1:9/audio_norm/{video_id}/norm.m4a"

        def local_playlist_url(self, video_id):
            return f"http://127.0.0.1:9/audio_norm/{video_id}/audio.m3u8"

        def request(self, *args, **kwargs):
            raise AssertionError("artifact exists: must not queue a render")

    body = "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",LANGUAGE="en",'
        'DEFAULT=YES,AUTOSELECT=YES,URI="https://remote.example/audio.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Bengali",LANGUAGE="bn",'
        'DEFAULT=NO,AUTOSELECT=NO,URI="https://remote.example/dub.m3u8"',
        '#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO="audio"',
        "https://remote.example/video.m3u8",
    ])
    url = manifest_server_mod.publish("yt_retarget1.m3u8", body)
    resolver = VideoResolver(bridge=object(), normalizer=_Norm())
    info = {
        "id": "retarget1",
        "stream_type": "hls_master",
        "playable_url": url,
        "audio_url": "https://remote.example/audio.m4a",
        "duration": 60,
    }

    out = resolver._with_audio("retarget1", info)
    assert out["audio_normalized"] is True
    assert out["audio_url"] == "http://127.0.0.1:9/audio_norm/retarget1/norm.m4a"

    patched = manifest_server_mod.fetch_manifest(url)
    assert 'URI="http://127.0.0.1:9/audio_norm/retarget1/audio.m3u8"' in patched
    assert "dub.m3u8" in patched                 # dubs untouched
    assert patched.count("/audio_norm/") == 1    # only the default rendition
    # Idempotent: nothing to change on a second resolve.
    assert resolver._retarget_master_audio("retarget1", info) is False


def test_audio_normalization_never_blocks_resolve():
    """No artifact yet: the render is queued and the original audio is used."""
    from resources.lib.resolver import VideoResolver

    queued = []

    class _Norm:
        enabled = True

        def has_artifact(self, video_id):
            return False

        def progressive_url(self, video_id):
            raise AssertionError("must not be consulted without an artifact")

        def request(self, video_id, source_url, duration):
            queued.append((video_id, source_url, duration))
            return True

    resolver = VideoResolver(bridge=object(), normalizer=_Norm())
    info = {
        "id": "cold1",
        "stream_type": "dash",
        "playable_url": "https://remote.example/x.mpd",
        "audio_url": "https://remote.example/audio.m4a",
        "duration": 120,
    }
    out = resolver._with_audio("cold1", info)
    assert out is info                      # original info, untouched
    assert queued == [("cold1", "https://remote.example/audio.m4a", 120)]


def test_audio_normalization_disabled_is_inert():
    """Disabled (or absent normalizer): the resolver is a pass-through."""
    from resources.lib.resolver import VideoResolver

    class _Off:
        enabled = False

        def __getattr__(self, name):
            raise AssertionError(f"disabled normalizer touched: {name}")

    info = {"id": "off1", "audio_url": "https://remote.example/a.m4a", "duration": 10}
    assert VideoResolver(bridge=object(), normalizer=_Off())._with_audio("off1", info) is info
    assert VideoResolver(bridge=object())._with_audio("off1", info) is info


def test_handoff_pending_gate():
    """The normalizer holds its ffmpeg child while a handoff is in flight."""
    player = KodiPlayerBridge(session=LoungeSession("s1", "t1", "d1"))
    assert player.handoff_pending() is False
    player._begin_transition(1.0)
    assert player.handoff_pending() is True


def test_audio_normalization_hold_check_is_fault_tolerant():
    """A throwing hold predicate must not kill the render."""
    import tempfile
    from resources.lib.audio_norm import AudioNormalizer

    def _boom():
        raise RuntimeError("hold check exploded")

    with tempfile.TemporaryDirectory() as cache:
        norm = AudioNormalizer(cache_dir=cache, hold_check=_boom)
        assert norm._hold_now() is False


def test_audio_normalization_spawns_without_preexec():
    """Regression: Kodi runs addons in a subinterpreter, where CPython refuses
    Popen(preexec_fn=...) — that killed every render on the device with
    "preexec_fn not supported within subinterpreters". Priority must be lowered
    through the nice binary instead."""
    import tempfile
    from resources.lib import audio_norm

    captured = {}

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return (b"", b"  Integrated loudness:\n    I: -14.0 LUFS\n  True peak:\n    Peak: -1.0 dBFS\n")

        def poll(self):
            return 0

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        return _FakeProc()

    real_popen = audio_norm.subprocess.Popen
    audio_norm.subprocess.Popen = _fake_popen
    try:
        with tempfile.TemporaryDirectory() as cache:
            normalizer = audio_norm.AudioNormalizer(cache_dir=cache)
            stderr = normalizer._run_capture(
                "/usr/bin/ffmpeg", ["/usr/bin/ffmpeg", "-i", "source.m4a"])
    finally:
        audio_norm.subprocess.Popen = real_popen

    assert "preexec_fn" not in captured["kwargs"], "preexec_fn breaks in Kodi's subinterpreters"
    command = captured["cmd"]
    assert command[-1] == "source.m4a" and command[-2] == "-i"
    assert "/usr/bin/ffmpeg" in command
    if shutil.which("nice"):
        assert command[0].endswith("nice") and command[1:3] == ["-n", "10"], command
    assert stderr is not None and "I: -14.0 LUFS" in stderr


if __name__ == "__main__":
    test_frame_parsing()
    test_frame_parsing_chunked()
    test_frame_parsing_incomplete_tail()
    test_persistence()
    test_resolver_cache()
    test_player_bridge_queue()
    test_player_bridge_index_resync()
    test_play_generation_supersedes()
    test_ytdlp_downloader_metadata()
    test_ytdlp_asset_prefers_importable_zipapp()
    test_ytdlp_reinstall_detects_non_importable_binary()
    test_ytdlp_download_keeps_old_binary_when_probe_fails()
    test_hls_master_generation()
    test_youtube_music_session()
    test_dial_and_ssdp_discovery()
    test_pairing_dialog_non_blocking()
    test_actions_module()
    test_kodi_queue_mode_ended_no_self_advance()
    test_ofs_increment_thread_safe()
    test_r7_publish_one_batch_per_changed_field_group()
    test_r7_superseded_versions_never_posted_and_ofs_monotonic()
    test_r7_failed_post_leaves_state_dirty()
    test_r7_heartbeat_emits_full_snapshot()
    test_r9_one_model_n_channels_same_state_independent_ofs()
    test_r9_force_publish_resends_shared_snapshot_without_second_writer()
    test_r5_index_derived_at_publication_tracks_queue_edits()
    test_r5_tv_side_pick_publishes_derived_index_with_original_listid()
    test_r8_reconcile_emits_one_corrective_event_for_a_drift()
    test_r8_vanished_player_is_a_corrective_stop()
    test_r6_unknown_published_when_item_fact_has_no_source()
    test_r10_vocabulary_declared_and_coverage_logged()
    test_r10_up_next_emitted_when_known_omitted_when_not()
    test_duration_reporting_and_fallback()
    test_sync_current_from_kodi_refresh_dispatches_duration()
    test_track_change_watchdog_adopts_on_kodi_native_advance()
    test_pause_and_resume_reporting()
    test_audio_normalization_ebur128_parsing()
    test_audio_normalization_gain_math()
    test_audio_normalization_artifact_paths()
    test_audio_normalization_range_parsing()
    test_audio_normalization_master_retarget()
    test_audio_normalization_never_blocks_resolve()
    test_audio_normalization_disabled_is_inert()
    test_handoff_pending_gate()
    test_audio_normalization_hold_check_is_fault_tolerant()
    test_audio_normalization_spawns_without_preexec()
    print("All unit tests passed successfully.")
