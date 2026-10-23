"""Explicit trusted-network policy against an isolated real Web process.

Traffic stays on loopback with synthetic Host/Origin/proxy headers. No reverse
proxy, Cloudflare connection, Apple request or real account is used.
"""
from pathlib import Path
import tempfile
import unittest

from test_lan import FixtureServer, PASSWORD

PROXY_ORIGIN = 'https://proxy-fixture.example'
LAN_ORIGIN = 'http://192.168.0.200:17654'
FOREIGN_ORIGIN = 'https://another-fixture.invalid'


class TrustedNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='duo-trusted-network-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.server = FixtureServer(Path(cls.temporary.name), trust_network=True,
                                   disable_tunnel=True)
        cls.addClassCleanup(cls.server.close)

    def login(self, *, target=PROXY_ORIGIN, origin=True, extra_headers=None):
        result, headers = self.server.request('/api/auth/login', {'password': PASSWORD},
            target=target, origin=origin, extra_headers=extra_headers)
        self.assertTrue(result['authenticated'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        return headers['Set-Cookie']

    def test_unconfigured_hosts_and_foreign_or_missing_origins_keep_password_gate(self):
        for host in (PROXY_ORIGIN, LAN_ORIGIN, 'http://not-configured.invalid:19876'):
            for origin in (None, FOREIGN_ORIGIN, 'null'):
                with self.subTest(host=host, origin=origin):
                    self.server.request('/api/snapshot', {}, target=host,
                                        origin=origin, expected=401)
                    status, _ = self.server.request('/api/auth/status', target=host,
                                                    origin=origin)
                    self.assertFalse(status['authenticated'])
        self.server.request('/api/auth/login', {'password': 'wrong'},
                            target=PROXY_ORIGIN, origin=None, expected=401)

    def test_password_session_accepts_cross_origin_and_originless_post_and_logout(self):
        cookie = self.login(origin=FOREIGN_ORIGIN).split(';', 1)[0]
        for origin in (None, FOREIGN_ORIGIN):
            with self.subTest(origin=origin):
                snapshot, _ = self.server.request('/api/snapshot', {}, target=PROXY_ORIGIN,
                                                  origin=origin, cookie=cookie)
                self.assertEqual(snapshot['accounts'], [])
        self.server.request('/api/auth/logout', {}, target=PROXY_ORIGIN,
                            origin=None, cookie=cookie)
        self.server.request('/api/snapshot', {}, target=PROXY_ORIGIN,
                            origin=None, cookie=cookie, expected=401)

    def test_originless_password_login_still_issues_working_session(self):
        cookie = self.login(origin=None).split(';', 1)[0]
        status, _ = self.server.request('/api/auth/status', target=PROXY_ORIGIN,
                                        origin=None, cookie=cookie)
        self.assertTrue(status['authenticated'])

    def test_bearer_works_for_multiple_unconfigured_hosts(self):
        for host in (PROXY_ORIGIN, LAN_ORIGIN, 'http://not-configured.invalid:19876'):
            with self.subTest(host=host):
                status, _ = self.server.request('/api/auth/status', target=host,
                                                origin=None, bearer=True)
                self.assertTrue(status['authenticated'])
                snapshot, _ = self.server.request('/api/snapshot', {}, target=host,
                                                  origin=FOREIGN_ORIGIN, bearer=True)
                self.assertEqual(snapshot['accounts'], [])

    def test_proxy_protocol_first_value_controls_secure_cookie(self):
        for forwarded, expected_secure in [('https', True), ('https, http', True),
                                            ('http', False), ('http, https', False)]:
            with self.subTest(forwarded=forwarded):
                cookie = self.login(target=LAN_ORIGIN, origin=FOREIGN_ORIGIN,
                    extra_headers={'X-Forwarded-Proto': forwarded})
                self.assertEqual('Secure' in cookie, expected_secure)

    def test_missing_or_invalid_proxy_protocol_falls_back_to_origin_scheme(self):
        for forwarded in (None, 'invalid', 'invalid, https'):
            for origin, expected_secure in [(PROXY_ORIGIN, True), (LAN_ORIGIN, False),
                                             (None, False), ('null', False)]:
                with self.subTest(forwarded=forwarded, origin=origin):
                    headers = {} if forwarded is None else {'X-Forwarded-Proto': forwarded}
                    cookie = self.login(target=LAN_ORIGIN, origin=origin, extra_headers=headers)
                    self.assertEqual('Secure' in cookie, expected_secure)

    def test_direct_lan_http_cookie_is_not_secure(self):
        cookie = self.login(target=LAN_ORIGIN)
        self.assertNotIn('Secure', cookie)

    def test_tunnel_disable_flag_remains_effective_with_network_trust(self):
        for host in (self.server.origin, PROXY_ORIGIN):
            with self.subTest(host=host):
                result, _ = self.server.request('/api/tunnel/register',
                    {'origin': 'https://fixture-network.trycloudflare.com'},
                    target=host, origin=None, bearer=True, expected=403)
                self.assertIn('Quick Tunnel', result['error'])

    def test_network_trust_is_opt_in_and_does_not_leak_into_strict_fixture(self):
        with tempfile.TemporaryDirectory(prefix='duo-strict-network-') as temporary:
            server = FixtureServer(Path(temporary), origins=LAN_ORIGIN, disable_tunnel=True)
            try:
                server.request('/api/snapshot', {}, target=PROXY_ORIGIN,
                               origin=FOREIGN_ORIGIN, bearer=True, expected=403)
                server.request('/api/snapshot', {}, target=LAN_ORIGIN,
                               origin=None, bearer=True, expected=403)
                server.request('/api/snapshot', {}, target=LAN_ORIGIN, bearer=True, expected=401)
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
