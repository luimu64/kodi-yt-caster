"""Fake xbmcaddon: in-memory settings + a real (tempdir) profile directory."""
import os
import shutil
import tempfile


class _State:
    settings = {}
    profile = os.path.join(tempfile.mkdtemp(prefix="ytc-emulator-"), "userdata", "addon_data", "plugin.service.ytlounge-cast") + os.sep


_state = _State()


class Addon:
    def __init__(self, addon_id=None):
        pass

    def getSetting(self, name):
        return _state.settings.get(name, "")

    def setSetting(self, name, value):
        _state.settings[name] = str(value)

    def getAddonInfo(self, key):
        return {"profile": _state.profile}.get(key, "")


def set_settings(mapping):
    _state.settings.update({k: str(v) for k, v in mapping.items()})


def profile_dir():
    return _state.profile


def _reset():
    _state.settings.clear()
    _state.profile = os.path.join(tempfile.mkdtemp(prefix="ytc-emulator-"), "userdata", "addon_data", "plugin.service.ytlounge-cast") + os.sep
