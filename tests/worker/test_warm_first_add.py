import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests
import duo_killer as shop
import interaction

class WarmFirstAddTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        self.root=Path(td.name)
        root=patch.object(shop,'ROOT',self.root);root.start();self.addCleanup(root.stop)
        handler=patch.object(interaction,'handler',SimpleNamespace(automatic_checkout=True));handler.start();self.addCleanup(handler.stop)
        self.session=requests.Session();self.addCleanup(self.session.close)
        for name in ('as_ltn_cn','shld_bt_ck'):
            self.session.cookies.set(name,'fixture',domain='.apple.com.cn',path='/')
        self.item={'d':{'sku':'MLH74FE/A'},'itemQuantity':{'d':{'quantity':1}}}
        self.flow=SimpleNamespace(session=self.session,page=Mock(),require=Mock(return_value={'items':{'item-1':self.item}}))
        self.cache=self.root/'warm_empty_bag.json'
        self.write()
    def write(self,**changes):
        data={'created_at':time.time(),'empty':True,'public_key':'public-fixture','fingerprint':shop.warm_cart_fingerprint(self.session)}
        data.update(changes);shop.write_private_json(self.cache,data)
    def test_empty_bag_requires_explicit_flag_and_accepts_metadata_only_items(self):
        flow=Mock()
        flow.page_response.text='<meta name="globalnav-store-key" content="public-fixture">'
        def load(session,data):session.cookies.update(self.session.cookies)
        path=self.root/'auth.json';path.write_text('{}')
        for flag,items,expected in ((False,{'d':{},'view':{}},True),
                                    (True,{'d':{}},False),(None,{},False),
                                    (False,{'item-1':self.item},False)):
            with self.subTest(flag=flag,items=items):
                flow.require.return_value={'d':{'hasItems':flag},'items':items}
                with patch.object(shop,'AUTH_PATH',path),patch.object(shop,'load_auth',side_effect=load), \
                        patch.object(shop,'save_auth'),patch.object(shop,'Checkout',return_value=flow):
                    self.assertEqual(shop.warm_empty_bag(),expected)
                self.assertEqual(self.cache.exists(),expected)
    def test_single_use_cache_still_reads_live_bag_and_checks_entire_contents(self):
        def add(*args,**kwargs):
            self.assertFalse(self.cache.exists())
            return ''
        with patch.object(shop,'_add_one_to_bag',side_effect=add) as add:
            self.assertTrue(shop.try_warm_first_add(self.flow,'MLH74FE/A',1))
            self.assertFalse(shop.try_warm_first_add(self.flow,'MLH74FE/A',1))
        add.assert_called_once()
        self.flow.page.assert_called_once_with(shop.BASE_URL+'/shop/bag','首次加购后购物袋')
    def test_expired_changed_session_future_or_nonempty_cache_falls_back_without_add(self):
        for changes in ({'created_at':time.time()-121},{'created_at':time.time()+60},{'fingerprint':'0'*64},{'empty':False}):
            with self.subTest(changes=changes):
                self.write(**changes)
                with patch.object(shop,'_add_one_to_bag') as add:
                    self.assertFalse(shop.try_warm_first_add(self.flow,'MLH74FE/A',1))
                    add.assert_not_called()
    def test_extra_items_or_duplicate_quantity_stops_without_replay(self):
        for cart in ({'items':{'a':self.item,'b':{'d':{'sku':'OTHER/A'},'itemQuantity':{'d':{'quantity':1}}}}},
                     {'items':{'a':{'d':{'sku':'MLH74FE/A'},'itemQuantity':{'d':{'quantity':2}}}}}):
            self.write();self.flow.require.return_value=cart
            with patch.object(shop,'_add_one_to_bag',return_value='') as add,self.assertRaisesRegex(ValueError,'未结算'):
                shop.try_warm_first_add(self.flow,'MLH74FE/A',1)
            add.assert_called_once();self.assertFalse(self.cache.exists())
    def test_ambiguous_network_failure_consumes_cache_before_write(self):
        with patch.object(shop,'_add_one_to_bag',side_effect=requests.Timeout()) as add,self.assertRaises(requests.Timeout):
            shop.try_warm_first_add(self.flow,'MLH74FE/A',1)
        add.assert_called_once();self.assertFalse(self.cache.exists())
    def test_missing_credentials_or_other_quantity_never_uses_fast_path(self):
        self.session.cookies.clear()
        with patch.object(shop,'_add_one_to_bag') as add:
            self.assertFalse(shop.try_warm_first_add(self.flow,'MLH74FE/A',1))
            self.assertFalse(shop.try_warm_first_add(self.flow,'MLH74FE/A',2))
            add.assert_not_called()
