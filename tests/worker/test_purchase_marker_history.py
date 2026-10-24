"""Real purchase_sku and strong-session gate with synthetic network/actions."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
import duo_killer
import interaction
from apple_login import browser_profile_path


class PurchaseMarkerHistoryTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        for name,value in (('ROOT',self.root),('AUTH_PATH',self.root/'auth.json')):
            mutation=patch.object(duo_killer,name,value);mutation.start();self.addCleanup(mutation.stop)
        handler=patch.object(interaction,'handler',SimpleNamespace(ensure_auth_file=lambda:False, automatic_checkout=False));handler.start();self.addCleanup(handler.stop)
        self.config={'account':'synthetic@example.test','cookies':{
            'account_session':[{'name':name,'value':'synthetic','domain':'.apple.com.cn',
                               'path':'/','secure':True,'expires':None}
                              for name in ('as_ltn_cn','shld_bt_ck')],
            'server_managed':[],'with_expiry':[],'expiry_unknown':[]}}
        self.auth=duo_killer.AUTH_PATH
        self.auth.write_text(json.dumps(self.config),encoding='utf-8')
        profile=browser_profile_path(self.auth);profile.mkdir(parents=True)
        self.marker=profile/'.authx-541'
        network=patch('requests.Session.request',side_effect=AssertionError('No real network allowed'))
        network.start();self.addCleanup(network.stop)

    def test_real_purchase_old_marker_and_no_marker_have_identical_buy_actions(self):
        traces=[]
        for marked in (False,True):
            if marked:self.marker.touch()
            else:self.marker.unlink(missing_ok=True)
            with patch.object(duo_killer.Checkout,'page') as read, \
                    patch.object(duo_killer,'check_login',return_value={'logged_in':True}) as check, \
                    patch.object(duo_killer,'login') as login, \
                    patch.object(duo_killer,'add_to_bag') as add, \
                    patch.object(duo_killer,'pay_bag',return_value={'success':True,'status':'pending_payment'}) as pay:
                result=duo_killer.purchase_sku('MK2P4CH',quantity=1)
            self.assertEqual(result['status'],'pending_payment')
            read.assert_called_once_with(duo_killer.BASE_URL+'/shop/bag','加购前购物袋')
            check.assert_called_once();login.assert_not_called();add.assert_called_once();pay.assert_called_once()
            self.assertIs(add.call_args.kwargs['flow'],pay.call_args.kwargs['flow'])
            traces.append((add.call_args.args,add.call_args.kwargs['quantity'],add.call_args.kwargs['set_total']))
        self.assertEqual(traces[0],traces[1])
        self.assertTrue(self.marker.exists())

    def test_real_purchase_current_541_fails_once_before_any_add_or_checkout(self):
        self.marker.touch()
        response=requests.Response();response.status_code=541
        response.url='https://www.apple.com.cn/shop/bag'
        with patch.object(duo_killer,'request_get',return_value=response) as request, \
                patch.object(duo_killer,'login') as login, patch.object(duo_killer,'add_to_bag') as add, \
                patch.object(duo_killer,'pay_bag') as pay:
            with self.assertRaises(requests.HTTPError):duo_killer.purchase_sku('MK2P4CH',quantity=1)
        request.assert_called_once();login.assert_not_called();add.assert_not_called();pay.assert_not_called()
        self.assertTrue(self.marker.exists())

    def test_missing_cookie_still_logs_in_once_and_old_marker_does_not_reject_fresh_session(self):
        self.marker.touch()
        missing=json.loads(json.dumps(self.config));missing['cookies']['account_session'].pop()
        self.auth.write_text(json.dumps(missing),encoding='utf-8')
        with requests.Session() as session:
            duo_killer.load_auth(session,missing)
            with patch.object(duo_killer,'login',side_effect=lambda **options:self.auth.write_text(
                    json.dumps(self.config),encoding='utf-8')) as login:
                self.assertTrue(duo_killer.ensure_strong_login(session))
            login.assert_called_once_with(sms=False)
        self.assertTrue(self.marker.exists())
