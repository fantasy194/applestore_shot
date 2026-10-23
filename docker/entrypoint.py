"""Keep the real GUI Chrome display alive until the Web engine has stopped."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    os.umask(0o077)
    if not os.environ.get('DUO_WEB_PASSWORD'):
        os.environ.pop('DUO_WEB_PASSWORD', None)
    port = int(os.environ.get('DUO_WEB_PORT', '17654'))
    if not 1 <= port <= 65535:
        raise ValueError('DUO_WEB_PORT must be between 1 and 65535')
    runtime = Path('/tmp/duo-runtime')
    runtime.mkdir(mode=0o700, exist_ok=True)
    os.environ['XDG_RUNTIME_DIR'] = str(runtime)
    children = []
    web = None
    stopping_at = None

    def stop(signum, _frame):
        nonlocal stopping_at
        if stopping_at is None:
            stopping_at = time.monotonic()
            if web is not None and web.poll() is None:
                web.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        display = subprocess.Popen(['Xvfb', os.environ.get('DISPLAY', ':99'),
            '-screen', '0', '1280x1024x24', '-nolisten', 'tcp', '-ac'])
        children.append(display)
        for _ in range(100):
            if display.poll() is not None:
                raise RuntimeError('Xvfb exited before its display was ready')
            if stopping_at is not None:
                return 0
            ready = subprocess.run(['xdpyinfo'], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=2)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('GUI display did not become ready')
        window_manager = subprocess.Popen(['openbox', '--sm-disable'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(window_manager)
        if stopping_at is not None:
            return 0
        web = subprocess.Popen(['/app/duo-killer-web', '--no-open',
            '--listen-address', '0.0.0.0', '--port', str(port), '--data-dir', '/data'])
        if stopping_at is not None:
            web.terminate()
        while web.poll() is None:
            if any(child.poll() is not None for child in children):
                print('GUI display stopped; shutting down the Web engine.', file=sys.stderr, flush=True)
                stop(signal.SIGTERM, None)
            if stopping_at is not None and time.monotonic() - stopping_at > 35:
                web.kill()
                break
            time.sleep(.2)
        return web.wait()
    finally:
        if web is not None and web.poll() is None:
            web.terminate()
            try:
                web.wait(timeout=35)
            except subprocess.TimeoutExpired:
                web.kill()
                web.wait()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    raise SystemExit(main())
