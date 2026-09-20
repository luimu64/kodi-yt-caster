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

# Window ids used by the receiver (Kodi's real values).
WINDOW_HOME = 10000
WINDOW_FULLSCREEN_VIDEO = 12005
WINDOW_VISUALISATION = 12006
# The music playlist view (MyMusicPlaylist.xml). Only used by scenarios that
# model the user browsing: to the receiver every window other than the
# visualisation one looks the same.
WINDOW_MUSIC_PLAYLIST = 10500

# Device-verified Kodi quirk: on LibreELEC 12 / Kodi 21.3 the service process
# receives NO xbmc.Player callbacks at all (not even the first cast's Started).
# Set by a scenario that needs to model that silence — the receiver then has to
# notice playback changes from its own polls.
SUPPRESS_PLAYER_CALLBACKS = False


class _Windows:
    """Kodi's window history, the part the receiver can disturb.

    Mirrors the real rules that matter here:
      * starting a VIDEO item activates the fullscreen video window (12005),
      * an audio item does NOT activate the visualisation window by itself
        (Kodi only requests fullscreen for the FIRST file of a music-playlist
        session, and only with the right setting) — the receiver activates
        12006 explicitly,
      * ActivateWindow() makes a window active and leaves everything above it
        closed (Kodi pops the duplicate entry out of the history).
    """

    def __init__(self):
        self.history = [WINDOW_HOME]

    @property
    def active(self):
        return self.history[-1]

    def activate(self, win):
        if win == self.active:
            return
        self.history = [w for w in self.history if w != win]
        self.history.append(win)

    def previous(self):
        if len(self.history) > 1:
            self.history.pop()

    def reset(self):
        self.history = [WINDOW_HOME]


_windows = _Windows()

# A modal dialog (Kodi's Busy dialog is up during playback start) makes
# CGUIWindowManager::ActivateWindow_Internal REFUSE the activation:
#   "Activate of window 'X' refused because there are active modal dialogs"
# The caller only learns by checking whether the window actually became active.
_modal_dialogs = 0


def set_modal_dialog(active):
    """Model a modal dialog (DialogBusy) being up; ActivateWindow is refused."""
    global _modal_dialogs
    _modal_dialogs = 1 if active else 0


def modal_dialog_active():
    return _modal_dialogs > 0

RESET_REQUESTS = queue.Queue()  # engine -> harness: url playing on fresh-Player Started

_volume = 50
_muted = False


def log(msg, level=LOG_INFO):
    LOG.append((level, f"[{threading.current_thread().name}] {msg}"))


def _reset():
    _engine.reset()
    # The music playlist singleton outlives the engine: without clearing it a
    # previous scenario's queue keeps auto-advancing into the next scenario.
    if PlayList._instance is not None:
        PlayList._instance.clear()
    while not RESET_REQUESTS.empty():
        try:
            RESET_REQUESTS.get_nowait()
        except queue.Empty:
            break
    LOG.clear()
    BUILTIN.clear()
    JSONRPC.clear()
    MEDIA.clear()
    _windows.reset()
    global _volume, _muted, SUPPRESS_PLAYER_CALLBACKS, _modal_dialogs
    _volume = 50
    _muted = False
    SUPPRESS_PLAYER_CALLBACKS = False
    _modal_dialogs = 0


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
    _apply_window_builtin(str(cmd))


def _apply_window_builtin(cmd):
    """Model the window builtins the receiver uses (ActivateWindow only)."""
    if not cmd.startswith("ActivateWindow("):
        return
    args = cmd[len("ActivateWindow("):].rstrip(")").split(",")
    if not args:
        return
    target = args[0].strip()
    win = None
    if target.isdigit():
        win = int(target)
    elif target.lower() in ("visualisation", "visualization"):
        win = WINDOW_VISUALISATION
    elif target.lower() == "fullscreenvideo":
        win = WINDOW_FULLSCREEN_VIDEO
    elif target.lower() == "home":
        win = WINDOW_HOME
    if win is not None:
        if _modal_dialogs and win != _windows.active:
            # Real Kodi behaviour (GUIWindowManager::ActivateWindow_Internal):
            # the activation is refused, not queued — the caller must retry.
            log(f"Activate of window '{win}' refused because there are active modal dialogs",
                LOG_INFO)
            return
        _windows.activate(win)


