"""Ordered pickup policies and verified official contacts; no network allowed."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import checkout_options as options
import duo_killer as shop
import pickup_options as pickup
from test_checkout_options import CONFIG
import test_pickup_checkout as pickup_fixtures
import test_prepared_pickup as prepared_fixtures
from test_checkout_options import fulfillment_model
from test_pickup_checkout import PickupFlow, signed_pickup_slots


STORES=[{'store_id':'R580','store_name':'测试门店','search':'200000'},
        {'store_id':'R581','store_name':'第二门店','search':'200001'}]


def policy(**extra):
    return {'selection_mode':'priority','stores':deepcopy(STORES),
            'contact_mode':'official','time_slot_rule':{'mode':'earliest','fallback':'earliest'},**extra}


def slots(store, start='10:00', end='10:15'):
    result=signed_pickup_slots()
    result['d']['storeId']=store
    data=result['dateTimeSlots']['d']
    data['storeSelected']=store
    data['pickUpDates']=[{'date':'2026-10-16','dayOfMonth':'16'}]
    row=deepcopy(data['timeSlotWindows'][1]['16'][0])
    row.update(SlotId='fixture-slot-'+store,signKey='private-sign-'+store,
               timeSlotValue='16-'+start+'-'+end,checkInStart=start,checkInEnd=end,Label=start+' – '+end)
    data['timeSlotWindows']=[{'16':[row]}]
    return result


class PolicyFlow(PickupFlow):
    def __init__(self, *, store_slots=None, disabled=None, official=None, masked=False,
                 wrong_review=False, identity=False):
        super().__init__()
        self.selected='R580'
        self.store_slots=store_slots or {}
        self.disabled=set(disabled or [])
        self.official=deepcopy(CONFIG['contact'] if official is None else official)
        self.masked,self.wrong_review,self.identity=masked,wrong_review,identity
    def action(self,module,name,data=None):
        if name=='select':self.selected=data[module+'.selectStore']
        super().action(module,name,data)
        if name=='checkout':
            self.model['checkout']['fulfillment']['pickupTab']['pickup']['storeLocator']['searchResults']['d']['retailStores']=[
                {'storeId':row['store_id'],'storeName':row['store_name'],
                 'storeDisabled':row['store_id'] in self.disabled} for row in STORES]
        elif name=='select':
            slot=self.store_slots.get(self.selected)
            if slot is not None:
                self.model['checkout']['fulfillment']['pickupTab']['pickup']['timeSlot']=deepcopy(slot)
        elif name=='continueFromFulfillmentToPickupContact':
            contact=self.model['checkout']['pickupContact']['selfPickupContact']
            contact['selfContact']['address']['d']=deepcopy(self.official)
            if self.masked:
                contact['selfContact']['address']['b']={key:{'masked':True,'submit':{'op':'NOT'}}
                    for key in ('emailAddress','fullDaytimePhone')}
            if self.identity:
                contact['nationalIdSelf']={'d':{'enabled':True,'nationalIdSelf':'••••'},
                    'b':{'nationalIdSelf':{'masked':True,'submit':{'op':'NOT'}}}}
        elif name=='continueFromBillingToReview':
            row=next(row for row in STORES if row['store_id']==self.selected)
            self.model['checkout']['companionBar']['orderDetails']['fulfillmentCompanionBar']['pickupSummary']['d']={
                'store_name':row['store_name'],'storeId':self.selected}
            contact=deepcopy(self.official)
            if self.wrong_review:contact['firstName']='其他人'
            self.model['checkout']['review']['pickupContactReview']={'pickupContactInfo':{'d':contact}}
        elif name=='continueFromPickupContactToBilling':
            prefix='checkout.pickupContact.selfPickupContact.selfContact.address.'
            self.official.update({key:data[prefix+key] for key in pickup.CONTACT_FIELDS if prefix+key in data})
        # This fixture supplies a new complete response model per action,
        # unlike the real wire's partial fragments. Never reuse its old object.
        self.current_response_body=deepcopy(self.model)


class PickupPolicyTests(TestCase):
    setUp=pickup_fixtures.PickupCheckoutTests.setUp
    tearDown=pickup_fixtures.PickupCheckoutTests.tearDown
    run_flow=pickup_fixtures.PickupCheckoutTests.run_flow
    review_flow=pickup_fixtures.PickupReviewTests.review_flow

    def enable(self, **extra):
        self.config={'pickup':policy(**extra)}
        options.save_config(self.service.checkout,self.config)

    def test_save_fixed_or_priority_preferences_without_local_contact(self):
        for preferences in ({'search':'200000','store_name':'测试门店'},policy()):
            result=self.service.run('address_save',{'pickup_only':True,'pickup':preferences})
            self.assertIn('pickup',result['config'])
        self.assertEqual(self.service.run('address_save',{'pickup_only':True,'pickup':policy(),
                         'contact':{'firstName':'保留'}})['config']['contact']['firstName'],'保留')
        with self.assertRaises(ValueError):
            self.service.run('address_save',{'pickup_only':True,'pickup':policy(),'contact':{'emailAddress':'invalid'}})

    def test_priority_validation_is_bounded_unique_and_rejects_contact_in_policy(self):
        for preferences in (policy(stores=[]),policy(stores=[STORES[0]]*2),policy(stores=STORES*16),
                            policy(contact={'firstName':'forbidden'}),policy(time_slot_rule={'mode':'window','start':'23:00','end':'01:00'})):
            with self.subTest(preferences=preferences),self.assertRaises(ValueError):pickup.validate_policy(preferences)
        self.assertEqual(pickup.validate_policy(policy())['selection_mode'],'priority')

    def test_disabled_first_store_selects_next_allowed_store_and_reports_actual_id(self):
        self.enable()
        flow=PolicyFlow(disabled=['R580'])
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['pickup_store_id'],'R581')
        self.assertEqual(result['pickup_store'],'第二门店')
        self.assertEqual([data[module+'.selectStore'] for module,name,data in flow.calls if name=='select'],['R581'])
        self.assertEqual(flow.submissions,0)

    def test_no_slot_first_store_uses_fresh_signature_for_next_store(self):
        self.enable()
        empty=slots('R580');empty['dateTimeSlots']['d']['timeSlotWindows']=[]
        flow=PolicyFlow(store_slots={'R580':empty,'R581':slots('R581')})
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['pickup_store_id'],'R581')
        submitted=next(data for _,name,data in flow.calls if name=='continueFromFulfillmentToPickupContact')
        prefix='checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.'
        self.assertEqual(submitted[prefix+'signKey'],'private-sign-R581')
        self.assertEqual(result['pickup_time_slot'],{'store_id':'R581','date':'2026-10-16',
                         'start':'10:00','end':'10:15','label':'10:00 – 10:15'})
        self.assertNotIn('signKey',json.dumps(result))
        self.assertNotIn('private-sign',json.dumps(result))
        self.assertNotIn('SlotId',json.dumps(result))

    def test_unknown_store_state_never_falls_back_or_submits(self):
        self.enable()
        for unknown in (None, 'false', 0):
            with self.subTest(state=unknown):
                flow=PolicyFlow()
                original=flow.action
                def action(module,name,data=None):
                    original(module,name,data)
                    if name=='search':
                        row=flow.current_response_body['checkout']['fulfillment']['pickupTab']['pickup']['storeLocator']['searchResults']['d']['retailStores'][0]
                        row['storeDisabled']=unknown
                flow.action=action
                with self.assertRaisesRegex(ValueError,'尚未明确确认预选门店'):
                    self.run_flow(flow,prepare=True)
                self.assertEqual(sum(name=='search' for _,name,_ in flow.calls),1)
                self.assertFalse(any(name=='select' for _,name,_ in flow.calls))
                self.assertEqual(flow.submissions,0)

    def test_window_stop_tries_next_store_while_earliest_fallback_keeps_priority(self):
        for fallback,wanted in [('stop','R581'),('earliest','R580')]:
            with self.subTest(fallback=fallback):
                self.enable(time_slot_rule={'mode':'window','start':'09:00','end':'11:00','fallback':fallback})
                flow=PolicyFlow(store_slots={'R580':slots('R580','15:00','15:15'),'R581':slots('R581')})
                self.assertEqual(self.run_flow(flow,prepare=True)['pickup_store_id'],wanted)

    def test_earliest_latest_and_window_only_choose_official_signed_candidates(self):
        flow=SimpleNamespace(model={'slot':signed_pickup_slots()})
        prefix='slot.dateTimeSlots.'
        for rule,wanted in [({'mode':'earliest'},'16-14:15-14:30'),({'mode':'latest'},'17-10:00-10:15'),
                            ({'mode':'window','start':'15:00','end':'15:30','fallback':'stop'},'16-15:00-15:15')]:
            self.assertEqual(pickup.time_slot_payload(flow,'slot','R580',rule=rule)[prefix+'timeSlotValue'],wanted)
        with self.assertRaises(pickup.NoPickupSlot):
            pickup.time_slot_payload(flow,'slot','R580',rule={'mode':'window','start':'08:00','end':'09:00','fallback':'stop'})
        self.assertEqual(pickup.time_slot_payload(SimpleNamespace(model={}), 'slot','R581'),{})
        self.assertIsNone(self.service.pickup_slot_selected)

    def test_all_unavailable_stops_and_never_uses_unlisted_store(self):
        self.enable()
        flow=PolicyFlow(disabled=['R580','R581'])
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['status'],'waiting_stock')
        self.assertFalse(result['order_ready'])
        self.assertFalse(any(name=='select' for _,name,_ in flow.calls))
        self.assertEqual(flow.submissions,0)
        flow=PolicyFlow(disabled=['R580','R581'])
        with self.assertRaisesRegex(ValueError,'预选门店目前均不可'):
            self.run_flow(flow)
        self.assertFalse(any(name=='select' for _,name,_ in flow.calls))
        self.assertEqual(flow.submissions,0)

    def test_one_region_explicitly_unavailable_still_checks_next_preselected_store(self):
        self.enable()
        flow=PolicyFlow()
        original=flow.action
        def action(module,name,data=None):
            original(module,name,data)
            if name=='search' and sum(name=='search' for _,name,_ in flow.calls)==1:
                node=flow.current_response_body['checkout']['fulfillment']['pickupTab']['pickup']['storeLocator']
                node['d']['isStoreSearchErrorPresent']=True
                node['m']=[{'level':'ERROR','text':'你所在的地区无货可取。请尝试重新搜索。'}]
        flow.action=action
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['status'],'prepared')
        self.assertEqual(result['pickup_store_id'],'R581')
        self.assertEqual(flow.submissions,0)

    def test_all_empty_official_slots_preserve_partial_preparation_only(self):
        self.enable()
        empty_a=slots('R580');empty_a['dateTimeSlots']['d']['timeSlotWindows']=[]
        empty_b=slots('R581');empty_b['dateTimeSlots']['d']['timeSlotWindows']=[]
        flow=PolicyFlow(store_slots={'R580':empty_a,'R581':empty_b})
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['status'],'waiting_stock')
        self.assertFalse(result['review_verified'])
        self.assertEqual(flow.submissions,0)
        self.assertIsNone(self.service.pickup_selected)
        self.assertIsNone(self.service.pickup_slot_selected)
        self.assertFalse(any(name=='continueFromFulfillmentToPickupContact' for _,name,_ in flow.calls))

    def test_official_contact_and_masked_bindings_are_reused_without_local_reentry(self):
        self.enable()
        actual={**CONFIG['contact'],'emailAddress':'t***@example.invalid','fullDaytimePhone':'138****0000'}
        flow=PolicyFlow(official=actual,masked=True,identity=True)
        self.assertEqual(self.run_flow(flow,prepare=True)['status'],'prepared')
        payload=next(data for _,name,data in flow.calls if name=='continueFromPickupContactToBilling')
        self.assertFalse(any(key.endswith(('.emailAddress','.fullDaytimePhone','.nationalIdSelf')) for key in payload))
        self.assertNotIn('••••',json.dumps(payload))
        self.service.channel.prompt.assert_not_called()

    def test_missing_official_field_only_requests_missing_value(self):
        self.enable()
        official=deepcopy(CONFIG['contact']);official.pop('emailAddress')
        flow=PolicyFlow(official=official)
        original=flow.action
        def action(module,name,data=None):
            original(module,name,data)
            if name=='continueFromBillingToReview':
                flow.model['checkout']['review']['pickupContactReview']['pickupContactInfo']['d']['emailAddress']='new@example.test'
        flow.action=action
        self.service.channel.prompt.side_effect=lambda kind,label,**fields:'new@example.test'
        self.assertEqual(self.run_flow(flow,prepare=True)['status'],'prepared')
        self.assertEqual(self.service.channel.prompt.call_count,1)
        self.assertIn('取货通知邮箱',self.service.channel.prompt.call_args.args[1])

    def test_changed_official_contact_at_review_stops_before_order(self):
        self.enable()
        flow=PolicyFlow(wrong_review=True)
        with self.assertRaisesRegex(ValueError,'取货联系人与本次确认'):
            self.run_flow(flow)
        self.assertEqual(flow.submissions,0)

    def test_temporary_priority_policy_ignores_monitor_anchor_and_never_changes_saved_config(self):
        before=self.service.checkout.read_bytes()
        desired=policy(stores=list(reversed(STORES)))
        flow=PolicyFlow()
        with patch.object(shop,'purchase_sku',side_effect=lambda *a,**kw:self.run_flow(flow)):
            result=self.service.run('purchase',{'sku':'MK2M4CH','quantity':1,'fulfillment':'RETAIL',
                'pickup_store_id':'R580','pickup_store_name':'测试门店','pickup_location':'200000',
                'pickup_policy':desired})
        self.assertEqual(result['pickup_store_id'],'R581')
        self.assertEqual(self.service.checkout.read_bytes(),before)
        self.assertEqual(flow.submissions,1)
        first=options.config_fingerprint(self.service.checkout)
        self.service.pickup_policy_override=pickup.validate_policy(policy())
        self.assertNotEqual(options.config_fingerprint(self.service.checkout),first)

    def test_network_or_unknown_slot_response_never_tries_another_store(self):
        self.enable()
        for enabled in (True, False):
            invalid=slots('R580');invalid['d'].update(storeId='R999',enableTimeSlotWindows=enabled)
            flow=PolicyFlow(store_slots={'R580':invalid,'R581':slots('R581')})
            with self.assertRaisesRegex(ValueError,'时段与所选门店不匹配'):
                self.run_flow(flow,prepare=True)
            self.assertEqual(sum(name=='select' for _,name,_ in flow.calls),1)
            self.assertEqual(flow.submissions,0)

    def test_missing_search_fragment_never_reuses_merged_store_list(self):
        self.enable()
        flow=PolicyFlow()
        original=flow.action
        def action(module,name,data=None):
            original(module,name,data)
            if name=='search':
                # The merged model still contains old, apparently available
                # stores. This wire response has no current searchResults.
                flow.current_response_body={'checkout':{'fulfillment':{'pickupTab':{'pickup':{
                    'storeLocator':{'d':{'searchInput':'200000'}}}}}}}
        flow.action=action
        with self.assertRaisesRegex(ValueError,'未返回可识别的自提门店列表'):
            self.run_flow(flow,prepare=True)
        self.assertEqual(sum(name=='search' for _,name,_ in flow.calls),1)
        self.assertFalse(any(name=='select' for _,name,_ in flow.calls))
        self.assertEqual(flow.submissions,0)

    def test_only_a_fresh_complete_official_review_can_supply_saved_contact(self):
        self.enable()
        flow=PolicyFlow()
        self.assertEqual(self.run_flow(flow,prepare=True)['status'],'prepared')
        self.service.pickup_contact_proof=None
        self.service.pickup_contact_accepted=False
        shop.confirm_order(flow.model,fresh_model=deepcopy(flow.model))
        self.assertTrue(self.service.pickup_contact_review_verified)
        self.service.pickup_contact_proof=None
        self.service.pickup_contact_accepted=False
        with self.assertRaisesRegex(ValueError,'尚未取得官网确认'):
            shop.confirm_order(flow.model,fresh_model={})
        self.assertEqual(flow.submissions,0)

    def test_official_missing_field_uses_local_supplement_before_prompt(self):
        self.enable()
        self.config['contact']=deepcopy(CONFIG['contact'])
        options.save_config(self.service.checkout,self.config)
        official=deepcopy(CONFIG['contact']);official.pop('emailAddress')
        flow=PolicyFlow(official=official)
        self.assertEqual(self.run_flow(flow,prepare=True)['status'],'prepared')
        self.service.channel.prompt.assert_not_called()
        payload=next(data for _,name,data in flow.calls if name=='continueFromPickupContactToBilling')
        self.assertEqual(payload['checkout.pickupContact.selfPickupContact.selfContact.address.emailAddress'],
                         CONFIG['contact']['emailAddress'])

    def test_prepared_appointment_reenters_selection_and_uses_new_signature(self):
        flow=prepared_fixtures.PreparedPickupTests.prepared_flow(self)
        self.service.pickup_slot_selected={'store_id':'R580','date':'2026-10-16',
                                          'start':'09:00','end':'09:15','label':'old slot'}
        original_page,original_action=flow.page.side_effect,flow.action
        def page(url,stage,*,fresh=False):
            original_page(url,stage,fresh=fresh)
            for model in (flow.model,flow.current_response_body):
                model['checkout']['review']['fulfillmentReview']['a']={'edit':{'url':'/fixture/edit-pickup'}}
        def action(module,name,data=None):
            if (module,name)==('checkout.review.fulfillmentReview','edit'):
                flow.calls.append((module,name,data))
                flow.update(fulfillment_model(mixed=False))
            else:
                original_action(module,name,data)
                if name=='select':
                    flow.model['checkout']['fulfillment']['pickupTab']['pickup']['timeSlot']=slots('R580')
        flow.page.side_effect,flow.action=page,action
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['status'],'pending_payment')
        self.assertEqual(flow.submissions,1)
        self.assertEqual(sum(name=='edit' for _,name,_ in flow.calls),1)
        payload=next(data for _,name,data in flow.calls if name=='continueFromFulfillmentToPickupContact')
        self.assertEqual(payload['checkout.fulfillment.pickupTab.pickup.timeSlot.dateTimeSlots.signKey'],
                         'private-sign-R580')
        self.assertEqual(result['pickup_time_slot']['start'],'10:00')
