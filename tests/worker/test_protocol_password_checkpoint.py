"""Real local SRP proofs; accepted passwords persist before later Shop failure."""
import base64
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import alipay_qr
import duo_killer
import interaction
from apple_login import AppleStoreLogin, LoginError, password_key, srp
from service import Service


class ProtocolPasswordCheckpointTests(unittest.TestCase):
    def service_fixture(self, stack):
        root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT'):
            stack.enter_context(patch.object(duo_killer,name,getattr(duo_killer,name)))
        stack.enter_context(patch.object(interaction,'handler',interaction.handler))
        stack.enter_context(patch.object(alipay_qr,'ROOT',alipay_qr.ROOT))
        stack.enter_context(redirect_stdout(io.StringIO()))
        vault=Mock();channel=Mock(account_id='fixture-account',task_id='fixture-task')
        service=Service(channel,root,root,'owner@example.test',vault)
        service.ensure_auth_file();service.password='fixture-password';service.remember=True
        client=AppleStoreLogin(auth_path=service.auth)
        stack.callback(client.session.close)
        stack.enter_context(patch.object(client,'discard_failed_login'))
        return service,client,vault

    def test_context_rejection_after_previous_success_does_not_delete_accepted_password(self):
        with ExitStack() as stack:
            service,client,vault=self.service_fixture(stack)
            service.password_submitted();service.confirm_password_authenticated()
            vault.save.assert_called_once()
            response=requests.Response();response.status_code=401
            response._content=b'{"serviceErrors":[{"code":"-20101"}]}'
            stack.enter_context(patch.object(client,'bootstrap'))
            request=stack.enter_context(patch.object(client,'request',return_value=response))
            with self.assertRaises(LoginError) as caught:
                client.login(service.account,service.password)
            self.assertEqual(caught.exception.phase,'authentication_context')
            self.assertIsNone(service._password_submitted)
            service.cleanup_credentials(caught.exception)
            vault.invalidate.assert_not_called()
            self.assertFalse(service.password_rejected)
            self.assertEqual(request.call_count,1)
            self.assertTrue(request.call_args.args[1].endswith('/federate'))

    def test_bootstrap_new_password_is_not_rejected_by_old_protocol_argument(self):
        with ExitStack() as stack:
            service,client,vault=self.service_fixture(stack)
            captured_password=service.password
            def bootstrap():
                service.password='fixture-new-password'
                service.password_submitted();service.confirm_password_authenticated()
            def auth(method,path,**kwargs):
                if path=='/federate':return Mock(),{'federated':False}
                if path=='/signin/init':return Mock(),{'salt':base64.b64encode(b'salt').decode(),
                    'b':base64.b64encode(b'challenge').decode(),'iteration':100,'protocol':'s2k','c':'fixture'}
                self.assertEqual(path,'/signin/complete')
                self.assertIsNone(service._password_submitted)
                raise LoginError('fixture old password refused',status=401,codes=('-20101',),phase='password_authentication')
            user=Mock(H_AMK=b'proof');user.start_authentication.return_value=(service.account,b'a')
            user.process_challenge.return_value=b'm1'
            stack.enter_context(patch.object(client,'bootstrap',side_effect=bootstrap))
            stack.enter_context(patch.object(client,'auth',side_effect=auth))
            stack.enter_context(patch('apple_login.srp.User',return_value=user))
            with self.assertRaises(LoginError) as caught:client.login(service.account,captured_password)
            self.assertEqual(caught.exception.phase,'password_authentication')
            vault.save.assert_called_once_with(service.channel.account_id,'fixture-new-password')
            service.cleanup_credentials(caught.exception)
            vault.invalidate.assert_not_called()
            self.assertFalse(service.password_rejected)

    def test_authentication_error_phase_survives_protocol_wrappers(self):
        for path,expected in (('/federate','authentication_context'),('/signin/init','authentication_context'),
                ('/signin/complete','password_authentication'),('/verify/trusteddevice/securitycode','authentication_context')):
            with self.subTest(path=path),ExitStack() as stack:
                service,client,vault=self.service_fixture(stack)
                response=requests.Response();response.status_code=401
                response._content=b'{"serviceErrors":[{"code":"-20101"}]}'
                stack.enter_context(patch.object(client,'request',return_value=response))
                with self.assertRaises(LoginError) as caught:client.auth('POST',path)
                self.assertEqual(caught.exception.phase,expected)
                stack.enter_context(patch.object(client,'_login',side_effect=caught.exception))
                with self.assertRaises(LoginError) as wrapped:client.login(service.account,service.password)
                self.assertEqual(wrapped.exception.phase,expected)

    def test_known_password_context_rejection_preserves_current_checkout_cookie_state(self):
        with ExitStack() as stack:
            service,client,vault=self.service_fixture(stack)
            service._password_authenticated=service.password
            client.session.cookies.set('as_atb','fixture-current-bag',domain='.apple.com.cn',path='/')
            client.session.cookies.set('as_ltn_cn','fixture-response-rotated',domain='.apple.com.cn',path='/')
            error=LoginError('fixture known context refused',status=401,codes=('-20101',),phase='password_authentication')
            AppleStoreLogin.discard_failed_login(client,error)
            self.assertEqual(client.session.cookies.get('as_atb'),'fixture-current-bag')
            self.assertEqual(client.session.cookies.get('as_ltn_cn'),'fixture-response-rotated')
            stored=__import__('json').loads(service.auth.read_text(encoding='utf-8'))
            values={row['name']:row['value'] for rows in stored['cookies'].values() for row in rows}
            self.assertEqual(values['as_atb'],'fixture-current-bag')
            self.assertEqual(values['as_ltn_cn'],'fixture-response-rotated')

    def test_accepted_password_is_saved_before_authx_541_but_unknown_auth_is_not(self):
        for auth_type, status, saved in (("hsa2", 200, True), ("unexpected", 200, False),
                ("hsa2", 409, True)):
            with self.subTest(auth_type=auth_type,status=status), ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
                old={name:getattr(duo_killer,name) for name in ("ROOT","AUTH_PATH","LOG_PATH","PAYMENT_ROOT")}
                for name,value in old.items():stack.enter_context(patch.object(duo_killer,name,value))
                stack.enter_context(patch.object(interaction,"handler",interaction.handler))
                stack.enter_context(patch.object(alipay_qr,"ROOT",alipay_qr.ROOT))
                stack.enter_context(redirect_stdout(io.StringIO()))
                vault=Mock();channel=Mock(account_id="fixture-account",task_id="fixture-task")
                service=Service(channel,root,root,"owner@example.test",vault)
                service.ensure_auth_file();service.password="fixture-password";service.remember=True
                client=AppleStoreLogin(auth_path=service.auth)
                stack.callback(client.session.close)
                salt=b"0123456789abcdef"
                key=password_key(service.password,salt,100,"s2k")
                n,g=srp.get_ng(srp.NG_2048,None,None)
                x=srp.gen_x(hashlib.sha256,salt,service.account,key)
                verifier=srp.long_to_bytes(pow(g,x,n));server=None
                def auth(method,path,**kwargs):
                    nonlocal server
                    payload=kwargs["json"]
                    if path=="/federate":return Mock(status_code=200),{"federated":False}
                    if path=="/signin/init":
                        server=srp.Verifier(service.account,salt,verifier,base64.b64decode(payload["a"]),hash_alg=srp.SHA256)
                        _,challenge=server.get_challenge()
                        return Mock(status_code=200),{"salt":base64.b64encode(salt).decode(),"b":base64.b64encode(challenge).decode(),"iteration":100,"protocol":"s2k","c":"fixture"}
                    self.assertEqual(path,"/signin/complete")
                    self.assertEqual(service._password_submitted,service.password)
                    self.assertEqual(server.verify_session(base64.b64decode(payload["m1"])),base64.b64decode(payload["m2"]))
                    return Mock(status_code=status),{"authType":auth_type}
                def exchange(**kwargs):
                    vault.save.assert_called_once_with(channel.account_id,service.password)
                    raise LoginError("fixture Shop rejected",status=541,phase="store_exchange")
                stack.enter_context(patch.object(client,"bootstrap"))
                stack.enter_context(patch.object(client,"auth",side_effect=auth))
                exchange_call=stack.enter_context(patch.object(client,"finish_store_login",side_effect=exchange))
                stack.enter_context(patch.object(client,"discard_failed_login"))
                if status==409:
                    def wait_for_otp(*args):
                        vault.save.assert_called_once_with(channel.account_id,service.password)
                        raise EOFError('fixture cancelled OTP input')
                    stack.enter_context(patch.object(client,'verify_two_factor',side_effect=wait_for_otp))
                with self.assertRaises((LoginError,EOFError)):client.login(service.account,service.password)
                if saved:
                    if status==409:exchange_call.assert_not_called()
                    else:exchange_call.assert_called_once()
                    service.cleanup_credentials(LoginError("fixture Shop rejected",status=541,phase="store_exchange"))
                    vault.invalidate.assert_not_called()
                else:
                    exchange_call.assert_not_called();vault.save.assert_not_called()
