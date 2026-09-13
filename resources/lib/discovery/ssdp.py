"""SSDP (Simple Service Discovery Protocol) responder for YouTube and YouTube Music."""

from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
from typing import Optional

logger = logging.getLogger("ytlounge.ssdp")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"


def get_local_ip(target_ip: str = "8.8.8.8") -> str:
    """Discover primary non-loopback IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class SSDPResponder(threading.Thread):
    def __init__(self, dial_port: int, device_uuid: str, local_ip: Optional[str] = None):
        super().__init__(name="SSDPResponder", daemon=True)
        self.dial_port = dial_port
        self.device_uuid = device_uuid
        self.local_ip = local_ip or get_local_ip()
        self._stop_event = threading.Event()
        self._sock: Optional[socket.socket] = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def run(self) -> None:
        logger.info("Starting SSDP responder on %s:%s (local IP: %s)", SSDP_ADDR, SSDP_PORT, self.local_ip)

        # Kodi starts services before the network is up on many devices (LibreELEC boot race):
        # the multicast join fails with 'No such device' and discovery stays dead all session.
        # Retry the bind until a real interface appears (max ~5 min), re-resolving the local IP.
        for attempt in range(60):
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass

                self._sock.bind(("", SSDP_PORT))
                mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self._sock.settimeout(1.0)
                break
            except OSError as e:
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                self._sock = None
                if self._stop_event.is_set():
                    return
                if attempt == 0:
                    logger.warning("SSDP bind failed (%s); network likely not up yet, retrying", e)
                for _ in range(50):  # 5s between attempts, interruptible
                    if self._stop_event.is_set():
                        return
                    time.sleep(0.1)
        else:
            logger.error("SSDP: giving up after 60 bind attempts")
            return
        # Re-resolve now that the network is up: the advertised LOCATION must carry the real IP.
        current_ip = get_local_ip()
        if current_ip and not current_ip.startswith("127."):
            self.local_ip = current_ip

        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
                if not data:
                    continue

                msg = data.decode("utf-8", errors="replace")
                if "M-SEARCH" not in msg or not (DIAL_ST in msg or "ssdp:all" in msg):
                    continue
                # SSDP requires the MAN: header per spec.
                if 'MAN: "ssdp:discover"' not in msg and "MAN: ssdp:discover" not in msg:
                    continue

                # Re-resolve the advertised IP: a stale address from startup
                # makes discovery advertise an unreachable device forever.
                current_ip = get_local_ip()
                if current_ip != "127.0.0.1":
                    self.local_ip = current_ip

                # MX handling: wait a random 0..MX seconds so M-SEARCH floods
                # do not desynchronise clients (SSDP/UPnP requirement).
                mx = 1.0
                m = re.search(r"^MX:\s*([\d.]+)", msg, re.MULTILINE)
                if m:
                    try:
                        mx = min(float(m.group(1)), 5.0)
                    except ValueError:
                        pass
                if mx > 0:
                    for _ in range(int(mx * 20)):
                        if self._stop_event.is_set():
                            return
                        time.sleep(0.05)

                response = (
                    "HTTP/1.1 200 OK\r\n"
                    f"CACHE-CONTROL: max-age=1800\r\n"
                    f"DATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
                    "EXT:\r\n"
                    f"LOCATION: http://{self.local_ip}:{self.dial_port}/ssdp/device-desc.xml\r\n"
                    "SERVER: UPnP/1.0\r\n"
                    f"ST: {DIAL_ST}\r\n"
                    f"USN: uuid:{self.device_uuid}::{DIAL_ST}\r\n\r\n"
                ).encode("utf-8")
                logger.debug("Sending SSDP response to %s", addr)
                self._sock.sendto(response, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.debug("SSDP receive exception: %s", e)
                time.sleep(0.5)

        logger.info("SSDP responder stopped")
