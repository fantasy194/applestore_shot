import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from inventory import (InventoryClient, PoolOwner, default_config, fulfillment_url, load_config,
                       parse_stores, public_config, run_inventory, stock_for_target, validate_config)
from private_state import read_private, write_private
from activity_log import ActivityLog
from inventory_proxy import ProxyError
from protocol import Cancelled, Channel
from inventory_broker import (BrokerUnavailable, REGISTRATION, SharedClient, StoreBroker,
                              query_running_monitor, _post)
import requests


def target(**changes):
    value = dict(id='cloth-qibao', sku='MLH74FE', quantity=1, store_id='R705', store_name='七宝',
                 location='201101', account_id='', auto_purchase=False, concurrency=1)
    return dict(value, **changes)


def stores(display='available', sku='MLH74FE/A', store='R705'):
    return {'body': {'stores': [{'storeNumber': store, 'storeName': '七宝', 'city': '上海',
                                'partsAvailability': {sku: {'pickupDisplay': display,
                                                           'pickupSearchQuote': {'storePickupQuote': '今天可取货'}}}}]}}


class Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self.body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, _size):
        yield self.body


class Session:
    def __init__(self, response, calls):
        self.response, self.calls = response, calls
        self.headers, self.proxies = {}, {}
        self.cookies = {'should-not-leak': 'cookie'}
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **options):
        self.calls.append((url, options, dict(self.cookies), self.trust_env, dict(self.proxies)))
        return self.response


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_anchor_omits_location_and_sku_is_normalized(self):
        query = parse_qs(urlsplit(fulfillment_url(['mlh74fe'], store_id='R705', location='北京')).query)
        self.assertEqual(query['store'], ['R705'])
        self.assertEqual(query['parts.0'], ['MLH74FE/A'])
        self.assertNotIn('location', query)
        self.assertEqual(parse_qs(urlsplit(fulfillment_url(['MLH74FE/A'], location='201101')).query)['location'], ['201101'])

    def test_exact_store_and_sku_and_machine_availability_required(self):
        available = stock_for_target(target(), parse_stores(stores()), 'at', 100)
        self.assertEqual(available['status'], 'available')
        self.assertEqual(available['checked_at_ms'], 100)
        for body in (stores(store='R000'), stores(sku='OTHERCH/A'), stores(display='今天有货'), stores(display=None)):
            self.assertEqual(stock_for_target(target(), parse_stores(body))['status'], 'unknown')
        for display in ('unavailable', 'ineligible'):
            self.assertEqual(stock_for_target(target(), parse_stores(stores(display)))['status'], 'unavailable')

    def test_tomorrow_pickup_is_available_without_today_text(self):
        body = stores()
        part = body['body']['stores'][0]['partsAvailability']['MLH74FE/A']
        part['pickupSearchQuote']['storePickupQuote'] = '明天可取货'
        result = stock_for_target(target(), parse_stores(body))
        self.assertEqual(result['status'], 'available')
        part['pickupDisplay'] = None
        self.assertEqual(stock_for_target(target(), parse_stores(body))['status'], 'unknown')

    def test_read_and_save_redact_secrets_and_preserve_blanks(self):
        raw = {'targets': [target()], 'proxy': {'enabled': True, 'providers': [
            {'id': 'dm', 'kind': 'dmdaili', 'extract_url': 'http://api.dmdaili.com/dmgetip.asp?apikey=SECRET&pwd=PRIVATE',
             'password': 'PRIVATE', 'username': 'owner'}]}}
        result = run_inventory('inventory_settings_save', {'config': raw}, None, self.root)
        serialized = json.dumps(result)
        self.assertNotIn('SECRET', serialized)
        self.assertNotIn('PRIVATE', serialized)
        self.assertTrue(result['config']['proxy']['providers'][0]['extract_url_configured'])
        self.assertNotIn(b'SECRET', (self.root / 'inventory-settings.enc').read_bytes())
        public = result['config']
        public['proxy']['providers'][0]['extract_url'] = ''
        run_inventory('inventory_settings_save', {'config': public}, None, self.root)
        self.assertIn('SECRET', load_config(self.root)['proxy']['providers'][0]['extract_url'])
        self.assertEqual(run_inventory('inventory_settings_read', {}, None, self.root)['config'], public_config(load_config(self.root)))

    def test_missing_key_and_cipher_tampering_fail_closed(self):
        path = self.root / 'state.enc'
        write_private(path, {'secret': 'value'})
        self.assertEqual(read_private(path), {'secret': 'value'})
        raw = json.loads(path.read_text())
        raw['ciphertext'] = raw['ciphertext'][:-4] + 'AAAA'
        path.write_text(json.dumps(raw))
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            write_private(path, {'reset': True})
        self.assertEqual(path.read_bytes(), before)
        (self.root / '.private-state.key').unlink()
        with self.assertRaises(ValueError):
            write_private(self.root / 'other.enc', {'reset': True})
        self.assertFalse((self.root / '.private-state.key').exists())

    def test_document_aad_prevents_file_swapping(self):
        write_private(self.root / 'one.enc', {'token': 'secret'})
        (self.root / 'two.enc').write_bytes((self.root / 'one.enc').read_bytes())
        with self.assertRaises(ValueError):
            read_private(self.root / 'two.enc')

    def test_first_private_documents_share_one_key_across_worker_processes(self):
        source = ('import sys,time; from pathlib import Path; from private_state import write_private; '
                  'root=Path(sys.argv[1]); print("ready",flush=True); '
                  '\nwhile not (root/"go").exists(): time.sleep(.01)\n'
                  'write_private(root/(sys.argv[2]+".enc"),{"worker":sys.argv[2]})')
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'worker'))
        processes = [subprocess.Popen([sys.executable, '-c', source, str(self.root), name],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, text=True)
                     for name in ('one', 'two')]
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
            (self.root / 'go').touch()
            for process in processes:
                _stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
            for name in ('one', 'two'):
                self.assertEqual(read_private(self.root / (name + '.enc')), {'worker': name})
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_invalid_config_rejects_unsafe_urls_missing_account_and_duplicates(self):
        for change in ({'auto_purchase': True}, {'concurrency': 0}, {'store_id': 'Fake'}, {'quantity': True}):
            item = target()
            item.update(change)
            with self.assertRaises(ValueError):
                validate_config({'targets': [item]})
        with self.assertRaises(ValueError):
            validate_config({'targets': [target(), target()]})
        with self.assertRaises(ValueError):
            validate_config({'proxy': {'providers': [{'id': 'lan', 'extract_url': 'http://127.0.0.1/private'}]}})

    def test_target_retry_count_defaults_validates_and_preserves_existing_value(self):
        config = validate_config({'targets': [target()]})
        self.assertEqual(config['targets'][0]['retry_count'], 0)
        saved = run_inventory('inventory_settings_save', {'config': {'targets': [target(retry_count=3)]}},
                              None, self.root)['config']
        self.assertEqual(saved['targets'][0]['retry_count'], 3)
        saved['targets'][0].pop('retry_count')
        saved = run_inventory('inventory_settings_save', {'config': saved}, None, self.root)['config']
        self.assertEqual(saved['targets'][0]['retry_count'], 3)
        saved['targets'][0]['retry_count'] = 0
        self.assertEqual(run_inventory('inventory_settings_save', {'config': saved}, None, self.root)
                         ['config']['targets'][0]['retry_count'], 0)
        for value in (-1, 11, True, '3', 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config({'targets': [target(retry_count=value)]})

    def test_concurrency_defaults_to_one_and_preserves_explicit_choice(self):
        missing = target()
        del missing['concurrency']
        for item in (missing, target(concurrency=None)):
            self.assertEqual(validate_config({'targets': [item]})['targets'][0]['concurrency'], 1)
        self.assertEqual(validate_config({'targets': [target(concurrency=3)]})['targets'][0]['concurrency'], 3)

    def test_anonymous_request_never_carries_account_cookies_or_environment_proxy(self):
        calls = []
        client = InventoryClient(default_config(), self.root,
                                 session_factory=lambda: Session(Response(stores()), calls), now=lambda: 100)
        result = client.check([target()])
        self.assertEqual(result['targets'][0]['status'], 'available')
        self.assertEqual(result['targets'][0]['checked_at_ms'], 100000)
        self.assertEqual(calls[0][2:], ({}, False, {}))
        self.assertFalse(calls[0][1]['allow_redirects'])
        self.assertNotIn('location=', calls[0][0])
        rows = ActivityLog(self.root).query()['entries']
        recorded = next(row for row in rows if row['event'] == 'inventory_target_checked')
        self.assertEqual(recorded['details']['sku'], 'MLH74FE')
        self.assertEqual(recorded['details']['store_id'], 'R705')
        self.assertEqual(recorded['details']['status'], 'available')
        self.assertEqual(recorded['details']['http_status'], 200)
        self.assertIsInstance(recorded['details']['duration_ms'], int)
        history = ActivityLog(self.root).query(category='stock')['entries']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['event'], 'inventory_available')
        self.assertEqual(history[0]['details'], {
            'sku': 'MLH74FE', 'store_id': 'R705', 'store_name': '七宝',
            'status': 'available', 'checked_at_ms': 100000})
        client.check([target()])
        restarted = InventoryClient(default_config(), self.root,
                                    session_factory=lambda: Session(Response(stores()), []), now=lambda: 100)
        restarted.check([target()])
        self.assertEqual(len(ActivityLog(self.root).query(category='stock')['entries']), 1)

    def test_blocked_bad_json_and_missing_store_are_unknown(self):
        for response in (Response(b'<html>challenge</html>', 541), Response(b'not json'), Response(stores(store='R000'))):
            client = InventoryClient(default_config(), self.root, session_factory=lambda: Session(response, []))
            result = client.check([target()])['targets'][0]
            self.assertEqual(result['status'], 'unknown')
            self.assertIn('error', result)

    def test_store_discovery_keeps_every_returned_store(self):
        body = stores()
        body['body']['stores'] += [dict(body['body']['stores'][0], storeNumber='R999', storeName='另一家')]
        client = InventoryClient(default_config(), self.root, session_factory=lambda: Session(Response(body), []))
        result = client.stores('MLH74FE', '201101')
        self.assertEqual([s['store_id'] for s in result['stores']], ['R705', 'R999'])
        self.assertEqual(result['scope'], 'nearby')

    def test_store_discovery_timeout_remains_distinct_and_safe(self):
        session = Mock(wraps=Session(Response(stores()), []))
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.get.side_effect = requests.Timeout('https://supplier.invalid/?token=private-secret')
        client = InventoryClient(default_config(), self.root, session_factory=lambda: session)
        with self.assertRaisesRegex(ValueError, '超时') as caught:
            client.stores('MLH74FE', '201101')
        self.assertNotIn('private-secret', str(caught.exception))

    def test_cold_proxy_pool_waits_for_all_concurrent_store_reads(self):
        client = InventoryClient(default_config(), self.root)
        client.pool = Mock()
        client.pool.status.side_effect = [
            {'dispatchable_count': count} for count in (1, 2, 3)]
        with patch('inventory.time.sleep') as pause:
            client.warm(minimum=3)
        self.assertEqual(client.pool.status.call_count, 3)
        self.assertEqual(pause.call_count, 2)
        client.pool.touch.assert_called_once()

    def test_empty_inventory_check_does_not_extract_paid_proxies(self):
        config = {'targets': [], 'proxy': {'enabled': True, 'providers': [{
            'id': 'dm', 'kind': 'dmdaili', 'extract_url': 'http://api.dmdaili.com/dmgetip.asp?apikey=fixture'}]}}
        run_inventory('inventory_settings_save', {'config': config}, None, self.root)
        pool = Mock()
        with patch('inventory.DynamicProxyPool', return_value=pool):
            self.assertEqual(run_inventory('inventory_check', {}, None, self.root)['targets'], [])
        pool.touch.assert_not_called()

    def test_single_check_warms_exact_query_count_not_target_count(self):
        config = {'targets': [target(), target(id='second-sku', sku='MK2P4CH'),
                              target(id='second-store', store_id='R643')]}
        run_inventory('inventory_settings_save', {'config': config}, None, self.root)
        with patch.object(InventoryClient, 'warm') as warm, patch.object(InventoryClient, 'check', return_value={'targets': []}):
            run_inventory('inventory_check', {}, None, self.root)
        warm.assert_called_once_with(None, minimum=2)

    def test_same_city_single_anchor_responses_sample_both_stores_every_round(self):
        client = InventoryClient(default_config(), self.root)
        targets = [target(store_id='R643', location='南京'), target(id='cloth-other', store_id='R688', location='南京')]
        called = []

        def fetch(url):
            store = parse_qs(urlsplit(url).query)['store'][0]
            called.append(store)
            display = 'available' if store == 'R643' else 'unavailable'
            return parse_stores(stores(store=store, display=display)), 'at', 100

        with patch.object(client, 'fetch', fetch):
            first = client.check(targets)
            second = client.check(targets)
        self.assertCountEqual(called, ['R643', 'R688', 'R643', 'R688'])
        for result in (first, second):
            self.assertEqual([t['status'] for t in result['targets']], ['available', 'unavailable'])
            self.assertEqual([t['checked_at_ms'] for t in result['targets']], [100, 100])

    def test_multiple_skus_at_same_store_share_one_actual_store_query(self):
        client = InventoryClient(default_config(), self.root)
        targets = [target(), target(id='other-sku', sku='MK2M4CH')]
        called = []

        def fetch(url):
            called.append(parse_qs(urlsplit(url).query))
            body = stores()
            body['body']['stores'][0]['partsAvailability']['MK2M4CH/A'] = {'pickupDisplay': 'unavailable'}
            return parse_stores(body), 'at', 100

        with patch.object(client, 'fetch', fetch):
            result = client.check(targets)
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0]['store'], ['R705'])
        self.assertEqual(called[0]['parts.0'], ['MLH74FE/A'])
        self.assertEqual(called[0]['parts.1'], ['MK2M4CH/A'])
        self.assertEqual([t['status'] for t in result['targets']], ['available', 'unavailable'])

    def test_proxy_round_capacity_uses_pool_size_up_to_ten_and_rotates_deferred(self):
        class FakePool:
            def touch(self):
                pass

            def status(self):
                return {'enabled': True}

        for pool_size, expected in ((3, 3), (7, 7), (20, 10)):
            with self.subTest(pool_size=pool_size):
                config = default_config()
                config['proxy']['pool_size'] = pool_size
                client = InventoryClient(config, self.root)
                client.pool = FakePool()
                targets = [target(id=f'store-{i}', store_id=f'R{700 + i}') for i in range(20)]
                called = []

                def fetch(url):
                    store = parse_qs(urlsplit(url).query)['store'][0]
                    called.append(store)
                    return parse_stores(stores(store=store)), 'at', 100

                with patch.object(client, 'fetch', fetch):
                    first = client.check(targets)
                    second = client.check(targets)
                self.assertEqual(len(called), expected * 2)
                self.assertEqual(len(set(called)), expected * 2)
                self.assertEqual(sum(t['status'] == 'available' for t in first['targets']), expected)
                self.assertEqual(sum(t['status'] == 'available' for t in second['targets']), expected)

    def test_many_distant_stores_have_bounded_round_and_deferred_rotation(self):
        client = InventoryClient(default_config(), self.root)
        targets = [target(id=f'store-{i}', store_id=f'R{700 + i}', location='') for i in range(10)]
        called = []

        def fetch(url):
            store = parse_qs(urlsplit(url).query)['store'][0]
            called.append(store)
            return parse_stores(stores(store=store)), 'at', 100

        with patch.object(client, 'fetch', fetch):
            first = client.check(targets)
            second = client.check(targets)
        self.assertEqual(len(called), 8)
        self.assertEqual(len(set(called)), 8)
        self.assertEqual(sum(t['status'] == 'available' for t in first['targets']), 4)
        self.assertEqual(sum(t['status'] == 'available' for t in second['targets']), 4)

    def test_monitor_emits_nonoverlapping_rounds_and_obeys_cancel(self):
        run_inventory('inventory_settings_save', {'config': {'targets': [target()]}}, None, self.root)
        output = io.StringIO()
        channel = Channel('account', 'task', output)
        count, active_count = [], []

        def check(client, targets):
            active_count.append(threading.active_count())
            count.append(time.monotonic())
            if len(count) == 2:
                channel.cancelled.set()
            return {'targets': [dict(targets[0], status='available', checked_at_ms=int(time.time() * 1000))]}

        with patch.object(InventoryClient, 'check', check), self.assertRaises(Cancelled):
            run_inventory('inventory_monitor', {}, channel, self.root)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        inventory_events = [event for event in events if event['type'] == 'inventory']
        self.assertEqual(len(inventory_events), 1)
        self.assertTrue(inventory_events[0]['result']['monitoring'])
        self.assertEqual(sum(event['type'] == 'log' for event in events), 1)
        self.assertGreaterEqual(count[1] - count[0], .95)

    def test_pool_owner_is_exclusive_but_settings_remain_readable(self):
        with PoolOwner(self.root / 'owner.lock'):
            with self.assertRaises(ValueError):
                with PoolOwner(self.root / 'owner.lock'):
                    pass
            self.assertEqual(run_inventory('inventory_settings_read', {}, None, self.root)['config'], default_config())

    def test_monitor_persists_every_target_round_but_emits_only_thirty_second_summary(self):
        run_inventory('inventory_settings_save', {'config': {'targets': [target()]}}, None, self.root)
        channel = Channel('account', 'task', io.StringIO())
        clock = [0.0]
        calls = [0]

        def fetch(_client, _url):
            calls[0] += 1
            if calls[0] == 35:
                channel.cancelled.set()
            return parse_stores(stores()), 'at', 100

        def sleep(delay):
            clock[0] += delay

        with patch.object(InventoryClient, 'fetch', fetch), patch('inventory.time.monotonic', lambda: clock[0]), \
                patch('inventory.time.sleep', sleep), self.assertRaises(Cancelled):
            run_inventory('inventory_monitor', {}, channel, self.root)
        events = [json.loads(line) for line in channel.output.getvalue().splitlines()]
        self.assertEqual(sum(e['type'] == 'log' for e in events), 2)
        rows = ActivityLog(self.root).query(limit=200)['entries']
        checked = [row for row in rows if row['event'] == 'inventory_target_checked']
        self.assertEqual(len(checked), 35)
        self.assertEqual({row['details']['round'] for row in checked}, set(range(1, 36)))


class InventoryBrokerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = self.root / 'settings'
        self.settings.mkdir()
        self.monitor = Channel('monitor-account', 'monitor-task', io.StringIO())
        self.instances = []
        self.active_pools = 0
        self.max_active_pools = 0
        self.discovery_started = threading.Event()
        self.discovery_release = None
        self.errors = []
        self.monitor_thread = None
        self.patch_client = None

    def tearDown(self):
        self.monitor.cancelled.set()
        if self.discovery_release:
            self.discovery_release.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=8)
            self.assertFalse(self.monitor_thread.is_alive(), 'monitor fixture did not stop')
        if self.patch_client:
            self.patch_client.stop()

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail('inventory broker fixture condition timed out')

    def events(self):
        return [json.loads(line) for line in self.monitor.output.getvalue().splitlines()]

    def start_monitor(self):
        owner = self

        class FixturePool:
            def __init__(self, config, _path):
                self.pool_size = config['pool_size']
                self.active = 0
                self.closed = False
                self.lock = threading.Lock()
                self.worker = None
                self.paid_calls = 0
                owner.instances.append(self)
                owner.active_pools += 1
                owner.max_active_pools = max(owner.max_active_pools, owner.active_pools)

            def touch(self):
                self.assert_open()

            def assert_open(self):
                if self.closed:
                    raise AssertionError('closed inventory pool was used')

            def acquire(self):
                with self.lock:
                    self.assert_open()
                    if self.active >= self.pool_size:
                        raise ValueError('fixture pool capacity exhausted')
                    self.active += 1
                    return type('Lease', (), {'url': 'http://8.8.8.8:8080'})()

            def release(self, _lease, failed=False):
                with self.lock:
                    self.active -= 1

            def status(self):
                return {'enabled': True, 'pool_size': self.pool_size, 'active_count': self.pool_size,
                        'dispatchable_count': self.pool_size - self.active}

            def close(self):
                if not self.closed:
                    if self.active:
                        raise AssertionError('inventory pool closed during in-flight discovery')
                    self.closed = True
                    owner.active_pools -= 1

        class FixtureSession(Session):
            def __init__(self):
                super().__init__(None, [])

            def get(self, url, **options):
                self.assert_anonymous()
                query = parse_qs(urlsplit(url).query)
                if 'location' in query:
                    owner.discovery_started.set()
                    if owner.discovery_release:
                        if not owner.discovery_release.wait(timeout=6):
                            raise ValueError('fixture discovery was not released')
                    else:
                        time.sleep(1.25)
                    body = stores()
                    body['body']['stores'].append(dict(body['body']['stores'][0], storeNumber='R999', storeName='另一家'))
                else:
                    body = stores(store=query['store'][0])
                return Response(body)

            def assert_anonymous(self):
                if self.cookies or self.trust_env or self.proxies.get('https') != 'http://8.8.8.8:8080':
                    raise AssertionError('inventory discovery lost its anonymous pool transport')

        original = InventoryClient
        self.patch_client = patch('inventory.InventoryClient', side_effect=lambda config, root: original(
            config, root, session_factory=FixtureSession, pool_factory=FixturePool))
        self.patch_client.start()
        run_inventory('inventory_settings_save', {'config': {'targets': [target()], 'proxy': {
            'enabled': True, 'pool_size': 2, 'providers': [{'id': 'fixture', 'kind': 'generic',
                'extract_url': 'https://provider.example.test/api?token=private-fixture'}]}}}, None, self.settings)

        def monitor():
            try:
                run_inventory('inventory_monitor', {}, self.monitor, self.settings)
            except Cancelled:
                pass
            except BaseException as error:
                self.errors.append(error)

        self.monitor_thread = threading.Thread(target=monitor, daemon=True)
        self.monitor_thread.start()
        self.wait(lambda: (self.settings / REGISTRATION).exists() and any(e['type'] == 'inventory' for e in self.events()))

    def test_another_worker_discovers_stores_through_real_http_while_monitor_keeps_running_one_pool(self):
        self.start_monitor()
        project = Path(__file__).resolve().parents[2]
        source = '''import sys,requests
original=requests.Session
class OnlyLoopback(original):
    def request(self,method,url,*args,**kwargs):
        assert url.startswith('http://127.0.0.1:'), 'fixture denied an external request'
        assert self.trust_env is False, 'loopback request used environment proxies'
        return super().request(method,url,*args,**kwargs)
requests.Session=OnlyLoopback
from main import main
sys.argv=['worker',*sys.argv[1:]]
main()
'''
        account_id = '00000000-0000-0000-0000-000000000000'
        task_id = '11111111-1111-4111-8111-111111111111'
        env = dict(os.environ, PYTHONPATH=str(project / 'worker'), PYTHONUTF8='1',
                   HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1', ALL_PROXY='http://127.0.0.1:1', NO_PROXY='')
        worker = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', source, '--data-dir', str(self.root),
                                    '--account-id', account_id, '--task-id', task_id],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', env=env)
        try:
            worker.stdin.write(json.dumps({'account_id': account_id, 'task_id': task_id, 'account': '',
                                           'action': 'store_search', 'params': {'sku': 'MLH74FE', 'location': '201101'}}) + '\n')
            worker.stdin.flush()
            result = json.loads(worker.stdout.readline())
            self.assertEqual(result['type'], 'result', result)
            self.assertEqual([row['store_id'] for row in result['result']['stores']], ['R705', 'R999'])
            _stdout, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, stderr)
            self.assertTrue(self.monitor_thread.is_alive())
            self.assertFalse(self.monitor.cancelled.is_set())
            self.assertGreaterEqual(sum(e['type'] == 'inventory' for e in self.events()), 2)
            self.assertEqual(len(self.instances), 1)
            self.assertEqual(self.max_active_pools, 1)
            self.assertEqual(self.instances[0].paid_calls, 0)
            with self.assertRaises(ValueError):
                with PoolOwner(self.settings / '.inventory-pool.lock'):
                    pass
            self.assertEqual(self.errors, [])
            registration = read_private(self.settings / REGISTRATION)
            self.assertNotIn(registration['token'].encode(), (self.settings / REGISTRATION).read_bytes())
            self.assertNotIn('private-fixture', json.dumps(result))
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()

    def test_proxy_switch_waits_for_old_query_and_closes_old_pool_before_replacement(self):
        self.discovery_release = threading.Event()
        self.start_monitor()
        result = []
        search = threading.Thread(target=lambda: result.append(query_running_monitor(
            self.settings, {'sku': 'MLH74FE', 'location': '201101'}, Channel('search', 'search', io.StringIO()))))
        search.start()
        self.assertTrue(self.discovery_started.wait(timeout=3))
        config = load_config(self.settings)
        config['proxy']['pool_size'] = 3
        run_inventory('inventory_settings_save', {'config': config}, None, self.settings)
        time.sleep(1.2)
        self.assertEqual(len(self.instances), 1)
        self.assertFalse(self.instances[0].closed)
        self.discovery_release.set()
        search.join(timeout=5)
        self.assertFalse(search.is_alive())
        self.assertEqual(len(result[0]['stores']), 2)
        self.wait(lambda: len(self.instances) == 2)
        self.assertTrue(self.instances[0].closed)
        self.assertEqual(self.instances[1].pool_size, 3)
        self.assertEqual(self.max_active_pools, 1)
        self.assertEqual(self.errors, [])

    def test_http_auth_host_origin_and_request_limits_reject_before_query(self):
        client = Mock(pool=None)
        shared = SharedClient(client)
        broker = StoreBroker(self.settings, shared, self.monitor).start()
        registration = read_private(self.settings / REGISTRATION)
        url = 'http://127.0.0.1:' + str(registration['port']) + '/stores'
        body = {'request_id': 'a' * 32, 'sku': 'MLH74FE', 'location': '201101'}
        try:
            with requests.Session() as session:
                session.trust_env = False
                base = {'Authorization': 'Bearer ' + registration['token']}
                for headers, payload, expected in (
                        ({'Authorization': 'Bearer incorrect'}, body, 403),
                        ({**base, 'Host': 'foreign.invalid'}, body, 403),
                        ({**base, 'Origin': 'https://foreign.invalid'}, body, 403),
                        (base, {**body, 'sku': 'https://foreign.invalid'}, 400),
                        (base, {**body, 'location': 'x' * 121}, 400),
                        (base, {**body, 'extra': 'x' * 9000}, 400)):
                    with self.subTest(headers=list(headers), size=len(json.dumps(payload))):
                        with session.post(url, json=payload, headers=headers, timeout=2) as response:
                            self.assertEqual(response.status_code, expected)
            client.stores.assert_not_called()
            client.warm.assert_not_called()
            self.assertEqual(shared.active, 0)
        finally:
            broker.close()
            shared.close()
        self.assertFalse((self.settings / REGISTRATION).exists())

    def test_broker_close_joins_terminal_log_after_http_reply_and_client_release(self):
        client = Mock(pool=None)
        client.stores.return_value = {'success': True, 'stores': []}
        shared = SharedClient(client)
        broker = StoreBroker(self.settings, shared, self.monitor).start()
        registration = read_private(self.settings / REGISTRATION)
        writing, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = ActivityLog.record
        def record(activity, event, *args, **kwargs):
            if event == 'inventory_shared_query_end':
                writing.set()
                if not release.wait(timeout=5):
                    raise AssertionError('fixture terminal log was not released')
            return original(activity, event, *args, **kwargs)
        def close():
            broker.close()
            closed.set()
        closer = threading.Thread(target=close)
        try:
            with patch('inventory_broker.ActivityLog.record', new=record):
                status, _ = _post(registration, '/stores', {
                    'request_id': 'd' * 32, 'sku': 'MLH74FE', 'location': '201101'}, timeout=(1, 2))
                self.assertEqual(status, 200)
                self.assertTrue(writing.wait(timeout=2))
                self.assertEqual(shared.active, 0)
                closer.start()
                self.assertFalse(closed.wait(timeout=.15), 'close returned before terminal log completed')
                release.set()
                closer.join(timeout=3)
                self.assertFalse(closer.is_alive())
                self.assertTrue(closed.is_set())
            self.assertEqual(broker.pending, {})
            rows = ActivityLog(self.root).query()['entries']
            self.assertIn('inventory_shared_query_end', [row['event'] for row in rows])
            # No database handle may remain live when the owner releases files.
            (self.root / 'logs' / 'activity.sqlite').unlink()
        finally:
            release.set()
            if closer.ident is not None:
                closer.join(timeout=5)
            broker.close()
            shared.close()

    def test_shared_query_errors_preserve_safe_categories_and_query_task_logs(self):
        client = Mock(pool=None)
        shared = SharedClient(client)
        broker = StoreBroker(self.settings, shared, self.monitor).start()
        secret = 'https://supplier.invalid/extract?token=private-supplier-secret'
        cases = (
            (ProxyError('proxy_pool_empty'), 'proxy_pool_empty'),
            (ProxyError('provider_expiry'), 'provider_expiry'),
            (requests.Timeout(secret), 'inventory_timeout'),
            (requests.ConnectionError(secret), 'inventory_network'),
            (ValueError('HTTP 541 ' + secret), 'inventory_challenge'),
            (ValueError('HTTP 500 ' + secret), 'inventory_http'),
            (ValueError('JSON ' + secret), 'inventory_response'),
            (RuntimeError(secret), 'inventory_query'),
        )
        channel = Channel('query-account', 'query-task', io.StringIO())
        try:
            for error, code in cases:
                with self.subTest(code=code):
                    client.stores.side_effect = error
                    with self.assertRaises(BrokerUnavailable) as caught:
                        query_running_monitor(self.settings, {'sku': 'MLH74FE', 'location': '201101'}, channel)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn('private-supplier-secret', str(caught.exception))
                    self.assertNotIn('supplier.invalid', str(caught.exception))
            rows = ActivityLog(self.root).query(task_id='query-task')['entries']
            errors = [row for row in rows if row['event'] == 'inventory_shared_query_error']
            self.assertEqual(len(errors), len(cases))
            self.assertEqual({row['details']['error_code'] for row in errors}, {code for _, code in cases})
            self.assertTrue(all(row['account_id'] == 'query-account' for row in errors))
            self.assertNotIn('private-supplier-secret', json.dumps(rows))
            self.assertNotIn('supplier.invalid', json.dumps(rows))
            self.assertFalse(self.monitor.cancelled.is_set())
        finally:
            broker.close()
            shared.close()

    def test_store_search_without_monitor_uses_original_exclusive_client_path(self):
        client = Mock(pool=None)
        client.stores.return_value = {'success': True, 'stores': [], 'total': 0}
        with patch('inventory.InventoryClient', return_value=client) as factory:
            result = run_inventory('store_search', {'sku': 'MLH74FE', 'location': '201101'}, None, self.settings)
        self.assertEqual(result['total'], 0)
        factory.assert_called_once()
        client.stores.assert_called_once_with('MLH74FE', '201101')
        client.close.assert_called_once()
        self.assertFalse((self.settings / REGISTRATION).exists())
        with PoolOwner(self.settings / '.inventory-pool.lock'):
            pass

    def test_broker_caps_concurrent_discovery_queries_without_extra_pool_or_queued_work(self):
        release = threading.Event()
        client = Mock(pool=None)
        client.warm.side_effect = lambda *_args, **_kwargs: release.wait(timeout=3)
        client.stores.return_value = {'success': True, 'stores': []}
        shared = SharedClient(client)
        broker = StoreBroker(self.settings, shared, self.monitor).start()
        registered = read_private(self.settings / REGISTRATION)
        results = []
        threads = [threading.Thread(target=lambda identity=identity: results.append(_post(
            registered, '/stores', {'request_id': identity * 32, 'sku': 'MLH74FE', 'location': '201101'}, timeout=(1, 4))))
                   for identity in ('a', 'b')]
        try:
            for thread in threads:
                thread.start()
            self.wait(lambda: shared.active == 2)
            status, _ = _post(registered, '/stores', {'request_id': 'c' * 32, 'sku': 'MLH74FE', 'location': '201101'}, timeout=(1, 2))
            self.assertEqual(status, 503)
            self.assertEqual(client.warm.call_count, 2)
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual([status for status, _ in results], [200, 200])
            self.assertEqual(client.stores.call_count, 2)
        finally:
            release.set()
            broker.close()
            shared.close()
            for thread in threads:
                thread.join(timeout=2)

    def test_query_cancel_is_cooperative_and_does_not_cancel_monitor(self):
        waiting = threading.Event()
        client = Mock(pool=None)

        def warm(channel, **_kwargs):
            waiting.set()
            while True:
                channel.checkpoint()
                time.sleep(.02)

        client.warm.side_effect = warm
        shared = SharedClient(client)
        broker = StoreBroker(self.settings, shared, self.monitor).start()
        channel = Channel('search', 'search', io.StringIO())
        caught = []

        def search():
            try:
                query_running_monitor(self.settings, {'sku': 'MLH74FE', 'location': '201101'}, channel)
            except BaseException as error:
                caught.append(error)

        thread = threading.Thread(target=search)
        thread.start()
        try:
            self.assertTrue(waiting.wait(timeout=3))
            channel.cancelled.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.wait(lambda: shared.active == 0)
            self.assertIsInstance(caught[0], Cancelled)
            self.assertFalse(self.monitor.cancelled.is_set())
            client.stores.assert_not_called()
        finally:
            broker.close()
            shared.close()
            thread.join(timeout=2)

    def test_cancel_arriving_before_store_request_cannot_start_late_query(self):
        client = Mock(pool=None)
        shared = SharedClient(client)
        broker = StoreBroker(self.settings, shared, self.monitor).start()
        registered = read_private(self.settings / REGISTRATION)
        identity = 'b' * 32
        try:
            status, _ = _post(registered, '/cancel', {'request_id': identity}, timeout=(1, 2))
            self.assertEqual(status, 200)
            status, _ = _post(registered, '/stores', {'request_id': identity, 'sku': 'MLH74FE', 'location': '201101'}, timeout=(1, 2))
            self.assertEqual(status, 409)
            client.warm.assert_not_called()
            client.stores.assert_not_called()
        finally:
            broker.close()
            shared.close()

    def test_stale_or_not_ready_registration_is_bounded_and_never_creates_another_pool(self):
        started = time.monotonic()
        with self.assertRaises(BrokerUnavailable):
            query_running_monitor(self.settings, {'sku': 'MLH74FE', 'location': '201101'}, None, readiness_timeout=.15)
        self.assertLess(time.monotonic() - started, .8)
        write_private(self.settings / REGISTRATION, {'version': 1, 'port': 1, 'token': 'a' * 64, 'pid': 0})
        with patch('inventory.InventoryClient') as factory, self.assertRaises(BrokerUnavailable):
            with PoolOwner(self.settings / '.inventory-pool.lock'):
                run_inventory('store_search', {'sku': 'MLH74FE', 'location': '201101'}, None, self.settings)
        factory.assert_not_called()

    def test_monitor_stop_waits_for_inflight_query_before_client_close_and_removes_registration(self):
        self.discovery_release = threading.Event()
        self.start_monitor()
        caught = []

        def search():
            try:
                query_running_monitor(self.settings, {'sku': 'MLH74FE', 'location': '201101'}, None)
            except BrokerUnavailable as error:
                caught.append(error)

        thread = threading.Thread(target=search)
        thread.start()
        self.assertTrue(self.discovery_started.wait(timeout=3))
        self.monitor.cancelled.set()
        time.sleep(.25)
        self.assertFalse(self.instances[0].closed)
        self.discovery_release.set()
        thread.join(timeout=5)
        self.monitor_thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.monitor_thread.is_alive())
        self.assertTrue(self.instances[0].closed)
        self.assertFalse((self.settings / REGISTRATION).exists())
        self.assertEqual(self.errors, [])
        self.assertTrue(caught)
        with PoolOwner(self.settings / '.inventory-pool.lock'):
            pass


if __name__ == '__main__':
    unittest.main()
