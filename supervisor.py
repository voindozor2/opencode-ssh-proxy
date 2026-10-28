"""Windows-only owner of an HTTP bridge and a reconnecting OpenSSH child.

No installers, network changes or OpenCode configuration changes. Configuration,
passwords and rotating event logs live outside the source checkout.
"""

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

import bridge


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        import msvcrt
        self.file = open(self.path, "a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            self.file = None
            raise AlreadyRunning from None
        return self

    def __exit__(self, *args):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
        self.file = None


@dataclass(frozen=True)
class Config:
    ssh_executable: str
    ssh_host: str
    ssh_user: str
    identity_file: str
    password_file: str
    listen_port: int
    socks_port: int


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    fields = set(Config.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("Unexpected or missing config fields")
    for name in ("ssh_executable", "identity_file", "password_file"):
        value = data[name]
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
            raise ValueError("Expected an absolute existing file")
    for name in ("ssh_host", "ssh_user"):
        value = data[name]
        pattern = r"[A-Za-z0-9][A-Za-z0-9._:-]*" if name == "ssh_host" else r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("Invalid SSH host or user")
    for name in ("listen_port", "socks_port"):
        if type(data[name]) is not int or not 1 <= data[name] <= 65535:
            raise ValueError("Invalid port")
    if data["listen_port"] == data["socks_port"]:
        raise ValueError("Ports must differ")
    return Config(**data)


def ssh_command(config):
    return [
        config.ssh_executable, "-F", os.devnull, "-N", "-T",
        "-D", f"127.0.0.1:{config.socks_port}", "-i", config.identity_file,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no", "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        f"{config.ssh_user}@{config.ssh_host}",
    ]


def retry_delay(failures):
    return min(30, 2 ** min(max(0, failures), 5))


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def listener_owned_by(pid, port):
    """Inspect IPv4 LISTEN rows in the Windows kernel; never trust a PID file."""
    class Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in
                    ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]

    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    result = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if result not in (0, 122):
        raise OSError("TCP table unavailable")
    for attempt in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if result == 122:
            continue
        if result != 0:
            raise OSError("TCP table unavailable")
        count = wintypes.DWORD.from_buffer(buffer).value
        for index in range(count):
            row = Row.from_buffer(buffer, ctypes.sizeof(wintypes.DWORD) + index * ctypes.sizeof(Row))
            address = socket.inet_ntoa(row.local_address.to_bytes(4, "little"))
            if (row.pid == pid and row.state == 2 and address == "127.0.0.1"
                    and socket.ntohs(row.local_port & 65535) == port):
                return True
        return False
    raise OSError("TCP table changed repeatedly")


class ChildJob:
    """Kill only assigned children if this supervisor exits, including abruptly."""
    def __enter__(self):
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.__exit__()
            raise ctypes.WinError(error)
        return self

    def assign(self, child):
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(child._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def __exit__(self, *args):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def stop_child(child):
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    else:
        child.wait()


class ManagedHandler(bridge.Handler):
    def authenticated(self):
        if not super().authenticated():
            return False
        if not self.server.ssh_available.is_set():
            self.reply(503)
            return False
        return True


def event_logger(directory):
    logger = logging.getLogger("ssh-http-supervisor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(directory / "supervisor.log", maxBytes=262144, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger, handler


def run(config, directory):
    stop_path = directory / "stop.request"
    with InstanceLock(directory / "instance.lock"):
        stop_path.unlink(missing_ok=True)
        logger, log_handler = event_logger(directory)
        child = None
        server = None
        worker = None
        ready = threading.Event()
        try:
            if not port_available(config.socks_port):
                raise OSError("SOCKS port already occupied")
            password = Path(config.password_file).read_text(encoding="utf-8").strip()
            server = bridge.make_server(password, config.listen_port, config.socks_port)
            server.RequestHandlerClass = ManagedHandler
            server.ssh_available = ready
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            logger.info("supervisor_started")
            failures = 0
            next_attempt = 0
            connected_since = None
            with ChildJob() as job:
                try:
                    while not stop_path.exists():
                        now = time.monotonic()
                        if child is None and now >= next_attempt:
                            if not port_available(config.socks_port):
                                logger.info("socks_port_busy_no_process_was_stopped")
                            else:
                                child = subprocess.Popen(ssh_command(config), stdin=subprocess.DEVNULL,
                                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                         creationflags=subprocess.CREATE_NO_WINDOW)
                                try:
                                    job.assign(child)
                                except Exception:
                                    stop_child(child)
                                    child = None
                                    raise
                                logger.info("ssh_started")
                            if child is None:
                                next_attempt = now + retry_delay(failures)
                                failures += 1
                        if child is not None:
                            exit_code = child.poll()
                            if exit_code is not None:
                                ready.clear()
                                child.wait()
                                child = None
                                logger.info("ssh_exited code=%s", exit_code)
                                if connected_since is not None and now - connected_since >= 60:
                                    failures = 0
                                connected_since = None
                                next_attempt = now + retry_delay(failures)
                                failures += 1
                            elif listener_owned_by(child.pid, config.socks_port):
                                if not ready.is_set():
                                    ready.set()
                                    connected_since = now
                                    logger.info("ssh_socks_listener_ready")
                            else:
                                ready.clear()
                        time.sleep(0.5)
                finally:
                    ready.clear()
                    stop_child(child)
        except KeyboardInterrupt:
            pass
        finally:
            ready.clear()
            if server is not None:
                if worker is not None:
                    server.shutdown()
                    worker.join(timeout=5)
                server.server_close()
            stop_path.unlink(missing_ok=True)
            logger.info("supervisor_stopped")
            logger.removeHandler(log_handler)
            log_handler.close()


def running(directory):
    try:
        with InstanceLock(directory / "instance.lock"):
            return False
    except AlreadyRunning:
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--stop", action="store_true")
    actions.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        print("Windows is required.", file=sys.stderr)
        return 2
    try:
        config_path = Path(args.config).resolve(strict=True)
        config = load_config(config_path)
        directory = config_path.parent
        if args.check:
            print("Configuration validated; no network connection was made.")
            return 0
        if args.status:
            active = running(directory)
            print("RUNNING (not an end-to-end health check)" if active else "STOPPED")
            return 0 if active else 1
        if args.stop:
            if not running(directory):
                print("Already stopped.")
                return 0
            (directory / "stop.request").write_text("stop\n", encoding="ascii")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if not running(directory):
                    print("Stopped.")
                    return 0
                time.sleep(0.25)
            print("Stop timed out; no unrelated process was terminated.", file=sys.stderr)
            return 2
        run(config, directory)
        return 0
    except AlreadyRunning:
        print("Supervisor already running for this state directory.")
        return 0
    except Exception as error:
        # Never print exception contents, config, key material or SSH stderr.
        print("Supervisor failed (" + type(error).__name__ + "). Check configuration and local ports.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
