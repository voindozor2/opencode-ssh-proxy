"""Environment-only wrapper for the ordinary OpenCode CLI. No global proxy settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote


SUBCOMMANDS = frozenset(('upgrade', 'update', 'uninstall', 'acp', 'api', 'debug', 'auth',
                         'mcp', 'plugin', 'models', 'stats', 'mini', 'run', 'session',
                         'service', 'reload', 'pair', 'serve'))
VALUE_FLAGS = frozenset(('--log-level', '--completions', '--session', '-s', '--prompt', '--server'))


def cli_arguments(arguments):
    """Only TUI/run/mini get private servers. Preserve explicit server selection."""
    args = list(arguments)
    command_index = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        if not arg.startswith('-'):
            if arg in SUBCOMMANDS:
                command_index = index
            break
        index += 2 if arg in VALUE_FLAGS else 1
    # Do not interpret prompt text / positional arguments after a -- separator as flags.
    options = args[:args.index('--')] if '--' in args else args
    if any(arg in ('--standalone', '--server', '--help', '-h', '--version', '-v', '--completions')
           or arg.startswith(('--server=', '--completions=')) for arg in options):
        return args
    if command_index is None:
        return ['--standalone', *args]
    if args[command_index] in ('run', 'mini'):
        return [*args[:command_index + 1], '--standalone', *args[command_index + 1:]]
    return args


def launch(config_path, arguments, *, environment=None, cwd=None):
    config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    native = Path(config['native_cli'])
    if not native.is_absolute() or not native.is_file() or native.suffix.lower() != '.exe':
        raise ValueError('Native CLI unavailable')
    settings = json.loads(Path(config['settings_file']).read_text(encoding='utf-8-sig'))
    port = settings['listen_port']
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    password = Path(settings['password_file']).read_text(encoding='utf-8-sig').strip()
    if not password:
        raise ValueError('Empty proxy password')
    proxy = f'http://opencode:{quote(password, safe="")}@127.0.0.1:{port}'
    child_env = dict(os.environ if environment is None else environment)
    # Override alternative casing too, for callers with hand-built environment dicts.
    for key in list(child_env):
        if key.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'):
            del child_env[key]
    child_env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY='localhost,127.0.0.1,::1')
    # No shell, secret argv, output capture, cwd switch, or service mutation.
    return subprocess.run([str(native), *cli_arguments(arguments)], env=child_env, cwd=cwd).returncode


def main():
    try:
        if len(sys.argv) < 2:
            raise ValueError('Installer configuration required')
        return launch(sys.argv[1], sys.argv[2:])
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('OpenCode proxy wrapper failed. Check the installed native CLI and private settings/password. '
              'No direct fallback. Private details suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
