"""Owned browser lifecycle, local on desktop and in the Chromium sidecar.

The sidecar's Unix socket is private to the shared data volume. A lifetime
connection owns each process; losing the worker closes its browser as well.
Only loopback CDP is forwarded, keeping existing preview/input code unchanged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import select
import socket
import socketserver
import subprocess
import threading
import time


def _request(connection, value):
    connection.sendall(json.dumps(value).encode() + b'\n')
    data = bytearray()
    while not data.endswith(b'\n'):
        part = connection.recv(1)
        if not part or len(data) > 65536:
            raise OSError('浏览器服务未正常响应。')
        data.extend(part)
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise OSError('浏览器服务响应无效。') from None
    if not isinstance(result, dict) or not result.get('ok'):
        raise OSError('浏览器服务拒绝请求；已有资料已保留。')
    return result


def _connect(path):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(path)
    except BaseException:
        connection.close()
        raise
    return connection


def sidecar_ready():
    path = os.environ.get('DUO_BROWSER_SOCKET')
    if not path:
        return False
    with _connect(path) as connection:
        return _request(connection, {'action': 'ping'})['protocol'] == 1


class SidecarProcess:
    # A sidecar PID must never be reported to the host's process killer.
    pid = None

    def __init__(self, args, path):
        self.path, self.returncode = path, None
        self.owner = _connect(path)
        self.server = None
        try:
            answer = _request(self.owner, {'action': 'launch', 'args': args[1:]})
            self.token = answer.get('token')
            self.port = answer.get('port')
            if not isinstance(self.token, str) or len(self.token) != 64 or type(self.port) is not int:
                raise OSError('浏览器服务响应无效。')
            self.owner.settimeout(None)
            process = self

            class Relay(socketserver.BaseRequestHandler):
                def handle(self):
                    try:
                        with _connect(process.path) as remote:
                            _request(remote, {'action': 'tunnel', 'token': process.token})
                            remote.settimeout(None)
                            sockets = (self.request, remote)
                            while process.returncode is None:
                                readable, _, _ = select.select(sockets, [], [], .2)
                                for source in readable:
                                    data = source.recv(65536)
                                    if not data:
                                        return
                                    (remote if source is self.request else self.request).sendall(data)
                    except OSError:
                        return

            class Server(socketserver.ThreadingTCPServer):
                daemon_threads = True
                allow_reuse_address = True

            self.server = Server(('127.0.0.1', self.port), Relay)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        except BaseException:
            self.owner.close()
            raise

    def _finish(self, code):
        self.returncode = code
        self.owner.close()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        return code

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            with _connect(self.path) as connection:
                code = _request(connection, {'action': 'poll', 'token': self.token})['returncode']
        except OSError:
            return self._finish(-1)
        return None if code is None else self._finish(code)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('sidecar-browser', timeout)
            time.sleep(.1)
        return self.returncode

    def terminate(self):
        self._signal('terminate')

    def kill(self):
        self._signal('kill')

    def _signal(self, action):
        if self.returncode is not None:
            return
        try:
            with _connect(self.path) as connection:
                _request(connection, {'action': action, 'token': self.token})
        except OSError:
            self._finish(-1)


_background_browser = ContextVar('duo_background_browser', default=False)


@contextmanager
def background_browser_scope():
    """Nested login/context helpers cannot accidentally open a visible window."""
    token = _background_browser.set(True)
    try:
        yield
    finally:
        _background_browser.reset(token)


def launch_browser(args, **options):
    if _background_browser.get():
        args = [arg for arg in args if not arg.startswith('--headless')]
        args.append('--headless=new')
        if os.name == 'nt':
            options['creationflags'] = options.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    path = os.environ.get('DUO_BROWSER_SOCKET')
    if path:
        return SidecarProcess(args, path)
    return subprocess.Popen(args, **options)
