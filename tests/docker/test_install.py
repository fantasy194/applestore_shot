"""Offline installer regression; no Docker socket, network or real account data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / 'scripts' / 'install-docker.sh'


@unittest.skipIf(os.name == 'nt', 'Runs in Linux CI or an isolated Linux container')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='duo-installer-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deploy = self.root / 'deployment with spaces'
        self.deploy.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'docker-calls.jsonl'
        self.env = dict(os.environ, DUO_INSTALL_DIR=str(self.deploy),
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        MOCK_ROOT=str(ROOT), MOCK_CALLS=str(self.calls))
        self.executable('docker', '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['MOCK_CALLS'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
if 'pull' in args:
    time.sleep(float(os.environ.get('MOCK_PULL_SLEEP', '0')))
    if os.environ.get('MOCK_PULL_FAILURE'):
        print('fixture registry unavailable', file=sys.stderr)
        sys.exit(23)
if 'config' in args and os.environ.get('MOCK_CONFIG_FAILURE'):
    print('fixture invalid compose', file=sys.stderr)
    sys.exit(15)
if 'up' in args and os.environ.get('MOCK_UP_FAILURE'):
    print('fixture application not healthy', file=sys.stderr)
    sys.exit(17)
if 'version' in args:
    print('Docker Compose fixture')
if 'info' in args:
    print('linux/aarch64')
if 'ps' in args:
    print('fixture app healthy')
''')
        self.executable('curl', '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
assert '--progress-bar' in args and '--max-time' in args and '--connect-timeout' in args
assert args[args.index('--retry') + 1] == '1'
destination = Path(args[args.index('--output') + 1])
if os.environ.get('MOCK_CURL_FAILURE'):
    destination.write_text('partial fixture download')
    print('fixture download timeout', file=sys.stderr)
    sys.exit(28)
url = next(value for value in args if value.startswith('https://'))
assert url.startswith('https://raw.githubusercontent.com/fantasy194/applestore_shot/main/')
relative = 'docker/chrome-seccomp.json' if url.endswith('chrome-seccomp.json') else 'compose.yaml'
destination.write_bytes((Path(os.environ['MOCK_ROOT']) / relative).read_bytes())
print('fixture download 100%', file=sys.stderr)
''')

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(source, encoding='utf-8')
        path.chmod(0o755)

    def run_installer(self, **env):
        return subprocess.run(['bash', str(INSTALLER)], env={**self.env, **env},
                              text=True, encoding='utf-8', capture_output=True, timeout=40)

    def docker_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_fresh_install_prints_stages_and_separates_pull_from_start(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for number in range(1, 7):
            self.assertIn(f'{number}/6', result.stdout)
        self.assertIn('100%', result.stderr)
        self.assertTrue((self.deploy / 'compose.yaml').stat().st_size)
        self.assertIn('watchtower', (self.deploy / 'compose.autoupdate.yaml').read_text())
        calls = self.docker_calls()
        pull = next(i for i, args in enumerate(calls) if 'pull' in args)
        start = next(i for i, args in enumerate(calls) if 'up' in args)
        self.assertLess(pull, start)
        self.assertIn('--progress', calls[pull])
        self.assertIn('--pull', calls[start])
        self.assertEqual(calls[start][calls[start].index('--pull') + 1], 'never')
        self.assertFalse(any('down' in args or '--volumes' in args for args in calls))
        self.assertFalse(list(self.deploy.rglob('*.download.*')))

    def test_existing_custom_configuration_and_data_are_unchanged(self):
        files = {
            'compose.yaml': 'name: custom-project\nservices: {}\n',
            'compose.autoupdate.yaml': 'services: {}\n# custom port and volumes\n',
            'docker/chrome-seccomp.json': '{"fixture": true}\n',
            '.env': 'DUO_WEB_PORT=18777\n',
            'data/browser-profile/cookie-fixture': 'persistent synthetic cookie',
        }
        for relative, text in files.items():
            path = self.deploy / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        result = self.run_installer(MOCK_CURL_FAILURE='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for relative, text in files.items():
            self.assertEqual((self.deploy / relative).read_text(), text)
        self.assertIn('保留已有自动更新配置', result.stdout)

    def test_zero_byte_files_from_interrupted_old_installer_are_downloaded_again(self):
        (self.deploy / 'compose.yaml').touch()
        (self.deploy / 'docker').mkdir()
        (self.deploy / 'docker/chrome-seccomp.json').touch()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('name:', (self.deploy / 'compose.yaml').read_text())
        self.assertTrue((self.deploy / 'docker/chrome-seccomp.json').stat().st_size)

    def test_failed_download_is_reported_and_partial_target_is_not_installed(self):
        result = self.run_installer(MOCK_CURL_FAILURE='1')
        self.assertEqual(result.returncode, 28, result.stdout + result.stderr)
        self.assertIn('2/6', result.stderr)
        self.assertIn('fixture download timeout', result.stderr)
        self.assertFalse((self.deploy / 'compose.yaml').exists())
        self.assertFalse(list(self.deploy.rglob('*.download.*')))
        self.assertFalse(any('pull' in args or 'up' in args for args in self.docker_calls()))

    def test_invalid_configuration_never_replaces_existing_overlay_or_starts(self):
        existing = self.deploy / 'compose.autoupdate.yaml'
        existing.write_text('existing invalid fixture')
        result = self.run_installer(MOCK_CONFIG_FAILURE='1')
        self.assertEqual(result.returncode, 15, result.stdout + result.stderr)
        self.assertEqual(existing.read_text(), 'existing invalid fixture')
        self.assertFalse(any('pull' in args or 'up' in args for args in self.docker_calls()))

    def test_pull_failure_does_not_restart_application(self):
        result = self.run_installer(MOCK_PULL_FAILURE='1')
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertIn('4/6', result.stderr)
        self.assertFalse(any('up' in args for args in self.docker_calls()))

    def test_start_failure_reports_health_phase(self):
        result = self.run_installer(MOCK_UP_FAILURE='1')
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertIn('5/6', result.stderr)
        self.assertIn('fixture application not healthy', result.stderr)

    def test_slow_registry_prints_waiting_heartbeat(self):
        result = self.run_installer(MOCK_PULL_SLEEP='6')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('仍在执行，已等待', result.stdout)


if __name__ == '__main__':
    unittest.main()
