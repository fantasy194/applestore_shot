"""OS differences shared by the source CLI and the packaged worker."""
import os
import sys
from pathlib import Path


def private_fd(fd):
    if hasattr(os, 'fchmod'):
        os.fchmod(fd, 0o600)


def browser_candidates(platform=None, environ=None):
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    if platform == 'darwin':
        return [Path(root) / name / 'Contents/MacOS' / exe
                for root in ('/Applications', str(Path.home() / 'Applications'))
                for name, exe in [('Google Chrome.app', 'Google Chrome'),
                                  ('Microsoft Edge.app', 'Microsoft Edge')]]
    if platform == 'win32':
        return [Path(env[root]) / suffix for root in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                if env.get(root) for suffix in ('Google/Chrome/Application/chrome.exe',
                                                'Microsoft/Edge/Application/msedge.exe')]
    if platform == 'linux':
        return [Path(path) for path in ('/usr/bin/google-chrome-stable',
                                       '/usr/bin/google-chrome',
                                       '/opt/google/chrome/google-chrome',
                                       '/usr/bin/microsoft-edge-stable',
                                       '/usr/bin/microsoft-edge',
                                       '/usr/bin/chromium')]
    return []


def find_browser():
    for path in browser_candidates():
        if path.is_file():
            return path
    raise ValueError('未找到 Chrome 或 Edge（Linux 也支持 Chromium），请先安装其中一种浏览器。')
