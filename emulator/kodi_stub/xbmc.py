"""Fake xbmc module: Player (simulated, Kodi quirks included), Monitor, PlayList,
executebuiltin, getCondVisibility, executeJSONRPC, log."""
import json
import queue
import threading
import time
import weakref

try:
    from .clock import SimulatedPlayback
except ImportError:  # imported as top-level module 'xbmc' via sys.path
    from clock import SimulatedPlayback

LOG_DEBUG, LOG_INFO, LOG_NOTICE, LOG_ERROR = 0, 1, 2, 4
PLAYLIST_MUSIC = 0
PLAYLIST_VIDEO = 1

LOG = []          # (level, message)
BUILTIN = []      # executebuiltin() call strings
JSONRPC = []      # executeJSONRPC() calls, decoded
MEDIA = {}        # url-prefix -> {"duration": s, "audio": bool}; longest prefix wins
DEFAULT_DURATION = 180.0
DEFAULT_RATE = 1.0

RESET_REQUESTS = queue.Queue()  # engine -> harness: url playing on fresh-Player Started

_volume = 50
_muted = False


def log(msg, level=LOG_INFO):
    LOG.append((level, f"[{threading.current_thread().name}] {msg}"))


def _reset():
    _engine.reset()
    while not RESET_REQUESTS.empty():
        try:
            RESET_REQUESTS.get_nowait()
        except queue.Empty:
            break
    LOG.clear()
    BUILTIN.clear()
    JSONRPC.clear()
    MEDIA.clear()
    global _volume, _muted
    _volume = 50
    _muted = False


def _lookup_media(url):
    """Longest-prefix match of url against the MEDIA registry."""
    m = {"duration": DEFAULT_DURATION, "audio": False}
    best = ""
    if url:
        for prefix, val in MEDIA.items():
            if url.startswith(prefix) and len(prefix) > len(best):
                best = prefix
                m.update(val if isinstance(val, dict) else {"duration": float(val)})
    return m


def executebuiltin(cmd):
    BUILTIN.append(str(cmd))


def getCondVisibility(cond):
    if cond == "Player.Paused":
        return bool(_engine and _engine.paused)
    return False


def executeJSONRPC(rpc):
    try:
        call = json.loads(rpc)
    except Exception:
        return json.dumps({"error": {"code": -32700, "message": "Parse error"}})
    JSONRPC.append(call)
    method = call.get("method")
    global _volume, _muted
    if method == "Application.SetVolume":
        v = call.get("params", {}).get("volume")
        if v == "mute":
            _muted = True
        elif v == "unmute":
            _muted = False
        else:
            try:
                _volume = max(0, min(int(v), 100))
            except (TypeError, ValueError):
                pass
    if method == "Application.GetProperties" and "volume" in call.get("params", {}).get("properties", []):
        return json.dumps({"result": {"volume": _volume}})
    return json.dumps({"result": "OK"})


class Monitor:
    _instance = None

    def __init__(self):
        self._abort = threading.Event()
        Monitor._instance = self

    def abortRequested(self):
        return self._abort.is_set()

    def waitForAbort(self, timeout=None):
        return self._abort.wait(timeout)

    def request_abort(self):
        self._abort.set()


class PlayList:
    """The ONE music playlist (xbmc.PlayList(PLAYLIST_MUSIC)).

    Kodi snapshots ListItem labels at add() time — we copy the label.
    """
    _instance = None
    _lock = threading.RLock()

    def __init__(self, playlist_type=PLAYLIST_MUSIC):
        self._type = playlist_type
        if playlist_type == PLAYLIST_MUSIC:
            PlayList._instance = self  # same underlying playlist every call
        self.clear()

    def clear(self):
        with self._lock:
            self._items = []   # (url, label_snapshotted, listitem_ref)
            self._position = -1

    def add(self, url, listitem=None, index=None):
        label = getattr(listitem, "_label", "") if listitem is not None else ""
        entry = (str(url), label, listitem)
        with self._lock:
            if index is None or index >= len(self._items):
                self._items.append(entry)
            else:
                self._items.insert(max(0, index), entry)

    def remove(self, url):
        url = str(url)
        with self._lock:
            idxs = [i for i, e in enumerate(self._items) if e[0] == url]
            for i in reversed(idxs):
                del self._items[i]
                if self._position > i:
                    self._position -= 1

    def size(self):
        with self._lock:
            return len(self._items)

    def getposition(self):
        with self._lock:
            return self._position

    def get_entry(self, idx):
        with self._lock:
            url, label, _ = self._items[idx]
            return type("Entry", (), {"url": url, "label": label})()

    # --- engine internals ---
    def _advance(self):
        with self._lock:
            self._position += 1
            return self._position < len(self._items)

    def _current_url(self):
        with self._lock:
            if 0 <= self._position < len(self._items):
                return self._items[self._position][0]
            return None


