import json
import unittest
from unittest.mock import patch
import requests
import duo_killer


def page(url, html):
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = html.encode()
    return response


class CartPublicConfigTests(unittest.TestCase):
    def test_current_bag_key_skips_product_get_but_fetches_new_token_and_adds_once(self):
        bag = page(duo_killer.BASE_URL+'/shop/bag', '<meta name="globalnav-store-key" content="fixture-key">')
        beacon = page(duo_killer.BASE_URL+'/shop/beacon/atb', '{}')
        added = page(duo_killer.BASE_URL+'/cn/shop/bag/add', json.dumps({'addedToBag': True}))
        with requests.Session() as session:
            session.cookies.set('as_atb','1|fixture|fresh-token',domain='.apple.com.cn',path='/')
            with patch.object(duo_killer,'request_get',side_effect=[beacon,added]) as request:
                self.assertEqual(duo_killer._add_one_to_bag(session,'MLH74FE/A',page=bag),'')
                self.assertEqual([c.args[1] for c in request.call_args_list],['加购令牌','执行加购'])
                self.assertEqual(request.call_args.kwargs['params']['apikey'],'fixture-key')

    def test_missing_or_untrusted_bag_key_falls_back_before_any_add(self):
        for url in [duo_killer.BASE_URL+'/shop/bag','https://example.com/shop/bag']:
            with self.subTest(url=url), requests.Session() as session:
                bag=page(url,'<html>no public key</html>')
                product=page(duo_killer.BASE_URL+'/shop/product/MLH74FE/A','<meta name="globalnav-store-key" content="fixture">')
                beacon=page(duo_killer.BASE_URL+'/shop/beacon/atb','{}')
                added=page(duo_killer.BASE_URL+'/cn/shop/bag/add','{"addedToBag":true}')
                session.cookies.set('as_atb','1|fixture|token',domain='.apple.com.cn',path='/')
                with patch.object(duo_killer,'request_get',side_effect=[product,beacon,added]) as request:
                    duo_killer._add_one_to_bag(session,'MLH74FE/A',page=bag)
                    self.assertEqual([c.args[1] for c in request.call_args_list],['商品页','加购令牌','执行加购'])
