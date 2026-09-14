"""Fake xbmcgui: ListItem (gettable state), Dialog, DialogProgress, notifications."""
import threading

NOTIFICATION_INFO = "info"
NOTIFICATION_WARNING = "warning"
NOTIFICATION_ERROR = "error"

NOTIFICATIONS = []   # (title, message, icon, ms)
DIALOGS = []         # DialogProgress lifecycle: (event, heading_or_pct, line)


class ListItem:
    def __init__(self, label="", label2="", path="", offscreen=False):
        self._label = label
        self._label2 = label2
        self._path = path
        self._info = {}
        self._art = {}
        self._properties = {}
        self._mimetype = None

    def setLabel(self, label):
        self._label = label

    def getLabel(self):
        return self._label

    def setLabel2(self, label2):
        self._label2 = label2

    def setInfo(self, type_, infoLabels):
        self._info.setdefault(type_, {}).update(infoLabels or {})

    def setArt(self, art):
        self._art.update(art or {})

    def setPath(self, path):
        self._path = path

    def getPath(self):
        return self._path

    def setProperty(self, key, value):
        self._properties[key] = value

    def setMimeType(self, mimetype):
        self._mimetype = mimetype


class Dialog:
    def notification(self, title, message, icon=NOTIFICATION_INFO, time_ms=5000):
        NOTIFICATIONS.append((title, message, icon, time_ms))

    def ok(self, heading, line):
        DIALOGS.append(("ok", heading, line))
        return True

    def yesno(self, heading, line):
        DIALOGS.append(("yesno", heading, line))
        return False

    def input(self, prompt, **kwargs):
        return ""


class DialogProgress:
    def __init__(self):
        self.canceled = threading.Event()
        self._created = threading.Event()
        self._closed = threading.Event()

    def create(self, heading, line=""):
        DIALOGS.append(("create", heading, line))
        self._created.set()
        self._closed.clear()

    def update(self, percent, line=""):
        DIALOGS.append(("update", percent, line))

    def iscanceled(self):
        return self.canceled.is_set()

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        DIALOGS.append(("close", None, None))


def _reset():
    NOTIFICATIONS.clear()
    DIALOGS.clear()
