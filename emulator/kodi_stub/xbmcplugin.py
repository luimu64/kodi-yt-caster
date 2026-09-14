"""Fake xbmcplugin: records setResolvedUrl / endOfDirectory calls (plugin entry)."""
import threading

RESOLVED = []     # (handle, succeeded, listitem)
EOD = []          # (handle, succeeded)


def setResolvedUrl(handle, succeeded, listitem):
    RESOLVED.append((handle, bool(succeeded), listitem))


def endOfDirectory(handle, succeeded=True, **kwargs):
    EOD.append((handle, bool(succeeded)))


def _reset():
    RESOLVED.clear()
    EOD.clear()
