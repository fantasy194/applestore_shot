from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from inventory_proxy import DynamicProxyPool, Lease, Provider, ProxyError, expires_at, public_url
from private_state import read_private, write_private
from activity_log import ActivityLog


class FakeProvider:
    def __init__(self, config):
        self.id = config['id']
        self.fingerprint = 'fixture-fingerprint'
        self.calls = []
        self.counter = 0

    def extract(self, count):
        self.calls.append((time.monotonic(), count))
        result = []
        for _ in range(count):
            self.counter += 1
            result.append(Lease(f'http://8.8.8.{self.counter}:8080', time.time() + 120, self.id))
        return result


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'pool.enc'
        self.pools = []

    def tearDown(self):
        for pool in self.pools:
            pool.close()
        self.directory.cleanup()

    def pool(self, **changes):
        config = {'enabled': True, 'pool_size': 10, 'providers': [{'id': 'fixture'}]}
        config.update(changes)
        pool = DynamicProxyPool(config, self.path, provider_factory=FakeProvider)
        self.pools.append(pool)
        return pool

    def wait(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.02)
        self.fail('proxy fixture condition timed out')

    def test_no_paid_calls_until_inventory_demand(self):
        pool = self.pool()
        time.sleep(.15)
        self.assertEqual(pool.providers[0].calls, [])
        self.assertFalse(self.path.exists())

    def test_async_small_batch_refill_is_paced_and_durably_reserved(self):
        import threading

        pool = self.pool()
        first_started, release_first = threading.Event(), threading.Event()
        provider = pool.providers[0]
        extract = provider.extract

        def controlled_extract(count):
            leases = extract(count)
            if len(provider.calls) == 1:
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError('fixture extraction was not released')
            return leases

        provider.extract = controlled_extract
        try:
            with self.assertRaises(ProxyError) as caught:
                pool.acquire()
            self.assertEqual(caught.exception.code, 'proxy_pool_empty')
            self.assertTrue(first_started.wait(timeout=3))
            self.assertEqual(pool.status()['dispatchable_count'], 0)
            release_first.set()
            self.wait(lambda: len(provider.calls) >= 2)
            calls = provider.calls
            self.assertEqual([count for _, count in calls[:2]], [2, 2])
            self.assertGreaterEqual(calls[1][0] - calls[0][0], .98)
            state = read_private(self.path)
            self.assertGreaterEqual(len(state['events']), 2)
            self.assertEqual(state['events'][0]['requested'], 2)
            self.assertNotIn(b'8.8.8.', self.path.read_bytes())
        finally:
            release_first.set()
            pool.close()
            if pool.worker:
                pool.worker.join(timeout=5)
                self.assertFalse(pool.worker.is_alive(), 'fixture refill worker did not finish')

    def test_busy_leases_not_dispatched_and_failed_lease_discarded(self):
        pool = self.pool(pool_size=1)
        pool.touch()
        self.wait(lambda: pool.status()['dispatchable_count'] == 1)
        lease = pool.acquire()
        self.assertEqual(pool.status()['dispatchable_count'], 0)
        with self.assertRaises(ProxyError):
            pool.acquire()
        pool.release(lease, failed=True)
        self.assertEqual(lease.status, 'discarded')
        self.assertEqual(pool.status()['dispatchable_count'], 0)
        self.assertEqual(pool.status()['discarded_count'], 1)
        events = ActivityLog(self.path.parent).query()['entries']
        names = {entry['event'] for entry in events}
        self.assertTrue({'proxy_refill_reserved', 'proxy_refill_complete', 'proxy_lease_discarded'} <= names)
        self.assertNotIn('http://8.8.8.', json.dumps(events))
        pool.close()
        restored = self.pool(pool_size=1)
        self.assertEqual(restored.status()['discarded_count'], 1)
        self.assertEqual(restored.status()['dispatchable_count'], 0)

    def test_vendor_dm_query_and_china_wall_clock_expiry(self):
        queries = []
        now = datetime(2026, 10, 22, 0, tzinfo=timezone.utc).timestamp()

        def query(url):
            queries.append(parse_qs(urlsplit(url).query))
            return {'code': 200, 'data': [{'ip': '8.8.8.8', 'port': '8080', 'endtime': '2026-10-22 08:01:00'}]}

        provider = Provider({'id': 'dm', 'kind': 'dmdaili', 'extract_url': 'http://api.dmdaili.com/dmgetip.asp?apikey=secret&pwd=hidden'}, query, lambda: now)
        lease = provider.extract(2)[0]
        self.assertEqual(lease.expires_at, now + 60)
        self.assertEqual(queries[0]['getnum'], ['2'])
        self.assertEqual(queries[0]['geshi'], ['2'])

    def test_dm_unpadded_midnight_expiry_in_real_shaped_success_payload(self):
        now = datetime(2026, 10, 22, 15, 58, 38, tzinfo=timezone.utc).timestamp()
        payload = {'code': 0, 'success': True, 'data': [
            {'ip': '8.8.8.8', 'port': '8080', 'endtime': '2026/10/23 0:06:38'}]}
        provider = Provider({'id': 'dm', 'kind': 'dmdaili', 'extract_url':
                             'http://api.dmdaili.com/dmgetip.asp?apikey=fixture&pwd=fixture'},
                            lambda _url: payload, lambda: now)
        lease = provider.extract(1)[0]
        self.assertEqual(lease.expires_at, now + 8 * 60)
        self.assertEqual(datetime.fromtimestamp(lease.expires_at, timezone.utc),
                         datetime(2026, 10, 22, 16, 6, 38, tzinfo=timezone.utc))

    def test_unpadded_vendor_month_day_hour_minute_second_are_china_wall_time(self):
        expected = datetime(2026, 1, 1, 19, 4, 5, tzinfo=timezone.utc).timestamp()
        for value in ('2026/1/2 3:4:5', '2026-1-2 3:4:5', '2026/01/02 03:04:05'):
            with self.subTest(value=value):
                self.assertEqual(expires_at(value, expected - 60), expected)

    def test_iso_and_second_millisecond_timestamps_remain_supported(self):
        expected = datetime(2026, 10, 22, 16, 6, 38, tzinfo=timezone.utc).timestamp()
        for value in ('2026-10-22T16:06:38Z', '2026-10-23T00:06:38+08:00', expected, expected * 1000):
            with self.subTest(value=value):
                self.assertEqual(expires_at(value, expected - 60), expected)

    def test_unpadded_vendor_expiry_rejects_bad_calendar_expired_and_far_future(self):
        now = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc).timestamp()
        values = ('2026/2/30 0:6:38', '2026/13/1 0:6:38', '2026/10/0 0:6:38',
                  '2026/10/23 24:6:38', '2026/10/23 0:60:38', '2026/10/23 0:6:60',
                  '2026/10/23 0:6:38 extra', '2026/10-23 00:06:38',
                  '2026/10/22 23:59:59', '2026/10/25 0:6:38')
        for value in values:
            with self.subTest(value=value), self.assertRaises(ProxyError) as caught:
                expires_at(value, now)
            self.assertEqual(caught.exception.code, 'provider_expiry')

    def test_bad_vendor_ip_and_error_payload_are_safe(self):
        config = {'id': 'dm', 'kind': 'dmdaili', 'extract_url': 'http://api.dmdaili.com/dmgetip.asp?apikey=SECRET'}
        for payload in ({'code': 400, 'message': 'SECRET'}, {'code': 200, 'data': [{'ip': '127.0.0.1', 'port': 80}]}):
            provider = Provider(config, lambda _url: payload)
            with self.assertRaises(ProxyError) as caught:
                provider.extract(1)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_xy_whitelist_uses_provider_observed_source_not_guessed_ip(self):
        queries = []

        def query(url):
            params = parse_qs(urlsplit(url).query)
            queries.append(params)
            if params['action'] == ['addwhite']:
                return {'code': 0, 'success': True}
            return {'status': 200, 'data': [{'ip': '8.8.8.8', 'port': 8080,
                                            'expire': time.time() + 120}]}

        provider = Provider({'id': 'xy', 'kind': 'xydaili', 'extract_url':
                             'http://api.xydaili.net:2022/tools/IP.ashx?action=GetIP&OrderNumber=SECRET&ip=1.2.3.4'}, query)
        provider.extract(1)
        self.assertEqual(queries[0]['action'], ['addwhite'])
        self.assertNotIn('ip', queries[0])
        self.assertEqual(queries[0]['OrderNumber'], ['SECRET'])

    def test_provider_urls_reject_local_targets_and_redirect_inputs(self):
        for url in ('http://127.0.0.1/api', 'https://[::1]/', 'http://localhost/', 'file:///tmp/key', 'https://user:password@example.com/', 'http://169.254.169.254/latest'):
            with self.assertRaises(ProxyError):
                public_url(url, resolve=False)

    def test_expiry_rejects_expired_and_malformed_dates(self):
        now = time.time()
        for value in ('not-a-date', now - 5, now + 90000):
            with self.assertRaises(ProxyError):
                expires_at(value, now)


if __name__ == '__main__':
    unittest.main()
