"""Bounded retries for explicitly read-only requests, never for order writes."""
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from time import perf_counter, sleep
import logging
from urllib.parse import urlsplit

import requests
import interaction


def fresh_checkout_connection(session, method, url, stage):
    """Refresh the transport pool, retaining the Session and all cookies.

    Cloth controls reproduced ~10s for shipping/billing on a reused H2
    connection with both HTTPX and system libcurl. Keeping H2 but opening a
    fresh connection reduced them to ~1.35s/~2.29s. One slow billing request
    finished uploading in 3ms, was TCP-acknowledged within 0.21s, and had no
    further outgoing retransmissions during its ~10.78s response wait.
    The remote internal cause is unknown: do not infer that H2 itself is slow,
    or trade this protection for a small fast-sample connection-reuse gain.
    The final order POST also starts on a fresh connection but is sent once.
    Password verification and cancellation operations retain their transports.
    """
    paths = {'continueFromShippingToBilling': '/shop/checkoutx/shipping',
             'continueFromBillingToReview': '/shop/checkoutx/billing',
             '交换商城会话凭证': '/shop/signIn/idms/authx',
             'continueFromReviewToProcess': '/shop/checkoutx/review',
             'checkStatus': '/shop/checkoutx/statusX'}
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if (method.upper() != 'POST' or stage not in paths or parsed.path != paths[stage]
            or parsed.scheme != 'https' or not (host == 'www.apple.com.cn'
            or host.startswith('secure') and host.endswith('.www.apple.com.cn'))):
        return False
    if not isinstance(session, requests.Session):
        return False
    session.get_adapter(url).close()
    logging.getLogger('duo_killer').info('[%s] 保留会话和 Cookie，使用新连接发送请求。', stage)
    return True


def request(session, method, url, *, read_only=False, stage='', **kwargs):
    automatic = (getattr(interaction.handler, 'automatic_checkout', False) is True
                 or getattr(interaction.handler, 'retry_enabled', False) is True)
    fresh_checkout_connection(session, method, url, stage)
    if not automatic:
        return session.request(method, url, timeout=kwargs.pop('timeout', 30), **kwargs)
    attempts = 3 if read_only else 1
    deadline = perf_counter() + 12
    for attempt in range(attempts):
        interaction.handler.channel.checkpoint()
        remaining = max(.1, deadline - perf_counter())
        timeout = (min(2, remaining / 3), min(6, remaining * 2 / 3)) if read_only else (3, 15)
        response = None
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in (408, 429, 500, 502, 503, 504) or not read_only:
                return response
        except (requests.Timeout, requests.ConnectionError):
            if not read_only or attempt == attempts - 1:
                raise
        delay = (.25, .75, 1)[attempt]
        if response is not None and response.headers.get('Retry-After'):
            hint = response.headers['Retry-After']
            try:
                delay = max(delay, float(hint))
            except ValueError:
                try:
                    delay = max(delay, (parsedate_to_datetime(hint) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    return response
        if attempt == attempts - 1 or perf_counter() + delay >= deadline:
            if response is not None:
                return response
            raise requests.Timeout('只读请求重试时间已用完')
        if response is not None:
            response.close()
        interaction.report_stage(f'{stage}暂时失败，重试 {attempt + 2}/{attempts}')
        sleep(delay)
