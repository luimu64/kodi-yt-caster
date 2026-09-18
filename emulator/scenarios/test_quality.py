#!/usr/bin/env python3
"""Unit tests for the automatic quality ladder (no Kodi, no network).

Runs standalone (`python3 emulator/scenarios/test_quality.py`) and is also
picked up by `emulator/run_all.py`. The ladder is pure logic over format
lists, so it needs no stub Kodi API — only the repo root on sys.path.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from resources.lib.quality import (  # noqa: E402
    DEFAULT_START_HEIGHT,
    MasterRewriter,
    QualityLadder,
    pick_audio_ladder,
    resolve_ceiling,
)


def _f(height, vcodec="avc1.4D400B", tbr=100.0, fid=None, url=None):
    return {
        "height": height,
        "vcodec": vcodec,
        "tbr": tbr,
        "format_id": fid or str(height),
        "url": url or f"http://manifest.googlevideo.com/itag/{height}/index.m3u8",
        "protocol": "m3u8_native",
        "acodec": "none",
    }


# --------------------------------------------------------------- the ladder
def test_ladder_sorts_low_to_high():
    ladder = QualityLadder("v", [_f(1080), _f(144), _f(720), _f(480)])
    assert ladder.heights() == [144, 480, 720, 1080], ladder.heights()
    print("  test_ladder_sorts_low_to_high OK")


def test_prefers_avc_at_same_height():
    """VP9 at the same height must never win: a Pi 4 cannot decode it."""
    ladder = QualityLadder("v", [
        _f(144, vcodec="vp09.00.10.08", tbr=75.0),
        _f(144, vcodec="avc1.4D400B", tbr=139.0),
    ])
    assert ladder.rung_count() == 1, ladder.rung_count()
    winner = ladder.rungs[0]
    assert winner["vcodec"].startswith("avc1"), winner["vcodec"]
    print("  test_prefers_avc_at_same_height OK")


def test_higher_bitrate_wins_within_codec():
    ladder = QualityLadder("v", [
        _f(720, vcodec="avc1", tbr=1000.0, fid="a"),
        _f(720, vcodec="avc1", tbr=2000.0, fid="b"),
    ])
    assert ladder.rungs[0]["format_id"] == "b", ladder.rungs[0]
    print("  test_higher_bitrate_wins_within_codec OK")


def test_ceiling_caps_the_ladder():
    ladder = QualityLadder("v", [_f(144), _f(480), _f(1080), _f(2160)],
                           max_resolution="720p")
    assert ladder.heights() == [144, 480], ladder.heights()
    print("  test_ceiling_caps_the_ladder OK")


def test_auto_ceiling_is_uncapped():
    assert resolve_ceiling("auto") is None
    assert resolve_ceiling("") is None
    assert resolve_ceiling("1080p") == 1080
    assert resolve_ceiling("4K") == 2160
    ladder = QualityLadder("v", [_f(144), _f(2160)], max_resolution="auto")
    assert ladder.heights() == [144, 2160], ladder.heights()
    print("  test_auto_ceiling_is_uncapped OK")


def test_starts_at_the_lowest_rung():
    ladder = QualityLadder("v", [_f(144), _f(480), _f(1080)])
    ladder.set_index(ladder.start_index())
    assert ladder.start_index() == 0
    assert ladder.current()["height"] == DEFAULT_START_HEIGHT
    print("  test_starts_at_the_lowest_rung OK")


def test_advance_climbs_one_rung_and_stops():
    ladder = QualityLadder("v", [_f(144), _f(480), _f(1080)])
    ladder.set_index(ladder.start_index())
    seen = [ladder.current()["height"]]
    while True:
        nxt = ladder.advance()
        if nxt is None:
            break
        seen.append(nxt["height"])
    assert seen == [144, 480, 1080], seen
    assert ladder.at_top()
    assert ladder.advance() is None, "advance past the top must return None"
    print("  test_advance_climbs_one_rung_and_stops OK")


def test_single_rendition_ladder_is_immediately_at_top():
    ladder = QualityLadder("v", [_f(720)])
    ladder.set_index(ladder.start_index())
    assert ladder.at_top(), "one rung means nothing to climb"
    assert ladder.advance() is None
    print("  test_single_rendition_ladder_is_immediately_at_top OK")


def test_empty_ladder_is_safe():
    ladder = QualityLadder("v", [])
    assert ladder.rung_count() == 0
    assert ladder.current() is None
    assert ladder.start_index() == 0
    assert ladder.advance() is None
    print("  test_empty_ladder_is_safe OK")


def test_audio_only_formats_make_no_video_ladder():
    """An art upload has no video renditions to climb — must not crash."""
    formats = [
        {"height": None, "vcodec": "none", "acodec": "mp4a", "tbr": 128,
         "url": "http://x/a.m4a", "protocol": "https", "format_id": "140"},
    ]
    ladder = QualityLadder("v", formats)
    assert ladder.rung_count() == 0
    assert ladder.at_top()
    print("  test_audio_only_formats_make_no_video_ladder OK")


# ------------------------------------------------------------ the rewriter
MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,URI="http://x/a-high.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Deutsch",LANGUAGE="de",DEFAULT=NO,AUTOSELECT=NO,URI="http://x/a-low.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,CODECS="avc1.4D401F",AUDIO="audio"
http://x/v-1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.4D401F",AUDIO="audio"
http://x/v-720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=256x144,CODECS="avc1.4D400B",AUDIO="audio"
http://x/v-144.m3u8
"""


