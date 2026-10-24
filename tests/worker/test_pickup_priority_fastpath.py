"""Use only current complete pickup evidence; deduplicate identical searches."""
from copy import deepcopy
from unittest import TestCase

import checkout_options as options
import duo_killer as shop
from pickup_options import PickupUnavailable
import test_pickup_checkout as fixtures
from test_pickup_policy import PolicyFlow, policy


class PriorityFastPathTests(TestCase):
    setUp=fixtures.PickupCheckoutTests.setUp
    tearDown=fixtures.PickupCheckoutTests.tearDown
    run_flow=fixtures.PickupCheckoutTests.run_flow
    locator=fixtures.PickupFreshResponseTests.locator
    pickup=fixtures.PickupFreshResponseTests.pickup
    fresh_flow=fixtures.PickupFreshResponseTests.fresh_flow

    def test_current_complete_sku_store_signed_slot_skips_priority_search_and_select(self):
        rows=[{'store_id':'R580','store_name':'测试门店','search':'成都'}]
        self.config['pickup']=policy(stores=rows,contact_mode='local')
        options.save_config(self.service.checkout,self.config)
        flow=self.fresh_flow()
        options.fulfillment(flow,self.service.checkout)
        self.assertEqual([name for _,name,_ in flow.calls],['continueFromFulfillmentToPickupContact'])
        self.assertEqual(flow.calls[-1][2][self.pickup+'.timeSlot.dateTimeSlots.signKey'],'fixture-sign')

    def test_same_query_disabled_first_store_reuses_current_search_response(self):
        preferences=policy()
        preferences['stores'][1]['search']=preferences['stores'][0]['search']
        options.save_config(self.service.checkout,{'pickup':preferences})
        flow=PolicyFlow(disabled=['R580'])
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['pickup_store_id'],'R581')
        self.assertEqual(sum(name=='search' for _,name,_ in flow.calls),1)
        self.assertEqual([data[module+'.selectStore'] for module,name,data in flow.calls if name=='select'],['R581'])
        self.assertEqual(flow.submissions,0)

    def test_different_queries_are_not_coalesced_without_current_complete_evidence(self):
        options.save_config(self.service.checkout,{'pickup':policy()})
        flow=PolicyFlow(disabled=['R580'])
        result=self.run_flow(flow,prepare=True)
        self.assertEqual(result['pickup_store_id'],'R581')
        self.assertEqual(sum(name=='search' for _,name,_ in flow.calls),2)

    def test_mismatched_current_sku_cannot_skip_priority_search_from_merged_model(self):
        rows=[{'store_id':'R580','store_name':'测试门店','search':'成都'}]
        self.config['pickup']=policy(stores=rows,contact_mode='local')
        options.save_config(self.service.checkout,self.config)
        flow=self.fresh_flow()
        shop.field(flow.current_response_body,'checkout.companionBar.orderDetails')['item-0']['d']['sku']='OTHER/A'
        with self.assertRaises(ValueError):options.fulfillment(flow,self.service.checkout)
        self.assertEqual([name for _,name,_ in flow.calls],['search'])
