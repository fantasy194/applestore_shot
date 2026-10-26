"""Virtual-clock scheduling and private lease admission; no network or sleeps."""
from copy import deepcopy
import io
import json
import queue
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import duo_killer as shop
import checkout_options
import requests
from protocol import Channel, Cancelled, PromptExpired, PromptRefresh
import test_pickup_checkout as fixtures


EPOCH = 1_793_000_000_000


class Clock:
    def __init__(self, now=0): self.now = now
    def monotonic(self): return self.now
    def wall(self): return EPOCH / 1000 + self.now


class TickFixture:
    def __init__(self, clock, close='closed'):
        self.clock = clock
        self.channel = Channel('fixture-account', 'fixture-task', io.StringIO())
        self.prepared = {'prepared_at_ms': EPOCH + int(clock.now * 1000),
                         'expires_at_ms': EPOCH + int((clock.now + 300) * 1000)}
        self.prompt = None
        self.close = close
        self.on_close = None
        emit = self.channel.emit
        def output(kind, **payload):
            emit(kind, **payload)
            if kind == 'prompt': self.prompt = payload['prompt']
            if kind == 'prompt_closed' and payload.get('prepared_close_handshake'):
                if self.on_close is not None: self.on_close(payload)
                else: self.reply({'_prepared_close_ack': self.close}, payload['request_id'])
        self.channel.emit = output
        original_get = self.channel.responses.get
        def get(block=True, timeout=None):
            if not block or not self.channel.responses.empty():
                return original_get(block=False)
            self.clock.now += timeout or .05
            raise queue.Empty()
        self.channel.responses.get = get

    def reply(self, value, request_id=None):
        self.channel.receive({'type':'reply', 'account_id':self.channel.account_id,
            'task_id':self.channel.task_id, 'request_id':request_id or self.prompt['id'], 'value':value})

    def claim(self, *, at=None):
        at = EPOCH + int(self.clock.now * 1000) if at is None else at
        return {'prepared_claim': {'v':1, 'account_id':self.channel.account_id,
            'prepared_task_id':self.channel.task_id, 'request_id':self.prompt['id'], 'source':'manual',
            'requested_at_ms':at, 'claimed_at_ms':at},
            'params':{'sku':'MK2M4CH','quantity':1,'fulfillment':'RETAIL'}}

    def wait(self, callback, *, initial=60, interval=60):
        return self.channel.prompt('prepared_purchase','fixture waiting', prepared=self.prepared,
            timeout=300, _on_tick=callback, _tick_seconds=interval, _tick_initial_seconds=initial)


