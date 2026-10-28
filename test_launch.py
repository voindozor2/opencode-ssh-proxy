"""Offline launcher fixtures: no real OpenCode, provider or startup directory."""
import json
import os
import shutil
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell fixtures')
class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='launcher fixture ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project space'
        self.project.mkdir()
        self.password = self.root / 'password'
        self.password.write_text('fixture:@%/ secret', encoding='utf-8')
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.addCleanup(self.listener.close)
        self.config = self.root / 'settings.local.json'
        self.config.write_text(json.dumps({'password_file': str(self.password),
                                          'listen_port': self.listener.getsockname()[1]}))
        self.fake = self.root / 'fake.ps1'
        self.fake.write_text('''
[ordered]@{ args=@($args); cwd=(Get-Location).Path;
http=$env:HTTP_PROXY; https=$env:HTTPS_PROXY; no=$env:NO_PROXY } |
ConvertTo-Json | Set-Content -LiteralPath $env:FIXTURE_OUTPUT
exit 7
''')
        self.output = self.root / 'result.json'

    def invoke(self):
        env = os.environ.copy()
        env.update(FIXTURE_OUTPUT=str(self.output), HTTP_PROXY='old-http',
                   HTTPS_PROXY='old-https', NO_PROXY='old-no')
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                               '-ExecutionPolicy', 'Bypass', '-File',
                               str(Path(__file__).with_name('launch-opencode.ps1')),
                               '-SettingsFile', str(self.config), '-ProjectDirectory', str(self.project),
                               '-OpenCodeExecutable', str(self.fake)],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_child_env_encoding_standalone_cwd_and_exit_code(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        data = json.loads(self.output.read_text(encoding='utf-8-sig'))
        port = self.listener.getsockname()[1]
        expected = f'http://opencode:fixture%3A%40%25%2F%20secret@127.0.0.1:{port}'
        self.assertEqual(data['http'], expected)
        self.assertEqual(data['https'], expected)
        self.assertEqual(data['no'], 'localhost,127.0.0.1,::1')
        self.assertEqual(data['args'], ['--standalone'])
        self.assertEqual(Path(data['cwd']), self.project)
        self.assertNotIn('fixture:@', result.stdout + result.stderr)
        self.assertNotIn(expected, result.stdout + result.stderr)

    def test_closed_proxy_does_not_launch(self):
        self.listener.close()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_missing_or_empty_password_does_not_launch(self):
        for value in ('', None):
            if value is None:
                self.password.unlink()
            else:
                self.password.write_text(value)
            result = self.invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.output.exists())

    def test_invalid_port_does_not_launch(self):
        self.config.write_text(json.dumps({'password_file': str(self.password), 'listen_port': 0}))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_environment_and_directory_restored_in_calling_powershell(self):
        script = Path(__file__).with_name('launch-opencode.ps1')
        def ps(value):
            return "'" + str(value).replace("'", "''") + "'"
        command = f'''
$env:HTTP_PROXY='original'; $env:HTTPS_PROXY=$null; $env:NO_PROXY='original-no'
$before=(Get-Location).Path
& {ps(script)} -SettingsFile {ps(self.config)} -ProjectDirectory {ps(self.project)} -OpenCodeExecutable {ps(self.fake)}
if($env:HTTP_PROXY -ne 'original' -or $null -ne $env:HTTPS_PROXY -or $env:NO_PROXY -ne 'original-no' -or (Get-Location).Path -ne $before) {{exit 2}}
exit 0
'''
        env = os.environ.copy()
        env['FIXTURE_OUTPUT'] = str(self.output)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-ExecutionPolicy', 'Bypass', '-Command', command],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.output.exists())

    def test_shortcut_idempotence_and_unrelated_file_protection(self):
        script = Path(__file__).with_name('install-launcher-shortcut.ps1')
        binary = self.root / 'bin'
        binary.mkdir()
        shutil.copyfile(os.environ['SystemRoot'] + '\\System32\\whoami.exe', binary / 'opencode.exe')
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(script), '-ProjectDirectory', str(self.project),
                   '-ShortcutDirectory', str(self.root), '-CommandDirectory', str(binary)]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shortcut = self.root / 'OpenCode via SSH.lnk'
        self.assertTrue(shortcut.is_file())
        # Changing the requested project must not silently repurpose the shortcut.
        before = shortcut.read_bytes()
        command[command.index('-ProjectDirectory') + 1] = str(self.root)
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(shortcut.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
