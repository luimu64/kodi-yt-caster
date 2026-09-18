"""Automatic quality ladder: start low, climb to the best rendition invisibly.

The problem this solves is time-to-first-frame, not throughput. On the video
lane Kodi opens the generated HLS master, inputstream.adaptive picks a
rendition and fetches its media playlist and first segments before anything is
on screen; on a 4K master the chosen rendition can be tens of MB of segment
data away. Playing a small rendition first and republishing the master with
bigger ones makes the picture appear in ~1s and lets the quality rise behind
the user's back.

Two facts about the manifests this addon generates decide the shape:

* The master is generated and served by this addon over localhost HTTP, so a
  "better quality" is not a re-resolve — it is a rewrite of a few hundred bytes
  of text we already hold. Climbing costs no yt-dlp work and no network fetch.
* inputstream.adaptive re-reads a VOD master only when asked to. It is handed a
  fresh rendition list by bumping the master's ``#EXT-X-MEDIA-SEQUENCE``-like
  version marker, which the addon does by republishing the body under the same
  name; IA's manifest update picks up the new variant set and switches to it at
  the next segment boundary.

The ladder is deliberately split from the user's ``max_resolution`` setting:
that setting is a CEILING (never exceed it), the ladder is the ORDER of
renditions tried. ``QualityLadder.rungs()`` returns the renditions from lowest
to highest, already filtered to H.264 where an H.264 option exists at that
height (a Pi 4 cannot hardware-decode VP9, so leading with VP9 shows a black
screen rather than a low-quality picture) and capped by the ceiling.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ytlounge.quality")

# Rung names are the resolutions inputstream.adaptive itself understands, plus
# the raw pixel heights we compute from. Keep the ordering authoritative: the
# ladder climbs this list and never goes back down within one item.
_RES_CEILINGS: Dict[str, int] = {
    "480p": 480,
    "640p": 640,
    "720p": 720,
    "1080p": 1080,
    "2K": 1440,
    "1440p": 1440,
    "4K": 2160,
}

# "Auto" is the default and means: start at the bottom rung, climb to the
# ceiling the user allows. Anything else in the existing max_resolution setting
# is honoured as a hard cap so the ladder can never contradict the user.
DEFAULT_START_HEIGHT = 144


def _is_avc(vcodec: str) -> bool:
    return str(vcodec or "").startswith("avc1")


def resolve_ceiling(max_resolution: str) -> Optional[int]:
    """Pixel-height cap implied by the user's max_resolution setting.

    Returns None for "auto"/unset (no cap beyond what the video offers).
    """
    key = str(max_resolution or "").strip()
    if not key or key == "auto":
        return None
    return _RES_CEILINGS.get(key)


class QualityLadder:
    """Ordered rendition list for one video, derived from its format list."""

    def __init__(self, video_id: str, video_formats: List[Dict[str, Any]],
                 max_resolution: str = "auto"):
        self.video_id = video_id
        self.max_resolution = max_resolution
        self._rungs: List[Dict[str, Any]] = self._build(video_formats)
        self._index = 0

    # ------------------------------------------------------------- building
    @staticmethod
    def _height(f: Dict[str, Any]) -> int:
        try:
            return int(f.get("height") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _tbr(f: Dict[str, Any]) -> float:
        try:
            return float(f.get("tbr") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _build(self, formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One rendition per height: H.264 when that height offers it, highest
        bitrate first within the codec family, sorted low height to high."""
        by_height: Dict[int, Dict[str, Any]] = {}
        for f in formats:
            height = self._height(f)
            if height <= 0:
                continue
            cur = by_height.get(height)
            if cur is None:
                by_height[height] = f
                continue
            new_avc = _is_avc(str(f.get("vcodec") or ""))
            cur_avc = _is_avc(str(cur.get("vcodec") or ""))
            if new_avc != cur_avc:
                # Different codec family at the same height: H.264 wins
                # outright. VP9 at the same height is not "better" on a device
                # that cannot hardware-decode it — it is unplayable.
                if new_avc:
                    by_height[height] = f
                continue
            if self._tbr(f) > self._tbr(cur):
                by_height[height] = f

        ceiling = resolve_ceiling(self.max_resolution)
        rungs = [f for h, f in by_height.items() if ceiling is None or h <= ceiling]
        rungs.sort(key=self._height)
        return rungs

    # -------------------------------------------------------------- querying
    @property
    def rungs(self) -> List[Dict[str, Any]]:
        return list(self._rungs)

    def rung_count(self) -> int:
        return len(self._rungs)

    def heights(self) -> List[int]:
        return [self._height(f) for f in self._rungs]

    def start_index(self) -> int:
        """Lowest rung that still buys a real latency win.

        The absolute floor (144p) is what actually starts in ~1s; if it is not
        offered, the lowest available is used. Only a single-rendition ladder
        starts at its top, because there is nothing to climb to.
        """
        if not self._rungs:
            return 0
        for i, f in enumerate(self._rungs):
            if self._height(f) >= DEFAULT_START_HEIGHT:
                return i
        return 0

    def top_index(self) -> int:
        return max(0, len(self._rungs) - 1)

    def current(self) -> Optional[Dict[str, Any]]:
        if not self._rungs:
            return None
        return self._rungs[min(self._index, len(self._rungs) - 1)]

    def advance(self) -> Optional[Dict[str, Any]]:
        """Move one rung up; None when already at the top."""
        if self._index >= len(self._rungs) - 1:
            return None
        self._index += 1
        return self._rungs[self._index]

    def set_index(self, index: int) -> Optional[Dict[str, Any]]:
        self._index = max(0, min(index, len(self._rungs) - 1))
        return self.current()

    def at_top(self) -> bool:
        return self._index >= len(self._rungs) - 1

    def summary(self) -> str:
        return "{} rungs {}".format(len(self._rungs), self.heights())


