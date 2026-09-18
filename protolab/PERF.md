# Device Performance Harness

`perf_probe.py` is the on-device performance gate for this addon. It runs on the
target (measured on a Raspberry Pi 4B / LibreELEC / Kodi 21) with the board's own
python3, and reports one `PERF <metric>=<value>` line per measurement plus a
`FAILURES:` summary. Dev-box numbers are meaningless for this hardware class —
the whole point is the Pi's CPU and SD-card-class I/O.

```bash
scp protolab/perf_probe.py root@<device>:/storage/
ssh root@<device> 'python3 /storage/perf_probe.py'          # ~1 min
ssh root@<device> 'python3 /storage/perf_probe.py --skip-norm'
```

## What it measures (and the budget)

| id | metric | budget | why it matters |
|----|--------|--------|----------------|
| A0 | yt-dlp binary is an importable zipapp | 1 | `resources/lib/ytdlp_inproc` can only import a zipapp; a PyInstaller build silently disables the whole fast path |
| A2 | subprocess spawn cost | — | per-resolve cost the inproc path removes |
| D1/D2 | manifest-server HEAD latency, idle | <20ms p50 | |
| D3/D4 | HEAD latency while a cold resolve saturates the service | <200ms p50 | Kodi `CCurlFile::Stat`s this path before starting the next item; a starved server delays `PlaybackCleanup` and the stall looks like "next track never starts" |
| D5/D6 | live `/resolve/<id>` cold, first vs second (warm HTTP session) | <3s | user-visible time from cast to first frame |
| B1/B3 | resolve in a probe process; cache hit | <100ms | |
| E1/E2 | HLS preload early-ready; first segment through the local proxy | <8s / <1.5s | gapless auto-advance |
| E4 | CDN variant playlist RTT | — | network floor; separates "our code is slow" from "the CDN is slow" |
| F1 | 12 parallel title fetches (Kodi snapshots labels at `playlist.add()`) | <3.5s | gates time-to-first-sound on the queue path |
| G1/G3 | ebur128 / AAC-render realtime factor | — | decides whether loudness normalization can ever sit in the play path (it cannot: ~36x vs ~12x) |
| H4 | keep-alive reuse on one socket | 1 | a demuxer fetching dozens of segments must not pay a TCP connect per segment |
| H5 | kodi.bin RSS / threads | — | the in-process resolver holds yt-dlp inside `kodi.bin` |

## Measured on the Pi 4 (2026-09, addon 1.4.2)

| metric | value |
|---|---|
| live cold resolve, first after boot | 2.97s |
| live cold resolve, warm session | 1.18–1.60s |
| live cached resolve | 33–35ms |
| HEAD under a saturating resolve | 5.1ms p50 / 6.7ms max |
| preload early-ready | 0.78s |
| first segment via local proxy | 159ms (181KB, 9.1 Mbps) |
| 12 parallel titles | 0.47s |
| ebur128 / AAC render | 35.5x / 11.8x realtime |
| kodi.bin RSS | 270MB, 55 threads |

Before the zipapp fix the same probe reported 4.67s / 4.99s cold resolves,
because the aarch64 asset is a PyInstaller ELF that `zipimport` cannot load.

## Companion probes

- `inproc_ab.py <zipapp|elf>` — A/B one binary at a time, through the addon's own
  `ytdlp_bridge`/`ytdlp_inproc`. One process = one binary: `ytdlp_inproc.try_init`
  caches its first answer, so testing both in one process measures the ELF twice.
- `inproc_probe.py` — imports the pure-python release (`yt-dlp.tar.gz`) and times
  raw `extract_info` cold/warm, independent of the addon's asset selection.
- `stat_latency_probe.py` — the narrow STAT-under-load probe (D3/D4 in isolation).
