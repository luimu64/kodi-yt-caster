"""SSDP (Simple Service Discovery Protocol) responder for YouTube and YouTube Music."""

from __future__ import annotations

import logging
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
        except Exception as e:
            logger.warning("Could not bind SSDP multicast socket on port 1900: %s", e)
            return

        response_template = (
            "HTTP/1.1 200 OK\r\n"
            f"LOCATION: http://{self.local_ip}:{self.dial_port}/ssdp/device-desc.xml\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            "EXT:\r\n"
            "BOOTID.UPNP.ORG: 1\r\n"
            "SERVER: UPnP/1.0\r\n"
            f"USN: uuid:{self.device_uuid}::{DIAL_ST}\r\n"
            f"ST: {DIAL_ST}\r\n\r\n"
        ).encode("utf-8")

        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
                if not data:
                    continue

                msg = data.decode("utf-8", errors="replace")
                if "M-SEARCH" in msg and (DIAL_ST in msg or "ssdp:all" in msg):
                    logger.debug("Received DIAL M-SEARCH from %s, sending response", addr)
                    self._sock.sendto(response_template, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.debug("SSDP receive exception: %s", e)
                time.sleep(0.5)

        logger.info("SSDP responder stopped")
