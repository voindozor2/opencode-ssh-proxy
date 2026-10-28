"""Loopback fixtures only; these tests do not verify the real VPS/provider."""

import base64
import http.client
import ipaddress
import socket
import socketserver
import struct
import threading
import unittest
from contextlib import contextmanager

import bridge


class TestServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class SocksFixture(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        if bridge.read_exact(self.request, 3) != b"\x05\x01\x00":
            return
        self.request.sendall(b"\x05\x00")
        version, command, reserved, kind = bridge.read_exact(self.request, 4)
        if kind == 1:
            host = str(ipaddress.ip_address(bridge.read_exact(self.request, 4)))
        elif kind == 3:
            host = bridge.read_exact(self.request, bridge.read_exact(self.request, 1)[0]).decode("ascii")
        else:
            return
        port = struct.unpack("!H", bridge.read_exact(self.request, 2))[0]
        self.server.destinations.append((host, port))
        # Test DNS resolution stays at the SOCKS side, without real DNS/network.
        if host == "fixture.invalid":
            host = "127.0.0.1"
        if host != "127.0.0.1":
            return
        with socket.create_connection((host, port), timeout=5) as upstream:
            self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            bridge.relay(self.request, upstream)


class OriginFixture(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        stream = self.request.makefile("rb", buffering=0)
        request_line = stream.readline()
        headers = {}
        while True:
            line = stream.readline()
            if line in (b"\r\n", b""):
                break
            name, value = line.split(b":", 1)
            headers[name.lower()] = value.strip()
        body = stream.read(int(headers.get(b"content-length", b"0")))
        self.server.requests.append((request_line, headers, body))
        response = b"fixture-response:" + body
        self.request.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                             + str(len(response)).encode() + b"\r\n\r\n" + response)


class EchoFixture(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(4096):
            self.request.sendall(data)


@contextmanager
def running(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


AUTH = "Basic " + base64.b64encode(b"opencode:fixture-secret").decode()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.origin = TestServer(("127.0.0.1", 0), OriginFixture)
        self.origin.requests = []
        self.socks = TestServer(("127.0.0.1", 0), SocksFixture)
        self.socks.destinations = []
        self.proxy = bridge.make_server("fixture-secret", 0, self.socks.server_address[1])
        for server in (self.origin, self.socks, self.proxy):
            context = running(server)
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def request(self, method, url, body=None, auth=AUTH):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        try:
            headers = {} if auth is None else {"Proxy-Authorization": auth}
            connection.request(method, url, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def url(self):
        return f"http://fixture.invalid:{self.origin.server_address[1]}/path?test=1"

    def test_missing_and_wrong_auth_never_reach_socks(self):
        self.assertEqual(self.request("GET", self.url(), auth=None)[0], 407)
        self.assertEqual(self.request("GET", self.url(), auth="Basic wrong")[0], 407)
        self.assertEqual(self.socks.destinations, [])

    def test_http_remote_dns_and_no_proxy_credential_leak(self):
        self.assertEqual(self.request("GET", self.url()), (200, b"fixture-response:"))
        line, headers, body = self.origin.requests[0]
        self.assertEqual(line, b"GET /path?test=1 HTTP/1.1\r\n")
        self.assertNotIn(b"proxy-authorization", headers)
        self.assertEqual(self.socks.destinations[0][0], "fixture.invalid")

    def test_post_body_is_not_lost_to_header_buffering(self):
        self.assertEqual(self.request("POST", self.url(), body=b"test-body"),
                         (200, b"fixture-response:test-body"))

    def test_connect_relays_opaque_bytes(self):
        with running(TestServer(("127.0.0.1", 0), EchoFixture)) as echo:
            with socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=5) as client:
                request = f"CONNECT fixture.invalid:{echo.server_address[1]} HTTP/1.1\r\nProxy-Authorization: {AUTH}\r\n\r\n"
                client.sendall(request.encode())
                response = bytearray()
                while not response.endswith(b"\r\n\r\n"):
                    response.extend(client.recv(1))
                self.assertIn(b"200 Connection Established", response)
                opaque = bytes(range(256)) * 8
                client.sendall(opaque)
                self.assertEqual(bridge.read_exact(client, len(opaque)), opaque)

    def test_socks_unavailable_fails_closed(self):
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            self.proxy.socks_port = unavailable.getsockname()[1]
            self.assertEqual(self.request("GET", self.url())[0], 502)
        self.assertEqual(self.origin.requests, [])

    def test_invalid_urls_rejected_before_socks(self):
        self.assertEqual(self.request("GET", "https://fixture.invalid/")[0], 400)
        self.assertEqual(self.request("CONNECT", "fixture.invalid:443/path")[0], 400)
        self.assertEqual(self.socks.destinations, [])

    def test_loopback_only(self):
        self.assertEqual(self.proxy.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