def test_rewriter_parses_streams_and_media():
    rw = MasterRewriter(MASTER)
    assert rw.stream_count == 3, rw.stream_count
    assert rw.variant_uris() == [
        "http://x/v-1080.m3u8", "http://x/v-720.m3u8", "http://x/v-144.m3u8",
    ], rw.variant_uris()
    print("  test_rewriter_parses_streams_and_media OK")


def test_body_with_one_rung_exposes_only_the_lowest():
    """The first played revision must reference the CHEAPEST rendition."""
    body = MasterRewriter(MASTER).body_with(1)
    assert "v-144.m3u8" in body, body
    assert "v-1080.m3u8" not in body, "high renditions must not be published yet"
    assert "v-720.m3u8" not in body, body
    assert body.startswith("#EXTM3U"), body
    print("  test_body_with_one_rung_exposes_only_the_lowest OK")


def test_body_with_all_rungs_restores_every_rendition():
    body = MasterRewriter(MASTER).body_with(3)
    for uri in ("v-1080.m3u8", "v-720.m3u8", "v-144.m3u8"):
        assert uri in body, f"{uri} missing from {body}"
    print("  test_body_with_all_rungs_restores_every_rendition OK")


def test_audio_media_lines_survive_every_revision():
    """A revision that drops the EXT-X-MEDIA group plays video without sound."""
    rw = MasterRewriter(MASTER)
    for n in (1, 2, 3):
        body = rw.body_with(n)
        assert 'NAME="English"' in body, f"audio group lost at n={n}"
        assert 'NAME="Deutsch"' in body, f"alt audio lost at n={n}"
        assert body.count("#EXT-X-MEDIA:") == 2, body
    print("  test_audio_media_lines_survive_every_revision OK")


def test_body_is_monotonic_growing():
    """Climbing must only ever ADD renditions, never silently drop one."""
    rw = MasterRewriter(MASTER)
    prev = 0
    for n in (1, 2, 3):
        body = rw.body_with(n)
        count = body.count("#EXT-X-STREAM-INF:")
        assert count >= prev, f"revision {n} shrank from {prev} to {count}"
        prev = count
    assert prev == 3
    print("  test_body_is_monotonic_growing OK")


def test_body_with_oversized_count_is_clamped():
    body = MasterRewriter(MASTER).body_with(99)
    assert body.count("#EXT-X-STREAM-INF:") == 3, body
    print("  test_body_with_oversized_count_is_clamped OK")


def test_rewriter_reparses_its_own_output():
    """Stability: rewriting a rewritten master must not lose renditions."""
    rw = MasterRewriter(MASTER)
    once = rw.body_with(2)
    again = MasterRewriter(once)
    assert again.stream_count == 2, again.stream_count
    assert again.variant_uris() == ["http://x/v-720.m3u8", "http://x/v-144.m3u8"]
    print("  test_rewriter_reparses_its_own_output OK")


# ------------------------------------------------------------------ audio
def test_audio_ladder_is_bitrate_ordered():
    formats = [
        {"vcodec": "none", "acodec": "mp4a", "abr": 256, "url": "http://x/hi.m4a",
         "protocol": "https", "format_id": "251", "format_note": "English - original"},
        {"vcodec": "none", "acodec": "mp4a", "abr": 64, "url": "http://x/lo.m4a",
         "protocol": "https", "format_id": "139", "format_note": "English - original"},
    ]
    ladder = pick_audio_ladder(formats)
    assert [f["abr"] for f in ladder] == [64, 256], ladder
    print("  test_audio_ladder_is_bitrate_ordered OK")


def test_audio_ladder_skips_hls_and_video():
    formats = [
        {"vcodec": "none", "acodec": "mp4a", "abr": 128,
         "url": "http://x/a.m3u8", "protocol": "m3u8_native", "format_id": "233"},
        {"vcodec": "avc1", "acodec": "mp4a", "abr": 128,
         "url": "http://x/progressive.mp4", "protocol": "https", "format_id": "18"},
        {"vcodec": "none", "acodec": "mp4a", "abr": 128,
         "url": "http://x/ok.m4a", "protocol": "https", "format_id": "140"},
    ]
    ladder = pick_audio_ladder(formats)
    assert len(ladder) == 1, ladder
    assert ladder[0]["url"] == "http://x/ok.m4a"
    print("  test_audio_ladder_skips_hls_and_video OK")


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in fns:
        fn()
    print("test_quality OK")


if __name__ == "__main__":
    main()
