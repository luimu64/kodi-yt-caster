#!/usr/bin/env python3
"""One runnable check for YouTube Lounge Cast Receiver."""

import os
import time
import unittest
from resources.lib.lounge.session import parse_frames, LoungeSession
from resources.lib.lounge.client import BASE_URL
from resources.lib.persistence import SessionStore
from resources.lib.resolver import VideoResolver
from resources.lib.player_bridge import KodiPlayerBridge, PlayerState
from resources.lib.lounge.listener import CommandDispatcher


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
    store.clear()
    cleared = store.load()
    assert "screen_id" not in cleared
    assert cleared["device_id"] == dev_id
    if os.path.exists(test_file):
        os.remove(test_file)


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
    with player._lock:
        player.current_video_id = "v4"
    # phone removes v1 and v2 from the queue while v4 plays
    player.update_playlist({"videoIds": "v3,v4"})
    player._resync_index()
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
        player.current_video_id = "v1"
        player.current_index = 0
        player._kodi_queue_mode = True
        before = player._play_gen
    player._on_playback_ended()
    time.sleep(0.2)
    with player._lock:
        assert player._play_gen == before, "queue mode Ended must not advance itself"
        assert player.current_index == 0
    # Non-queue mode still advances
    player.state = PlayerState.PLAYING
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
    test_hls_master_generation()
    test_youtube_music_session()
    test_dial_and_ssdp_discovery()
    test_pairing_dialog_non_blocking()
    test_actions_module()
    test_kodi_queue_mode_ended_no_self_advance()
    test_ofs_increment_thread_safe()
    print("All unit tests passed successfully.")