def _active_window():
    return _windows.active


def window_is_active(win):
    return _windows.active == win


def user_opens_window(win):
    """Model the USER navigating to a window (home, playlist, settings, ...).

    Same history rule as ``ActivateWindow``, but this is user input: whatever
    the receiver asserts afterwards is a fight with the person holding the
    remote. Scenarios use it to hold the receiver to the rule that the GUI
    belongs to the user — the receiver cannot tell this from one of Kodi's own
    window pops, so it must simply stop asserting outside its bounded repair
    window.
    """
    _windows.activate(win)


def getCondVisibility(cond):
    if cond == "Player.Paused":
        return bool(_engine and _engine.paused)
    if cond == "Window.IsActive(visualisation)":
        return _windows.active == WINDOW_VISUALISATION
    if cond == "Window.IsActive(fullscreenvideo)":
        return _windows.active == WINDOW_FULLSCREEN_VIDEO
    if cond == "Window.IsActive(home)":
        return _windows.active == WINDOW_HOME
    return False


def getInfoLabel(label):
    """Kodi info labels.

    ``Player.FileNameAndPath`` carries the URL the player was handed — including
    ``plugin://<addon-id>/?play=<id>`` for music-queue items. Device-verified:
    that label is the receiver's ONLY track-change signal on Kodi 21, because
    ``getPlayingFile()`` returns the final resolved CDN URL (no video id), so a
    ``?play=`` parse there never matches and the watchdog stays silent.
    """
    label = str(label)
    url = _engine.url or ""
    if label == "Player.FileNameAndPath":
        return url
    if label == "Player.Title":
        return url.rsplit("/", 1)[-1] if url else ""
    return ""


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

    ``xbmc.PlayList(PLAYLIST_MUSIC)`` hands back the SAME live playlist object
    on every call; constructing one must never disturb its contents. The
    receiver builds the queue, plays it, and later re-opens the playlist to
    patch upcoming labels — if a "new" PlayList came back empty (or replaced
    the object the engine is advancing), the queue would silently vanish
    mid-playback, which is not something Kodi does.
    """
    _instance = None
    _lock = threading.RLock()

    def __new__(cls, playlist_type=PLAYLIST_MUSIC):
        inst = cls._instance
        if inst is None:
            inst = super().__new__(cls)
            inst._type = playlist_type
            inst._items = []   # (url, label_snapshotted, listitem_ref)
            inst._position = -1
            cls._instance = inst
        return inst

    def __init__(self, playlist_type=PLAYLIST_MUSIC):
        # Deliberately empty: re-obtaining the playlist is not a reset.
        pass

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
        # Device-observed race: when the audio player starts while the video
        # player is still the active one, Kodi's PlaybackCleanup skips the
        # fullscreen-video window cleanup (it only closes that window when the
        # video player is already gone and the window is the active one). The
        # stale 12005 then keeps rendering the video's last frame on top while
        # the audio plays underneath. An explicit video stop before the audio
        # play avoids it — that is what the receiver has to do.
        self.stale_video_window_on_lane_switch = True
        self._window_left_stale = False
        # Device-verified lane-switch race: when Kodi switches from a video item
        # to an audio one, the outgoing VIDEO player is still closing while the
        # next item already plays — kodi.log shows 'Saving file state for video
        # item <old>' after the new item started, and a receiver that decides
        # the lane once at that instant sees isPlayingVideo() == True and gives
        # up. Scenarios set a lag (seconds) to model it; 0 disables it.
        self.video_teardown_lag = 0.0
        self._video_teardown_until = 0.0
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
            if SUPPRESS_PLAYER_CALLBACKS:
                # LibreELEC/Kodi 21 device behaviour: the service process gets
                # no callbacks at all, so the receiver must poll for changes.
                continue
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
        self.stale_video_window_on_lane_switch = True
        self.video_teardown_lag = 0.0
        self._video_teardown_until = 0.0
        self._window_left_stale = False
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
        was_video = self.url is not None and not self.audio_only
        audio = bool(media.get("audio", False))
        if was_video and audio:
            if self.stale_video_window_on_lane_switch:
                # Lane switch with the video player still the active one: Kodi's
                # window cleanup misses it (see the flag's comment) — 12005 stays
                # on top until someone closes it.
                self._window_left_stale = True
            else:
                # Device-verified (probe on Kodi 21.3 / LibreELEC, video -> audio
                # switch): Kodi closes the fullscreen video window itself, leaves
                # nothing in its place, and the audio item activates no window at
                # all — the GUI is left on whatever sat underneath (Home). The
                # receiver has to request the music window explicitly.
                if _windows.active == WINDOW_FULLSCREEN_VIDEO:
                    _windows.previous()
            if self.video_teardown_lag > 0:
                # ...but the outgoing video PLAYER lingers: isPlayingVideo()
                # keeps reporting True for a while even though the audio item is
                # already playing (device: 'Saving file state for video item').
                self._video_teardown_until = time.monotonic() + self.video_teardown_lag
        self.url = str(url)
        self.listitem = listitem
        self.audio_only = audio
        self.clock = SimulatedPlayback(media["duration"], rate=DEFAULT_RATE)
        self.clock.play()
        if not audio:
            # Kodi activates the fullscreen video window for a video item, and
            # the music window goes away with it (device-verified: a music video
            # starting Deinits MusicVisualisation.xml, and the later video->audio
            # switch leaves Home on screen, not the visualiser underneath).
            if WINDOW_VISUALISATION in _windows.history:
                _windows.history = [w for w in _windows.history if w != WINDOW_VISUALISATION]
            _windows.activate(WINDOW_FULLSCREEN_VIDEO)
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
        # The outgoing video player's lingering mode cannot survive a stop —
        # this is what makes the receiver's own re-open land on the audio lane.
        self._video_teardown_until = 0.0
        self.url = None
        self.listitem = None
        self.audio_only = False
        # Kodi's PlaybackCleanup: leaving fullscreen video when it is the
        # active window — unless the stale-switch race left it behind.
        if _windows.active == WINDOW_FULLSCREEN_VIDEO and not self._window_left_stale:
            _windows.previous()
        self._window_left_stale = False
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

    def play(self, item=None, listitem=None, windowed=False, startpos=-1):
        """Kodi's xbmc.Player.play(item, listitem=None, windowed=False, startpos=-1).

        ``item`` may be a URL string or a PlayList (Kodi's ``playPlaylist``:
        the item at ``startpos`` is played — the receiver relies on that for
        the music-queue lane).
        """
        if item is None:
            self.stop()
            return
        if isinstance(item, PlayList):
            pl = item
            pos = int(startpos)
            if not (0 <= pos < pl.size()):
                pos = 0
            pl._position = pos - 1  # advance_playlist() below brings it to pos
            _engine.advance_playlist()
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
        if self.isPlaying() and _engine._video_teardown_until > time.monotonic():
            # Device-verified: the item opened in the outgoing video player's
            # mode reports NOT-audio as well, even though only its audio decoder
            # runs (isPlayingAudio() False, isPlayingVideo() True).
            return False
        return self.isPlaying() and _engine.audio_only

    def isPlayingVideo(self):
        if self.isPlaying() and _engine._video_teardown_until > time.monotonic():
            # The outgoing video player is still closing (device lane-switch
            # race) even though the next item is already playing.
            return True
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