class PreparedProtocolTicksTests(TestCase):
    def test_one_minute_runs_at_sixty_and_private_options_do_not_leak(self):
        clock = Clock(); fixture = TickFixture(clock); observed = []
        def tick():
            observed.append(clock.now); fixture.reply(fixture.claim())
        with patch('protocol.time.monotonic', clock.monotonic): result = fixture.wait(tick)
        self.assertEqual(observed, [60])
        self.assertIsInstance(result['prepared_claim'], dict)
        self.assertFalse(any(key.startswith('_') for key in fixture.prompt))
        self.assertEqual(fixture.prepared['expires_at_ms'], EPOCH + 300_000)

    def test_timely_claim_survives_callback_crossing_expiry_and_admission_is_single_use(self):
        clock = Clock(); fixture = TickFixture(clock)
        def tick():
            fixture.reply(fixture.claim()); clock.now = 310
        with patch('protocol.time.monotonic', clock.monotonic): result = fixture.wait(tick)
        request = fixture.prompt['id']
        self.assertTrue(fixture.channel.consume_prepared_admission(request, result))
        self.assertFalse(fixture.channel.consume_prepared_admission(request, result))
        self.assertEqual(fixture.prepared['expires_at_ms'], EPOCH + 300_000)

    def test_claim_during_tick_wins_over_refresh_signal_or_query_failure(self):
        for failure in (False, True):
            clock = Clock(); fixture = TickFixture(clock)
            def tick():
                fixture.reply(fixture.claim())
                if failure: raise ValueError('fixture query failure')
                return {'_refresh_required':True}
            with self.subTest(failure=failure), patch('protocol.time.monotonic', clock.monotonic):
                result = fixture.wait(tick)
            self.assertIn('prepared_claim', result)
            self.assertNotIn('prompt_closed', fixture.channel.output.getvalue())

    def test_close_ack_claim_first_and_ack_before_reply_both_keep_purchase(self):
        for ack_first in (False, True):
            clock = Clock(); fixture = TickFixture(clock)
            def close(payload):
                claim = fixture.claim(at=EPOCH + 60_000)
                if ack_first: fixture.reply({'_prepared_close_ack':'claimed'})
                fixture.reply(claim)
                if not ack_first: fixture.reply({'_prepared_close_ack':'claimed'})
            fixture.on_close = close
            with self.subTest(ack_first=ack_first), patch('protocol.time.monotonic', clock.monotonic):
                result = fixture.wait(lambda:{'_refresh_required':True})
            self.assertIn('prepared_claim', result)

    def test_closed_ack_precedes_outer_authentication_and_late_claim_cannot_revive(self):
        clock = Clock(); fixture = TickFixture(clock)
        def close(payload):
            fixture.reply(fixture.claim(at=EPOCH + 300_001))
            fixture.reply({'_prepared_close_ack':'closed'})
        fixture.on_close = close
        with patch('protocol.time.monotonic', clock.monotonic), self.assertRaises(PromptRefresh):
            fixture.wait(lambda:{'_refresh_required':True})
        events = [json.loads(line) for line in fixture.channel.output.getvalue().splitlines()]
        self.assertTrue(events[-1]['prepared_close_handshake'])
        self.assertIsNone(fixture.channel._active_prepared_prompt)
        self.assertIsNone(fixture.channel._prepared_reply_admission)

    def test_foreign_ack_does_not_close_current_lease_and_disconnect_cancels(self):
        for disconnect in (False, True):
            clock = Clock(); fixture = TickFixture(clock)
            def close(payload):
                fixture.reply({'_prepared_close_ack':'closed'}, 'old-request')
                if disconnect:
                    fixture.channel.read_replies(io.StringIO(''))
                else: fixture.reply({'_prepared_close_ack':'closed'})
            fixture.on_close = close
            with self.subTest(disconnect=disconnect), patch('protocol.time.monotonic', clock.monotonic), \
                    self.assertRaises(Cancelled if disconnect else PromptRefresh):
                fixture.wait(lambda:{'_refresh_required':True})

    def test_missing_close_ack_fails_without_opening_another_prompt(self):
        clock = Clock(); fixture = TickFixture(clock); fixture.on_close = lambda payload:None
        with patch('protocol.time.monotonic', clock.monotonic), self.assertRaisesRegex(ValueError, '关闭确认'):
            fixture.wait(lambda:{'_refresh_required':True})
        self.assertEqual(fixture.channel.output.getvalue().count('"type": "prompt"'), 1)

    def test_seven_and_ten_minute_due_times_cross_five_minute_leases(self):
        for minutes, expected in ((7,420),(10,600)):
            clock = Clock(); flow = SimpleNamespace(model={'checkout':{'d':{'page':'Fulfillment'}}},
                _prepared_inventory_queried_at=0)
            observed = []
            handler = SimpleNamespace(prepared_inventory_interval_minutes=minutes)
            with self.subTest(minutes=minutes), patch.object(shop.interaction, 'handler', handler), \
                    patch('protocol.time.monotonic', clock.monotonic):
                for _ in range(3):
                    fixture = TickFixture(clock)
                    def tick():
                        observed.append(clock.now); flow._prepared_inventory_queried_at=clock.now
                        fixture.reply(fixture.claim())
                    try:
                        fixture.wait(tick, initial=shop.prepared_inventory_wait_delay(flow, now=clock.now),
                                     interval=minutes*60)
                        break
                    except PromptExpired: pass
            self.assertEqual(len(observed),1)
            self.assertAlmostEqual(observed[0],expected,delta=.002)


