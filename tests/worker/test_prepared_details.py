"""Official contact/address preparation contracts. Synthetic models only."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import requests

import checkout_options as options
import interaction
import pickup_options as pickup
from test_checkout_options import CONFIG, CONTACT, FakeFlow, contact_model, fulfillment_model, shipping_model


class DetailFlow(FakeFlow):
    def __init__(self, model, mixed=False):
        super().__init__(model, mixed=mixed)
        self.sync()

    def sync(self):
        page = self.model['checkout']['d']['page']
        names = {'PickupContact': ('continueFromPickupContactToShipping' if self.mixed
                                  else 'continueFromPickupContactToBilling'),
                 'Shipping': 'continueFromShippingToBilling'}
        if page in names:
            section = self.model['checkout']['pickupContact' if page == 'PickupContact' else 'shipping']
            section['a'] = {names[page]: {'url': '/shop/checkoutx/fixture'}}
        self.current_response_body = deepcopy(self.model)

    def action(self, module, name, data=None):
        super().action(module, name, data)
        self.sync()


class PreparedDetailTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'checkout.json'
        options.save_config(self.path, deepcopy(CONFIG))
        handler = SimpleNamespace(automatic_checkout=True, prefill_checkout=True, stage=Mock())
        self.handler = handler
        previous = interaction.handler
        interaction.handler = handler
        self.addCleanup(setattr, interaction, 'handler', previous)
        block = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        block.start(); self.addCleanup(block.stop)

    def test_fulfillment_reports_only_local_readiness_without_guessing_contact_route(self):
        flow = DetailFlow(fulfillment_model(mixed=False))
        result = options.prepare_checkout_details(flow, self.path)
        self.assertEqual(result['status'], 'waiting_stock')
        self.assertTrue(result['local_contact_ready'])
        self.assertTrue(result['local_address_ready'])
        self.assertFalse(result['contact_accepted'])
        self.assertFalse(result['address_accepted'])
        self.assertEqual(flow.calls, [])

    def test_real_contact_action_accepts_local_fields_and_preserves_same_flow(self):
        flow = DetailFlow(contact_model(mixed=False))
        result = options.prepare_checkout_details(flow, self.path)
        self.assertEqual(result['stage'], 'Billing')
        self.assertTrue(result['contact_accepted'])
        self.assertEqual(len(flow.calls), 1)
        self.assertEqual(flow.calls[0][1], 'continueFromPickupContactToBilling')
        self.assertTrue(all(row['server_accepted'] for row in self.handler.pickup_contact_proof.values()))
        for key, val in CONTACT.items():
            self.assertEqual(self.handler.pickup_contact_proof[key]['value'], val)

    def test_contact_then_required_shipping_uses_the_shared_forms_only(self):
        flow = DetailFlow(contact_model(mixed=True), mixed=True)
        result = options.prepare_checkout_details(flow, self.path)
        self.assertTrue(result['contact_accepted'])
        self.assertTrue(result['address_accepted'])
        self.assertEqual([call[1] for call in flow.calls],
                         ['continueFromPickupContactToShipping', 'continueFromShippingToBilling'])

    def test_stale_action_in_merged_model_does_not_authorize_form_post(self):
        flow = DetailFlow(contact_model(mixed=False))
        flow.current_response_body['checkout']['pickupContact'].pop('a')
        result = options.prepare_checkout_details(flow, self.path)
        self.assertFalse(result['contact_accepted'])
        self.assertEqual(flow.calls, [])

    def test_billing_keeps_accepted_proof_and_does_not_refill_or_pay(self):
        flow = DetailFlow({'checkout': {'d': {'page': 'Billing'}, 'billing': {}}})
        proof = {'fixture': {'value': 'synthetic', 'server_accepted': True}}
        self.handler.pickup_contact_proof = proof
        options.prepare_checkout_details(flow, self.path)
        self.assertIs(self.handler.pickup_contact_proof, proof)
        self.assertEqual(flow.calls, [])

    def test_rejected_contact_form_does_not_gain_acceptance_or_repeat_post(self):
        flow = DetailFlow(contact_model(mixed=False))
        flow.action = Mock()  # Official page remains on the same contact form.
        flow.page = Mock()
        with self.assertRaisesRegex(ValueError, '不重复发送'):
            options.prepare_checkout_details(flow, self.path)
        self.assertFalse(self.handler.pickup_contact_accepted)
        self.assertTrue(all('server_accepted' not in row for row in self.handler.pickup_contact_proof.values()))
        self.assertEqual(flow.action.call_count, 1)

    def timed_out_contact(self):
        flow = DetailFlow(contact_model(mixed=False))
        flow.action = Mock(side_effect=requests.Timeout())
        with self.assertRaises(requests.Timeout):
            options.prepare_checkout_details(flow, self.path)
        return flow

    def test_timeout_after_acceptance_can_finalize_from_fresh_current_step_without_post(self):
        flow = self.timed_out_contact()
        flow.model = {'checkout': {'d': {'page': 'Billing'}, 'billing': {}}}
        flow.current_response_body = deepcopy(flow.model)
        self.assertTrue(options.confirm_pickup_contact_submission(flow, fresh=True))
        self.assertTrue(self.handler.pickup_contact_accepted)
        self.assertTrue(all(row['server_accepted'] for row in self.handler.pickup_contact_proof.values()))
        self.assertEqual(flow.action.call_count, 1)
        self.assertFalse(options.confirm_pickup_contact_submission(flow, fresh=True))

    def test_unadvanced_stale_or_different_owner_or_fields_never_finalize_pending_submission(self):
        for change in ('unadvanced', 'stale_response', 'handler', 'store', 'proof'):
            with self.subTest(change=change):
                self.handler.pickup_selected = None
                interaction.handler = self.handler
                flow = self.timed_out_contact()
                if change != 'unadvanced':
                    flow.model = {'checkout': {'d': {'page': 'Billing'}, 'billing': {}}}
                    flow.current_response_body = deepcopy(flow.model)
                if change == 'stale_response':
                    flow.current_response_body = contact_model(mixed=False)
                elif change == 'handler':
                    interaction.handler = SimpleNamespace(pickup_contact_proof=deepcopy(self.handler.pickup_contact_proof))
                elif change == 'store':
                    self.handler.pickup_selected = {'storeId': 'R999'}
                elif change == 'proof':
                    self.handler.pickup_contact_proof['fullDaytimePhone']['value'] = '13900000000'
                self.assertFalse(options.confirm_pickup_contact_submission(flow, fresh=True))
                self.assertFalse(self.handler.pickup_contact_accepted)
                self.assertEqual(flow.action.call_count, 1)


class PreparedReviewProofTests(TestCase):
    def setUp(self):
        self.now = patch.object(pickup, 'pickup_calendar_now', return_value=
                               datetime(2026, 10, 26, 9, tzinfo=ZoneInfo('Asia/Shanghai')))
        self.now.start(); self.addCleanup(self.now.stop)
        self.details = {'store': '测试门店', 'store_id': 'R580', 'pickup_time_slot': {
            'store_id': 'R580', 'date': '2026-10-27', 'start': '10:00', 'end': '10:15'}}
        self.model = {'checkout': {'d': {'page': 'Review'}, 'review': {
            'summary': {'d': {'pickupSummary': True}},
            'fulfillmentReview': {'reviewGroup-1': {'d': {'pickup': {
                'storeId': 'R580', 'date': '2026-10-27', 'timeSlotValue': '27-10:00-10:15'}},
                'item-0': {'d': {'sku': 'MK2P4CH/A'}, 'itemQuantity': {'d': {'quantity': 1}}}}},
            'pickupContactReview': {'address': {'d': deepcopy(CONTACT)}}}}}

    def test_explicit_review_appointment_avoids_reselection_without_any_signature_post(self):
        self.assertTrue(pickup.prepared_pickup_slot_confirmed(self.model, self.details))

    def test_missing_expired_changed_or_other_store_appointment_cannot_confirm(self):
        for field, value in (('date', None), ('date', '2026-10-28'), ('timeSlotValue', ''),
                             ('timeSlotValue', '27-11:00-11:15')):
            with self.subTest(field=field, value=value):
                model = deepcopy(self.model)
                model['checkout']['review']['fulfillmentReview']['reviewGroup-1']['d']['pickup'][field] = value
                self.assertFalse(pickup.prepared_pickup_slot_confirmed(model, self.details))
        model = deepcopy(self.model)
        model['checkout']['review']['fulfillmentReview']['reviewGroup-1']['d']['pickup']['storeId'] = 'R999'
        with self.assertRaisesRegex(ValueError, '门店.*不一致'):
            pickup.prepared_pickup_slot_confirmed(model, self.details)
        self.details['pickup_time_slot']['date'] = '2026-10-25'
        self.assertFalse(pickup.prepared_pickup_slot_confirmed(self.model, self.details))

    def test_inactive_review_cannot_confirm_contact_or_slot(self):
        self.model['checkout']['d']['page'] = 'Fulfillment'
        self.assertFalse(pickup.prepared_pickup_slot_confirmed(self.model, self.details))
        self.assertIsNone(pickup.prepared_contact_review_proof(self.model))

    def test_accepted_local_contact_can_be_reverified_after_current_review_redaction(self):
        proof = {key: {'value': val, 'server_accepted': True} for key, val in CONTACT.items()}
        self.model['checkout']['review']['pickupContactReview']['address']['d'].update(
            emailAddress='t**t@example.invalid', fullDaytimePhone='138****0000')
        self.assertEqual(pickup.prepared_contact_review_proof(self.model, proof), proof)
        self.assertIsNone(pickup.prepared_contact_review_proof(self.model))

    def test_different_current_contact_stops_instead_of_reusing_old_proof(self):
        proof = {key: {'value': val, 'server_accepted': True} for key, val in CONTACT.items()}
        self.model['checkout']['review']['pickupContactReview']['address']['d']['fullDaytimePhone'] = '13900000000'
        with self.assertRaises(ValueError):
            pickup.prepared_contact_review_proof(self.model, proof)

    def test_missing_review_contact_is_not_an_accepted_local_config(self):
        proof = {key: {'value': val, 'server_accepted': True} for key, val in CONTACT.items()}
        self.model['checkout']['review']['pickupContactReview'] = {}
        self.assertIsNone(pickup.prepared_contact_review_proof(self.model, proof))
