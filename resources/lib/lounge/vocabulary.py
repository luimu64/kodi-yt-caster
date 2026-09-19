"""Declared outbound report vocabulary (R10).

The ten families the phone's casting UI is fed by, which of them this receiver
emits, and — for the ones it does not — why not. The coverage line below is
logged at session start and whenever it changes, so a missing family is a
declared fact rather than a silent default on the phone:

    vocabulary: 7/10 (missing: onSubtitlesTrackChanged, onPlaybackSpeedChanged,
                                autoplayModeChanged)

Adding a family is a one-line entry here plus the emitter site; it must never
require a second report path (R7/R9).
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple

class Family(NamedTuple):
    name: str
    payload: str
    implemented: bool
    why_not: str = ""

# The ten families the official sender expects (analysis §4.1).
VOCABULARY: List[Family] = [
    Family(
        "nowPlaying",
        "videoId, currentTime, duration, state, cpn, currentIndex, listId, "
        "seekableStartTime, seekableEndTime, loadedTime",
        True,
    ),
    Family(
        "nowPlayingPlaylist",
        "videoIds, videoId, currentIndex, currentTime, duration, state, listId",
        True,
    ),
    Family("onStateChange", "state, currentTime, duration, cpn", True),
    Family("onVolumeChanged", "volume, muted", True),
    Family(
        "onAdStateChange",
        "adState, adDuration, adPosition, isSkippable",
        True,
    ),
    Family(
        "onAdPlaying",
        "adState, adDuration, adPosition, isSkippable",
        True,
    ),
    Family(
        "autoplayUpNext",
        "videoId, listId (omitted when the receiver does not know what is next)",
        True,
    ),
    Family(
        "onSubtitlesTrackChanged",
        "trackName, languageCode, isDefault",
        False,
        "we do not enumerate or select subtitle tracks; the receiver has no source for them",
    ),
    Family(
        "onPlaybackSpeedChanged",
        "playbackRate",
        False,
        "we do not read or set the Kodi player's speed; the receiver has no source for it",
    ),
    Family(
        "autoplayModeChanged",
        "autoplayMode",
        False,
        "the phone's autoplay mode is not part of the state we receive or own",
    ),
]

def implemented_names() -> List[str]:
    return [f.name for f in VOCABULARY if f.implemented]

def missing_names() -> List[str]:
    return [f.name for f in VOCABULARY if not f.implemented]

def coverage_line() -> str:
    """`vocabulary: <n>/<total> (missing: a, b, ...)` — the R10 coverage line."""
    missing = missing_names()
    base = f"vocabulary: {len(implemented_names())}/{len(VOCABULARY)}"
    if missing:
        base += f" (missing: {', '.join(missing)})"
    return base

def is_implemented(name: str) -> bool:
    return any(f.name == name and f.implemented for f in VOCABULARY)

def as_table() -> List[Dict[str, str]]:
    """The declared table, for docs/tests. Payload fields are a declaration,
    not enforcement."""
    return [
        {
            "name": f.name,
            "payload": f.payload,
            "implemented": "yes" if f.implemented else "no",
            "why_not": f.why_not,
        }
        for f in VOCABULARY
    ]

if __name__ == "__main__":
    assert len(VOCABULARY) == 10, len(VOCABULARY)
    assert len({f.name for f in VOCABULARY}) == 10
    assert coverage_line().startswith("vocabulary: ")
    assert len(implemented_names()) == 7, implemented_names()
    assert set(missing_names()) == {
        "onSubtitlesTrackChanged", "onPlaybackSpeedChanged", "autoplayModeChanged"
    }
    assert is_implemented("onAdStateChange") and not is_implemented("autoplayModeChanged")
    print(coverage_line())
