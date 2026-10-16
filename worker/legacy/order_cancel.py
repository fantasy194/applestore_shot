"""Use live Apple order actions; never infer success from an HTTP 200."""
import re
from time import sleep
from urllib.parse import urlsplit

import requests
import duo_killer as shop
from order_access import account_detail_url, read_detail


def inspect_order(flow, number):
    if shop.field(flow.model, 'orderDetail.orderHeader.d.orderNumber') != number:
        raise ValueError('官网订单编号与待取消订单不符，未取消。')
    nodes = shop.field(flow.model, 'orderDetail.orderItems', {})
    items = [(key, node) for key, node in nodes.items() if key.startswith('orderItem-') and isinstance(node, dict)]
    if len(items) != 1:
        raise ValueError('多商品订单请在官网选择取消范围，未取消。')
    key, node = items[0]
    status = shop.field(node, 'orderItemStatusTracker.d.currentStatus', '')
    canceled = bool(re.fullmatch(r'CANCEL(?:ED|LED)(?:_[A-Z_]+)?', str(status)))
    return 'orderDetail.orderItems.' + key, node, canceled


def locate_order(session, number, listing, contact, *, allow_lookup=True):
    """Read the observed account detail route, refresh, then try official lookup.

    The form may associate a matching order with this logged-in account. Never
    invent an account route, submit a separate linking action, or replay lookup.
    """
    order = next((o for o in listing['orders'] if o['order_number'] == number), None)
    if not order:
        direct = account_detail_url(listing, number)
        if direct:
            try:
                detail = read_detail(session, number, direct)
            except (requests.RequestException, ValueError):
                pass
            else:
                inspect_order(detail, number)
                return detail, detail.url, listing.get('lookup_url'), None
    if not order:
        sleep(.5)
        listing = shop.order_list(session, fresh=True)
        order = next((o for o in listing['orders'] if o['order_number'] == number), None)
    flow = shop.Checkout(session)
    lookup_url = listing.get('lookup_url')
    lookup_url = flow.apple_url(lookup_url) if lookup_url else None
    lookup_reason = None
    if not order and lookup_url and allow_lookup:
        email = contact.get('emailAddress', '')
        phone = contact.get('fullDaytimePhone', '')
        if (urlsplit(lookup_url).path == '/shop/order/link/verify'
                and isinstance(email, str) and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)
                and isinstance(phone, str) and re.fullmatch(r'\d{7,15}', phone)):
            flow.page(lookup_url, '通过官网入口核对未列出的订单')
            module = 'orderLinkModule'
            action = shop.field(flow.model, module + '.a.submit.url')
            bindings = shop.field(flow.model, module + '.b', {})
            if (action and urlsplit(flow.apple_url(action)).path == '/shop/order/linkx/verify'
                    and all(shop.field(bindings, key + '.submit') is True
                            for key in ('orderNumber', 'phoneNumber', 'emailAddress'))):
                shop.interaction.report_stage('正在用本地联系方式通过官网验证查找订单；尚未发送取消')
                try:
                    flow.action(module, 'submit', {
                        module + '.orderNumber': number, module + '.phoneNumber': phone,
                        module + '.emailAddress': email})
                except (requests.RequestException, ValueError):
                    # Lookup may have succeeded remotely. Do not replay its POST.
                    pass
                if isinstance(shop.field(flow.model, 'orderDetail'), dict):
                    inspect_order(flow, number)
                    return flow, flow.url, lookup_url, None
                alert = shop.field(flow.model, 'orderLinkModule.d.alert', '')
                if isinstance(alert, str) and 'already linked to an Apple ID' in alert:
                    # This is the actual server alert, not b.*.p validation
                    # templates. Do not expose the email embedded in its HTML.
                    lookup_reason = 'already_linked'
                listing = shop.order_list(session, fresh=True)
                order = next((o for o in listing['orders'] if o['order_number'] == number), None)
    if not order:
        return None, None, lookup_url, lookup_reason
    urls = {item.get('detail_url') for item in order['items']}
    if len(urls) != 1 or not next(iter(urls)):
        raise ValueError('订单详情入口不明确，请在官网取消。')
    url = flow.apple_url(next(iter(urls)))
    flow.page(url, '读取待取消订单')
    inspect_order(flow, number)
    return flow, url, lookup_url, None


