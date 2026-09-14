"""Script bootstrap: repo root + emulator/ on sys.path."""
import os
import sys

_emu = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_repo = os.path.dirname(_emu)
for p in (_repo, _emu):
    if p not in sys.path:
        sys.path.insert(0, p)
