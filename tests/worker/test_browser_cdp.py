"""Offline connection budgets and safe diagnostics; no real browser or Apple."""
import io
import json
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as BrowserError
from protocol import Cancelled
import browser_cdp


class BrowserCdpTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.chromium = Mock()
        self.process = Mock()
        self.process.poll.return_value = None
        self.checkpoint = Mock()
        for target, value in [('browser_cdp.time.monotonic', lambda: self.clock),
                              ('browser_cdp.time.sleep', self.advance)]:
            patcher = patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.clock += seconds

    def connect(self, **kwargs):
        return browser_cdp.connect_browser(self.chromium, self.process, 12345,
            checkpoint=self.checkpoint, **kwargs)

    def test_waits_for_port_then_allows_complete_slow_attachment(self):
        def attach(*args, **kwargs):
            self.assertGreater(kwargs['timeout'], 1000)
            self.advance(2)
            return self.chromium.browser
        self.chromium.connect_over_cdp.side_effect = attach
        with patch('browser_cdp._debug_ready', side_effect=[False, False, True]) as ready:
            result = self.connect()
        self.assertIs(result, self.chromium.browser)
        self.assertEqual(ready.call_count, 3)
        self.chromium.connect_over_cdp.assert_called_once_with(
            'http://127.0.0.1:12345', timeout=15000, no_defaults=True)
        self.assertGreaterEqual(self.checkpoint.call_count, 7)

    def test_attach_uses_only_remaining_original_deadline_and_redacts_errors(self):
        def probe(*args):
            self.advance(1.5)
            return True
        def attach(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 500)
            self.advance(.5)
            raise BrowserError('private credential ws://127.0.0.1/secret')
        self.chromium.connect_over_cdp.side_effect = attach
        with patch('browser_cdp._debug_ready', side_effect=probe):
            with self.assertRaises(browser_cdp.BrowserConnectError) as captured:
                self.connect(timeout=2)
        self.assertEqual(captured.exception.code, 'BROWSER_CONNECT_TIMEOUT')
        self.assertNotIn('secret', str(captured.exception))
        self.assertNotIn('ws:', str(captured.exception))
        self.assertEqual(self.clock, 2)

    def test_failed_attempts_do_not_reset_total_budget(self):
        def attach(*args, **kwargs):
            self.advance(kwargs['timeout'] / 1000)
            raise BrowserError('private transport detail')
        self.chromium.connect_over_cdp.side_effect = attach
        with patch('browser_cdp._debug_ready', return_value=True):
            with self.assertRaises(browser_cdp.BrowserConnectError) as captured:
                self.connect(timeout=31)
        self.assertEqual(captured.exception.code, 'BROWSER_CONNECT_TIMEOUT')
        self.assertEqual(self.clock, 31)
        self.assertEqual(self.chromium.connect_over_cdp.call_count, 3)
        self.assertLessEqual(self.chromium.connect_over_cdp.call_args.kwargs['timeout'], 1000)

    def test_port_never_ready_does_not_attempt_playwright(self):
        def probe(*args):
            self.advance(args[-1])
            return False
        with patch('browser_cdp._debug_ready', side_effect=probe):
            with self.assertRaises(browser_cdp.BrowserConnectError) as captured:
                self.connect(timeout=2)
        self.assertEqual(captured.exception.code, 'BROWSER_DEBUG_PORT_TIMEOUT')
        self.chromium.connect_over_cdp.assert_not_called()
        self.assertEqual(self.clock, 2)

    def test_early_process_exit_is_distinct_and_does_not_probe(self):
        self.process.poll.return_value = 1
        with patch('browser_cdp._debug_ready') as ready:
            with self.assertRaises(browser_cdp.BrowserConnectError) as captured:
                self.connect()
        self.assertEqual(captured.exception.code, 'BROWSER_EXITED_EARLY')
        ready.assert_not_called()
        self.chromium.connect_over_cdp.assert_not_called()

    def test_cancellation_before_or_after_probe_propagates_without_attachment(self):
        for calls in (1, 2):
            with self.subTest(calls=calls):
                self.checkpoint.reset_mock(side_effect=True)
                self.checkpoint.side_effect = [None] * (calls - 1) + [Cancelled()]
                with patch('browser_cdp._debug_ready', return_value=True):
                    with self.assertRaises(Cancelled):
                        self.connect()
                self.chromium.connect_over_cdp.assert_not_called()

    def test_cancellation_after_attach_disconnects_only_owned_client(self):
        self.checkpoint.side_effect = [None, None, Cancelled()]
        with patch('browser_cdp._debug_ready', return_value=True):
            with self.assertRaises(Cancelled):
                self.connect()
        self.chromium.connect_over_cdp.return_value.close.assert_called_once_with()
        self.process.terminate.assert_not_called()
        self.process.kill.assert_not_called()

    def test_preview_may_connect_without_an_owned_process(self):
        with patch('browser_cdp._debug_ready', return_value=True):
            result = browser_cdp.connect_browser(self.chromium, None, 12345)
        self.assertIs(result, self.chromium.connect_over_cdp.return_value)

    def test_endpoint_probe_uses_no_environment_proxy_and_is_size_bounded(self):
        with patch('browser_cdp.build_opener') as factory:
            opener = factory.return_value
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
                {'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/fixture'}).encode()
            result = self.connect()
        self.assertIs(result, self.chromium.connect_over_cdp.return_value)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        opener.open.assert_called_once_with('http://127.0.0.1:12345/json/version', timeout=1.0)
        opener.open.return_value.__enter__.return_value.read.assert_called_once_with(65537)

    def test_probe_rejects_invalid_and_oversized_responses(self):
        for payload in (b'not-json', b'[]', b'{}', b'x' * 65537):
            with self.subTest(size=len(payload)):
                opener = Mock()
                opener.open.return_value = io.BytesIO(payload)
                self.assertFalse(browser_cdp._debug_ready(opener, 'http://127.0.0.1:12345', 1))


if __name__ == '__main__':
    unittest.main()
