#!/usr/bin/env python3
"""Which rendition of a generated HLS master the player ends up on.

The video lane plays the addon's own localhost master through Kodi's ffmpeg
demuxer (inputstream.adaptive is deliberately NOT used for these detached-audio
masters), and a non-adaptive HLS demuxer does not honour playlist order or a
resolution cap — it takes the variant with the HIGHEST BANDWIDTH in the master.
So the master must not merely list a decodable rendition first; it must not
offer an undecodable one at all.

Device evidence this scenario encodes (Pi 4, LibreELEC 12.2.1): the master
listed AVC 1080p60 6.9 Mbps first and VP9 2160p60 43 Mbps later; Kodi opened
itag/628 (the VP9 4K rendition) and produced

    CDVDVideoCodecDRMPRIME::AddData - send packet failed: Invalid data ...
    CVideoPlayerAudio::Process - stream stalled
    CVideoPlayer::HandlePlaySpeed - audio stream stalled, triggering re-sync
    VideoPlayer::Sync - Video - pts: ..., cache: 50000  (audio cache: 301859)

i.e. a still frame with audio playing on: the multi-second A/V desync.

Assertions are on the SERVED master body — the artifact the demuxer actually
reads — not on the in-memory format list.
"""
import re
import urllib.request

import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()

from resources.lib.ytdlp_bridge import build_hls_master_manifest  # noqa: E402

# Format list shaped like a real YouTube HLS resolve for a 4K60 video: the AVC
# ladder (itags 312/311/231/230/229/269) tops out at 1080p, and the VP9
# renditions (628/623/602) are the high-bitrate ones the demuxer would pick.
LIVE_SHAPED_FORMATS = [
    {"format_id": "234", "url": "https://example.com/audio.m3u8", "vcodec": "none",
     "acodec": "mp4a", "format_note": "American English - original"},
    {"format_id": "312", "url": "https://example.com/312.m3u8", "vcodec": "avc1.64002A",
     "height": 1080, "width": 1920, "fps": 60, "tbr": 6943.926, "resolution": "1920x1080"},
    {"format_id": "311", "url": "https://example.com/311.m3u8", "vcodec": "avc1.640020",
     "height": 720, "width": 1280, "fps": 60, "tbr": 4358.389, "resolution": "1280x720"},
    {"format_id": "231", "url": "https://example.com/231.m3u8", "vcodec": "avc1.4D401F",
     "height": 480, "width": 854, "fps": 30, "tbr": 1492.4, "resolution": "854x480"},
    {"format_id": "230", "url": "https://example.com/230.m3u8", "vcodec": "avc1.4D401E",
     "height": 360, "width": 640, "fps": 30, "tbr": 898.266, "resolution": "640x360"},
    {"format_id": "229", "url": "https://example.com/229.m3u8", "vcodec": "avc1.4D4015",
     "height": 240, "width": 426, "fps": 30, "tbr": 417.592, "resolution": "426x240"},
    {"format_id": "269", "url": "https://example.com/269.m3u8", "vcodec": "avc1.4D400C",
     "height": 144, "width": 256, "fps": 30, "tbr": 210.355, "resolution": "256x144"},
    {"format_id": "628", "url": "https://example.com/628.m3u8", "vcodec": "vp09.00.51.08",
     "height": 2160, "width": 3840, "fps": 60, "tbr": 42997.562, "resolution": "3840x2160"},
    {"format_id": "623", "url": "https://example.com/623.m3u8", "vcodec": "vp09.00.50.08",
     "height": 1440, "width": 2560, "fps": 60, "tbr": 26215.94, "resolution": "2560x1440"},
    {"format_id": "602", "url": "https://example.com/602.m3u8", "vcodec": "vp09.00.10.08",
     "height": 144, "width": 256, "fps": 15, "tbr": 175.796, "resolution": "256x144"},
]

BANDWIDTH_RE = re.compile(r"#EXT-X-STREAM-INF:[^\n]*BANDWIDTH=(\d+)[^\n]*", re.M)


def _fetch(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode("utf-8")


def _variants(body):
    """[(bandwidth, stream_inf_line), ...] in playlist order."""
    out = []
    for line in body.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            bw = BANDWIDTH_RE.match(line)
            out.append((int(bw.group(1)) if bw else 0, line))
    return out


def _demuxer_pick(body):
    """The rendition Kodi's ffmpeg HLS demuxer selects: highest BANDWIDTH."""
    variants = _variants(body)
    assert variants, "master must expose at least one video rendition"
    return max(variants, key=lambda v: v[0])[1]


def test_master_hides_undecodable_renditions():
    url = build_hls_master_manifest(LIVE_SHAPED_FORMATS, "v_4k60")
    assert url, "master must be built"
    body = _fetch(url)

    assert "vp09" not in body, (
        "master must not expose VP9 renditions when an H.264 ladder exists — "
        "the demuxer picks the highest bandwidth, so VP9 4K wins and the Pi 4 "
        f"cannot play it:\n{body}"
    )
    assert "RESOLUTION=3840x2160" not in body, "4K rendition must not be exposed"
    assert "RESOLUTION=1920x1080" in body, "the H.264 ladder must still be exposed"
    assert "#EXT-X-MEDIA:TYPE=AUDIO" in body, "audio group must survive the codec ceiling"


def test_demuxer_pick_is_the_h264_top_rung():
    """The rendition the non-adaptive demuxer lands on must be decodable.

    This is the assertion that fails while merely ORDERING H.264 first: the pick
    is max-BANDWIDTH, not first-listed.
    """
    body = _fetch(build_hls_master_manifest(LIVE_SHAPED_FORMATS, "v_4k60"))
    pick = _demuxer_pick(body)
    assert 'CODECS="avc1' in pick, f"demuxer would pick a non-H.264 rendition: {pick}"
    assert "RESOLUTION=1920x1080" in pick, f"pick should be the 1080p H.264 rung: {pick}"
    assert "BANDWIDTH=42997562" not in body, "the 43 Mbps VP9 rendition must be gone"


def test_h264_only_master_is_unchanged():
    """A master with no undecodable family at all keeps its full ladder."""
    avc_only = [f for f in LIVE_SHAPED_FORMATS if str(f.get("vcodec") or "").startswith("avc1")]
    avc_only = avc_only + [LIVE_SHAPED_FORMATS[0]]
    body = _fetch(build_hls_master_manifest(avc_only, "v_1080"))
    for res in ("1920x1080", "1280x720", "854x480", "640x360", "426x240", "256x144"):
        assert f"RESOLUTION={res}" in body, f"ladder rung {res} must be exposed:\n{body}"


def test_vp9_only_master_still_builds():
    """When VP9 is all YouTube offers, the master still gets built (no crash)."""
    vp9_only = [LIVE_SHAPED_FORMATS[0]] + [f for f in LIVE_SHAPED_FORMATS if str(f.get("vcodec")).startswith("vp09")]
    body = _fetch(build_hls_master_manifest(vp9_only, "v_vp9only"))
    assert "vp09" in body, "a VP9-only video must still expose something to play"


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"{name} OK")
            except Exception:
                failed += 1
                print(f"{name} FAILED")
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
