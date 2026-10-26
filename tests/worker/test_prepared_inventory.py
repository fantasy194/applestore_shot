"""Account Checkout inventory refresh is bounded, direct and never an order."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from unittest.mock import Mock
from pathlib import Path
from types import SimpleNamespace
import tempfile
import json
import os

import requests
import duo_killer as shop
import interaction
from prepared_inventory import LOCATOR,refresh_prepared_inventory
from session_maintenance import MaintenanceFailure


CONFIG={'pickup':{'search':'200000','store_name':'测试门店','store_id':'R580','contact_mode':'official'}}
EXPECTED=[('MK2M4CH/A',1)]


class PreparedInventoryTests(TestCase):
    def setUp(self):
        self.patch=patch.object(interaction,'handler',None);self.patch.start();self.addCleanup(self.patch.stop)
        session=requests.Session();self.addCleanup(session.close)
        session.cookies.set('as_ltn_cn','synthetic',domain='.apple.com.cn',path='/')
        self.flow=shop.Checkout(session)
        self.flow._prepared_inventory_queried_at=-1000
        query_clock=patch('pickup_options.monotonic',side_effect=lambda: __import__('prepared_inventory').monotonic())
        query_clock.start();self.addCleanup(query_clock.stop)
        self.flow.url='https://secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init'
        self.body={'meta':{'h':{'x-aos-stk':'current-stk'}},'checkout':{'d':{'page':'Fulfillment'},
            'fulfillment':{'fulfillmentOptions':{'d':{'selectFulfillmentLocation':'RETAIL'}},
                'items':{'item-1':{'d':{'sku':'MK2M4CH/A'},'itemQuantity':{'d':{'quantity':1}}}},
                'pickupTab':{'pickup':{'storeLocator':{
                    'd':{'selectStore':'','searchInput':'200000'},
                    'a':{'search':{'url':'/shop/checkout?_a=search&_m=checkout.fulfillment.pickupTab.pickup.storeLocator'}},
                    'address':{'stateCitySelectorForCheckout':{'d':{'state':'上海','city':'上海','countryCode':'CN'}}}}}}}}}
        self.flow.update(deepcopy(self.body));self.calls=[]
        self.reply={'checkout':{'fulfillment':{'pickupTab':{'pickup':{'storeLocator':{
            'd':{'selectStore':'','searchInput':'200000'},
            'searchResults':{'d':{'retailStores':[{'storeName':'测试门店','storeId':'R580','storeDisabled':False}]}}}}}}}}
        def action(module,name,data):
            self.calls.append((module,name,deepcopy(data)))
            self.assertIs(self.flow.session,session)
            self.assertEqual(session.cookies.get('as_ltn_cn'),'synthetic')
            self.flow.update(deepcopy(self.reply))
        self.flow.action=action

    def refresh(self):return refresh_prepared_inventory(self.flow,EXPECTED,CONFIG)

    def test_same_session_uses_one_shared_query_no_selection_and_accepts_incremental_response(self):
        with patch('prepared_inventory.monotonic',return_value=100):result=self.refresh()
        self.assertEqual(result,{'state':'available','reason':'query_completed','experimental':True,
            'stage_proven':'Fulfillment','order_ready':False,'interval_seconds':300})
        self.assertEqual(len(self.calls),1)
        module,name,data=self.calls[0]
        self.assertEqual((module,name),(LOCATOR,'search'))
        self.assertEqual(data[LOCATOR+'.showAllStores'],'true')
        self.assertEqual(data[LOCATOR+'.selectStore'],'')
        self.assertEqual(data[LOCATOR+'.searchInput'],'200000')
        self.assertEqual(data[LOCATOR+'.address.stateCitySelectorForCheckout.countryCode'],'CN')
        self.assertIsNone(shop.field(self.flow.current_response_body,'checkout.d.page'))
        self.assertEqual(self.flow.current_response_body,self.reply)

    def test_renewal_queries_once_and_retains_partial_waiting_for_incremental_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'checkout.json'
            config.write_text(json.dumps(CONFIG),encoding='utf-8')
            handler=SimpleNamespace(submitted=False,channel=Mock(activity=None),
                validate_preparation_scope=Mock(),order_details=None,pickup_contact_proof=None)
            def fresh_page(*_args,**_kwargs):
                self.flow.update(deepcopy(self.body))
            self.flow.page=Mock(side_effect=fresh_page)
            with patch.object(interaction,'handler',handler),patch.object(shop,'save_auth') as save:
                result=shop.renew_prepared_pickup(self.flow,EXPECTED,
                    {'success':True,'status':'waiting_stock'},config,shop.checkout_config_hash(config))
            self.flow.page.assert_called_once()
            self.assertEqual([name for _,name,_ in self.calls],['search'])
            self.assertEqual(result['prepared_inventory_refresh']['state'],'available')
            self.assertEqual(result['status'],'waiting_stock')
            self.assertFalse(result['order_ready']);self.assertFalse(result['review_verified'])
            self.assertFalse(result['order_submitted'])
            self.assertEqual(result['checkout_details_prepared']['stage'],'Fulfillment')
            self.assertIsNone(shop.field(self.flow.current_response_body,'checkout.d.page'))
            self.assertFalse(handler.submitted)
            save.assert_called_once_with(self.flow.session)

    def test_pending_purchase_yields_without_search_or_consuming_query_interval(self):
        handler=SimpleNamespace(submitted=False,prepared_inventory_interval_minutes=5,
            channel=SimpleNamespace(prepared_claim_waiting=lambda:True))
        previous=self.flow._prepared_inventory_queried_at
        with patch.object(interaction,'handler',handler):result=self.refresh()
        self.assertEqual(result['reason'],'purchase_waiting')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.flow._prepared_inventory_queried_at,previous)

    def test_current_stk_accepts_header_case_without_accepting_ambiguous_tokens(self):
        self.flow.current_response_body['meta']['h']={'X-AOS-STK':'current-stk'}
        self.assertEqual(self.refresh()['state'],'available')
        self.assertEqual(len(self.calls),1)
        self.flow.update(deepcopy(self.body));self.flow._prepared_inventory_queried_at=-1000
        self.flow.current_response_body['meta']['h']['X-AOS-STK']='other'
        self.assertEqual(self.refresh()['reason'],'no_current_stk')
        self.assertEqual(len(self.calls),1)

    def test_direct_query_disables_environment_and_session_proxies_only_in_this_scope(self):
        session=self.flow.session;proxies={'https':'http://session-proxy.invalid:8899'}
        session.proxies=proxies;original=self.flow.action
        def action(*args):
            self.assertFalse(session.trust_env);self.assertEqual(session.proxies,{})
            settings=session.merge_environment_settings(self.flow.url,{},None,None,None)
            self.assertEqual(settings['proxies'],{})
            self.assertTrue(settings['verify'])
            original(*args)
        self.flow.action=action
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://environment-proxy.invalid:8888'}):self.refresh()
        self.assertTrue(session.trust_env);self.assertIs(session.proxies,proxies)

    def test_network_failure_restores_proxy_state_never_replays_or_marks_order(self):
        session=self.flow.session;proxies={'https':'http://session.invalid'};session.proxies=proxies
        def fail(*args):self.calls.append(args);raise requests.Timeout('synthetic-private')
        self.flow.action=fail
        with patch('prepared_inventory.monotonic',return_value=100),self.assertRaises(requests.Timeout):self.refresh()
        self.assertTrue(session.trust_env);self.assertIs(session.proxies,proxies)
        # Restore a freshly verified page: the helper's minimum interval still
        # prevents replay, even if the prior query's outcome was unknown.
        with patch('prepared_inventory.monotonic',return_value=399):result=self.refresh()
        self.assertEqual(result['reason'],'minimum_interval');self.assertEqual(len(self.calls),1)
        self.assertFalse(getattr(self.flow,'_final_submit_attempted',False))

    def test_five_minute_minimum_applies_only_to_maintenance_not_normal_purchase_query(self):
        from pickup_options import query_pickup_stores
        with patch('prepared_inventory.monotonic',return_value=100):self.refresh()
        for stamp,state in ((399,'skipped'),(400,'available')):
            self.flow.update(deepcopy(self.body))
            with patch('prepared_inventory.monotonic',return_value=stamp):result=self.refresh()
            self.assertEqual(result['state'],state)
        self.assertEqual(len(self.calls),2)
        query_pickup_stores(self.flow,'200000')
        self.assertEqual(len(self.calls),3)

    def test_configured_one_five_sixty_minute_cadence_is_independent_and_first_query_waits(self):
        from types import SimpleNamespace
        for minutes in (1,5,60):
            with self.subTest(minutes=minutes):
                self.calls.clear();self.flow.update(deepcopy(self.body))
                self.flow.__dict__.pop('_prepared_inventory_queried_at',None)
                handler=SimpleNamespace(prepared_inventory_interval_minutes=minutes,interval_minutes=60 if minutes==1 else 1)
                with patch.object(interaction,'handler',handler),patch('prepared_inventory.monotonic',return_value=100):
                    initialized=self.refresh()
                self.assertEqual(initialized['reason'],'minimum_interval')
                self.assertEqual(initialized['interval_seconds'],minutes*60)
                with patch.object(interaction,'handler',handler),patch('prepared_inventory.monotonic',return_value=100+minutes*60-0.1):
                    self.assertEqual(self.refresh()['reason'],'minimum_interval')
                self.assertEqual(self.calls,[])
                with patch.object(interaction,'handler',handler),patch('prepared_inventory.monotonic',return_value=100+minutes*60):
                    self.assertEqual(self.refresh()['state'],'available')
                self.assertEqual(len(self.calls),1)

    def test_actual_purchase_search_starts_maintenance_interval_but_is_never_gated(self):
        from types import SimpleNamespace
        from pickup_options import query_pickup_stores
        with patch('prepared_inventory.monotonic',return_value=100):
            query_pickup_stores(self.flow,'200000');query_pickup_stores(self.flow,'200000')
        self.assertEqual(len(self.calls),2)
        self.flow.update(deepcopy(self.body))
        with patch.object(interaction,'handler',SimpleNamespace(prepared_inventory_interval_minutes=1)):
            with patch('prepared_inventory.monotonic',return_value=159):
                self.assertEqual(self.refresh()['reason'],'minimum_interval')
            with patch('prepared_inventory.monotonic',return_value=160):
                self.assertEqual(self.refresh()['state'],'available')
        self.assertEqual(len(self.calls),3)

    def test_no_stock_missing_target_or_disabled_is_partial_not_failure(self):
        for rows in ([],[{'storeName':'其他门店','storeId':'R999','storeDisabled':False}],
                     [{'storeName':'测试门店','storeId':'R580','storeDisabled':True}]):
            with self.subTest(rows=rows):
                self.flow.update(deepcopy(self.body));self.flow._prepared_inventory_queried_at=-1000
                shop.field(self.reply,LOCATOR)['searchResults']['d']['retailStores']=rows
                result=self.refresh()
                self.assertEqual(result['state'],'unavailable');self.assertEqual(result['stage_proven'],'Fulfillment')
                self.assertFalse(result['order_ready'])

    def test_explicit_no_stock_message_is_partial_but_unknown_result_is_not_reused(self):
        node=shop.field(self.reply,LOCATOR)
        node.update(m=[{'level':'ERROR','text':'你所在的地区无货可取'}])
        self.assertEqual(self.refresh()['state'],'unavailable')
        self.flow.update(deepcopy(self.body));self.flow._prepared_inventory_queried_at=-1000
        self.reply={'checkout':{'fulfillment':{}}}
        with self.assertRaises(MaintenanceFailure):self.refresh()
        self.assertEqual(len(self.calls),2)

    def test_matching_store_id_with_wrong_name_is_unknown_not_no_stock(self):
        shop.field(self.reply,LOCATOR)['searchResults']['d']['retailStores']=[{
            'storeId':'R580','storeName':'身份不同的门店','storeDisabled':False}]
        with self.assertRaises(MaintenanceFailure):self.refresh()
        self.assertEqual(len(self.calls),1)

    def test_no_ready_current_scope_skips_without_query(self):
        cases=(('not_fulfillment',lambda: self.flow.update({'checkout':{'d':{'page':'Review'}}})),
            ('not_pickup',lambda: shop.field(self.flow.current_response_body,'checkout.fulfillment.fulfillmentOptions.d').update(selectFulfillmentLocation='HOME')),
            ('store_already_selected',lambda: shop.field(self.flow.current_response_body,LOCATOR+'.d').update(selectStore='R580')),
            ('item_evidence_mismatch',lambda: shop.field(self.flow.current_response_body,'checkout.fulfillment.items.item-1.itemQuantity.d').update(quantity=2)),
            ('no_current_search_action',lambda: shop.field(self.flow.current_response_body,LOCATOR+'.a.search').update(url='https://evil.invalid/shop/checkout')),
            ('no_current_stk',lambda: self.flow.headers.update({'x-aos-stk':'different'})),
            ('submission_started',lambda: setattr(self.flow,'_final_submit_attempted',True)))
        for reason,mutate in cases:
            with self.subTest(reason=reason):
                self.flow._final_submit_attempted=False;self.flow.update(deepcopy(self.body));mutate()
                result=self.refresh();self.assertEqual(result['reason'],reason)
                self.assertEqual(self.calls,[])

    def test_receipt_only_fills_missing_items_not_mismatch_and_cannot_export_private_data(self):
        shop.field(self.flow.current_response_body,'checkout.fulfillment').pop('items')
        with patch.object(shop,'prepared_items_verified_for',return_value=False,create=True):
            self.assertEqual(self.refresh()['reason'],'no_current_item_evidence')
        with patch.object(shop,'prepared_items_verified_for',return_value=True,create=True) as receipt:
            result=self.refresh()
        receipt.assert_called_once_with(self.flow,EXPECTED)
        encoded=json.dumps(result)
        for private in ('synthetic','200000','R580','current-stk','MK2M4CH'):self.assertNotIn(private,encoded)

    def issue_bag_receipt(self):
        shop.field(self.flow.current_response_body,'checkout.fulfillment').pop('items',None)
        body=self.flow.current_response_body;session=self.flow.session
        def bag(reader,url,stage,*,fresh=False):
            self.assertIs(reader.session,session);self.assertTrue(fresh)
            self.assertEqual(url,'https://secure8.www.apple.com.cn/shop/bag')
            reader.current_response_body={'shoppingCart':{'items':{'item-1':{
                'd':{'sku':'MK2M4CH/A'},'itemQuantity':{'d':{'quantity':1}}}}}}
        with patch.object(shop.Checkout,'page',autospec=True,side_effect=bag) as read, \
                patch.object(shop,'perf_counter',return_value=100):
            shop.verify_prepared_physical_items(self.flow,EXPECTED)
        read.assert_called_once()
        self.assertIs(self.flow.current_response_body,body)
        self.assertIs(self.flow.session,session)

    def test_real_fresh_bag_receipt_supplies_missing_current_item_evidence(self):
        self.issue_bag_receipt()
        with patch.object(shop,'perf_counter',return_value=110):
            self.assertTrue(shop.prepared_items_verified_for(self.flow,EXPECTED))
            result=self.refresh()
        self.assertEqual(result['state'],'available');self.assertEqual(len(self.calls),1)
        # The query's new body cannot inherit the prior document's receipt.
        with patch.object(shop,'perf_counter',return_value=110):
            self.assertFalse(shop.prepared_items_verified_for(self.flow,EXPECTED))

    def test_real_receipt_rejects_wrong_body_session_selection_and_expired_clock(self):
        for invalid in ('body','session','expected','expired','future'):
            with self.subTest(invalid=invalid):
                self.flow.update(deepcopy(self.body));self.issue_bag_receipt()
                expected=EXPECTED;stamp=101
                if invalid=='body':self.flow.current_response_body=deepcopy(self.flow.current_response_body)
                elif invalid=='session':
                    self.flow.session=requests.Session();self.addCleanup(self.flow.session.close)
                elif invalid=='expected':expected=[('MK2M4CH/A',2)]
                elif invalid=='expired':stamp=110.001
                else:stamp=99.999
                with patch.object(shop,'perf_counter',return_value=stamp):
                    self.assertFalse(shop.prepared_items_verified_for(self.flow,expected))
                    result=refresh_prepared_inventory(self.flow,expected,CONFIG)
                self.assertEqual(result['reason'],'no_current_item_evidence')
                self.assertEqual(self.calls,[])

    def test_failed_fresh_item_verification_clears_previous_receipt(self):
        self.issue_bag_receipt()
        with patch.object(shop,'perf_counter',return_value=101):
            self.assertTrue(shop.prepared_items_verified_for(self.flow,EXPECTED))
            with patch.object(shop.Checkout,'page',side_effect=requests.Timeout('synthetic')),self.assertRaises(requests.Timeout):
                shop.verify_prepared_physical_items(self.flow,EXPECTED)
            self.assertFalse(shop.prepared_items_verified_for(self.flow,EXPECTED))
            self.assertEqual(self.refresh()['reason'],'no_current_item_evidence')
        self.assertEqual(self.calls,[])

    def test_query_response_cannot_change_goods_or_move_to_another_checkout_step(self):
        for change in ('goods','page','stale'):
            with self.subTest(change=change):
                self.flow.update(deepcopy(self.body));self.flow._prepared_inventory_queried_at=-1000
                self.reply={'checkout':{'fulfillment':{'pickupTab':{'pickup':{'storeLocator':{
                    'searchResults':{'d':{'retailStores':[]}}}}}}}}
                if change=='goods':
                    self.reply['checkout']['fulfillment']['items']={'item-1':{
                        'd':{'sku':'MK2M4CH/A'},'itemQuantity':{'d':{'quantity':2}}}}
                elif change=='page':self.reply['checkout']['d']={'page':'Review'}
                else:self.flow.action=lambda *args:None
                with self.assertRaises(MaintenanceFailure):self.refresh()

    def test_priority_queries_only_first_authorized_search_without_switching_or_selecting(self):
        config={'pickup':{'selection_mode':'priority','contact_mode':'official','stores':[
            {'store_name':'测试门店','store_id':'R580','search':'200000'},
            {'store_name':'第二门店','store_id':'R581','search':'200001'}]}}
        result=refresh_prepared_inventory(self.flow,EXPECTED,config)
        self.assertEqual(result['state'],'available');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][2][LOCATOR+'.searchInput'],'200000')
