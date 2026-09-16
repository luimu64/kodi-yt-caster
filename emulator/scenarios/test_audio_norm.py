#!/usr/bin/env python3
"""Audio normalization: render queue, artifact store, and the serving path.

Covered here (all through the real addon code):

* a cold item's render is queued by the resolver with the remote audio URL,
  and the item keeps playing its original audio meanwhile (no blocking);
* the worker measures, computes the gain and writes both artifacts into the
  profile cache, and the same item is not rendered twice;
* once rendered, the served stream info hands out the local audio URL, and
  that URL is fetchable over the SAME port Kodi talks to — including a byte
  range (206), which is what makes seeking in a normalized track work. That
  exercises the out-of-process front end's /audio_norm/ forwarding;
* with normalization off, nothing is queued, nothing is fetched, and the
  original audio URL is untouched.

NOT covered (device-only): real ffmpeg (measurement, gain application,
segmentation, A/V sync against the untouched video rendition) and the live
Lounge cast. The ffmpeg call is stubbed through audio_norm's documented test
runner — see emulator/README.md.
"""
import json
import os
import time
import urllib.request

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402

COLD_ID = "a1"
NEXT_ID = "a2"

# The stubbed measurement: a quiet track with headroom, so the expected gain is
# derivable by hand (target -14, measured -22 -> +8 dB, capped by headroom at
# -1.0 - (-6.0) = +5.0 dB).
MEASURED_LUFS = -22.0
MEASURED_PEAK = -6.0
EXPECTED_GAIN_DB = 5.0

EBUR128_STDERR = (
    "  Integrated loudness:\n"
    f"    I:         {MEASURED_LUFS} LUFS\n"
    "    Threshold: -32.0 LUFS\n\n"
    "  True peak:\n"
    f"    Peak:       {MEASURED_PEAK} dBFS\n"
)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)


def _arm_ffmpeg_stub():
    """Install the deterministic runner + fetch stub BEFORE the service boots.

    Arming before boot matters: the service kicks an ffmpeg download during
    bootstrap, and a real 120 MB fetch in a scenario is both slow and a network
    dependency.
    """
    import resources.lib.audio_norm as audio_norm

    calls = []
    fetches = []

    def runner(binary, args):
        calls.append(list(args))
        joined = " ".join(args)
        if "ebur128" in joined:
            return EBUR128_STDERR
        out = args[-1]
        if out.endswith(".m4a"):
            _write(out, b"NORM-M4A")
            return ""
        # Segmentation pass: rewrite the playlist and one segment beside it.
        _write(out, "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:6.000,\nseg0000.ts\n#EXT-X-ENDLIST\n")
        _write(os.path.join(os.path.dirname(out), "seg0000.ts"), b"TS-SEGMENT")
        return ""

    def fetch_stub(dest_dir, notify=None):
        fetches.append(dest_dir)
        return os.path.join(dest_dir, "ffmpeg")

    audio_norm.set_test_runner(runner)
    audio_norm.fetch_ffmpeg = fetch_stub
    return calls, fetches


