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
                        HOME=str(self.root / 'fixture-home'),
                        MOCK_ROOT=str(ROOT), MOCK_CALLS=str(self.calls))
        self.env.pop('DOCKER_CONFIG', None)
        self.env.pop('DUO_REGISTRY_CONFIG_DIR', None)
        self.executable('docker', '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
effective = {}
env_file = Path('.env')
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            effective[key] = value
effective.update(os.environ)
registry = (effective.get('DUO_REGISTRY_CONFIG_DIR') or effective.get('DOCKER_CONFIG')
            or str(Path(effective['HOME']) / '.docker'))
Path(os.environ['MOCK_CALLS'] + '.registry').write_text(registry)
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
if 'config' in args and '--environment' in args:
    for key in ['DUO_REGISTRY_CONFIG_DIR', 'DOCKER_CONFIG', 'HOME']:
        print(key + '=' + effective.get(key, ''))
    print('UNRELATED_SECRET=synthetic-environment-secret')
if 'up' in args:
    assert Path(registry).is_dir(), 'registry directory was not prepared'
if 'run' in args and '/vault-init.py' in args:
    import subprocess
    mounts = [args[i + 1] for i, item in enumerate(args) if item == '--volume']
    script = next(item.rsplit(':', 2)[0] for item in mounts if item.endswith(':/vault-init.py:ro'))
    result = subprocess.run([sys.executable, script,
                             '--key-file', str(Path.cwd() / 'secrets/duo-vault.key'),
                             '--vault-dir', str(Path.cwd() / 'fixture-volume/.credentials'),
                             '--uid', str(os.getuid()), '--gid', str(os.getgid())])
    sys.exit(result.returncode)
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
relative = url.split('/main/', 1)[1]
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
        self.assertEqual((self.deploy / 'secrets/duo-vault.key').stat().st_size, 32)
        self.assertTrue((self.deploy / 'compose.vault.yaml').is_file())
        start_args = calls[start]
        self.assertIn('compose.vault.yaml', start_args)

    def test_vault_key_survives_reinstall_and_is_only_mounted_to_app(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        key = self.deploy / 'secrets/duo-vault.key'
        original = key.read_bytes()
        self.assertEqual(key.stat().st_mode & 0o777, 0o400)
        self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(key.read_bytes(), original)
        self.assertIn('Existing vault key retained.', result.stdout)
        overlay = (self.deploy / 'compose.vault.yaml').read_text()
        self.assertNotIn('browser:', overlay)
        self.assertIn('/run/secrets/duo_vault_key', overlay)

    def test_missing_vault_key_with_ciphertext_never_regenerates_or_starts(self):
        vault = self.deploy / 'fixture-volume/.credentials'
        vault.mkdir(parents=True)
        ciphertext = vault / 'synthetic.enc'
        ciphertext.write_bytes(b'synthetic encrypted fixture')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('restore the original key', result.stderr)
        self.assertFalse((self.deploy / 'secrets/duo-vault.key').exists())
        self.assertEqual(ciphertext.read_bytes(), b'synthetic encrypted fixture')
        self.assertFalse(any('up' in args for args in self.docker_calls()))

    def test_explicit_vault_opt_out_does_not_create_key_or_overlay(self):
        result = self.run_installer(DUO_ENABLE_VAULT='0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.deploy / 'secrets/duo-vault.key').exists())
        self.assertFalse((self.deploy / 'compose.vault.yaml').exists())

    def test_existing_vault_key_with_unsafe_permissions_is_not_replaced(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        key = self.deploy / 'secrets/duo-vault.key'
        original = key.read_bytes()
        key.chmod(0o644)
        self.calls.write_text('')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('repair permissions explicitly', result.stderr)
        self.assertEqual(key.read_bytes(), original)
        self.assertNotIn(original.hex(), result.stdout + result.stderr)
        self.assertFalse(any('up' in args for args in self.docker_calls()))

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
        self.assertIn('保留已有自定义配置', result.stdout)

    def test_registry_directory_is_optional_empty_and_created_for_anonymous_pulls(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        directory = self.root / 'fixture-home' / '.docker'
        self.assertTrue(directory.is_dir())
        self.assertFalse((directory / 'config.json').exists())
        self.assertEqual(Path(str(self.calls) + '.registry').read_text(), str(directory))
        overlay = (self.deploy / 'compose.autoupdate.yaml').read_text()
        self.assertIn('DOCKER_CONFIG: /docker-config', overlay)
        self.assertIn('read_only: true', overlay)
        self.assertIn('create_host_path: false', overlay)
        self.assertIn('WATCHTOWER_LOG_LEVEL: ${WATCHTOWER_LOG_LEVEL:-info}', overlay)

    def test_registry_directory_override_preserves_credentials_and_env_without_logging_them(self):
        directory = self.root / 'registry directory with spaces'
        directory.mkdir()
        credentials = '{"auths":{"ghcr.io":{"auth":"synthetic-secret-marker"}}}\n'
        (directory / 'config.json').write_text(credentials)
        env_file = self.deploy / '.env'
        env_file.write_text('DUO_WEB_PORT=18777\n')
        result = self.run_installer(DUO_REGISTRY_CONFIG_DIR=str(directory),
                                    DOCKER_CONFIG=str(self.root / 'unused-config'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(str(self.calls) + '.registry').read_text(), str(directory))
        self.assertEqual((directory / 'config.json').read_text(), credentials)
        self.assertEqual(env_file.read_text(), 'DUO_WEB_PORT=18777\n')
        self.assertNotIn('synthetic-secret-marker', result.stdout + result.stderr)
        self.assertFalse((self.root / 'unused-config').exists())

    def test_standard_docker_config_directory_is_used_when_no_override_exists(self):
        directory = self.root / 'docker caller config'
        result = self.run_installer(DOCKER_CONFIG=str(directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(str(self.calls) + '.registry').read_text(), str(directory))
        self.assertTrue(directory.is_dir())

    def test_env_file_registry_path_is_prepared_without_exposing_environment(self):
        directory = self.root / 'env registry with spaces'
        env_file = self.deploy / '.env'
        contents = f'DUO_REGISTRY_CONFIG_DIR={directory}\nDUO_WEB_PORT=18777\n'
        env_file.write_text(contents)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(directory.is_dir())
        self.assertEqual(env_file.read_text(), contents)
        self.assertNotIn('synthetic-environment-secret', result.stdout + result.stderr)

    def test_env_file_relative_registry_path_uses_deployment_directory(self):
        (self.deploy / '.env').write_text('DUO_REGISTRY_CONFIG_DIR=registry config\n')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.deploy / 'registry config').is_dir())

    def test_unmanaged_previous_published_official_files_are_safely_migrated(self):
        for name in ['compose.yaml', 'compose.autoupdate.yaml']:
            fixture = ROOT / 'tests/docker/fixtures' / name.replace('.yaml', '.013.yaml')
            (self.deploy / name).write_bytes(fixture.read_bytes())
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ['compose.yaml', 'compose.autoupdate.yaml']:
            self.assertEqual((self.deploy / name).read_bytes(), (ROOT / name).read_bytes())
            self.assertEqual(len(list(self.deploy.glob(name + '.backup.*'))), 1)
            self.assertTrue((self.deploy / (name + '.duo-sha256')).exists())

    def seed_legacy_official_configuration(self):
        for name in ['compose.yaml', 'compose.autoupdate.yaml']:
            fixture = ROOT / 'tests/docker/fixtures' / name.replace('.yaml', '.legacy.yaml')
            (self.deploy / name).write_bytes(fixture.read_bytes())
        (self.deploy / '.env').write_text('DUO_WEB_PORT=18777\n')
        data = self.deploy / 'data/browser-profile/cookie-fixture'
        data.parent.mkdir(parents=True)
        data.write_text('persistent synthetic cookie')

    def test_previous_official_install_migrates_both_files_and_keeps_backups_and_data(self):
        self.seed_legacy_official_configuration()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ['compose.yaml', 'compose.autoupdate.yaml']:
            self.assertEqual((self.deploy / name).read_bytes(), (ROOT / name).read_bytes())
            self.assertTrue((self.deploy / (name + '.duo-sha256')).exists())
            backups = list(self.deploy.glob(name + '.backup.*'))
            self.assertEqual(len(backups), 1)
            fixture = ROOT / 'tests/docker/fixtures' / name.replace('.yaml', '.legacy.yaml')
            self.assertEqual(backups[0].read_bytes(), fixture.read_bytes())
        overlay = (self.deploy / 'compose.autoupdate.yaml').read_text()
        self.assertIn('${BROWSER_IMAGE:-${APP_IMAGE:-', overlay)
        self.assertIn('  browser:', overlay)
        self.assertEqual(overlay.count('watchtower.enable: "true"'), 2)
        self.assertEqual((self.deploy / '.env').read_text(), 'DUO_WEB_PORT=18777\n')
        self.assertEqual((self.deploy / 'data/browser-profile/cookie-fixture').read_text(),
                         'persistent synthetic cookie')
        self.assertFalse(list(self.deploy.rglob('*.download.*')))

    def test_failed_migration_validation_keeps_both_existing_files(self):
        self.seed_legacy_official_configuration()
        before = {name: (self.deploy / name).read_bytes()
                  for name in ['compose.yaml', 'compose.autoupdate.yaml']}
        result = self.run_installer(MOCK_CONFIG_FAILURE='1')
        self.assertEqual(result.returncode, 15, result.stdout + result.stderr)
        for name, original in before.items():
            self.assertEqual((self.deploy / name).read_bytes(), original)
            self.assertFalse(list(self.deploy.glob(name + '.backup.*')))
        self.assertFalse(list(self.deploy.rglob('*.download.*')))
        self.assertFalse(any('pull' in args or 'up' in args for args in self.docker_calls()))

    def test_later_install_updates_managed_files_but_preserves_local_edits(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        original = (self.deploy / 'compose.yaml').read_bytes()
        edited = original + b'\n# local deployment edit\n'
        (self.deploy / 'compose.yaml').write_bytes(edited)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.deploy / 'compose.yaml').read_bytes(), edited)
        self.assertIn('保留已有自定义配置：compose.yaml', result.stdout)
        self.assertFalse(list(self.deploy.glob('compose.yaml.backup.*')))

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
