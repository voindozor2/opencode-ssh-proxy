"""Supervisor tests; all sockets/processes are local fixtures, never real SSH."""

import json
import http.client
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import bridge
from test_bridge import EchoFixture, TestServer, running

import supervisor


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.key = self.directory / "fixture-key"
        self.password = self.directory / "fixture-password"
        self.key.write_text("fixture only", encoding="utf-8")
        self.password.write_text("fixture-secret", encoding="utf-8")
        self.path = self.directory / "settings.local.json"
        self.data = {
            "ssh_executable": sys.executable,
            "ssh_host": "fixture.invalid",
            "ssh_user": "fixture",
            "identity_file": str(self.key),
            "password_file": str(self.password),
            "listen_port": 18080,
            "socks_port": 18081,
        }

    def config(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        return supervisor.load_config(self.path)

    def test_config_and_safe_ssh_command(self):
        config = self.config()
        command = supervisor.ssh_command(config)
        for value in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                      "ExitOnForwardFailure=yes", "PasswordAuthentication=no"):
            self.assertIn(value, command)
        self.assertIn("127.0.0.1:18081", command)
        self.assertIn(str(self.key), command)
        self.assertNotIn("fixture-secret", " ".join(command))
        self.assertEqual(command[-1], "fixture@fixture.invalid")

    def test_reject_unknown_fields_option_injection_and_port_collision(self):
        for field, value in (("unexpected", True), ("ssh_host", "-oProxyCommand=bad"),
                             ("ssh_user", "user@other"), ("socks_port", 18080),
                             ("listen_port", True), ("socks_port", 65536)):
            with self.subTest(field=field, value=value):
                original = self.data.copy()
                self.data[field] = value
                with self.assertRaises(ValueError):
                    self.config()
                self.data = original

    def test_backoff_bounded_and_increases(self):
        self.assertEqual([supervisor.retry_delay(n) for n in range(7)], [1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(supervisor.retry_delay(100000), 30)

    @unittest.skipUnless(os.name == "nt", "Windows OpenSSH configuration inspection")
    def test_installed_ssh_accepts_options_without_connecting(self):
        ssh = shutil.which("ssh.exe")
        if ssh is None:
            self.skipTest("OpenSSH executable not installed")
        self.data["ssh_executable"] = ssh
        command = supervisor.ssh_command(self.config())
        result = subprocess.run([command[0], "-G", *command[1:]], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.lower()
        self.assertIn("hostname fixture.invalid", output)
        self.assertIn("passwordauthentication no", output)
        self.assertRegex(output, r"(?m)^dynamicforward (?:127\.0\.0\.1|\[127\.0\.0\.1\]):18081$")
        self.assertIn("userknownhostsfile", output)

    def test_single_instance_lock_released_after_context(self):
        lock_path = self.directory / "instance.lock"
        with supervisor.InstanceLock(lock_path):
            with self.assertRaises(supervisor.AlreadyRunning):
                with supervisor.InstanceLock(lock_path):
                    self.fail("second instance entered")
        with supervisor.InstanceLock(lock_path):
            pass

    def test_occupied_port_detected_without_killing_owner(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(supervisor.port_available(port))
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass

    def test_only_passed_child_is_stopped_and_waited(self):
        child = Mock()
        child.poll.return_value = None
        supervisor.stop_child(child)
        child.terminate.assert_called_once()
        child.wait.assert_called_once()
        child.kill.assert_not_called()

    def test_unresponsive_child_is_killed_and_reaped(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
        supervisor.stop_child(child)
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_managed_bridge_rejects_requests_until_own_ssh_is_ready(self):
        server = bridge.make_server("fixture-secret", 0, 1)
        server.RequestHandlerClass = supervisor.ManagedHandler
        server.ssh_available = threading.Event()
        with running(server):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                import base64
                authorization = base64.b64encode(b"opencode:fixture-secret").decode()
                connection.request("CONNECT", "fixture.invalid:443",
                                   headers={"Proxy-Authorization": "Basic " + authorization})
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
            finally:
                connection.close()

    @unittest.skipUnless(os.name == "nt", "Windows TCP ownership API")
    def test_windows_listener_ownership(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(supervisor.listener_owned_by(os.getpid(), port))
            self.assertFalse(supervisor.listener_owned_by(os.getpid() + 999999, port))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_job_close_terminates_only_owned_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(supervisor.stop_child, child)
        with supervisor.ChildJob() as job:
            job.assign(child)
            self.assertIsNone(child.poll())
        child.wait(timeout=5)
        self.assertIsNotNone(child.returncode)

    @unittest.skipUnless(os.name == "nt", "Real Windows child/Job Object fixture")
    def test_running_supervisor_restarts_owned_child_and_stops_cleanly(self):
        def free_port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        self.data["listen_port"] = free_port()
        self.data["socks_port"] = free_port()
        config = self.config()
        children = []
        errors = []
        original_popen = subprocess.Popen
        # Only the process launcher is substituted. The actual fixture process,
        # sockets, bridge, ownership API, backoff, job and stop file are real.
        fixture = (
            "from test_bridge import TestServer,SocksFixture; "
            f"s=TestServer(('127.0.0.1',{config.socks_port}),SocksFixture); "
            "s.destinations=[]; s.serve_forever()"
        )

        def launch(*args, **kwargs):
            child = original_popen([sys.executable, "-c", fixture],
                                   cwd=Path(__file__).parent, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(child)
            return child

        def wait_for(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                if errors:
                    raise errors[0]
                time.sleep(0.05)
            self.fail("fixture timed out")

        def target():
            try:
                supervisor.run(config, self.directory)
            except Exception as error:
                errors.append(error)

        def tunnel_echo(port):
            import base64
            authorization = base64.b64encode(b"opencode:fixture-secret").decode()
            with socket.create_connection(("127.0.0.1", config.listen_port), timeout=3) as client:
                client.settimeout(3)
                request = f"CONNECT fixture.invalid:{port} HTTP/1.1\r\nProxy-Authorization: Basic {authorization}\r\n\r\n"
                client.sendall(request.encode())
                header = bytearray()
                while not header.endswith(b"\r\n\r\n"):
                    chunk = client.recv(1)
                    if not chunk:
                        break
                    header.extend(chunk)
                if b"200 Connection Established" not in header:
                    return False
                client.sendall(b"opaque-fixture-data")
                self.assertEqual(bridge.read_exact(client, 19), b"opaque-fixture-data")
                return True

        with running(TestServer(("127.0.0.1", 0), EchoFixture)) as echo:
            with patch("supervisor.subprocess.Popen", side_effect=launch):
                worker = threading.Thread(target=target, daemon=True)
                worker.start()
                try:
                    wait_for(lambda: children and supervisor.listener_owned_by(children[-1].pid, config.socks_port))
                    wait_for(lambda: tunnel_echo(echo.server_address[1]))
                    with self.assertRaises(supervisor.AlreadyRunning):
                        supervisor.run(config, self.directory)
                    children[0].kill()
                    children[0].wait(timeout=5)
                    wait_for(lambda: len(children) >= 2 and supervisor.listener_owned_by(children[-1].pid, config.socks_port))
                    wait_for(lambda: tunnel_echo(echo.server_address[1]))
                finally:
                    (self.directory / "stop.request").write_text("stop", encoding="ascii")
                    worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertTrue(supervisor.port_available(config.listen_port))
                self.assertTrue(supervisor.port_available(config.socks_port))
        log = (self.directory / "supervisor.log").read_text(encoding="utf-8")
        self.assertNotIn("fixture-secret", log)
        self.assertNotIn("fixture.invalid", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
