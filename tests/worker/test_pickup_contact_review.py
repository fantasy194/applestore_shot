"""Synthetic contact presentation regressions; fixtures prohibit Apple traffic."""
from copy import deepcopy
from unittest import TestCase

import checkout_options as options
import duo_killer as shop
import pickup_options as pickup
from test_checkout_options import CONTACT
import test_pickup_checkout as fixtures
from test_pickup_policy import PolicyFlow, policy


def review(contact):
    return {'checkout': {'review': {'pickupContactReview': {'pickupContactInfo': {'d': contact}}}}}


def proof(contact):
    return {key: {'value': value} for key, value in contact.items()}


class ContactPresentationTests(TestCase):
    def test_phone_display_formats_normalize_and_verify_without_suffix_matching(self):
        for phone in ('13800000000', '138-0000-0000', '(138) 0000 0000',
                      '+86 (138) 0000-0000', '0086 13800000000'):
            with self.subTest(phone=phone):
                self.assertEqual(pickup.normalize_contact_value('fullDaytimePhone', phone), '13800000000')
                self.assertTrue(pickup.verify_contact_review(review({**CONTACT, 'fullDaytimePhone': phone}), proof(CONTACT)))
        for phone in ('13800000001', '+1 13800000000', '+8612800000000',
                      '9913800000000', '13800000000 ext 1', '１３８００００００００'):
            with self.subTest(phone=phone), self.assertRaises(ValueError):
                pickup.verify_contact_review(review({**CONTACT, 'fullDaytimePhone': phone}), proof(CONTACT))

    def test_html_entities_are_normalized_symmetrically(self):
        expected = {**CONTACT, 'lastName': 'A&amp;B', 'emailAddress': 'test&#64;example.invalid'}
        for actual in (expected, {**expected, 'lastName': 'A&B', 'emailAddress': 'TEST@example.invalid'}):
            self.assertTrue(pickup.verify_contact_review(review(actual), proof(expected)))

    def test_full_name_whitespace_does_not_authorize_reversed_names(self):
        contact = {key: value for key, value in CONTACT.items() if key not in ('firstName', 'lastName')}
        self.assertTrue(pickup.verify_contact_review(review({**contact, 'fullName': '测试 用户'}), proof(CONTACT)))
        with self.assertRaises(ValueError):
            pickup.verify_contact_review(review({**contact, 'fullName': '用户 测试'}), proof(CONTACT))

    def test_review_only_or_unbound_masks_are_rejected_but_bound_masks_remain_valid(self):
        actual = {**CONTACT, 'emailAddress': 't***@example.invalid', 'fullDaytimePhone': '138****0000'}
        with self.assertRaises(ValueError):
            pickup.verify_contact_review(review(actual), proof(CONTACT))
        masked = proof(CONTACT)
        for key in ('emailAddress', 'fullDaytimePhone'):
            masked[key] = {'masked': actual[key]}
        with self.assertRaises(ValueError):
            pickup.verify_contact_review(review(actual), masked)
        for key in ('emailAddress', 'fullDaytimePhone'):
            masked[key]['bound'] = True
        self.assertTrue(pickup.verify_contact_review(review(actual), masked))

    def test_real_mismatch_diagnostics_identify_field_without_leaking_values(self):
        for key, changed in (('fullDaytimePhone', '13900000000'), ('emailAddress', 'other@example.invalid'),
                             ('firstName', '另一取货人')):
            with self.subTest(key=key), self.assertRaises(ValueError) as raised:
                pickup.verify_contact_review(review({**CONTACT, key: changed}), proof(CONTACT))
            message = str(raised.exception)
            self.assertIn(key, message)
            self.assertIn(pickup.CONTACT_FIELDS[key], message)
            self.assertNotIn(changed, message)
            self.assertNotIn(CONTACT[key], message)

    def test_missing_review_field_never_becomes_a_match(self):
        actual = deepcopy(CONTACT)
        actual.pop('emailAddress')
        with self.assertRaises(ValueError):
            pickup.verify_contact_review(review(actual), proof(CONTACT))

    def test_review_only_masks_require_successfully_submitted_contact_evidence(self):
        actual = {**CONTACT, 'emailAddress': 't•••••••t@example.invalid',
                  'fullDaytimePhone': '••••••••0000'}
        expected = proof(CONTACT)
        with self.assertRaises(ValueError):
            pickup.verify_contact_review(review(actual), expected)
        for record in expected.values():
            record['server_accepted'] = True
        self.assertTrue(pickup.verify_contact_review(review(actual), expected))

    def test_accepted_contact_masks_still_require_matching_visible_identity(self):
        expected = proof(CONTACT)
        for record in expected.values():
            record['server_accepted'] = True
        actual = {**CONTACT, 'emailAddress': 't•••••••t@example.invalid',
                  'fullDaytimePhone': '••••••••0000'}
        invalid = (
            ('emailAddress', 'x•••••••t@example.invalid'),
            ('emailAddress', 't•••••••x@example.invalid'),
            ('emailAddress', 't•••••••t@other.invalid'),
            ('emailAddress', '••••••••@example.invalid'),
            ('emailAddress', 't••••••••@example.invalid'),
            ('fullDaytimePhone', '••••••••0001'),
            ('fullDaytimePhone', '••••••••••••'),
            ('fullDaytimePhone', '••••••••000'),
        )
        for key, changed in invalid:
            with self.subTest(key=key, changed=changed), self.assertRaises(ValueError):
                pickup.verify_contact_review(review({**actual, key: changed}), expected)


