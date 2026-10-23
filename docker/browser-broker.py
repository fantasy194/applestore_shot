"""Private, bounded process and CDP broker for the jlesage Chromium image."""
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from urllib.parse import urlsplit

SOCKET = '/data/.browser-control/control.sock'
PROCESSES = {}
LOCK = threading.Lock()
MAX_BROWSERS = 32


def validate_args(args):
    if not isinstance(args, list) or not 4 <= len(args) <= 14:
        raise ValueError('Invalid browser arguments')
    profile, port = None, None
    for value in args:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError('Invalid browser argument')
        if value.startswith('--user-data-dir=') and profile is None:
            profile = Path(value.split('=', 1)[1]).resolve()
            if not profile.is_relative_to('/data') or profile == Path('/data') or profile.is_relative_to('/data/.browser-control'):
                raise ValueError('Browser profile must belong to application data')
        elif value.startswith('--remote-debugging-port=') and port is None:
            port = int(value.split('=', 1)[1])
            if not 1024 <= port <= 65535:
                raise ValueError('Invalid CDP port')
        elif value in ('--remote-debugging-address=127.0.0.1', '--restore-last-session',
                       '--no-first-run', '--no-default-browser-check', '--headless=new', 'about:blank'):
            pass
        elif re.fullmatch(r'--window-size=[1-9][0-9]{1,3},[1-9][0-9]{1,3}', value):
            pass
        elif value == '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE duo-app, EXCLUDE 127.0.0.1':
            pass  # Offline container fixture; does not change the login path.
        elif value.startswith('--proxy-server='):
            proxy = urlsplit(value.split('=', 1)[1])
            if proxy.scheme not in ('http', 'https', 'socks5') or not proxy.hostname or proxy.username or proxy.password:
                raise ValueError('Invalid browser proxy')
        else:
            raise ValueError('Unsupported browser argument')
    if profile is None or port is None or '--remote-debugging-address=127.0.0.1' not in args:
        raise ValueError('Missing private profile or loopback CDP')
    return profile, port


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class Handler(socketserver.StreamRequestHandler):
    def reply(self, **payload):
        self.wfile.write(json.dumps({'ok': True, **payload}).encode() + b'\n')
        self.wfile.flush()

    def handle(self):
        owned = None
        self.connection.settimeout(5)
        try:
            data = self.rfile.readline(65537)
            if len(data) > 65536 or not data.endswith(b'\n'):
                raise ValueError('Invalid request')
            request = json.loads(data)
            if not isinstance(request, dict):
                raise ValueError('Invalid request')
            action = request.get('action')
            if action == 'ping':
                self.reply(protocol=1)
                return
            if action == 'launch':
                profile, port = validate_args(request.get('args'))
                with LOCK:
                    if len(PROCESSES) >= MAX_BROWSERS or any(item[1] == profile and item[0].poll() is None for item in PROCESSES.values()):
                        raise ValueError('Browser profile already in use or capacity reached')
                    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                    # Execute the installed browser directly, retaining its sandbox.
                    process = subprocess.Popen(['/usr/bin/chromium-browser', *request['args']],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    owned = secrets.token_hex(32)
                    PROCESSES[owned] = (process, profile, port)
                self.reply(token=owned, port=port)
                self.connection.settimeout(None)
                # The worker's lifetime connection is the process lease. A crash,
                # container restart, or cancellation cannot orphan its browser.
                while self.connection.recv(1):
                    pass
                return
            token = request.get('token')
            with LOCK:
                record = PROCESSES.get(token) if isinstance(token, str) else None
            if record is None:
                raise ValueError('Unknown browser process')
            process, _, port = record
            if action == 'poll':
                self.reply(returncode=process.poll())
            elif action in ('terminate', 'kill'):
                if process.poll() is None:
                    getattr(process, action)()
                self.reply()
            elif action == 'tunnel':
                with socket.create_connection(('127.0.0.1', port), timeout=5) as remote:
                    self.reply()
                    self.connection.settimeout(None)
                    remote.settimeout(None)
                    sockets = (self.connection, remote)
                    while process.poll() is None:
                        readable, _, _ = select.select(sockets, [], [], .2)
                        for source in readable:
                            part = source.recv(65536)
                            if not part:
                                return
                            (remote if source is self.connection else self.connection).sendall(part)
            else:
                raise ValueError('Unknown action')
        except (OSError, ValueError, TypeError):
            try:
                self.wfile.write(b'{"ok":false}\n')
                self.wfile.flush()
            except OSError:
                pass
        finally:
            if owned is not None:
                with LOCK:
                    record = PROCESSES.pop(owned, None)
                if record is not None:
                    stop(record[0])


class Server(getattr(socketserver, 'ThreadingUnixStreamServer', socketserver.ThreadingTCPServer)):
    daemon_threads = True


def main():
    if '--healthcheck' in sys.argv:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(SOCKET)
            connection.sendall(b'{"action":"ping"}\n')
            assert json.loads(connection.recv(1024)) == {'ok': True, 'protocol': 1}
        return
    os.umask(0o077)
    if os.geteuid() == 0:
        raise RuntimeError('The browser broker must run as the application user')
    directory = Path(SOCKET).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    if os.path.lexists(SOCKET):
        # Never replace another live broker. A stopped container can leave its
        # socket behind; only that unresponsive socket is removed.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(SOCKET)
            except ConnectionRefusedError:
                Path(SOCKET).unlink()
            else:
                raise RuntimeError('A browser broker is already running')
    server = Server(SOCKET, Handler)
    os.chmod(SOCKET, 0o600)

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
        with LOCK:
            remaining = list(PROCESSES.values())
        cleaners = [threading.Thread(target=stop, args=(process,)) for process, _, _ in remaining]
        for cleaner in cleaners:
            cleaner.start()
        for cleaner in cleaners:
            cleaner.join(timeout=10)
        Path(SOCKET).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
