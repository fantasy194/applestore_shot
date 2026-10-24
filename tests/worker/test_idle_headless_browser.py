"""Launch-policy tests: capture arguments before Chrome or any HTTP can start."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import apple_login
import browser_login
import interaction


class CapturedBrowserLaunch(RuntimeError):
    pass


class IdleHeadlessBrowserPolicyTests(unittest.TestCase):
    def capture(self, entrypoint, flag):
        with tempfile.TemporaryDirectory(prefix='duo-headless-policy-fixture-') as directory:
            auth = Path(directory) / 'auth.json'
            service = SimpleNamespace(auth=auth, root=Path(directory), account='fixture@example.test',
                headless_browser=flag, channel=SimpleNamespace(checkpoint=lambda: None), stage=Mock())
            process = Mock(side_effect=CapturedBrowserLaunch('launch arguments captured; no process started'))
            with patch.object(interaction, 'handler', service), \
                    patch('requests.utils.get_environ_proxies', return_value={}), \
                    patch('requests.Session.request', side_effect=AssertionError('HTTP is forbidden')), \
                    patch('subprocess.Popen', process), \
                    patch('apple_login.find_browser', return_value=Path(directory) / 'fixture-chrome'), \
                    patch('browser_login.find_browser', return_value=Path(directory) / 'fixture-chrome'):
                with self.assertRaises(CapturedBrowserLaunch):
                    if entrypoint == 'context':
                        apple_login.AppleStoreLogin(auth_path=auth).initialize_browser_context()
                    else:
                        browser_login.login_in_browser(service, maintenance=False)
            return process.call_args.args[0]

    def test_manual_browser_remains_headful_unless_automatic_scope_sets_exact_true(self):
        for entrypoint in ('context', 'login'):
            for flag in (None, False, 'true', 1):
                with self.subTest(entrypoint=entrypoint, flag=flag):
                    args = self.capture(entrypoint, flag)
                    self.assertFalse(any(arg.startswith('--headless') for arg in args))

    def test_exact_true_selects_headless_for_both_production_launch_points(self):
        for entrypoint in ('context', 'login'):
            with self.subTest(entrypoint=entrypoint):
                args = self.capture(entrypoint, True)
                self.assertTrue(any(arg.startswith('--headless') for arg in args))
                self.assertTrue(any(arg.startswith('--user-data-dir=') for arg in args))
                self.assertIn('--remote-debugging-address=127.0.0.1', args)

    def test_headless_trust_detection_does_not_click_or_reserve_a_trust_choice(self):
        frame = Mock(url='https://idmsa.apple.com.cn/appleauth/auth/authorize')
        frame.locator.return_value.inner_text.return_value = '是否信任此浏览器？'
        button = frame.get_by_role.return_value
        button.count.return_value = 1
        button.is_visible.return_value = True
        page = Mock(frames=[frame])
        self.assertTrue(browser_login.trust_browser_if_offered(page, accept=False))
        button.click.assert_not_called()
        self.assertNotIn(frame, browser_login._TRUST_SENT)
        # A later user-started visible flow can still accept this exact question.
        self.assertTrue(browser_login.trust_browser_if_offered(page))
        button.click.assert_called_once()
        self.assertEqual(button.click.call_args.kwargs['timeout'], 1000)
        self.assertTrue(100 <= button.click.call_args.kwargs['delay'] <= 200)


if __name__ == '__main__':
    unittest.main()
