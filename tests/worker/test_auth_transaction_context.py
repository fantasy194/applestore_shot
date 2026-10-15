import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import requests
from apple_login import AppleStoreLogin

class AuthenticationTransactionContextTests(unittest.TestCase):
    def test_renewal_does_not_overwrite_saved_trust_before_verification(self):
        with tempfile.TemporaryDirectory() as td, requests.Session() as session:
            path = Path(td) / 'auth.json'
            path.write_text(json.dumps({'account': 'fixture@example.invalid',
                'trust_cookies': [{'name': 'DESabcdef', 'value': 'old'}],
                'local_address': {'street': 'fixture'}}))
            original = path.read_bytes()
            session.cookies.set('DESabcdef', 'old', domain='.idmsa.apple.com.cn', path='/')
            client = AppleStoreLogin(auth_path=path, session=session, renew_device_trust=True)
            client.account = 'fixture@example.invalid'
            client.restore_trust()
            self.assertFalse(any(c.name == 'DESabcdef' for c in session.cookies))
            response = requests.Response()
            response.headers['Set-Cookie'] = 'DESabcdef=new'
            client.update_trust(response)
            self.assertEqual(path.read_bytes(), original)

    def test_verified_renewal_persists_new_trust_and_preserves_account_fields(self):
        with tempfile.TemporaryDirectory() as td, requests.Session() as session:
            path = Path(td) / 'auth.json'
            saved = {'account': 'fixture@example.invalid', 'local_address': {'street': 'fixture'}}
            path.write_text(json.dumps(saved))
            client = AppleStoreLogin(auth_path=path, session=session, renew_device_trust=True)
            client.account = saved['account']
            response = requests.Response(); response._content = b'{}'
            response.headers['Set-Cookie'] = 'DESabcdef=new'
            def auth(method, endpoint, **kwargs):
                if endpoint == '/2sv/trust':
                    session.cookies.set('DESabcdef', 'new', domain='.idmsa.apple.com.cn', path='/')
                return response, {'hasTrustedDevices': True}
            with patch.object(client, 'auth', side_effect=auth), patch('builtins.input', return_value='123456'):
                client.verify_two_factor()
            result = json.loads(path.read_text())
            self.assertEqual(result['account'], saved['account'])
            self.assertEqual(result['local_address'], saved['local_address'])
            self.assertEqual(result['trust_cookies'][0]['value'], 'new')

    def test_only_old_session_aasp_is_removed_and_saved_account_is_untouched(self):
        with tempfile.TemporaryDirectory() as td, requests.Session() as session:
            path=Path(td)/'auth.json'
            path.write_text(json.dumps({'account':'fixture@example.invalid','trust_cookies':[{'name':'DESabcdef','value':'fixture'}]}))
            original=path.read_bytes()
            cookies=[('aasp','idmsa.apple.com.cn','old'),
                     ('aasp','.idmsa.apple.com.cn','old-domain'),
                     ('DESabcdef','.idmsa.apple.com.cn','trusted'),
                     ('as_ltn_cn','.apple.com.cn','store'),
                     ('shld_bt_ck','.apple.com.cn','browser'),
                     ('aasp','.apple.com.cn','other-domain')]
            for name,domain,value in cookies:session.cookies.set(name,value,domain=domain,path='/')
            client=AppleStoreLogin(auth_path=path,session=session)
            client.auth_headers={'scnt':'old'};client.hc={'X-Apple-HC-Challenge':'old'}
            client.begin_authentication_context()
            self.assertEqual([(c.name,c.domain) for c in session.cookies if c.name=='aasp'],[('aasp','.apple.com.cn')])
            for name in ('DESabcdef','as_ltn_cn','shld_bt_ck'):self.assertTrue(any(c.name==name for c in session.cookies))
            self.assertEqual(client.auth_headers,{})
            self.assertEqual(client.hc,{})
            self.assertEqual(path.read_bytes(),original)
    def test_fresh_transaction_cookie_survives_all_password_authentication_steps(self):
        with requests.Session() as session:
            session.cookies.set('aasp','old',domain='.idmsa.apple.com.cn',path='/')
            client=AppleStoreLogin(session=session)
            client.begin_authentication_context()
            session.cookies.set('aasp','fresh',domain='.idmsa.apple.com.cn',path='/')
            reply=requests.Response();reply.status_code=200;reply._content=b'{}'
            with patch.object(session,'request',return_value=reply) as request:
                for path in ('/federate','/signin/init','/signin/complete'):
                    client.request('POST','https://idmsa.apple.com.cn/appleauth/auth'+path,json={})
                    self.assertEqual(session.cookies.get('aasp'),'fresh')
            self.assertEqual(request.call_count,3)
    def test_server_cookie_with_explicit_future_expiration_is_retained(self):
        import time
        with requests.Session() as session:
            session.cookies.set('aasp','server-lifetime',domain='.idmsa.apple.com.cn',path='/',expires=int(time.time())+60)
            client=AppleStoreLogin(session=session);client.begin_authentication_context()
            self.assertEqual(session.cookies.get('aasp'),'server-lifetime')
