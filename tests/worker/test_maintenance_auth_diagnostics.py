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
from session_maintenance import MaintenanceFailure, maintain_session


class MaintenanceAuthenticationDiagnosticsTests(unittest.TestCase):
    def test_historical_auth_flags_do_not_reclassify_current_model_or_network_failure(self):
        for error,code in ((ValueError('fixture-private-model'),'SESSION_CHECK_INCOMPLETE'),
                (requests.Timeout('fixture-private-url'),'SESSION_NETWORK_FAILED')):
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as directory:
                service=Mock(root=Path(directory));service.authentication_requested=True;service.authentication_failed=True
                service.warm_login.side_effect=error
                result=maintain_session(service,now=lambda:100,warm_only=True)
                self.assertEqual(result['status'],'failed');self.assertFalse(result['requires_login'])
                self.assertEqual(result['error_code'],code)
                self.assertNotIn('fixture-private',str(result))

    def test_redirect_scope_guard_reports_safe_current_reason_without_claiming_password_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service=Mock(root=Path(directory));service.authentication_requested=True;service.authentication_failed=True
            service.warm_login.side_effect=LoginError('fixture-private-target',phase='store_redirect',reason='redirect_scope_mismatch')
            result=maintain_session(service,now=lambda:100,warm_only=True)
            self.assertEqual(result['status'],'failed');self.assertFalse(result['requires_login'])
            self.assertEqual(result['error_code'],'STORE_LOGIN_REDIRECT_INVALID')
            self.assertEqual(result['authentication_reason'],'redirect_scope_mismatch')
            self.assertNotIn('fixture-private',str(result))
            details=service.channel.activity.record.call_args.kwargs['details']
            self.assertEqual(details['authentication_phase'],'store_redirect')
            self.assertEqual(details['authentication_reason'],'redirect_scope_mismatch')

    def test_unknown_prepared_response_has_typed_failure_even_with_old_auth_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            service=Mock(root=Path(directory));service.authentication_requested=True
            service.warm_pickup_prepare.side_effect=MaintenanceFailure('PREPARED_RENEWAL_UNVERIFIED',
                '原结算返回未识别页面，本次准备已结束。',reason='checkout_response_unverified')
            result=maintain_session(service,now=lambda:100,warm_only=True,preheat_flow='pickup_prepare',sku='MK2P4CH',count=1)
            self.assertEqual(result['status'],'failed');self.assertFalse(result['requires_login'])
            self.assertEqual(result['error_code'],'PREPARED_RENEWAL_UNVERIFIED')
            self.assertEqual(result['authentication_phase'],'prepared_renewal')
            self.assertEqual(result['authentication_reason'],'checkout_response_unverified')

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
