"""Authenticated loopback HTTP proxy over an existing loopback SSH SOCKS5 proxy.

Python standard library only. No direct-connect fallback, TLS interception,
downloads, persistence, credential logging, or server changes.
"""

import argparse
import base64
import hmac
import ipaddress
import select
import socket
import socketserver
import struct
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise OSError("SOCKS connection closed")
        result.extend(chunk)
    return bytes(result)


def socks_connect(host, port, socks_port):
    if not host or not 1 <= port <= 65535:
        raise ValueError("Invalid destination")
    sock = socket.create_connection(("127.0.0.1", socks_port), timeout=15)
    try:
        sock.sendall(b"\x05\x01\x00")
        if read_exact(sock, 2) != b"\x05\x00":
            raise OSError("SOCKS authentication negotiation failed")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            encoded = host.encode("idna")
            if not 1 <= len(encoded) <= 255:
                raise ValueError("Invalid hostname")
            target = b"\x03" + bytes([len(encoded)]) + encoded
        else:
            target = (b"\x01" if address.version == 4 else b"\x04") + address.packed
        sock.sendall(b"\x05\x01\x00" + target + struct.pack("!H", port))
        version, status, reserved, kind = read_exact(sock, 4)
        if version != 5 or status != 0 or reserved != 0:
            raise OSError("SOCKS connection failed")
        if kind == 1:
            read_exact(sock, 4)
        elif kind == 4:
            read_exact(sock, 16)
        elif kind == 3:
            read_exact(sock, read_exact(sock, 1)[0])
        else:
            raise OSError("Invalid SOCKS reply")
        read_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def relay(left, right):
    readers = [left, right]
    left.settimeout(15)
    right.settimeout(15)
    while readers:
        ready, _, _ = select.select(readers, [], [], 180)
        if not ready:
            return
        for source in ready:
            destination = right if source is left else left
            data = source.recv(65536)
            if not data:
                readers.remove(source)
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            else:
                destination.sendall(data)


class BridgeServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # Never print a traceback containing request headers or credentials.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0  # Do not consume body/tunnel bytes while parsing headers.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def reply(self, status):
        self.send_response(status)
        if status == 407:
            self.send_header("Proxy-Authenticate", 'Basic realm="OpenCode local proxy"')
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def authenticated(self):
        values = self.headers.get_all("Proxy-Authorization", [])
        if len(values) != 1:
            self.reply(407)
            return False
        try:
            valid = hmac.compare_digest(values[0].encode("ascii"), self.server.authorization)
        except UnicodeEncodeError:
            valid = False
        if not valid:
            self.reply(407)
        return valid

    def do_CONNECT(self):
        if not self.authenticated():
            return
        try:
            target = urlsplit("//" + self.path)
            if (target.username is not None or target.password is not None
                    or target.path or target.query or target.fragment
                    or target.port is None):
                raise ValueError("Invalid CONNECT target")
            host, port = target.hostname, target.port
            if not host or any(character.isspace() for character in host):
                raise ValueError("Invalid CONNECT host")
        except ValueError:
            self.reply(400)
            return
        self.close_connection = True
        try:
            upstream = socks_connect(host, port, self.server.socks_port)
        except (OSError, ValueError, UnicodeError):
            self.reply(502)
            return
        with upstream:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            try:
                relay(self.connection, upstream)
            except OSError:
                pass

    def do_GET(self):
        if not self.authenticated():
            return
        try:
            target = urlsplit(self.path)
            if (target.scheme != "http" or not target.hostname
                    or target.username is not None or target.password is not None
                    or target.fragment):
                raise ValueError("Absolute HTTP URL required")
            host, port = target.hostname, target.port or 80
            if any(character.isspace() for character in host):
                raise ValueError("Invalid host")
            host_header = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
            if port != 80:
                host_header += f":{port}"
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            removed = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "host"}
            removed.update(value.strip().lower() for value in self.headers.get("Connection", "").split(","))
            headers = [f"{self.command} {path} HTTP/1.1", f"Host: {host_header}", "Connection: close"]
            for name, value in self.headers.items():
                if name.lower() not in removed:
                    if "\r" in value or "\n" in value:
                        raise ValueError("Invalid header")
                    headers.append(f"{name}: {value}")
            request = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1")
        except (ValueError, UnicodeError):
            self.reply(400)
            return
        self.close_connection = True
        try:
            upstream = socks_connect(host, port, self.server.socks_port)
        except (OSError, ValueError, UnicodeError):
            self.reply(502)
            return
        with upstream:
            try:
                upstream.sendall(request)
                relay(self.connection, upstream)
            except OSError:
                pass

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET


def make_server(password, listen_port=18080, socks_port=18081):
    if not password or ":" in password or "\r" in password or "\n" in password:
        raise ValueError("Invalid proxy password")
    server = BridgeServer(("127.0.0.1", listen_port), Handler)
    server.authorization = b"Basic " + base64.b64encode(("opencode:" + password).encode("utf-8"))
    server.socks_port = socks_port
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--listen-port", type=int, default=18080)
    parser.add_argument("--socks-port", type=int, default=18081)
    args = parser.parse_args()
    password = Path(args.password_file).read_text(encoding="utf-8").strip()
    with make_server(password, args.listen_port, args.socks_port) as server:
        print("Local HTTP bridge ready on 127.0.0.1:" + str(server.server_port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
