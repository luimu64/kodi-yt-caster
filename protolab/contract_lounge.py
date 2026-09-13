#!/usr/bin/env python3
"""Contract test harness for YouTube Lounge API (Receiver / Screen role).

Proves screen registration, pairing code generation, session binding, and command reception
outside Kodi using Python standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

BASE_URL = "https://www.youtube.com/api/lounge"
TOKENS_FILE = os.path.join(os.path.dirname(__file__), "tokens.json")

DEFAULT_HEADERS = {
    "Origin": "https://www.youtube.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

CMD_PATTERN = re.compile(r"\[(?P<code>\d+),\[\"(?P<cmd>.+?)\"(?:,(?P<data>.*?))?\]\]")


def make_request(url: str, data: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> str:
    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=encoded_data, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def generate_screen_id() -> str:
    """Obtain a new screenId from YouTube Lounge."""
    url = f"{BASE_URL}/pairing/generate_screen_id"
    return make_request(url).strip()


def get_lounge_token_batch(screen_id: str) -> Tuple[str, int]:
    """Fetch loungeToken and expiration timestamp for screen_id."""
    url = f"{BASE_URL}/pairing/get_lounge_token_batch"
    raw = make_request(url, data={"screen_ids": screen_id})
    payload = json.loads(raw)
    screen = payload["screens"][0]
    return screen["loungeToken"], screen.get("expiration", 0)


def get_pairing_code(screen_id: str, lounge_token: str, screen_name: str = "Kodi") -> str:
    """Fetch a 12-digit pairing code for entering in the YouTube mobile app."""
    url = f"{BASE_URL}/pairing/get_pairing_code?ctx=pair"
    data = {
        "access_type": "permanent",
        "app": "kodi-ytcast",
        "lounge_token": lounge_token,
        "screen_id": screen_id,
        "screen_name": screen_name,
    }
    raw = make_request(url, data=data).strip()
    if len(raw) == 12:
        return f"{raw[0:3]}-{raw[3:6]}-{raw[6:9]}-{raw[9:12]}"
    return raw


def parse_frames(body: str) -> List[Tuple[int, str, Any]]:
    """Parse length-delimited or regex-patterned command chunks from Lounge bind response."""
    commands: List[Tuple[int, str, Any]] = []
    # Try frame-based parsing first
    pos = 0
    while pos < len(body):
        newline = body.find("\n", pos)
        if newline == -1:
            break
        length_str = body[pos:newline].strip()
        if not length_str:
            pos = newline + 1
            continue
        try:
            length = int(length_str)
        except ValueError:
            break
        start = newline + 1
        payload = body[start : start + length]
        try:
            items = json.loads(payload)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, list) and len(item) >= 2:
                        idx = item[0]
                        action = item[1]
                        if isinstance(action, list) and action:
                            cmd_name = action[0]
                            cmd_data = action[1] if len(action) > 1 else None
                            commands.append((idx, cmd_name, cmd_data))
        except Exception:
            pass
        pos = start + length

    if not commands:
        for match in CMD_PATTERN.finditer(body):
            code = int(match.group("code"))
            name = match.group("cmd")
            raw_data = match.group("data")
            data = None
            if raw_data:
                try:
                    data = json.loads(raw_data)
                except Exception:
                    data = raw_data
            commands.append((code, name, data))

    return commands


class LoungeScreenSession:
    def __init__(self, screen_id: str, lounge_token: str, device_id: str, screen_name: str = "Kodi"):
        self.screen_id = screen_id
        self.lounge_token = lounge_token
        self.device_id = device_id
        self.screen_name = screen_name
        self.sid: Optional[str] = None
        self.gsessionid: Optional[str] = None
        self.ofs = 0
        self.last_code = -1

    def bind_handshake(self) -> None:
        """Perform initial bind to retrieve session IDs (SID and gsessionid)."""
        zx = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        params = {
            "device": "LOUNGE_SCREEN",
            "id": self.device_id,
            "name": self.screen_name,
            "app": "kodi-ytcast",
            "theme": "cl",
            "capabilities": "",
            "mdx-version": "2",
            "loungeIdToken": self.lounge_token,
            "VER": "8",
            "v": "2",
            "RID": "1337",
            "AID": "42",
            "zx": zx,
            "t": "1",
            "CVER": "1",
        }
        url = f"{BASE_URL}/bc/bind?{urllib.parse.urlencode(params)}"
        body = make_request(url, data={"count": "0"})
        for code, name, data in parse_frames(body):
            if name == "c":
                self.sid = str(data)
            elif name == "S":
                self.gsessionid = str(data)

        if not self.sid or not self.gsessionid:
            raise RuntimeError(f"Handshake failed: SID={self.sid}, gsessionid={self.gsessionid}")

    def listen(self, duration: float = 60.0) -> Iterator[Tuple[int, str, Any]]:
        """Stream chunks from long-poll bind endpoint."""
        self.ofs += 1
        zx = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        params = {
            "device": "LOUNGE_SCREEN",
            "id": self.device_id,
            "name": self.screen_name,
            "app": "kodi-ytcast",
            "theme": "cl",
            "capabilities": "",
            "mdx-version": "2",
            "loungeIdToken": self.lounge_token,
            "VER": "8",
            "v": "2",
            "RID": "rpc",
            "AID": "3",
            "CI": "0",
            "TYPE": "xmlhttp",
            "SID": self.sid,
            "gsessionid": self.gsessionid,
            "zx": zx,
            "t": "1",
        }
        url = f"{BASE_URL}/bc/bind?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        start_time = time.time()

        with urllib.request.urlopen(req, timeout=duration + 10.0) as resp:
            buf = ""
            while time.time() - start_time < duration:
                chunk = resp.read(1024)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                cmds = parse_frames(buf)
                if cmds:
                    for c in cmds:
                        if c[0] > self.last_code:
                            self.last_code = c[0]
                            yield c
                    buf = ""


def acquire_flow() -> None:
    print("==> [Phase 0.1] Acquiring new Lounge Screen Registration...")
    screen_id = generate_screen_id()
    print(f"  screenId: {screen_id}")

    token, expiration = get_lounge_token_batch(screen_id)
    print(f"  loungeToken: {token[:20]}... (expires: {expiration})")

    code = get_pairing_code(screen_id, token, screen_name="Kodi Lounge Test")
    print(f"\n========================================================")
    print(f"  TV PAIRING CODE: {code}")
    print(f"  Open YouTube on your phone -> Settings -> Watch on TV -> Enter TV Code")
    print(f"========================================================\n")

    import uuid
    device_id = str(uuid.uuid4())
    session_data = {
        "screen_id": screen_id,
        "lounge_token": token,
        "expiration": expiration,
        "device_id": device_id,
        "screen_name": "Kodi Lounge Test",
        "pairing_code": code,
        "saved_at": time.time(),
    }
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2)
    print(f"  Saved pairing state to {TOKENS_FILE}")

    print("  Connecting bind handshake to activate screen...")
    sess = LoungeScreenSession(screen_id, token, device_id, screen_name="Kodi Lounge Test")
    sess.bind_handshake()
    print(f"  Handshake successful! SID={sess.sid}, gsessionid={sess.gsessionid}")
    print("  Listening for 30s for phone pair / commands (press Ctrl+C to stop)...")
    try:
        for code_idx, name, data in sess.listen(duration=30.0):
            print(f"  [EVENT {code_idx}] {name} => {data}")
    except KeyboardInterrupt:
        print("  Interrupted by user.")


def resume_flow() -> None:
    print("==> [Phase 0.1] Resuming existing Lounge Screen session from tokens.json...")
    if not os.path.exists(TOKENS_FILE):
        print(f"ERROR: {TOKENS_FILE} does not exist. Run --acquire first.", file=sys.stderr)
        sys.exit(1)

    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    screen_id = data["screen_id"]
    lounge_token = data["lounge_token"]
    device_id = data["device_id"]
    screen_name = data.get("screen_name", "Kodi Lounge Test")

    print(f"  Loaded screenId: {screen_id}")
    print(f"  Loaded deviceId: {device_id}")

    # Re-fetch fresh token batch if needed or test token validity
    print("  Verifying lounge token refresh...")
    new_token, new_exp = get_lounge_token_batch(screen_id)
    print(f"  Lounge token verified! Active until: {new_exp}")
    sess = LoungeScreenSession(screen_id, new_token, device_id, screen_name)
    sess.bind_handshake()
    print(f"  Resumed session! SID={sess.sid}, gsessionid={sess.gsessionid}")
    print("  Listening for 15s (press Ctrl+C to stop)...")
    try:
        for code_idx, name, data in sess.listen(duration=15.0):
            print(f"  [EVENT {code_idx}] {name} => {data}")
    except KeyboardInterrupt:
        print("  Interrupted by user.")
    print("  Resume test PASSED.")


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube Lounge protocol contract tester")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--acquire", action="store_true", help="Acquire screen ID and display TV pairing code")
    group.add_argument("--resume", action="store_true", help="Resume session from tokens.json")
    args = parser.parse_args()

    if args.acquire:
        acquire_flow()
    elif args.resume:
        resume_flow()


if __name__ == "__main__":
    main()