class _Engine:
    """The single playback backend shared by all Player views.

    Kodi has one player engine; xbmc.Player objects are views onto it. A
    pump thread fires lifecycle callbacks one at a time in FIFO order, so
    they always run on a non-caller thread AND in deterministic order.
    """
    def __init__(self):
        self.start_latency = 0.0  # seconds to delay Started (demuxer-open realism)
        self.url = None
        self.listitem = None
        self.clock = None
        self.audio_only = False
        self.events = queue.Queue()   # (event, url)
        self.event_log = []           # (event, url) as fired — assertion surface
        self.players = weakref.WeakSet()
        self._alive = threading.Event()
        self._alive.set()
        self._pump = threading.Thread(target=self._run, name="SimPlayerPump", daemon=True)
        self._pump.start()

    @property
    def paused(self):
        return bool(self.clock and self.clock.state == "paused")

    def register(self, player):
        self.players.add(player)

    def _run(self):
        while self._alive.is_set():
            try:
                event, url = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            self.event_log.append((event, url))
            if event == "started":
                # fresh-Player playback: ask holders of stale views to retire
                RESET_REQUESTS.put(url)
            for p in list(self.players):
                cb = getattr(p, f"onPlayBack{event.capitalize()}", None)
                if cb is None:
                    continue
                try:
                    cb()
                except Exception:
                    import traceback
                    log("stub player callback error: " + traceback.format_exc(), LOG_ERROR)

    def reset(self):
        self.start_latency = 0.0
        self.set_stopped()
        self.players.clear()
        self.event_log.clear()
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def stop_pump(self):
        self._alive.clear()

    # --- engine control ----------------------------------------------------
    def play_url(self, url, listitem=None):
        media = _lookup_media(url)
        self.url = str(url)
        self.listitem = listitem
        self.audio_only = bool(media.get("audio", False))
        self.clock = SimulatedPlayback(media["duration"], rate=DEFAULT_RATE)
        self.clock.play()
        if self.start_latency > 0:
            def _go():
                time.sleep(self.start_latency)
                self.events.put(("started", self.url))
            threading.Thread(target=_go, daemon=True, name="SimPlayerStartDelay").start()
        else:
            self.events.put(("started", self.url))

    def set_stopped(self):
        if self.clock:
            self.clock.stop()
        self.url = None
        self.listitem = None
        self.audio_only = False
        pl = PlayList._instance
        if pl:
            pl._position = -1

    def advance_playlist(self):
        pl = PlayList._instance
        if pl and pl._advance():
            self.play_url(pl._current_url())
        else:
            if pl:
                pl._position = -1
            self.set_stopped()
            self.events.put(("stopped", None))


_engine = _Engine()


def shutdown_engine():
    _engine.stop_pump()


class Player:
    """Simulated xbmc.Player with Kodi's quirks:

    - isPlaying() returns True while paused
    - pause() toggles pause/resume
    - callbacks fire asynchronously from the SimPlayerPump thread
    - end_of_media() (harness-driven) fires Ended, then auto-advances a playlist
    """

    def __init__(self):
        _engine.register(self)

    def play(self, item=None, listitem=None, windowed=False, playlist=None, startpos=-1):
        if playlist is not None:
            pl = playlist if isinstance(playlist, PlayList) else PlayList._instance
            pos = int(startpos)
            if not (0 <= pos < pl.size()):
                pos = 0
            pl._position = pos - 1  # advance_playlist() below brings it to pos
            _engine.advance_playlist()
            return
        if item is None:
            self.stop()
            return
        _engine.play_url(item, listitem)

    def pause(self):
        c = _engine.clock
        if c and c.state == "playing":
            c.pause()
            _engine.events.put(("paused", _engine.url))
        elif c and c.state == "paused":
            c.resume()
            _engine.events.put(("resumed", _engine.url))

    def stop(self):
        if _engine.url is not None:
            _engine.set_stopped()
            _engine.events.put(("stopped", None))

    def seekTime(self, t):
        if _engine.clock:
            _engine.clock.seek(t)

    # --- queries ---
    def isPlaying(self):
        return _engine.url is not None and _engine.clock is not None and _engine.clock.state != "stopped"

    def isPlayingAudio(self):
        return self.isPlaying() and _engine.audio_only

    def isPlayingVideo(self):
        return self.isPlaying() and not _engine.audio_only

    def getTime(self):
        if self.isPlaying() and _engine.clock:
            return _engine.clock.get_time()
        return 0.0

    def getTotalTime(self):
        if self.isPlaying() and _engine.clock:
            return _engine.clock.duration
        return 0.0

    def getPlayingFile(self):
        if _engine.url is None:
            raise RuntimeError("xbmc stub: no file playing")
        return _engine.url

    def getPlayingTitle(self):
        return getattr(_engine.listitem, "_label", "") or ""


def media_finished() -> bool:
    """True when the current clock reached its duration (harness-driven end)."""
    c = _engine.clock
    return bool(c and c.state == "playing" and c.get_time() >= c.duration)


def end_of_media():
    """Simulate the current media finishing (harness/simulator-driven).

    Kodi behaviour: Ended fires; with a playlist the next item Starts; the
    last item also fires Stopped.
    """
    if not media_finished():
        return False
    _engine.events.put(("ended", _engine.url))
    _engine.advance_playlist()
    return True
