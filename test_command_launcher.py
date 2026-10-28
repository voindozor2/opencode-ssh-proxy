"""Command wrapper tests with fake CLI, never with a real provider."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, unquote


class CommandLauncherTests(unittest.TestCase):
    def test_private_server_only_for_supported_commands(self):
        from command_launcher import cli_arguments
        cases = [
            ([], ['--standalone']),
            (['E:\\project space'], ['--standalone', 'E:\\project space']),
            (['run', '--model', 'openai/test', 'a message'],
             ['run', '--standalone', '--model', 'openai/test', 'a message']),
            (['mini', '-c'], ['mini', '--standalone', '-c']),
            (['--log-level', 'debug', 'run', 'OK'],
             ['--log-level', 'debug', 'run', '--standalone', 'OK']),
            (['run', '--standalone', 'OK'], ['run', '--standalone', 'OK']),
            (['--server=http://localhost:1234'], ['--server=http://localhost:1234']),
            (['run', '--server', 'http://localhost:1234', 'OK'],
             ['run', '--server', 'http://localhost:1234', 'OK']),
            (['service', 'status'], ['service', 'status']),
            (['api', 'get', '/api/info'], ['api', 'get', '/api/info']),
            (['debug', 'paths', 'db'], ['debug', 'paths', 'db']),
            (['--version'], ['--version']),
            (['--help'], ['--help']),
        ]
        for original, expected in cases:
            with self.subTest(original=original):
                self.assertEqual(cli_arguments(original), expected)

    def test_child_environment_arguments_cwd_and_return_code(self):
        from command_launcher import launch
        with tempfile.TemporaryDirectory(prefix='command fixture ') as temp:
            root = Path(temp)
            password = root / 'password'
            secret = 'fixture:@%/ password'
            password.write_text(secret)
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'password_file': str(password), 'listen_port': 18080}))
            fake = root / 'fake.py'
            output = root / 'result.json'
            fake.write_text('''import json, os, sys
from pathlib import Path
Path(os.environ['FIXTURE_OUTPUT']).write_text(json.dumps({
'args':sys.argv[1:], 'cwd':os.getcwd(),
'http':os.environ['HTTP_PROXY'], 'https':os.environ['HTTPS_PROXY'],
'no':os.environ['NO_PROXY']}))
sys.exit(7)
''')
            # Production accepts only a native exe; fake.py is an ordinary argument to Python.
            config = root / 'command.json'
            config.write_text(json.dumps({'native_cli': sys.executable, 'settings_file': str(settings)}))
            old = os.environ.copy()
            environment = dict(old, FIXTURE_OUTPUT=str(output))
            args = [str(fake), 'space argument', 'a&b', '100%', 'quote"inside', 'Юникод']
            real_run = subprocess.run
            def fake_run(command, **kwargs):
                self.assertEqual(command[1], '--standalone')
                return real_run([command[0], *command[2:]], **kwargs)
            with patch('command_launcher.subprocess.run', side_effect=fake_run):
                code = launch(config, args, environment=environment, cwd=root)
            self.assertEqual(code, 7)
            data = json.loads(output.read_text())
            self.assertEqual(data['args'], args[1:])
            self.assertEqual(Path(data['cwd']), root)
            parsed = urlsplit(data['http'])
            self.assertEqual(unquote(parsed.password), secret)
            self.assertEqual(parsed.hostname, '127.0.0.1')
            self.assertEqual(parsed.port, 18080)
            self.assertEqual(data['https'], data['http'])
            self.assertEqual(data['no'], 'localhost,127.0.0.1,::1')
            self.assertEqual(os.environ.copy(), old)

    def test_invalid_config_never_starts_cli(self):
        from command_launcher import launch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            password = root / 'password'
            password.write_text('')
            settings = root / 'settings.json'
            config = root / 'config.json'
            config.write_text(json.dumps({'native_cli': sys.executable, 'settings_file': str(settings)}))
            for port, secret in ((0, 'fixture'), (18080, ''), (True, 'fixture')):
                password.write_text(secret)
                settings.write_text(json.dumps({'password_file': str(password), 'listen_port': port}))
                with patch('command_launcher.subprocess.run') as run:
                    with self.assertRaises(ValueError):
                        launch(config, [])
                    run.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows installation fixtures')
class CommandInstallTests(unittest.TestCase):
    def test_install_remove_keeps_unrelated_path_edits_and_owns_files(self):
        script = Path(__file__).with_name('manage-command.ps1')
        with tempfile.TemporaryDirectory(prefix='command install fixture ') as temp:
            root = Path(temp)
            state = root / 'state'
            state.mkdir()
            settings = state / 'settings.local.json'
            settings.write_text('{}')
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                       '-File', str(script), '-CommandDirectory', str(root / 'bin'),
                       '-StateDirectory', str(state), '-PythonExecutable', sys.executable,
                       '-NativeExecutable', sys.executable, '-PathFixture', str(root / 'user-path.json')]
            path_file = root / 'user-path.json'
            path_file.write_text(json.dumps({'value': 'C:\\fixture-one;C:\\fixture-two',
                                             'kind': 'ExpandString'}))
            def invoke(action):
                return subprocess.run(command + ['-Action', action], capture_output=True, text=True, timeout=20)
            for _ in range(2):
                result = invoke('Install')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            installed = json.loads(path_file.read_text(encoding='utf-8-sig'))
            self.assertTrue(installed['value'].startswith(str(root / 'bin') + ';'))
            self.assertEqual(installed['kind'], 'ExpandString')
            # Exercise the actual compiled shim -> Python -> fake CLI chain.
            password = state / 'password'
            password.write_text('fixture:@%/ secret')
            settings.write_text(json.dumps({'password_file': str(password), 'listen_port': 18080}))
            fake = root / 'fake.py'
            fake.write_text('''import os, json, sys
from pathlib import Path
Path(os.environ['FIXTURE_OUTPUT']).write_text(json.dumps({
'args':sys.argv[1:], 'cwd':os.getcwd(), 'proxy':os.environ['HTTPS_PROXY']}))
sys.exit(7)
''')
            output = root / 'result.json'
            env = dict(os.environ, FIXTURE_OUTPUT=str(output))
            args = ['--help', '', 'a space', 'quote"inside', 'trailing\\', 'a&b', '%PATH%', '!x!', 'Юникод']
            result = subprocess.run([str(root / 'bin' / 'opencode.exe'), str(fake), *args],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            data = json.loads(output.read_text())
            self.assertEqual(data['args'], args)
            self.assertEqual(Path(data['cwd']), root)
            self.assertEqual(unquote(urlsplit(data['proxy']).password), 'fixture:@%/ secret')
            self.assertNotIn('secret', result.stdout + result.stderr)
            installed['value'] += ';C:\\added-after-install'
            path_file.write_text(json.dumps(installed))
            for _ in range(2):
                result = invoke('Remove')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            restored = json.loads(path_file.read_text(encoding='utf-8-sig'))
            self.assertEqual(restored['value'], 'C:\\fixture-one;C:\\fixture-two;C:\\added-after-install')
            self.assertFalse((root / 'bin' / 'opencode.exe').exists())
            self.assertEqual(len(list(state.glob('command-install-before-*.json'))), 1)
            result = invoke('Install')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = invoke('Remove')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unrelated_wrapper_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='command install fixture ') as temp:
            root = Path(temp)
            binary = root / 'bin'
            binary.mkdir()
            wrapper = binary / 'opencode.exe'
            wrapper.write_text('user work')
            fixture = root / 'path.json'
            fixture.write_text(json.dumps({'value': 'original', 'kind': 'String'}))
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                     '-ExecutionPolicy', 'Bypass', '-File',
                                     str(Path(__file__).with_name('manage-command.ps1')),
                                     '-Action', 'Install', '-CommandDirectory', str(binary),
                                     '-StateDirectory', str(root / 'state'),
                                     '-PythonExecutable', sys.executable, '-NativeExecutable', sys.executable,
                                     '-PathFixture', str(fixture)],
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(wrapper.read_text(), 'user work')
            self.assertEqual(json.loads(fixture.read_text())['value'], 'original')


if __name__ == '__main__':
    unittest.main(verbosity=2)
