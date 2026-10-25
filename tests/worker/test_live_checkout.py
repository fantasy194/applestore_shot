"""Live checkout reuse requires a new authorized owner and positive no-write proof."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
import live_checkout
from pickup_options import PickupUnavailable, NoPickupSlot


class LiveCheckoutTests(TestCase):
    def setUp(self):
        self.owner = SimpleNamespace(_live_checkout_enabled=True)
        self.service = SimpleNamespace(_live_checkout_owner=self.owner,
            channel=SimpleNamespace(account_id='fixture-account'), submitted=False,
            authentication_failed=False, purchase_auth_interaction=False)
        self.session = Mock()
        self.flow = SimpleNamespace(session=self.session, model={'checkout': {'d': {'page': 'Fulfillment'}}})
        self.selection = ('MK2M4CH/A', 1)

    def retain(self, error=None):
        return live_checkout.retain_unavailable(self.service, self.flow, self.selection, 'config',
                                                error or PickupUnavailable('fixture unavailable'))

    def test_only_explicit_unavailable_at_uncontinued_fulfillment_can_be_retained(self):
        self.assertTrue(self.retain(NoPickupSlot('fixture slot unavailable')))
        for key in ('submitted', 'authentication_failed', 'purchase_auth_interaction'):
            with self.subTest(key=key):
                setattr(self.service, key, True)
                self.assertFalse(self.retain())
                setattr(self.service, key, False)
        self.assertFalse(self.retain(requests.Timeout()))
        self.assertFalse(self.retain(ValueError('unknown failure')))
        self.flow._continuation_attempted = True
        self.assertFalse(self.retain())
        self.flow._continuation_attempted = False
        self.flow.model['checkout']['d']['page'] = 'Billing'
        self.assertFalse(self.retain())

    def test_consumes_same_session_once_and_closes_if_not_retained_again(self):
        self.assertTrue(self.retain())
        with patch('live_checkout.requests.Session') as create:
            with live_checkout.purchase_flow(self.service, self.selection, 'config') as (session, flow):
                self.assertIs(session, self.session)
                self.assertIs(flow, self.flow)
                self.assertIsNone(self.owner._live_checkout)
            create.assert_not_called()
        self.session.close.assert_called_once()

    def test_next_unavailable_target_keeps_session_until_owner_closes(self):
        self.assertTrue(self.retain())
        with live_checkout.purchase_flow(self.service, self.selection, 'config'):
            self.assertTrue(self.retain())
        self.session.close.assert_not_called()
        live_checkout.discard(self.owner)
        self.session.close.assert_called_once()

    def test_expiry_config_product_or_account_change_disposes_lease(self):
        cases = [('selection', ('MK2P4CH/A', 1)), ('config_hash', 'changed'),
                 ('account_id', 'other-account'), ('created', -1)]
        for key, value in cases:
            with self.subTest(key=key):
                self.assertTrue(self.retain())
                self.owner._live_checkout[key] = value
                fresh = Mock()
                with patch('live_checkout.requests.Session', return_value=fresh):
                    with live_checkout.purchase_flow(self.service, self.selection, 'config') as (session, flow):
                        self.assertIsNone(flow)
                        self.assertIs(session, fresh)
                fresh.close.assert_called_once()
        self.assertEqual(self.session.close.call_count, len(cases))
