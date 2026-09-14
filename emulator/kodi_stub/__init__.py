"""Injectable fake Kodi Python API.

install() puts this directory on sys.path so `import xbmc` etc. resolve to
the fakes. The addon runtime code runs unmodified against them.
"""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_MODULES = ("xbmc", "xbmcgui", "xbmcaddon", "xbmcplugin", "xbmcvfs")


def install() -> None:
    if _here not in sys.path:
        sys.path.insert(0, _here)


def uninstall() -> None:
    while _here in sys.path:
        sys.path.remove(_here)
    for m in _MODULES:
        sys.modules.pop(m, None)


def reset() -> None:
    """Reset all stub state (fresh Kodi boot). Modules stay imported.

    Uses sys.modules['xbmc'] etc. — the top-level names the addon sees —
    never 'from . import', which would yield a SECOND, distinct module.
    """
    import xbmc, xbmcgui, xbmcaddon, xbmcplugin  # top-level via sys.path
    xbmc._reset()
    xbmcgui._reset()
    xbmcaddon._reset()
    xbmcplugin._reset()