class PreparedWorkerTicksTests(TestCase):
    setUp = fixtures.PickupCheckoutTests.setUp
    tearDown = fixtures.PickupCheckoutTests.tearDown

    def test_tick_signin_or_expired_returns_before_any_nested_authentication(self):
        for path in ('https://secure11.www.apple.com.cn/shop/signIn?ssi=fixture',
                     'https://www.apple.com.cn/shop/sorry/session_expired'):
            flow = shop.Checkout(Mock())
            response = __import__('requests').Response(); response.status_code=200; response.url=path
            response._content=b'<script id="init_data">{"signIn":{}}</script>'
            flow.page=Mock(side_effect=lambda *a,**kw:flow.read_page(response,'tick fixture'))
            with self.subTest(path=path), patch.object(shop,'reauthenticate_prepared_once') as auth, \
                    patch.object(shop,'recover_expired_checkout') as recovery:
                result=shop.renew_prepared_pickup(flow,[('MK2M4CH/A',1)],{},self.service.checkout,
                    shop.checkout_config_hash(self.service.checkout),tick_only=True)
            self.assertEqual(result,{'_refresh_required':True})
            auth.assert_not_called();recovery.assert_not_called()

    def test_claim_after_fresh_get_yields_before_goods_read_or_stock_search(self):
        clock=Clock();fixture=TickFixture(clock)
        self.service.channel=fixture.channel
        flow=shop.Checkout(Mock())
        body={'checkout':{'d':{'page':'Fulfillment'},'fulfillment':{}}}
        def page(*args,**kwargs):
            flow.model=deepcopy(body);flow.current_response_body=deepcopy(body)
            fixture.reply(fixture.claim())
        flow.page=Mock(side_effect=page)
        def tick():
            return shop.renew_prepared_pickup(flow,[('MK2M4CH/A',1)],{},self.service.checkout,
                shop.checkout_config_hash(self.service.checkout),tick_only=True)
        with patch('protocol.time.monotonic',clock.monotonic), \
                patch.object(shop,'verify_prepared_physical_items') as goods, \
                patch('prepared_inventory.refresh_prepared_inventory') as search:
            result=fixture.wait(tick)
        self.assertIn('prepared_claim',result)
        flow.page.assert_called_once();goods.assert_not_called();search.assert_not_called()

    def test_late_wall_clock_claim_is_accepted_only_with_channel_admission(self):
        clock=Clock();fixture=TickFixture(clock)
        fixture.channel.account_id=self.service.channel.account_id
        fixture.channel.task_id=self.service.channel.task_id
        self.service.channel=fixture.channel
        self.service.purchase_selection=('MK2M4CH/A',1)
        self.service.prepared_inventory_interval_minutes=1
        self.config['pickup']['store_id']='R580'
        checkout_options.save_config(self.service.checkout,self.config)
        def tick():
            fixture.reply(fixture.claim());clock.now=310
            return {'success':True,'preparation_stage':'fulfillment','review_verified':False,'order_ready':False}
        self.service._prepared_wait_tick=tick
        with patch('protocol.time.monotonic',clock.monotonic),patch('service.time.time',clock.wall):
            accepted=self.service.wait_prepared_pickup({'preparation_stage':'fulfillment','review_verified':False,
                'order_ready':False,'pickup_store_id':'R580'},timeout=300)
        self.assertTrue(accepted)
        self.assertTrue(self.service._prepared_claim_timely_admission)
        self.assertIsNotNone(self.service._prepared_claim)

    def test_claim_during_tick_get_continues_same_flow_once_without_maintenance_search(self):
        clock=Clock();fixture=TickFixture(clock)
        fixture.channel.account_id=self.service.channel.account_id
        fixture.channel.task_id=self.service.channel.task_id
        self.service.channel=fixture.channel
        self.service.purchase_selection=('MK2M4CH/A',1)
        self.service.preparing=self.service.keep_prepared_pickup=True
        self.service.prepared_inventory_interval_minutes=1
        self.config['pickup']['store_id']='R580';checkout_options.save_config(self.service.checkout,self.config)
        flow=fixtures.PickupFlow();flow.session=requests.Session();self.addCleanup(flow.session.close)
        body=fixtures.fulfillment_model(mixed=False)
        body['checkout']['fulfillment']['item-0']=deepcopy(flow.item)
        flow.model=deepcopy(body);flow.current_response_body=deepcopy(body)
        flow._prepared_inventory_queried_at=0
        def page(*args,**kwargs):
            flow.model=deepcopy(body);flow.current_response_body=deepcopy(body)
            if flow.page.call_count==1: fixture.reply(fixture.claim())
        flow.page=Mock(side_effect=page)
        with patch('protocol.time.monotonic',clock.monotonic),patch('service.time.time',clock.wall), \
                patch.object(shop,'monotonic',clock.monotonic),patch.object(shop,'save_auth'), \
                patch.object(shop,'get_payment_qr',return_value={}),patch.object(shop,'add_to_bag') as add, \
                patch('prepared_inventory.refresh_prepared_inventory') as refresh:
            result=shop.resume_prepared_pickup(flow,[('MK2M4CH/A',1)],{'success':True,'status':'waiting_stock',
                'preparation_stage':'fulfillment','review_verified':False,'order_ready':False})
        self.assertEqual(result['status'],'pending_payment')
        self.assertEqual(flow.submissions,1)
        refresh.assert_not_called();add.assert_not_called()
        self.assertEqual(clock.now,60)

    def test_rebuilt_checkout_generation_discards_captured_review_slot_and_amount(self):
        self.service.purchase_selection=('MK2M4CH/A',1)
        self.service.order_details={'store':'测试门店','store_id':'R580','total':'RMB 15,999'}
        self.service.pickup_slot_selected={'old':True}
        flow=fixtures.PickupFlow()
        def wait(*args,**kwargs):
            if wait.calls==0:
                wait.calls+=1;self.service.prepared_expired=True;return False
            return True
        wait.calls=0
        def renew(*args,**kwargs):
            flow._expired_checkout_recovery_generation=1
            self.service.order_details=None;self.service.pickup_slot_selected=None
            return {'preparation_stage':'fulfillment','review_verified':False,'order_ready':False}
        with patch.object(shop,'save_auth'),patch.object(self.service,'wait_prepared_pickup',side_effect=wait), \
                patch.object(shop,'renew_prepared_pickup',side_effect=renew), \
                patch.object(self.service,'execute_prepared_purchase',side_effect=lambda operation,**kw:operation()), \
                patch.object(shop,'_resume_prepared_checkout',return_value={'success':True}) as resume:
            shop.resume_prepared_pickup(flow,[('MK2M4CH/A',1)],
                {'preparation_stage':'review','review_verified':True,'order_ready':True})
        self.assertEqual(resume.call_args.args[2:5],({},False,False))

    def test_review_skip_has_no_tight_callback_loop(self):
        clock=Clock();fixture=TickFixture(clock);seen=[]
        flow=SimpleNamespace(model={'checkout':{'d':{'page':'Review'}}},_prepared_inventory_queried_at=0)
        handler=SimpleNamespace(prepared_inventory_interval_minutes=1)
        def tick():
            seen.append(clock.now)
            if len(seen)==4:fixture.reply(False)
        with patch.object(shop.interaction,'handler',handler),patch('protocol.time.monotonic',clock.monotonic):
            fixture.wait(tick,initial=shop.prepared_inventory_wait_delay(flow,now=0),interval=60)
        self.assertEqual(seen,[60,120,180,240])

    def test_due_unsupported_query_backs_off_without_faking_actual_search_time(self):
        clock=Clock()
        self.service.purchase_selection=('MK2M4CH/A',1)
        self.service.prepared_inventory_interval_minutes=1
        flow=fixtures.PickupFlow()
        flow._prepared_inventory_queried_at=0
        body=fixtures.fulfillment_model(mixed=False)
        body['checkout']['fulfillment']['item-0']=deepcopy(flow.item)
        body['checkout']['fulfillment']['fulfillmentOptions']['d']['selectFulfillmentLocation']='RETAIL'
        flow.model=deepcopy(body);flow.current_response_body=deepcopy(body)
        def page(*args,**kwargs):
            flow.model=deepcopy(body);flow.current_response_body=deepcopy(body)
        flow.page=Mock(side_effect=page)
        def wait(*args,**kwargs):
            clock.now=60
            refreshed=self.service._prepared_wait_tick()
            self.assertEqual(refreshed['prepared_inventory_refresh']['state'],'skipped')
            self.assertEqual(flow._prepared_inventory_queried_at,0)
            self.assertAlmostEqual(self.service._prepared_wait_tick_delay(),60)
            for tick in range(1,100):
                clock.now=60+tick/1000
                self.assertIsNone(self.service._prepared_wait_tick())
            self.service.prepared_expired=False
            return False
        with patch.object(shop,'monotonic',clock.monotonic),patch('prepared_inventory.monotonic',clock.monotonic), \
                patch.object(shop,'save_auth'),patch.object(self.service,'wait_prepared_pickup',side_effect=wait):
            result=shop.resume_prepared_pickup(flow,[('MK2M4CH/A',1)],
                {'preparation_stage':'fulfillment','review_verified':False,'order_ready':False})
        self.assertEqual(result['status'],'preparation_ended')
        flow.page.assert_called_once()
