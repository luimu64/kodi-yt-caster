"""Frame encoder: exact inverse of resources.lib.lounge.session.parse_frames."""
import json


def _normalized(items):
    """[(code, name, data)] the way the addon's parse_frames yields them."""
    out = []
    for it in items:
        code, action = it[0], it[1]
        out.append((code, action[0], action[1] if len(action) > 1 else None))
    return out

def encode_frame(items) -> bytes:
    payload = json.dumps(items, separators=(",", ":"))
    return f"{len(payload)}\n{payload}\n".encode("utf-8")
