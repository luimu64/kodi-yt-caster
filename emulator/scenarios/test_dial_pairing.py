#!/usr/bin/env python3
"""Scenario: DIAL pairing and SSDP discovery responsiveness.

Ensures that:
1. DIAL /apps/YouTube POST returns HTTP 201 Created immediately (< 0.25s) even
   when cloud pairing registration takes time (WAN network latency to Google),
   preventing mobile casting client connection timeouts.
2. Cloud pairing registration runs concurrently and registers both screen
   identities on the Lounge server.
3. SSDP discovery responds to DIAL device ST (urn:dial-multiscreen-org:device:dial:1)
   as well as service ST (urn:dial-multiscreen-org:service:dial:1) with case-insensitive
   header parsing (man:, st:, mx:).
4. SSDP MX handling responds promptly (< 0.5s) instead of sleeping for the full MX duration.
"""
import re
import socket
import time
import urllib.parse
import urllib.request

from harness import Scenario
import _bootstrap  # noqa: F401
import kodi_stub
kodi_stub.install()
import xbmc
from resources.lib.discovery.ssdp import SSDPResponder, DIAL_ST, get_local_ip
from resources.lib.discovery.dial_server import DIALServer


def test_dial_post_responds_promptly_without_blocking_on_cloud():
    """DIAL POST must return 201 Created immediately without blocking on cloud calls."""
    import resources.lib.lounge.pairing as pairing_mod
    orig_register = pairing_mod.register_pairing_code

    # Simulate realistic network delay (0.3s) on each cloud pairing request to Google
    def _delayed_register(*args, **kwargs):
        time.sleep(0.3)
        return orig_register(*args, **kwargs)

    pairing_mod.register_pairing_code = _delayed_register

    try:
        with Scenario(settings={"enable_discovery": "true", "dial_port": "0"}) as s:
            dial_service = getattr(s.service, "_emu_dial", None)
            deadline = time.monotonic() + 5.0
            port = None
            while time.monotonic() < deadline:
                if dial_service and dial_service.server:
                    port = dial_service.server.server_address[1]
                    break
                time.sleep(0.05)

            assert port and port > 0, "DIAL server must be running on a valid port"

            app_url = f"http://127.0.0.1:{port}/apps/YouTube"
            post_data = urllib.parse.urlencode({
                "pairingCode": "444-555-666-777",
                "theme": "cl"
            }).encode("utf-8")
            req = urllib.request.Request(app_url, data=post_data)

            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                elapsed = time.monotonic() - t0
                assert resp.status == 201, f"Expected 201 Created, got {resp.status}"
                loc = resp.headers.get("Location")
                assert "/apps/YouTube/run" in loc

            # If registration blocked synchronously, elapsed would be >= 0.6s (2 x 0.3s)
            assert elapsed < 0.25, f"DIAL POST took {elapsed:.3f}s; must respond immediately (<0.25s)"

            # Verify that in the background both screens were successfully registered
            s.wait_until(
                lambda: len([c for c in s.lounge.PAIRING_CALLS if c[0] == "register_pairing_code" and c[1].get("pairing_code") == "444-555-666-777"]) == 2,
                timeout=5.0,
                what="both cl + m screens registered on Lounge",
            )

            # Verify notification was shown
            s.wait_until(
                lambda: s.notifications("Linked device via Wi-Fi"),
                timeout=5.0,
                what="Wi-Fi link notification",
            )
    finally:
        pairing_mod.register_pairing_code = orig_register


def test_ssdp_discovery_device_st_and_case_insensitive():
    """SSDP must discover DIAL device ST with case-insensitive headers and prompt response."""
    device_st = "urn:dial-multiscreen-org:device:dial:1"
    server = DIALServer(
        port=0,
        device_uuid="test-uuid-1234",
        friendly_name="Kodi Discovery Test",
        screen_id="screen_test",
    )
    dial_port = server.server_address[1]

    # Create SSDP responder on an ephemeral UDP port for direct testing
    responder = SSDPResponder(dial_port=dial_port, device_uuid="test-uuid-1234", local_ip="127.0.0.1")
    # Bind responder to an ephemeral port
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    test_sock.bind(("127.0.0.1", 0))
    resp_port = test_sock.getsockname()[1]
    test_sock.settimeout(1.5)

    responder._sock = test_sock
    import threading
    t = threading.Thread(target=responder.run, daemon=True)
    t.start()

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_sock.settimeout(1.5)

    try:
        # 1. Test case-insensitive headers and device ST:
        # "man: ssdp:discover", "st: urn:dial-multiscreen-org:device:dial:1", "mx: 3"
        query = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'man: "ssdp:discover"\r\n'
            f"st: {device_st}\r\n"
            "mx: 3\r\n\r\n"
        ).encode("utf-8")

        t0 = time.monotonic()
        client_sock.sendto(query, ("127.0.0.1", resp_port))
        data, _ = client_sock.recvfrom(2048)
        elapsed = time.monotonic() - t0

        resp_text = data.decode("utf-8")
        assert "HTTP/1.1 200 OK" in resp_text
        assert f"ST: {device_st}" in resp_text or f"st: {device_st}" in resp_text.lower()
        assert f":{dial_port}/ssdp/device-desc.xml" in resp_text
        assert "LOCATION: http://" in resp_text
        # Must respond promptly (< 0.5s), NOT sleep for the full 3.0s MX!
        assert elapsed < 0.5, f"SSDP response took {elapsed:.3f}s, expected < 0.5s"

    finally:
        responder.stop()
        server.server_close()
        client_sock.close()


def main():
    test_dial_post_responds_promptly_without_blocking_on_cloud()
    print("  test_dial_post_responds_promptly_without_blocking_on_cloud OK")
    test_ssdp_discovery_device_st_and_case_insensitive()
    print("  test_ssdp_discovery_device_st_and_case_insensitive OK")
    print("test_dial_pairing OK")


if __name__ == "__main__":
    main()
