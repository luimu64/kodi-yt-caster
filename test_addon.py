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


if __name__ == "__main__":
    test_frame_parsing()
    test_persistence()
    test_resolver_cache()
    test_player_bridge_queue()
    print("All unit tests passed successfully.")