def _place_ffmpeg_binary():
    """Fake binary so ffmpeg_path() resolves and no download is attempted."""
    bin_dir = os.path.join(xbmcaddon.profile_dir(), "bin")
    os.makedirs(bin_dir, exist_ok=True)
    ffmpeg = os.path.join(bin_dir, "ffmpeg")
    with open(ffmpeg, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(ffmpeg, 0o755)
    return ffmpeg


def _reset_ffmpeg_stub():
    import resources.lib.audio_norm as audio_norm

    audio_norm.set_test_runner(None)


def _artifact_exists(video_id):
    """Ground truth on disk (plus the served-instance wiring the route needs)."""
    import resources.lib.audio_norm as audio_norm

    normalizer = audio_norm._INSTANCE
    assert normalizer is not None, "service must publish the normalizer instance"
    return normalizer.has_artifact(video_id)


def _source_calls(calls, video_id):
    return [c for c in calls if any(f"audio.example/{video_id}/" in a for a in c)]


def _public_port():
    import resources.lib.manifest_server as manifest_server

    return manifest_server.server_port()


def _get(url, range_header=None):
    req = urllib.request.Request(url)
    if range_header:
        req.add_header("Range", range_header)
    return urllib.request.urlopen(req, timeout=10)


def test_cold_item_is_queued_not_blocked_then_served():
    """Cold: original audio now, normalized audio from the next resolve on."""
    calls = []
    try:
        calls, _fetches = _arm_ffmpeg_stub()
        with Scenario(settings={"audio_normalize": "true"}) as s:
            _place_ffmpeg_binary()

            # Resolving a video the user just cast must return immediately, with
            # the ORIGINAL audio URL: rendering happens in the background.
            port = _public_port()
            with _get(f"http://127.0.0.1:{port}/resolve/{COLD_ID}") as resp:
                info = json.loads(resp.read().decode("utf-8"))
            assert info["audio_url"].startswith("http://audio.example/"), info["audio_url"]
            assert not info.get("audio_normalized")

            # The render was queued with the remote audio URL and completed.
            s.wait_until(lambda: _artifact_exists(COLD_ID), timeout=20.0,
                         what="normalized artifact to be rendered")
            assert _source_calls(calls, COLD_ID), "render must use the remote audio URL"
            measure = [c for c in calls if "ebur128" in " ".join(c)]
            assert len(measure) == 1, f"exactly one measurement pass, got {len(measure)}"
            encode = [c for c in calls if any("volume=" in a for a in c)]
            assert len(encode) == 1, "exactly one encode pass"
            assert f"volume={EXPECTED_GAIN_DB}dB" in " ".join(encode[0]), \
                f"gain must come from the measurement: {encode[0]}"

            # The rendered track is served on the port Kodi talks to, so the
            # next resolve of this video picks it up.
            with _get(f"http://127.0.0.1:{port}/resolve/{COLD_ID}") as resp:
                info = json.loads(resp.read().decode("utf-8"))
            assert info.get("audio_normalized") is True
            audio_url = info["audio_url"]
            assert f"/audio_norm/{COLD_ID}/norm.m4a" in audio_url, audio_url
            assert str(port) in audio_url, "must use the port Kodi talks to"

            # Whole-file fetch, then a byte range: seeking needs the 206.
            with _get(audio_url) as resp:
                assert resp.status == 200
                assert resp.read() == b"NORM-M4A"
                assert resp.headers.get("Accept-Ranges") == "bytes"
            with _get(audio_url, range_header="bytes=0-3") as resp:
                assert resp.status == 206, f"range request must be honoured, got {resp.status}"
                assert resp.headers.get("Content-Range") == "bytes 0-3/8"
                assert resp.read() == b"NORM"

            # The segmented HLS rendition for the video lane is served too, and
            # its segment URLs use the same origin as the playlist.
            base = audio_url.rsplit("/", 1)[0]
            with _get(f"{base}/audio.m3u8") as resp:
                playlist = resp.read().decode("utf-8")
            assert f"http://127.0.0.1:{port}/audio_norm/{COLD_ID}/seg0000.ts" in playlist, playlist
            with _get(f"{base}/seg0000.ts") as resp:
                assert resp.read() == b"TS-SEGMENT"

            # Rendering is once per video: another resolve must not re-queue it.
            before = len(calls)
            with _get(f"http://127.0.0.1:{port}/resolve/{COLD_ID}") as resp:
                resp.read()
            time.sleep(0.3)
            assert len(calls) == before, "a rendered video must not be rendered again"

            # A different video queues its own render.
            with _get(f"http://127.0.0.1:{port}/resolve/{NEXT_ID}") as resp:
                resp.read()
            s.wait_until(lambda: _source_calls(calls, NEXT_ID), timeout=20.0,
                         what="second video's render to be queued")

            # Casting still works with normalization on (no regression).
            s.phone.set_playlist(COLD_ID, [COLD_ID], current_time=0)
            s.wait_until(lambda: COLD_ID in (s.playing_file() or ""), timeout=15.0,
                         what=f"{COLD_ID} to start playing")
    finally:
        _reset_ffmpeg_stub()


def test_normalization_disabled_touches_nothing():
    """Off: no queue, no ffmpeg fetch, original audio URL untouched."""
    calls = []
    fetches = []
    try:
        calls, fetches = _arm_ffmpeg_stub()
        with Scenario(settings={"audio_normalize": "false"}) as s:
            _place_ffmpeg_binary()
            port = _public_port()
            for _ in range(2):
                with _get(f"http://127.0.0.1:{port}/resolve/{COLD_ID}") as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                assert info["audio_url"].startswith("http://audio.example/"), info["audio_url"]
                assert not info.get("audio_normalized")
            time.sleep(0.3)
            assert calls == [], f"disabled normalization must not call ffmpeg: {calls}"
            assert fetches == [], "disabled normalization must not download ffmpeg"
            assert not _artifact_exists(COLD_ID)
    finally:
        _reset_ffmpeg_stub()


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
