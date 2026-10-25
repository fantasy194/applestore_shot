"""Maintenance failures retain safe phase facts and do not undo successful auth."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import alipay_qr
import duo_killer
import interaction
from apple_login import LoginError
from service import Service
from session_maintenance import maintain_session


class MaintenanceAuthenticationDiagnosticsTests(unittest.TestCase):
    def test_post_authentication_bag_failure_does_not_request_another_login(self):
        for failure,code in ((requests.Timeout('fixture-private-payload'),'SESSION_NETWORK_FAILED'),
                (ValueError('fixture-private-model'),'SESSION_CHECK_INCOMPLETE')):
            with self.subTest(failure=type(failure).__name__),ExitStack() as stack:
                root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
                for name in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT'):
                    stack.enter_context(patch.object(duo_killer,name,getattr(duo_killer,name)))
                stack.enter_context(patch.object(interaction,'handler',interaction.handler))
                stack.enter_context(patch.object(alipay_qr,'ROOT',alipay_qr.ROOT))
                vault=Mock();channel=Mock(account_id='fixture-account',task_id='fixture-task')
                service=Service(channel,root,root,'owner@example.test',vault)
                service.password='fixture-password';service.remember=True
                def verified(owner):
                    owner.authentication_requested=True;owner.purchase_auth_interaction=True
                    owner.password_submitted();owner.confirm_password_authenticated()
                    return {'success':True,'logged_in':True,'status':'healthy','apple_id':owner.account}
                stack.enter_context(patch('purchase_session.verify_account_session',side_effect=verified))
                stack.enter_context(patch('duo_killer.warm_empty_bag',side_effect=failure))
                stack.enter_context(patch('requests.Session.request',side_effect=AssertionError('network forbidden')))
                result=service.run('session_maintain',{'warm_only':True})
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['error_code'],code)
                self.assertFalse(result['requires_login'])
                self.assertFalse(service.authentication_requested)
                self.assertFalse(service.purchase_auth_interaction)
                self.assertTrue(service.has_verified_password())
                vault.invalidate.assert_not_called();channel.prompt.assert_not_called()
                self.assertNotIn('fixture-private',str(result))

    def test_wrapped_protocol_failure_keeps_only_allowlisted_phase_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            service=Mock(root=Path(directory))
            service.authentication_requested=True
            def fail():
                raise ValueError('fixture-private-url-token') from LoginError('fixture-private-response',
                    status=401,codes=('-20101',),phase='password_authentication')
            service.warm_login.side_effect=fail
            result=maintain_session(service,now=lambda:100,warm_only=True)
            self.assertEqual(result['http_status'],401)
            self.assertEqual(result['authentication_phase'],'password_authentication')
            self.assertNotIn('fixture-private',str(result))
            details=service.channel.activity.record.call_args.kwargs['details']
            self.assertEqual(details['http_status'],401)
            self.assertEqual(details['authentication_phase'],'password_authentication')
