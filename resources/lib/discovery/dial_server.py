"""DIAL HTTP service for YouTube and YouTube Music app pairing."""

from __future__ import annotations

import http.server
import logging
import re
import threading
import urllib.parse
from typing import Callable, Optional

logger = logging.getLogger("ytlounge.dial")


class DIALRequestHandler(http.server.BaseHTTPRequestHandler):
    server: "DIALServer"

    # Pairing codes are 12 digits (YouTube may deliver with or without dashes).
    # The YouTube app sends a 12-digit code (431-874-395-211); YouTube Music sends a UUID.
    # Both are registered with the Lounge API as-is, so accept either shape.
    PAIRING_CODE_RE = re.compile(r"^(?:\d{3}-?\d{3}-?\d{3}-?\d{3}|[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})$")

    def log_message(self, format: str, *args) -> None:
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def do_OPTIONS(self) -> None:
        # No CORS headers: DIAL clients are native apps, not browsers, and
        # Access-Control-Allow-Origin:* would let any web page drive pairing.
        self.send_response(200)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/ssdp/device-desc.xml":
            self._send_device_desc()
        elif path in ("/apps/YouTube", "/apps/YouTube/"):
            self._send_app_status()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/apps/YouTube", "/apps/YouTube/"):
            content_len = int(self.headers.get("Content-Length", 0) or 0)
            post_body = self.rfile.read(content_len).decode("utf-8", errors="replace")
            params = urllib.parse.parse_qs(post_body)
            pairing_code = params.get("pairingCode", [""])[0]

            if not pairing_code or not self.PAIRING_CODE_RE.match(pairing_code):
                logger.warning("Rejected malformed DIAL pairing code from %s", self.client_address[0])
                self.send_error(400, "Bad Request")
                return

            logger.info("Received DIAL pairing code from %s", self.client_address[0])

            # The phone sends its theme alongside the code ('cl' = YouTube,
            # 'm' = YouTube Music). Register ONLY that theme's screen: a
            # pairing code is single-use server-side, so registering both
            # made the second (music) register silently fail and YT Music
            # retried forever.
            theme = params.get("theme", [""])[0]
            if self.server.on_pairing_code:
                try:
                    self.server.on_pairing_code(pairing_code, theme)
                except Exception as e:
                    logger.error("Error processing DIAL pairing code: %s", e)

            host = self.headers.get("Host", f"127.0.0.1:{self.server.port}")
            self.send_response(201, "Created")
            self.send_header("Location", f"http://{host}/apps/YouTube/run")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/apps/YouTube", "/apps/YouTube/", "/apps/YouTube/run"):
            self.send_response(200, "OK")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def _send_device_desc(self) -> None:
        host = self.headers.get("Host", f"127.0.0.1:{self.server.port}")
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\r\n'
            '<root xmlns="urn:schemas-upnp-org:device-1-0">\r\n'
            "  <specVersion><major>1</major><minor>0</minor></specVersion>\r\n"
            "  <device>\r\n"
            "    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>\r\n"
            f"    <friendlyName>{self.server.friendly_name}</friendlyName>\r\n"
            "    <manufacturer>Kodi</manufacturer>\r\n"
            "    <modelName>Kodi YouTube Receiver</modelName>\r\n"
            f"    <UDN>uuid:{self.server.device_uuid}</UDN>\r\n"
            "    <serviceList>\r\n"
            "      <service>\r\n"
            "        <serviceType>urn:dial-multiscreen-org:service:dial:1</serviceType>\r\n"
            "        <serviceId>urn:dial-multiscreen-org:serviceId:dial</serviceId>\r\n"
            "        <controlURL>/apps</controlURL>\r\n"
            "        <eventSubURL>/apps</eventSubURL>\r\n"
            "        <SCPDURL>/apps</SCPDURL>\r\n"
            "      </service>\r\n"
            "    </serviceList>\r\n"
            "  </device>\r\n"
            "</root>\r\n"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(xml)))
        self.send_header("Application-URL", f"http://{host}/apps/")
        self.end_headers()
        self.wfile.write(xml)

    def _send_app_status(self) -> None:
        host = self.headers.get("Host", f"127.0.0.1:{self.server.port}")
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\r\n'
            '<service xmlns="urn:dial-multiscreen-org:schemas:dial" dialVer="2.1">\r\n'
            "  <name>YouTube</name>\r\n"
            '  <options allowStop="true"/>\r\n'
            "  <state>running</state>\r\n"
            '  <link rel="run" href="run"/>\r\n'
            "  <additionalData>\r\n"
            f"    <screenId>{self.server.screen_id}</screenId>\r\n"
            "  </additionalData>\r\n"
            "</service>\r\n"
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(xml)))
        self.end_headers()
        self.wfile.write(xml)


class DIALServer(http.server.ThreadingHTTPServer):
    def __init__(
        self,
        port: int,
        device_uuid: str,
        friendly_name: str,
        screen_id: str,
        on_pairing_code: Optional[Callable[[str, str], None]] = None,
    ):
        super().__init__(("0.0.0.0", port), DIALRequestHandler)
        self.port = self.server_address[1]
        self.device_uuid = device_uuid
        self.friendly_name = friendly_name
        self.screen_id = screen_id
        self.on_pairing_code = on_pairing_code
        self.daemon_threads = True


class DIALService(threading.Thread):
    def __init__(
        self,
        port: int,
        device_uuid: str,
        friendly_name: str,
        screen_id: str,
        on_pairing_code: Optional[Callable[[str, str], None]] = None,
    ):
        super().__init__(name="DIALService", daemon=True)
        self.port = port
        self.device_uuid = device_uuid
        self.friendly_name = friendly_name
        self.screen_id = screen_id
        self.on_pairing_code = on_pairing_code
        self.server: Optional[DIALServer] = None

    def run(self) -> None:
        try:
            self.server = DIALServer(
                port=self.port,
                device_uuid=self.device_uuid,
                friendly_name=self.friendly_name,
                screen_id=self.screen_id,
                on_pairing_code=self.on_pairing_code,
            )
            logger.info("DIAL HTTP server running on 0.0.0.0:%s", self.port)
            self.server.serve_forever()
        except Exception as e:
            logger.warning("DIAL HTTP server failed on port %s: %s", self.port, e)

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
