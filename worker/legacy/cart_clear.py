"""Confirm a live cart snapshot; delete only those rows, never place an order."""
import requests
import duo_killer as shop


def rows(flow):
    cart = flow.require('shoppingCart')
    if not isinstance(cart, dict) or not isinstance(cart.get('items'), dict):
        raise ValueError('官网未返回完整购物袋，未确认清空。')
    result = []
    for key, item in cart['items'].items():
        if not key.startswith('item-'):
            continue
        if not isinstance(item, dict):
            raise ValueError('购物袋商品数据不完整，未继续移除。')
        sku = shop.field(item, 'd.sku')
        quantity = shop.field(item, 'itemQuantity.d.quantity')
        if not isinstance(sku, str) or type(quantity) is not int or quantity < 1:
            raise ValueError('购物袋商品数据不完整，未继续移除。')
        result.append({'id': key, 'sku': sku, 'quantity': quantity,
                       'name': shop.order_text(shop.field(item, 'd.name') or shop.field(item, 'd.productName')),
                       'subtotal': shop.order_text(shop.field(item, 'd.totalPrice'))})
    flag = shop.field(cart, 'd.hasItems')
    if type(flag) is bool and flag != bool(result):
        raise ValueError('购物袋状态与商品数据不一致，未确认清空。')
    return result


def fingerprint(items):
    return sorted((item['id'], item['sku'], item['quantity']) for item in items)


def clear_bag(session, channel):
    flow = shop.Checkout(session)
    def read():
        flow.page(shop.BASE_URL + '/shop/bag', '核对实时购物袋', fresh=True)
        return rows(flow)
    original = read()
    if not original:
        return {'success': True, 'status': 'bag_cleared', 'items': [], 'removed_count': 0, 'message': '官网已确认购物袋为空。'}
    for row in original:
        if not shop.field(flow.model, 'shoppingCart.items.' + row['id'] + '.a.delete.url'):
            raise ValueError('官网未提供商品移除入口，未清空购物袋。')
    if channel.prompt('confirm', '确认清空购物袋', details={
            'operation': 'clear_bag', 'items': original,
            'total': shop.order_text(shop.field(flow.model, 'shoppingCart.summary.d.total'))}) is not True:
        return {'success': False, 'status': 'bag_clear_aborted', 'items': original,
                'removed_count': 0, 'message': '已保留购物袋，未移除商品。'}
    remaining = read()
    removed = 0
    def stopped(message, status='bag_clear_changed'):
        return {'success': False, 'status': status, 'items': remaining,
                'removed_count': removed, 'message': message}
    if fingerprint(remaining) != fingerprint(original):
        return stopped('确认期间购物袋已变化，未发送移除；请重新核对清单。')
    expected = list(original)
    for row in original:
        channel.checkpoint()
        if fingerprint(remaining) != fingerprint(expected):
            return stopped('购物袋在清空过程中发生变化，已停止，不会移除新加入或数量变化的商品。')
        module = 'shoppingCart.items.' + row['id']
        if not shop.field(flow.model, module + '.a.delete.url'):
            return stopped('官网商品移除入口已变化，已停止；请重新读取购物袋。')
        try:
            flow.action(module, 'delete')  # Once per confirmed row, never replay.
        except (requests.RequestException, ValueError):
            shop.interaction.report_stage('商品移除结果不明，正在读取购物袋；不会重复发送移除')
        remaining = read()
        if any(item['id'] == row['id'] for item in remaining):
            return stopped('官网尚未确认商品移除，已停止；请查询购物袋，不会自动重复移除。', 'bag_clear_unconfirmed')
        removed += 1
        expected = [item for item in expected if item['id'] != row['id']]
    if remaining:
        return stopped('确认清单外还有商品，已保留并停止；未将购物袋报告为空。')
    return {'success': True, 'status': 'bag_cleared', 'items': [], 'removed_count': removed,
            'message': f'官网已确认购物袋为空，已移除 {removed} 个商品条目；未创建订单。'}
