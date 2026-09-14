"""Fake xbmcvfs: translatePath is identity-ish in the emulator."""

special_map = {}


def translatePath(path):
    for k, v in special_map.items():
        if path.startswith(k):
            return v + path[len(k):]
    return path


def exists(path):
    import os
    return os.path.exists(path)


def mkdirs(path):
    import os
    os.makedirs(path, exist_ok=True)
    return True


def File(path, mode="r"):
    return open(path, mode)