def cancel_order(session, number, channel, before_submit, *, allow_submit=True, contact=None):
    if not isinstance(number, str) or not re.fullmatch(r'W\d{6,20}', number):
        raise ValueError('订单编号无效。')
    listing = shop.order_list(session)
    flow, url, lookup_url, lookup_reason = locate_order(session, number, listing, contact or {}, allow_lookup=allow_submit)
    if flow is None:
        return {'success': False, 'status': 'cancellation_lookup_required', 'order_number': number,
                'detail_url': lookup_url, 'lookup_reason': lookup_reason,
                'message': ('官网提示该订单号已关联 Apple ID，但当前账号列表未列出，未取得订单详情。'
                            '请核对下单时的账号与官网订单详情；尚未发送取消。'
                            if lookup_reason == 'already_linked' else
                            '刷新后官网账号列表仍未列出此订单，自动查找也未取得可核对的详情；这不代表订单不存在。'
                            '尚未发送取消，请使用官网“查找订单”入口，凭订单号、下单手机号和邮箱核对。')}
    path, node, canceled = inspect_order(flow, number)
    result = {'success': False, 'order_number': number, 'detail_url': url}
    if canceled:
        return {**result, 'success': True, 'status': 'order_cancelled', 'message': '官网已确认订单取消。'}
    if shop.field(node, 'orderItemStatusTracker.d.currentStatus') == 'PAYMENT_EXPIRED_STORED_ORDER':
        return {**result, 'success': True, 'status': 'order_expired', 'message': '官网已确认此订单超过付款期限，已过期；无需再次取消。'}
    if not allow_submit:
        return {**result, 'status': 'cancellation_unknown', 'message': '上一笔取消结果尚未确认，已仅查询状态；请稍后查询或在官网核对，不会重复发送取消。'}
    if shop.field(node, 'd.cancelEligible') is not True:
        return {**result, 'status': 'cancellation_unavailable', 'message': '官网当前不允许取消此订单，请到订单详情核对。'}
    details = shop.field(node, 'orderItemDetails.d', {})
    if details.get('quantity') != 1 or shop.field(node, 'orderItemStatusTracker.d.currentStatus') != 'PAYMENT_DUE_STORED_ORDER':
        return {**result, 'status': 'cancellation_unavailable', 'message': '当前支持单商品、1 件、待付款订单的取消；其他订单请在官网办理。'}
    flow.action(path, 'cancelItem')  # Official order-detail.js opens cancelOverlay.
    inspect_order(flow, number)
    overlay = path + '.cancelOverlay'
    data = shop.field(flow.model, overlay + '.d', {})
    if (not data or data.get('paymentComplete') is not False or data.get('promoOrder')
            or data.get('splitTender') or data.get('hasTradeUpItem') or data.get('partiallyShipped')
            or not shop.field(flow.model, overlay + '.a.continue.url')):
        return {**result, 'status': 'cancellation_unavailable', 'message': '官网取消确认流程需要额外处理，未提交取消；请在官网继续。'}
    payload = {}
    reasons = data.get('cancelReasons') or []
    if reasons:
        labels = [r['label'] for r in reasons if isinstance(r, dict) and isinstance(r.get('label'), str) and r['label']]
        if len(labels) != len(reasons):
            raise ValueError('官网取消原因数据无法识别，未取消。')
        reason = channel.prompt('select', '选择取消原因', options=[{'value': label, 'label': label} for label in labels])
        if reason not in labels:
            raise ValueError('取消原因无效，未取消。')
        payload[overlay + '.selectedCancelReason'] = reason  # Official dropdown submits the label.
    if channel.prompt('confirm', '确认取消订单 ' + number, details={
            'operation': 'cancel_order', 'order_number': number,
            'items': [{'name': shop.order_text(details.get('productName') or details.get('itemShortName')),
                       'quantity': 1, 'subtotal': shop.order_text(details.get('totalPrice'))}],
            'total': shop.order_text(details.get('totalPrice'))}) is not True:
        return {**result, 'status': 'cancellation_aborted', 'message': '已放弃取消，原订单保留。'}
    before_submit()
    try:
        flow.action(overlay, 'continue', payload)  # Exactly once; never replay on timeout.
        for _ in range(2):
            flow.page(url, '核对取消结果')
            _, _, canceled = inspect_order(flow, number)
            if canceled:
                return {**result, 'success': True, 'status': 'order_cancelled', 'message': '官网已确认订单取消。'}
    except (requests.RequestException, ValueError):
        pass
    return {**result, 'status': 'cancellation_unknown', 'message': '取消请求已发送，但官网结果尚未确认；请查询订单或到官网核对，不要重复取消。'}