def pick_audio_ladder(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Audio renditions ordered worst-to-best, de-duplicated per track name.

    The audio lane has no resolution to trade, so the crawl is a bitrate ramp.
    YouTube serves each track as a low and a high itag variant; both are kept
    here because the low one is the fast start and the high one the upgrade.
    """
    cands = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        and str(f.get("protocol") or "").startswith("http")
        and ".m3u8" not in str(f.get("url") or "")
    ]
    if not cands:
        return []

    def _q(f: Dict[str, Any]) -> float:
        for key in ("abr", "tbr"):
            val = f.get(key)
            if val:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        fid = str(f.get("format_id") or "")
        return float(fid) if fid.isdigit() else 0.0

    cands.sort(key=_q)
    return cands


class MasterRewriter:
    """Rewrites a generated HLS master to expose a chosen prefix of the ladder.

    The addon's master already contains one ``#EXT-X-STREAM-INF`` block per
    rendition (best first). Serving only the first N rungs — lowest first — is
    what makes the initial fetch tiny; republishing with more rungs is the
    upgrade. Audio renditions are carried through untouched on every revision
    so the detached audio group never goes missing mid-climb.
    """

    _STREAM_INF = "#EXT-X-STREAM-INF:"
    _MEDIA = "#EXT-X-MEDIA:"

    def __init__(self, master_body: str):
        self._lines = [l for l in master_body.splitlines() if l.strip()]
        self._header: List[str] = []
        self._streams: List[Tuple[str, str]] = []   # (stream-inf line, uri line)
        self._media: List[str] = []
        self._tail: List[str] = []
        self._parse()

    def _parse(self) -> None:
        i = 0
        lines = self._lines
        while i < len(lines):
            line = lines[i]
            s = line.strip()
            if s.startswith(self._MEDIA):
                self._media.append(line)
                i += 1
                continue
            if s.startswith(self._STREAM_INF):
                uri = ""
                if i + 1 < len(lines) and not lines[i + 1].strip().startswith("#"):
                    uri = lines[i + 1]
                self._streams.append((line, uri))
                i += 2 if uri else 1
                continue
            if self._streams or self._media:
                self._tail.append(line)
            else:
                self._header.append(line)
            i += 1

    @property
    def stream_count(self) -> int:
        return len(self._streams)

    def variant_uris(self) -> List[str]:
        return [uri for _line, uri in self._streams if uri]

    def body_with(self, first_n: int) -> str:
        """Master exposing at most ``first_n`` renditions (best-first order).

        ``first_n`` counts from the TOP of the existing list, because the
        addon's generator already sorts best-first and the ladder climbs by
        widening this window from the bottom of the quality range upward.
        """
        first_n = max(1, min(int(first_n), len(self._streams) or 1))
        out: List[str] = list(self._header)
        out.extend(self._media)
        for line, uri in self._streams[-first_n:]:
            out.append(line)
            if uri:
                out.append(uri)
        out.extend(self._tail)
        if not out or not out[0].startswith("#EXTM3U"):
            out.insert(0, "#EXTM3U")
        return "\n".join(out) + "\n"
