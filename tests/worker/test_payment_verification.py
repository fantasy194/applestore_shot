import json
import unittest
from urllib.parse import urlencode

import duo_killer as shop


class PaymentVerificationTests(unittest.TestCase):
    def result(self, amount='9999.00', subject='W1234567890'):
        return {'order_number': 'W1234567890', 'amount_due': None,
                'payment_url': 'https://openapi.alipay.com/gateway.do?' + urlencode({
                    'biz_content': json.dumps({'subject': subject, 'total_amount': amount})})}

    def test_phone_and_cloth_payment_reference_matches_actual_total(self):
        for amount, total in [('69.00', 'RMB 69'), ('9999.00', 'RMB 9,999'), ('30000.00', 'RMB 30,000')]:
            with self.subTest(amount=amount):
                result = self.result(amount)
                shop.validate_payment_result(result, total)
                self.assertTrue(result['payment_reference_verified'])
                self.assertEqual(result['amount_due'], 'RMB ' + amount)

    def test_mismatched_amounts_and_subjects_are_rejected(self):
        for result in [self.result('69.00'), self.result(subject='W9999999999'), self.result('0'), self.result('NaN'), self.result('-1')]:
            with self.subTest(result=result), self.assertRaises(ValueError):
                shop.validate_payment_result(result, 'RMB 9,999')

    def test_missing_ambiguous_or_invalid_payment_data_is_rejected(self):
        for query in ['biz_content=[]', 'biz_content=not-json', 'fixture=only',
                      'biz_content={}&biz_content={}']:
            with self.subTest(query=query), self.assertRaises(ValueError):
                shop.validate_payment_result({'order_number': 'W1234567890', 'payment_url': 'https://openapi.alipay.com/gateway.do?' + query})

    def test_header_amount_and_alipay_amount_must_agree(self):
        result = self.result()
        result['amount_due'] = 'RMB 69'
        with self.assertRaises(ValueError):
            shop.validate_payment_result(result, 'RMB 9,999')
