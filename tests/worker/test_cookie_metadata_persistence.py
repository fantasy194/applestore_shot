"""Synthetic HTTP/browser session metadata survives a persisted round trip."""
import json
from pathlib import Path
import tempfile
import time
import unittest

import requests
import duo_killer


class CookieMetadataPersistenceTests(unittest.TestCase):
    def test_http_only_same_site_and_scope_survive_save_then_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth.json'
            path.write_text(json.dumps({'account':'synthetic@example.test', 'trust_account':'synthetic@example.test'}))
            with requests.Session() as original, requests.Session() as restored:
                for index, same_site in enumerate(('Strict', 'Lax', 'None')):
                    original.cookies.set_cookie(requests.cookies.create_cookie(
                        'synthetic-'+str(index), 'fixture', domain='secure8.www.apple.com.cn',
                        path='/shop', secure=True, expires=int(time.time())+600,
                        rest={'HttpOnly':None, 'SameSite':same_site}))
                duo_killer.save_auth(original, auth_path=path)
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['trust_account'], 'synthetic@example.test')
                duo_killer.load_auth(restored, saved)
                cookies = list(restored.cookies)
                self.assertEqual(len(cookies), 3)
                for cookie, same_site in zip(cookies, ('Strict', 'Lax', 'None')):
                    self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))
                    self.assertEqual(cookie.get_nonstandard_attr('SameSite'), same_site)
                    self.assertEqual(cookie.domain, 'secure8.www.apple.com.cn')
                    self.assertEqual(cookie.path, '/shop')
                    self.assertTrue(cookie.secure)
                request = restored.prepare_request(requests.Request('GET', 'https://secure8.www.apple.com.cn/shop/account/home'))
                self.assertIn('synthetic-0=fixture', request.headers.get('Cookie', ''))
                request = restored.prepare_request(requests.Request('GET', 'https://www.apple.com.cn/'))
                self.assertNotIn('synthetic-0', request.headers.get('Cookie', ''))

    def test_legacy_rows_still_load_and_unrecognized_metadata_is_not_restored(self):
        groups = {name:[] for name in ('account_session','server_managed','with_expiry','expiry_unknown')}
        groups['expiry_unknown'] = [
            {'name':'legacy','value':'fixture','domain':'.apple.com.cn','path':'/'},
            {'name':'invalid','value':'fixture','domain':'.apple.com.cn','path':'/',
             'httpOnly':'true', 'sameSite':'unrecognized'}]
        with requests.Session() as session:
            duo_killer.load_auth(session, {'cookies':groups})
            self.assertEqual(len(session.cookies), 2)
            for cookie in session.cookies:
                self.assertFalse(cookie.has_nonstandard_attr('HttpOnly'))
                self.assertIsNone(cookie.get_nonstandard_attr('SameSite'))


if __name__ == '__main__':
    unittest.main()
