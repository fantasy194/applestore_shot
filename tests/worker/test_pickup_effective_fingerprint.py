"""Preparation identity follows effective authorized settings, never JSON source."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import checkout_options as options
from test_pickup_policy import policy
from test_checkout_options import CONFIG


class EffectiveFingerprintTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'checkout.json'
        self.config={**deepcopy(CONFIG),'pickup':policy()}
        self.path.write_text(json.dumps(self.config,ensure_ascii=False),encoding='utf-8')
        self.handler=SimpleNamespace(checkout=self.path,pickup_policy_override=None,pickup_override=None)
        handler=patch('interaction.handler',self.handler);handler.start();self.addCleanup(handler.stop)

    def test_saved_template_and_identical_frozen_priority_policy_have_one_fingerprint(self):
        baseline=options.config_fingerprint(self.path)
        self.handler.pickup_policy_override=deepcopy(self.config['pickup'])
        self.assertEqual(options.config_fingerprint(self.path),baseline)
        self.path.write_text(json.dumps(self.config,ensure_ascii=False,sort_keys=True,indent=4),encoding='utf-8')
        self.assertEqual(options.config_fingerprint(self.path),baseline)

    def test_order_contact_or_authorized_store_changes_invalidate(self):
        baseline=options.config_fingerprint(self.path)
        for change in ('order','contact','store','last4'):
            with self.subTest(change=change):
                config=deepcopy(self.config)
                if change=='order':config['pickup']['stores'].reverse()
                elif change=='contact':config['contact']['firstName']='不同联系人'
                elif change=='store':config['pickup']['stores'][0]['store_id']='R999'
                else:config['pickup']['id_last4']='A123'
                self.path.write_text(json.dumps(config),encoding='utf-8')
                self.assertNotEqual(options.config_fingerprint(self.path),baseline)

    def test_same_fixed_monitor_override_keeps_scope_but_other_store_changes_scope(self):
        fixed={'selection_mode':'fixed','contact_mode':'official','store_id':'R580',
               'store_name':'测试门店','search':'200000'}
        self.config['pickup']=fixed
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        baseline=options.config_fingerprint(self.path)
        self.handler.pickup_override={key:fixed[key] for key in ('store_id','store_name','search')}
        self.assertEqual(options.config_fingerprint(self.path),baseline)
        self.handler.pickup_override={**self.handler.pickup_override,'store_id':'R581'}
        self.assertNotEqual(options.config_fingerprint(self.path),baseline)

    def test_old_rule_sources_normalize_to_current_earliest_seven_day_contract(self):
        baseline=options.config_fingerprint(self.path)
        self.config['pickup']['time_slot_rule']={'mode':'window','start':'15:00','end':'18:00','fallback':'stop'}
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        self.assertEqual(options.config_fingerprint(self.path),baseline)

    def test_override_for_other_config_path_cannot_change_fingerprint(self):
        baseline=options.config_fingerprint(self.path)
        self.handler.checkout=self.path.parent/'different.json'
        self.handler.pickup_policy_override=policy(stores=list(reversed(self.config['pickup']['stores'])))
        self.assertEqual(options.config_fingerprint(self.path),baseline)
