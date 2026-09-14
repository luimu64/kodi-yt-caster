"""Path bootstrapping for the emulator package.

When scenarios are run as scripts (python3 emulator/scenarios/test_x.py),
this puts both the repo root and emulator/ on sys.path.
"""
import os
import sys


def _bootstrap():
    emulator_root = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(emulator_root)
    for p in (repo_root, emulator_root):
        if p not in sys.path:
            sys.path.insert(0, p)


_bootstrap()

kodi_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kodi_stub")
