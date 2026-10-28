"""Windows PowerShell/ACL/shortcut fixtures; never touch real Startup or VPS."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows PowerShell fixtures")
class ManageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ssh proxy fixture ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state = self.directory / "private state"
        self.startup = self.directory / "fake Startup"
        self.startup.mkdir()
        self.key = self.directory / "fixture-key"
        self.key.write_text("fixture only", encoding="utf-8")
        self.script = Path(__file__).with_name("manage.ps1")
        self.ssh = shutil.which("ssh.exe")
        if self.ssh is None:
            self.skipTest("OpenSSH executable not present")

    def invoke(self, action, *arguments):
        return subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.script), "-Action", action,
            "-StateDirectory", str(self.state), "-StartupDirectory", str(self.startup),
            "-PythonExecutable", sys.executable, *arguments,
        ], capture_output=True, text=True, timeout=20)

    def configure(self):
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            listen_port = first.getsockname()[1]
            socks_port = second.getsockname()[1]
        result = self.invoke("Configure", "-VpsHost", "fixture.invalid", "-SshUser", "fixture",
                             "-IdentityFile", str(self.key), "-SshExecutable", self.ssh,
                             "-ListenPort", str(listen_port), "-SocksPort", str(socks_port))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((self.state / "settings.local.json").read_text(encoding="utf-8"))

    def test_configure_creates_private_state_without_starting(self):
        config = self.configure()
        password = Path(config["password_file"]).read_text(encoding="utf-8")
        self.assertGreaterEqual(len(password), 40)
        self.assertFalse((self.state / "instance.lock").exists())
        self.assertEqual(list(self.startup.iterdir()), [])
        # Protection is checked through the real Windows ACL API, not just text output.
        command = "$a=Get-Acl -LiteralPath '" + str(self.state).replace("'", "''") + "'; if(-not $a.AreAccessRulesProtected){exit 2}; @($a.Access).Count"
        acl = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(acl.returncode, 0, acl.stderr)
        self.assertEqual(acl.stdout.strip(), "2")

    def test_configure_does_not_overwrite_existing_settings(self):
        self.configure()
        before = (self.state / "settings.local.json").read_bytes()
        result = self.invoke("Configure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.state / "settings.local.json").read_bytes(), before)

    def test_install_remove_shortcut_are_idempotent(self):
        self.configure()
        for _ in range(2):
            result = self.invoke("InstallStartup")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(list(self.startup.glob("*.lnk"))), 1)
        for _ in range(2):
            result = self.invoke("RemoveStartup")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.startup.iterdir()), [])

    def test_unrelated_shortcut_is_not_overwritten_or_removed(self):
        self.configure()
        shortcut = self.startup / "OpenCodeSSHProxy.lnk"
        command = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(shortcut).replace("'", "''") + "'); $s.TargetPath='C:\\Windows\\System32\\notepad.exe'; $s.Save()"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = shortcut.read_bytes()
        for action in ("InstallStartup", "RemoveStartup"):
            result = self.invoke(action)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(shortcut.read_bytes(), before)

    def test_stop_and_status_with_no_running_instance(self):
        self.configure()
        for action in ("Status", "Stop"):
            result = self.invoke(action)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state / "stop.request").exists())

    def test_background_start_is_idempotent_and_can_be_stopped(self):
        config = self.configure()
        # Python deliberately rejects SSH arguments and exits. This exercises
        # pythonw/background control and retry without making any SSH connection.
        config["ssh_executable"] = sys.executable
        path = self.state / "settings.local.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        try:
            first = self.invoke("Start")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            second = self.invoke("Start")
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("Already running", second.stdout)
        finally:
            stopped = self.invoke("Stop")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        status = self.invoke("Status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("STOPPED", status.stdout)
        self.assertEqual(list(self.startup.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
