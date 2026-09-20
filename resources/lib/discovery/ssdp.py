"""SSDP (Simple Service Discovery Protocol) responder for YouTube and YouTube Music."""

from __future__ import annotations

import logging
import random
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
DIAL_DEV_ST = "urn:dial-multiscreen-org:device:dial:1"


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
            if self._sock is not None:
                break
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

        last_ip_check = time.monotonic()

        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
                if not data:
                    continue

                msg = data.decode("utf-8", errors="replace")
                if "M-SEARCH" not in msg.upper():
                    continue

                # SSDP requires MAN: "ssdp:discover" (case-insensitive)
                if not re.search(r'MAN:\s*"?ssdp:discover"?', msg, re.MULTILINE | re.IGNORECASE):
                    continue

                # Check Search Target ST (matches service ST, device ST, or ssdp:all)
                m_st = re.search(r"^ST:\s*(.+)$", msg, re.MULTILINE | re.IGNORECASE)
                st = m_st.group(1).strip() if m_st else ""
                msg_lower = msg.lower()
                is_service = DIAL_ST in msg or DIAL_ST.lower() in msg_lower
                is_device = DIAL_DEV_ST in msg or DIAL_DEV_ST.lower() in msg_lower
                is_all = "ssdp:all" in msg_lower or "upnp:rootdevice" in msg_lower
                if not (is_service or is_device or is_all):
                    continue

                target_st = DIAL_DEV_ST if (is_device and not is_service) else DIAL_ST

                # Periodic IP re-resolution if not non-loopback
                now = time.monotonic()
                if self.local_ip == "127.0.0.1" or (now - last_ip_check > 30.0):
                    last_ip_check = now
                    current_ip = get_local_ip()
                    if current_ip != "127.0.0.1":
                        self.local_ip = current_ip

                # MX handling: wait a random 0..MX seconds so M-SEARCH floods
                # do not desynchronise clients (SSDP/UPnP requirement).
                # Cap the random delay to 0.2s so cast discovery responds promptly.
                mx = 1.0
                m = re.search(r"^MX:\s*([\d.]+)", msg, re.MULTILINE | re.IGNORECASE)
                if m:
                    try:
                        mx = min(float(m.group(1)), 5.0)
                    except ValueError:
                        pass
                if mx > 0:
                    delay = random.uniform(0.01, min(mx, 0.2))
                    time.sleep(delay)

                response = (
                    "HTTP/1.1 200 OK\r\n"
                    f"CACHE-CONTROL: max-age=1800\r\n"
                    f"DATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n"
                    "EXT:\r\n"
                    f"LOCATION: http://{self.local_ip}:{self.dial_port}/ssdp/device-desc.xml\r\n"
                    "SERVER: UPnP/1.0\r\n"
                    f"ST: {target_st}\r\n"
                    f"USN: uuid:{self.device_uuid}::{target_st}\r\n\r\n"
                ).encode("utf-8")
                logger.debug("Sending SSDP response to %s", addr)
                self._sock.sendto(response, addr)
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
