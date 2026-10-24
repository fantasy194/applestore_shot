"""Private, bounded process and CDP broker for the jlesage Chromium image."""
import base64
import hashlib
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
from urllib.request import ProxyHandler, build_opener

SOCKET = '/data/.browser-control/control.sock'
MAX_BROWSERS = 32


class BrokerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


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


def stop(process, *, kill=False):
    try:
        if process.poll() is None:
            def send_signal(hard=False):
                if getattr(process, 'duo_owned_group', False):
                    if hard:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.terminate()  # The supervisor closes Chrome via CDP first.
                else:
                    process.kill() if hard else process.terminate()
            send_signal(kill)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                send_signal(True)
                process.wait(timeout=5)
    except ProcessLookupError:
        pass
    finally:
        if getattr(process, 'duo_owned_group', False) and process.stdin is not None:
            process.stdin.close()


def launch_owned_browser(args, **options):
    """The pipe belongs to the broker, rather than to a transient task lease."""
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--owned-browser', *args],
        stdin=subprocess.PIPE, start_new_session=True, **options)
    process.duo_owned_group = True
    return process


def close_chrome(args):
    """Request normal browser shutdown so Cookies/profile updates are flushed.

    This local-only minimal WebSocket sends one fixed CDP command; it never
    navigates, authenticates, reads account content, or retries a purchase.
    """
    port = next(int(arg.split('=', 1)[1]) for arg in args if arg.startswith('--remote-debugging-port='))
    opener = build_opener(ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
        value = json.loads(response.read(65536))
    endpoint = urlsplit(value['webSocketDebuggerUrl'])
    if endpoint.hostname not in ('127.0.0.1', 'localhost') or endpoint.port != port:
        raise ValueError('Unexpected owned browser endpoint')
    key = base64.b64encode(os.urandom(16)).decode()
    with socket.create_connection(('127.0.0.1', port), timeout=1) as connection:
        request = (f'GET {endpoint.path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                   'Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n'
                   f'Sec-WebSocket-Key: {key}\r\n\r\n')
        connection.sendall(request.encode())
        headers = bytearray()
        while b'\r\n\r\n' not in headers:
            part = connection.recv(1024)
            if not part or len(headers) > 8192:
                raise OSError('Owned browser handshake failed')
            headers.extend(part)
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if not bytes(headers).startswith(b'HTTP/1.1 101 ') or expected not in headers:
            raise OSError('Owned browser handshake rejected')
        payload = b'{"id":1,"method":"Browser.close"}'
        mask = os.urandom(4)
        connection.sendall(bytes((0x81, 0x80 | len(payload))) + mask +
            bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))


def own_browser(args):
    """A broker crash closes stdin; stop only our Chrome and its process group."""
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopping.set())
    signal.signal(signal.SIGINT, lambda *_args: stopping.set())
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None and not stopping.is_set():
            readable, _, _ = select.select([sys.stdin.buffer], [], [], .2)
            if readable and not os.read(sys.stdin.fileno(), 1):
                break
    finally:
        if process.poll() is None:
            try:
                close_chrome(args)
                process.wait(timeout=4)
            except Exception:
                # Any local-CDP failure must still reach the bounded process
                # cleanup below; there is no login/order retry in this helper.
                pass
        stop(process)
        # Chromium normally reaps its children. A stuck descendant still belongs
        # to this account's dedicated group; no other browser is ever targeted.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except ProcessLookupError:
            pass
    return process.returncode or 0


