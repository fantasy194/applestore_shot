"""Recover only delivery/address/payment preparation, never add or place an order."""
import logging
from time import perf_counter, sleep
from urllib.parse import urlsplit

import requests
import interaction

log = logging.getLogger('duo_killer')
STEPS = {
    '送货': ('Fulfillment', ('Shipping', 'Billing', 'Review'), 'fulfillment', 'continueFromFulfillmentToShipping'),
    '自提': ('Fulfillment', ('PickupContact', 'Billing', 'Review'), 'fulfillment', 'continueFromFulfillmentToPickupContact'),
    '取货联系人': ('PickupContact', ('Shipping', 'Billing', 'Review'), 'pickupContact', 'continueFromPickupContactToBilling'),
    '地址': ('Shipping', ('Billing', 'Review'), 'shipping', 'continueFromShippingToBilling'),
    '支付方式': ('Billing', ('Review',), 'billing', 'continueFromBillingToReview'),
}


class CheckoutActionError(ValueError):
    def __init__(self, message, *, recoverable=False, retry_after=0):
        super().__init__(message)
        self.recoverable = recoverable
        self.retry_after = retry_after


def recover_step(flow, label, operation):
    automatic = getattr(interaction.handler, 'automatic_checkout', False) is True
    if not automatic:
        return operation()
    source, advanced, section, action = STEPS[label]
    deadline = perf_counter() + 30
    for attempt in range(3):
        try:
            return operation()
        except (requests.Timeout, requests.ConnectionError, CheckoutActionError) as error:
            if isinstance(error, CheckoutActionError) and not error.recoverable:
                raise
            # Stop sending forms immediately. All following probes are fresh GETs.
            interaction.report_stage(f'{label}响应异常，正在核对官网当前状态')
            log.warning('[结算恢复] %s异常=%s；先查状态，不直接重放。', label, type(error).__name__)
            delay = max((.25, .75, 1)[attempt], getattr(error, 'retry_after', 0))
            if perf_counter() + delay >= deadline:
                raise ValueError(f'{label}恢复等待超过预算；未创建订单。') from error
            sleep(delay)
            # Observe twice before retrying a timed-out write: an earlier write
            # may still be finishing. page() replaces all tokens and page data.
            for observation in range(2):
                flow.page(flow.apple_url('/shop/checkout'), f'{label}失败后核对结算状态', fresh=True)
                path = urlsplit(flow.url).path
                checkout = flow.model.get('checkout', {})
                current = checkout.get('d', {}).get('page')
                if path != '/shop/checkout' or not isinstance(current, str):
                    raise ValueError(f'{label}恢复未取得明确结算步骤；已停止，未重试。') from error
                if current in advanced:
                    node = checkout.get('pickupContact' if current == 'PickupContact' else current.lower())
                    if not isinstance(node, dict):
                        raise ValueError(f'{label}恢复页面状态不完整；已停止，未重试。') from error
                    if label == '取货联系人':
                        from checkout_options import confirm_pickup_contact_submission
                        if not confirm_pickup_contact_submission(flow, fresh=True):
                            raise ValueError('官网已推进结算，但未能确认本次取货联系人受理依据；未重复发送表单。') from error
                    log.info('[结算恢复] %s已完成，官网当前步骤=%s，跳过重复提交。', label, current)
                    return
                if current != source:
                    raise ValueError(f'{label}恢复发现结算步骤变化；已停止，未重试。') from error
                node = checkout.get(section, {})
                state = node.get('d', {})
                if (state.get('error') or state.get('errors') or state.get('hasError') is True
                        or state.get('enableContinue') is False
                        or node.get('continueButton', {}).get('d', {}).get('enabled') is False
                        or not node.get('a', {}).get(action, {}).get('url')
                        or not flow.headers.get('x-aos-stk')):
                    raise ValueError(f'{label}恢复页面不允许继续；已停止，未重试。') from error
                if observation == 0:
                    if perf_counter() + .25 >= deadline:
                        raise ValueError(f'{label}恢复时间已用完；未创建订单。') from error
                    sleep(.25)
            if attempt == 2 or perf_counter() >= deadline:
                raise ValueError(f'{label}恢复次数或时间已用完；未创建订单。') from error
            # Re-run the step builder, not the old POST: recompute available
            # shipping choices/address fields/ALIPAY from the refreshed model.
            interaction.report_stage(f'{label}仍在原步骤，使用新页面重新准备 {attempt + 2}/3')