class ContactCheckoutChainTests(TestCase):
    setUp = fixtures.PickupCheckoutTests.setUp
    tearDown = fixtures.PickupCheckoutTests.tearDown
    run_flow = fixtures.PickupCheckoutTests.run_flow

    def enable(self):
        options.save_config(self.service.checkout, {'pickup': policy()})

    def formatted_review_flow(self, **changes):
        flow = PolicyFlow()
        original = flow.action
        def action(module, name, data=None):
            original(module, name, data)
            if name == 'continueFromBillingToReview':
                contact = flow.model['checkout']['review']['pickupContactReview']['pickupContactInfo']['d']
                contact.update({'fullDaytimePhone': '+86 (138) 0000-0000', **changes})
                flow.current_response_body = deepcopy(flow.model)
        flow.action = action
        return flow

    def test_contact_post_then_formatted_review_can_prepare_without_order_submission(self):
        self.enable()
        flow = self.formatted_review_flow()
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        payload = next(data for _, name, data in flow.calls if name == 'continueFromPickupContactToBilling')
        self.assertEqual(payload['checkout.pickupContact.selfPickupContact.selfContact.address.fullDaytimePhone'], '13800000000')
        self.assertTrue(shop.prepared_pickup_contact_confirmed(flow.model, {'pickup': policy(), 'contact': CONTACT}))
        self.assertEqual(flow.submissions, 0)

    def test_official_formatted_phone_posts_only_canonical_digits(self):
        self.enable()
        flow = PolicyFlow(official={**CONTACT, 'fullDaytimePhone': '0086 (138) 0000-0000'})
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        payload = next(data for _, name, data in flow.calls if name == 'continueFromPickupContactToBilling')
        self.assertEqual(payload['checkout.pickupContact.selfPickupContact.selfContact.address.fullDaytimePhone'], '13800000000')
        self.service.channel.prompt.assert_not_called()
        self.assertEqual(flow.submissions, 0)

    def test_prepared_contact_check_uses_same_comparison_and_rejects_actual_changes(self):
        config = {'pickup': {'contact_mode': 'local'}, 'contact': deepcopy(CONTACT)}
        model = review({**CONTACT, 'fullDaytimePhone': '0086 (138) 0000-0000'})
        self.assertTrue(shop.prepared_pickup_contact_confirmed(model, config))
        for key, value in (('fullDaytimePhone', '13900000000'), ('emailAddress', 'other@example.invalid'),
                           ('firstName', '另一取货人')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                shop.prepared_pickup_contact_confirmed(review({**CONTACT, key: value}), config)

    def test_actual_review_contact_changes_stop_the_chain_before_any_submission(self):
        self.enable()
        for key, value in (('fullDaytimePhone', '13900000000'), ('emailAddress', 'other@example.invalid'),
                           ('firstName', '另一取货人')):
            with self.subTest(key=key):
                flow = self.formatted_review_flow(**{key: value})
                with self.assertRaises(ValueError):
                    self.run_flow(flow, prepare=True)
                self.assertEqual(flow.submissions, 0)
                self.assertTrue(any(name == 'continueFromPickupContactToBilling' for _, name, _ in flow.calls))

    def test_contact_post_then_real_shape_unbound_review_masks_can_prepare(self):
        self.enable()
        flow = self.formatted_review_flow(emailAddress='t•••••••t@example.invalid',
                                          fullDaytimePhone='••••••••0000')
        self.assertEqual(self.run_flow(flow, prepare=True)['status'], 'prepared')
        for key in ('emailAddress', 'fullDaytimePhone'):
            self.assertTrue(self.service.pickup_contact_proof[key].get('server_accepted'))
        review_contact = flow.model['checkout']['review']['pickupContactReview']['pickupContactInfo']
        self.assertNotIn('b', review_contact)
        self.assertEqual(flow.submissions, 0)

    def test_failed_contact_post_or_rejected_step_never_marks_server_accepted(self):
        self.enable()
        for failure in ('post_error', 'contact_validation'):
            with self.subTest(failure=failure):
                flow = PolicyFlow()
                original = flow.action
                def action(module, name, data=None):
                    if name == 'continueFromPickupContactToBilling' and failure == 'post_error':
                        raise ValueError('synthetic contact POST failed')
                    original(module, name, data)
                    if name == 'continueFromPickupContactToBilling':
                        flow.model['checkout']['d']['page'] = 'PickupContact'
                        flow.model['checkout'].setdefault('pickupContact', {})['m'] = [
                            {'level': 'ERROR', 'text': 'synthetic contact validation failed'}]
                        flow.current_response_body = deepcopy(flow.model)
                flow.action = action
                with self.assertRaises(ValueError):
                    self.run_flow(flow, prepare=True)
                self.assertFalse(any(record.get('server_accepted')
                                     for record in self.service.pickup_contact_proof.values()))
                self.assertEqual(flow.submissions, 0)
