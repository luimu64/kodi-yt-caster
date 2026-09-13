#!/usr/bin/env python3
"""One runnable check for YouTube Lounge Cast Receiver."""

import os
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
    raw_payload = (
        "58\n"
        "[[0,[\"c\",\"TEST_SID\",\"\",8]],[1,[\"S\",\"TEST_GSESSIONID\"]]]\n"
    )
    frames = parse_frames(raw_payload)
    assert len(frames) == 2
    assert frames[0] == (0, "c", "TEST_SID")
    assert frames[1] == (1, "S", "TEST_GSESSIONID")


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
    assert os.path.exists(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "#EXT-X-STREAM-INF" in content
    assert "RESOLUTION=1920x1080" in content
    assert "RESOLUTION=640x360" in content
    assert "#EXT-X-MEDIA:TYPE=AUDIO" in content
    os.remove(manifest_path)


if __name__ == "__main__":
    test_frame_parsing()
    test_persistence()
    test_resolver_cache()
    test_player_bridge_queue()
    test_ytdlp_downloader_metadata()
    test_hls_master_generation()
    print("All unit tests passed successfully.")
