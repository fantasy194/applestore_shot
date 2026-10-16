"""Read known orders without treating Apple's recent-order list as exhaustive."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
import duo_killer as shop

NUMBER = re.compile(r'W\d{6,20}')
DETAIL = re.compile(r'(/shop/order/detail/\d{1,20}/)(W\d{6,20})/?')


def account_detail_url(listing, number):
    """Use only the unanimous account route actually supplied by the server.

    A read of the resulting URL must still return the exact order number.
    This route is an address, not evidence of existence or permission.
    """
    if not isinstance(number, str) or not NUMBER.fullmatch(number):
        raise ValueError('订单编号无效。')
    bases = set()
    for order in listing.get('orders', []):
        for item in order.get('items', []):
            url = item.get('detail_url')
            if not isinstance(url, str):
                continue
            parsed = urlsplit(url)
            match = DETAIL.fullmatch(parsed.path)
            try:
                port = parsed.port
            except ValueError:
                return None
            if (parsed.scheme != 'https' or parsed.username or parsed.password
                    or port not in (None, 443)
                    or not re.fullmatch(r'secure\d*\.www\.apple\.com\.cn', parsed.hostname or '')
                    or not match or match[2] != order.get('order_number')):
                return None
            bases.add((parsed.scheme, parsed.netloc, match[1]))
    if len(bases) != 1:
        return None
    scheme, host, prefix = bases.pop()
    return urlunsplit((scheme, host, prefix + number, '', ''))


def read_detail(session, number, url):
    """GET only. Require an exact official detail response before trusting it."""
    flow = shop.Checkout(session)
    flow.page(flow.apple_url(url), '按订单号核对官网详情', fresh=True)
    if shop.field(flow.model, 'orderDetail.orderHeader.d.orderNumber') != number:
        raise ValueError('官网尚未返回匹配的订单详情；未将地址或 HTTP 成功当作订单存在。')
    return flow


def known_order_numbers(root, limit=20):
    """Local account-scoped references only; each must be verified remotely."""
    refs = []
    for path in (Path(root) / 'tasks').glob('*/payment.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            number = data.get('order_number')
            if isinstance(number, str) and NUMBER.fullmatch(number):
                refs.append((path.stat().st_mtime, number))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return list(dict.fromkeys(number for _, number in sorted(refs, reverse=True)))[:limit]


def detail_summary(flow, number):
    if shop.field(flow.model, 'orderDetail.orderHeader.d.orderNumber') != number:
        raise ValueError('订单详情编号不符。')
    items = []
    for key, node in shop.field(flow.model, 'orderDetail.orderItems', {}).items():
        if not key.startswith('orderItem-') or not isinstance(node, dict):
            continue
        data = shop.field(node, 'orderItemDetails.d', {})
        quantity = data.get('quantity')
        name = shop.order_text(data.get('productName') or data.get('itemShortName'))
        if not name or type(quantity) is not int or quantity < 1:
            raise ValueError('官网详情商品数据不完整。')
        status = shop.field(node, 'orderItemStatusTracker.d.currentStatus', '')
        if not isinstance(status, str):
            raise ValueError('官网详情订单状态无法识别。')
        label = ('订单已过期' if 'EXPIRED' in status else '已取消' if re.fullmatch(r'CANCEL(?:ED|LED)(?:_[A-Z_]+)?', status)
                 else '等待付款' if status == 'PAYMENT_DUE_STORED_ORDER'
                 else shop.order_text(shop.field(node, 'orderItemStatusTracker.d.statusDescription')))
        items.append({'name': name, 'quantity': quantity, 'detail_url': flow.url,
                      'delivery_info': label or '请在官网详情查看进度', 'status': status})
    if not items:
        raise ValueError('官网详情没有订单商品。')
    return {'order_number': number, 'items': items, 'verified_by': 'official_detail'}


def supplement_known_orders(session, listing, root):
    result = {**listing, 'orders': list(listing['orders'])}
    present = {order['order_number'] for order in listing['orders']}
    verified, unresolved = [], []
    for number in known_order_numbers(root):
        if number in present:
            continue
        url = account_detail_url(listing, number)
        if not url:
            unresolved.append(number)
            continue
        try:
            summary = detail_summary(read_detail(session, number, url), number)
        except (requests.RequestException, ValueError):
            unresolved.append(number)
            continue
        verified.append(summary)
        present.add(number)
    result['orders'] = verified + result['orders']
    result['count'] = len(result['orders'])
    result['detail_verified_count'] = len(verified)
    result['unverified_order_numbers'] = unresolved
    if verified:
        result['message'] = f'官网账号列表之外，已通过官方详情另行核实 {len(verified)} 笔本地已知订单。'
    if unresolved:
        result['message'] = result.get('message', '') + f' 另有 {len(unresolved)} 笔本地记录尚未核实，未作为有效订单加入。'
    return result