class BrowserBroker:
    """Own one normal Chromium per profile and grant exclusive task leases.

    Releasing a task never destroys the account's browser. A token belongs to
    one acquisition only, so late tunnel/poll/cleanup cannot reach a later task.
    Browser records are intentionally memory-only: restart never replays work.
    """
    def __init__(self, launch=None):
        self.launch = launch or launch_owned_browser
        self.profiles = {}
        self.leases = {}
        self.lock = threading.Lock()
        self.closing_all = False

    def acquire(self, args):
        profile, requested_port = validate_args(args)
        # Docker always uses normal Chrome, also for automatic maintenance.
        normalized = [arg for arg in args if not arg.startswith('--headless')]
        network = tuple(arg for arg in normalized if arg.startswith(('--proxy-server=', '--host-resolver-rules=')))
        with self.lock:
            if self.closing_all:
                raise BrokerError('BROWSER_SERVICE_STOPPING')
            record = self.profiles.get(profile)
            if record is not None and record['process'].poll() is not None:
                self._forget(record)
                record = None
            if record is not None and (record['lease'] is not None or record['closing']):
                raise BrokerError('BROWSER_PROFILE_BUSY')
            reused = record is not None
            if record is None:
                # Dead processes in other idle profiles do not consume capacity.
                for candidate in list(self.profiles.values()):
                    if candidate['process'].poll() is not None:
                        self._forget(candidate)
                if len(self.profiles) >= MAX_BROWSERS:
                    raise BrokerError('BROWSER_CAPACITY_REACHED')
                profile.mkdir(mode=0o700, parents=True, exist_ok=True)
                with socket.socket() as endpoint:
                    endpoint.bind(('127.0.0.1', 0))
                    remote_port = endpoint.getsockname()[1]
                normalized = [f'--remote-debugging-port={remote_port}'
                              if arg.startswith('--remote-debugging-port=') else arg for arg in normalized]
                process = self.launch(['/usr/bin/chromium-browser', *normalized],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                record = {'process': process, 'profile': profile, 'port': remote_port,
                          'network': network, 'lease': None, 'closing': False}
                self.profiles[profile] = record
            elif record['network'] != network:
                # A retained browser cannot silently adopt changed proxy/DNS
                # configuration. Explicitly close it before changing these.
                raise BrokerError('BROWSER_PROFILE_CONFIGURATION_CHANGED')
            token = secrets.token_hex(32)
            record['lease'] = token
            self.leases[token] = record
            return {'token': token, 'port': requested_port, 'remote_port': record['port'],
                    'reused': reused, 'persistent': True}

    def _forget(self, record):
        token = record['lease']
        if token is not None:
            self.leases.pop(token, None)
        record['lease'] = None
        if self.profiles.get(record['profile']) is record:
            self.profiles.pop(record['profile'])

    def leased(self, token):
        with self.lock:
            record = self.leases.get(token) if isinstance(token, str) else None
            if record is None or record['lease'] != token or record['closing']:
                raise BrokerError('BROWSER_LEASE_EXPIRED')
            return record

    def active(self, token, record):
        with self.lock:
            return self.leases.get(token) is record and record['lease'] == token and not record['closing']

    def release(self, token):
        with self.lock:
            record = self.leases.pop(token, None)
            if record is not None and record['lease'] == token:
                record['lease'] = None

    def close_profile(self, profile, *, force=False, kill=False, expected_token=None):
        with self.lock:
            record = self.profiles.get(profile)
            if record is None:
                return False
            if expected_token is not None and record['lease'] != expected_token:
                raise BrokerError('BROWSER_LEASE_EXPIRED')
            if record['closing'] or (record['lease'] is not None and not force):
                raise BrokerError('BROWSER_PROFILE_BUSY')
            record['closing'] = True
            token = record['lease']
            if token is not None:
                self.leases.pop(token, None)
                record['lease'] = None
        try:
            stop(record['process'], kill=kill)
        finally:
            with self.lock:
                self._forget(record)
        return True

    def close_lease(self, token, *, kill=False):
        record = self.leased(token)
        return self.close_profile(record['profile'], force=True, kill=kill, expected_token=token)

    def close_all(self):
        with self.lock:
            self.closing_all = True
            profiles = list(self.profiles)
        try:
            cleaners = [threading.Thread(target=self.close_profile, args=(profile,),
                kwargs={'force': True}) for profile in profiles]
            for cleaner in cleaners:
                cleaner.start()
            for cleaner in cleaners:
                cleaner.join(timeout=14)
        finally:
            with self.lock:
                self.closing_all = False


BROKER = BrowserBroker()


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
                answer = self.server.broker.acquire(request.get('args'))
                owned = answer['token']
                self.reply(**answer)
                self.connection.settimeout(None)
                # EOF relinquishes this task's operation lease. The broker keeps
                # the account browser alive for a later task in the same profile.
                while self.connection.recv(1):
                    pass
                return
            if action == 'close_profile':
                raw = request.get('profile')
                if not isinstance(raw, str) or type(request.get('force', False)) is not bool:
                    raise ValueError('Invalid profile request')
                profile, _ = validate_args(['--user-data-dir=' + raw, '--remote-debugging-port=30123',
                    '--remote-debugging-address=127.0.0.1', 'about:blank'])
                self.reply(closed=self.server.broker.close_profile(profile, force=request.get('force', False)))
                return
            if action == 'shutdown_all':
                self.server.broker.close_all()
                self.reply()
                return
            token = request.get('token')
            record = self.server.broker.leased(token)
            process, port = record['process'], record['port']
            if action == 'poll':
                self.reply(returncode=process.poll())
            elif action == 'release':
                self.server.broker.release(token)
                self.reply()
            elif action in ('terminate', 'kill', 'shutdown'):
                self.server.broker.close_lease(token, kill=action == 'kill')
                self.reply()
            elif action == 'tunnel':
                with socket.create_connection(('127.0.0.1', port), timeout=5) as remote:
                    self.reply()
                    # Forward writes are serialized with lease release below;
                    # bound local socket writes so a stalled reader cannot hold
                    # the registry lock indefinitely.
                    self.connection.settimeout(1)
                    remote.settimeout(1)
                    sockets = (self.connection, remote)
                    while process.poll() is None and self.server.broker.active(token, record):
                        readable, _, _ = select.select(sockets, [], [], .2)
                        for source in readable:
                            part = source.recv(65536)
                            if not part:
                                return
                            with self.server.broker.lock:
                                if (self.server.broker.leases.get(token) is not record
                                        or record['lease'] != token or record['closing']):
                                    return
                                (remote if source is self.connection else self.connection).sendall(part)
            else:
                raise ValueError('Unknown action')
        except (OSError, ValueError, TypeError) as error:
            try:
                # Only bounded protocol codes are returned, never arguments,
                # profile paths, credentials, or underlying exceptions.
                code = error.code if isinstance(error, BrokerError) else 'BROWSER_REQUEST_REJECTED'
                self.wfile.write(json.dumps({'ok': False, 'code': code}).encode() + b'\n')
                self.wfile.flush()
            except OSError:
                pass
        finally:
            if owned is not None:
                self.server.broker.release(owned)


class Server(getattr(socketserver, 'ThreadingUnixStreamServer', socketserver.ThreadingTCPServer)):
    daemon_threads = True

    def __init__(self, address, handler=Handler, *, broker=None):
        self.broker = broker or BROKER
        super().__init__(address, handler)


def main():
    if '--owned-browser' in sys.argv:
        return own_browser(sys.argv[sys.argv.index('--owned-browser') + 1:])
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
        server.broker.close_all()
        Path(SOCKET).unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
