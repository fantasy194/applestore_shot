"""Private Web Push daemon. It observes results; it never logs in or places orders."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from private_state import read_private, write_private


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def decoded(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', value):
        raise ValueError('推送订阅密钥无效。')
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def validate_subscription(value):
    if not isinstance(value, dict):
        raise ValueError('推送订阅无效。')
    endpoint = value.get('endpoint', '')
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        raise ValueError('推送地址无效。')
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ''
    allowed = host in ('web.push.apple.com', 'fcm.googleapis.com', 'updates.push.services.mozilla.com') or bool(
        re.fullmatch(r'[a-z0-9-]+\.notify\.windows\.com', host))
    if (parsed.scheme != 'https' or not allowed or parsed.username or parsed.password
            or parsed.port is not None or parsed.fragment):
        raise ValueError('只允许 Apple、Google、Mozilla 或 Microsoft 官方 HTTPS 推送地址。')
    keys = value.get('keys', {})
    public = decoded(keys.get('p256dh'))
    if len(public) != 65 or public[0] != 4 or len(decoded(keys.get('auth'))) != 16:
        raise ValueError('推送订阅密钥无效。')
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except ValueError:
        raise ValueError('推送订阅公钥无效。') from None
    return {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}


class NoRedirectSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False

    def request(self, method, url, **kwargs):
        kwargs['allow_redirects'] = False
        return super().request(method, url, **kwargs)


class PushService:
    def __init__(self, directory, *, now=time.time, send=None, delivery_enabled=True):
        self.directory = Path(directory).resolve()
        self.path = self.directory / 'settings' / 'web-push.enc'
        self.now = now
        self.send = send or self._send
        self.delivery_enabled = delivery_enabled
        self.state = read_private(self.path)
        if self.state is None:
            key = ec.generate_private_key(ec.SECP256R1())
            self.state = {'version': 1, 'private_key': b64(key.private_numbers().private_value.to_bytes(32, 'big')),
                          'public_key': b64(key.public_key().public_bytes(serialization.Encoding.X962,
                              serialization.PublicFormat.UncompressedPoint)),
                          'subscriptions': [], 'orders': {}, 'stock': {}, 'outbox': [], 'last_delivery': None}
            self.save()
        if not isinstance(self.state, dict) or self.state.get('version') != 1:
            raise ValueError('推送状态格式无效，未重置订阅。')

    def save(self):
        write_private(self.path, self.state)

    def status(self):
        return {'public_key': self.state['public_key'], 'subscription_count': len(self.state['subscriptions']),
                'pending_reminders': sum(x['status'] == 'pending' and x['expires_at'] > self.now()
                    for x in self.state['orders'].values()), 'last_delivery': self.state['last_delivery']}

    def _payment_outbox_key(self, item):
        """Bind a queued payment to one account, including old on-disk items."""
        tag = item.get('payload', {}).get('tag')
        key = item.get('order_key')
        if key is not None:
            if not isinstance(key, str):
                return None
            order = self.state['orders'].get(key)
            number = order.get('number') if isinstance(order, dict) else None
            return key if isinstance(number, str) and tag == 'payment-' + number else None
        matches = [key for key, order in self.state['orders'].items()
                   if isinstance(order, dict) and isinstance(order.get('number'), str)
                   and tag == 'payment-' + order['number']]
        # A legacy item with duplicate order numbers cannot be assigned to a
        # particular account, so do not deliver it after either one resolves.
        return matches[0] if len(matches) == 1 else None

    def enqueue(self, kind, title, body, tag, *, expires_at=None, order_key=None):
        recipients = [s['endpoint'] for s in self.state['subscriptions']]
        if recipients:
            # A newer minute replaces retries of the same reminder instead of
            # delivering an outdated and a current reminder together.
            def different(item):
                if item['payload'].get('tag') != tag:
                    return True
                if kind != 'payment' or item['payload'].get('kind') != 'payment':
                    return False
                existing = self._payment_outbox_key(item)
                return existing is not None and existing != order_key
            self.state['outbox'] = [item for item in self.state['outbox'] if different(item)]
            queued = {'payload': {'kind': kind, 'title': title[:80], 'body': body[:200],
                'tag': tag[:100], 'url': '/?view=orders' if kind == 'payment' else '/?view=monitor'},
                'pending': recipients, 'next_at': self.now(), 'expires_at': min(expires_at or self.now()+120, self.now()+120),
                'attempts': 0}
            if kind == 'payment':
                queued['order_key'] = order_key
            self.state['outbox'].append(queued)
            self.state['outbox'] = self.state['outbox'][-100:]

    def observe(self, task, *, persist=True):
        if not isinstance(task, dict):
            return
        result = task.get('result')
        if not isinstance(result, dict):
            return
        if task.get('action') in ('inventory_monitor', 'inventory_check'):
            for target in result.get('targets', []):
                if not isinstance(target, dict):
                    continue
                key = str(target.get('sku', '')) + ':' + str(target.get('store_id', ''))
                status = target.get('status')
                sampled = target.get('checked_at_ms')
                if status in ('available', 'unavailable') and (type(sampled) not in (int, float)
                        or not 0 <= self.now() * 1000 - sampled <= 10000):
                    # Restored checks cannot announce or rearm an old stock episode.
                    continue
                previous = self.state['stock'].get(key)
                if status == 'available' and previous != 'available':
                    self.enqueue('stock', '门店有库存', f"{target.get('store_name') or target.get('store_id', '')} · {target.get('sku', '')} 可自提，请查看工作台。", 'stock-'+key)
                # Unknown cannot reset an already notified restock episode.
                if status in ('available', 'unavailable'):
                    self.state['stock'][key] = status
            if len(self.state['stock']) > 1000:
                self.state['stock'] = dict(list(self.state['stock'].items())[-1000:])
        if task.get('action') in ('purchase', 'checkout', 'cancel_order', 'orders', 'qr'):
            account = task.get('account_id')
            if not isinstance(account, str) or not account:
                return
            nodes = [(result, 'result')]
            pending_nodes, order_nodes = result.get('pending_orders'), result.get('orders')
            if isinstance(pending_nodes, list):
                nodes.extend((x, 'pending_orders') for x in pending_nodes if isinstance(x, dict))
            if isinstance(order_nodes, list):
                nodes.extend((x, 'orders') for x in order_nodes if isinstance(x, dict))
            for node, source in nodes:
                number = node.get('order_number')
                if not isinstance(number, str) or not re.fullmatch(r'W\d{6,20}', number):
                    continue
                key = account + ':' + number
                status = node.get('status')
                known = self.state['orders'].get(key)
                action = task.get('action')
                terminal = None
                if action == 'cancel_order' and source == 'result':
                    if status == 'cancellation_unknown' and node.get('success') is False:
                        # This is a reminder suppression, not proof the store
                        # accepted the cancellation or the order is cancelled.
                        terminal = 'cancellation_unknown'
                    elif status in ('order_cancelled', 'order_expired') and node.get('success') is True:
                        terminal = status
                elif action == 'orders' and source == 'orders' and node.get('verified_by') == 'official_detail':
                    items = node.get('items')
                    if isinstance(items, list) and items and all(isinstance(item, dict) for item in items):
                        item_statuses = [item.get('status') for item in items]
                        paid = ('PAID', 'PROCESSING', 'PREPARING_TO_SHIP', 'SHIPPED', 'DELIVERED',
                                'READY_FOR_PICKUP', 'PICKED_UP', 'PICKUP_COMPLETE', 'COMPLETE', 'PAYMENT_COMPLETE')
                        def resolved(value):
                            if not isinstance(value, str):
                                return False
                            return (value == 'PAYMENT_EXPIRED_STORED_ORDER' or value in paid
                                    or bool(re.fullmatch(r'CANCEL(?:ED|LED)(?:_[A-Z_]+)?', value)))
                        if all(resolved(value) for value in item_statuses):
                            if all(value == 'PAYMENT_EXPIRED_STORED_ORDER' for value in item_statuses):
                                terminal = 'order_expired'
                            elif all(re.fullmatch(r'CANCEL(?:ED|LED)(?:_[A-Z_]+)?', value) for value in item_statuses):
                                terminal = 'order_cancelled'
                            elif all(value in paid for value in item_statuses):
                                terminal = 'order_paid'
                            else:
                                terminal = 'order_resolved'
                if terminal:
                    if known is None:
                        known = self.state['orders'][key] = {'number': number, 'account_id': task.get('account_id', ''),
                            'created_at': self.now(), 'expires_at': 0, 'next_at': 0}
                    if terminal != 'cancellation_unknown' or known.get('status') == 'pending':
                        known['status'] = terminal
                    elif 'status' not in known:
                        known['status'] = terminal
                    continue
                if (action not in ('purchase', 'checkout', 'qr') or source == 'orders'
                        or status != 'pending_payment' or node.get('success') is not True):
                    continue
                if known is None:
                    # On initial observation use the actual task trigger time as a conservative bound.
                    trigger = task.get('schedule', {}) or {}
                    confirmed = node.get('payment_confirmed_at_ms')
                    confirmed = confirmed / 1000 if type(confirmed) in (int, float) and confirmed > 0 else None
                    created = confirmed or trigger.get('triggered_at_ms', 0) / 1000 or task.get('created_at') or self.now()
                    created = min(float(created), self.now())
                    deadline = node.get('payment_deadline_ms')
                    deadline = deadline / 1000 if type(deadline) in (int, float) and deadline > 0 else created + 1800
                    known = self.state['orders'][key] = {'number': number, 'status': 'pending',
                        'created_at': created, 'expires_at': min(deadline, created + 1800), 'next_at': self.now(),
                        'account_id': task.get('account_id', '')}
                elif known['status'] == 'pending':
                    deadline = node.get('payment_deadline_ms')
                    if type(deadline) in (int, float) and deadline > 0:
                        known['expires_at'] = min(known['expires_at'], deadline / 1000)
                # Repeated events may shorten a deadline but never extend or rearm it.
        if persist:
            self.save()

    def _cancellation_status(self, key, order, histories):
        """Read one account-scoped durable cancellation without exposing it."""
        accounts_root = self.directory / 'accounts'
        if not isinstance(order, dict):
            return None
        account, number = order.get('account_id'), order.get('number')
        if not isinstance(account, str) or not isinstance(number, str) or key != account + ':' + number:
            return None
        try:
            if str(uuid.UUID(account)) != account or not re.fullmatch(r'W\d{6,20}', number):
                return None
        except (ValueError, AttributeError):
            return None
        if account not in histories:
            path = accounts_root / account / 'order_cancellations.json'
            try:
                # A UUID directory or marker symlink cannot redirect the
                # check to another account or outside the application root.
                if accounts_root.is_symlink() or path.is_symlink() or path.parent.is_symlink():
                    raise OSError('linked account marker')
                raw = json.loads(path.read_text(encoding='utf-8'))
                histories[account] = raw if isinstance(raw, dict) else {}
            except (OSError, UnicodeError, ValueError):
                histories[account] = {}
        status = histories[account].get(number)
        return status if status in ('cancellation_unknown', 'order_cancelled', 'order_expired') else None

    def _reconcile_cancellations(self):
        """Observe only this account's durable submitted-cancellation state."""
        histories = {}
        for key, order in self.state['orders'].items():
            if not isinstance(order, dict) or order.get('status') not in ('pending', 'cancellation_unknown'):
                continue
            status = self._cancellation_status(key, order, histories)
            if status and (order['status'] == 'pending' or status in ('order_cancelled', 'order_expired')):
                order['status'] = status

    def tick(self):
        current = self.now()
        self._reconcile_cancellations()
        for key, order in self.state['orders'].items():
            if order['status'] != 'pending':
                continue
            if order['expires_at'] <= current:
                order['status'] = 'expired'
            elif self.delivery_enabled and order['next_at'] <= current:
                remaining = max(1, int((order['expires_at'] - current + 59) // 60))
                self.enqueue('payment', '订单待付款', f"订单 {order['number']} 已创建，剩余约 {remaining} 分钟，请打开工作台完成付款。",
                             'payment-'+order['number'], expires_at=order['expires_at'], order_key=key)
                order['next_at'] = current + 60
        # Persist the deadline reservation before contacting any push provider.
        active_orders = {key for key, order in self.state['orders'].items()
                         if order['status'] == 'pending' and order['expires_at'] > current}
        self.state['outbox'] = [x for x in self.state['outbox'] if x['pending'] and x['expires_at'] > current
                               and (x['payload']['kind'] != 'payment' or self._payment_outbox_key(x) in active_orders)]
        self.save()
        if not self.delivery_enabled:
            return self.status()
        delivered = failed = 0
        jobs = []
        touched = []
        # Enqueue timestamps may be slightly later than this tick's initial
        # clock read. Refresh it so a new reminder is immediately eligible.
        delivery_time = self.now()
        for item in self.state['outbox']:
            if item['next_at'] > delivery_time:
                continue
            subscriptions = [s for s in self.state['subscriptions'] if s['endpoint'] in item['pending']]
            selected = subscriptions[:max(0, 16-len(jobs))]
            if selected:
                jobs.extend((item, subscription) for subscription in selected)
                touched.append(item)
            if len(jobs) >= 16:
                break
        def deliver(job):
            item, subscription = job
            if item['payload']['kind'] == 'payment':
                key = self._payment_outbox_key(item)
                status = self._cancellation_status(key, self.state['orders'].get(key), {})
                if status:
                    return item, subscription['endpoint'], 'suppressed', status
            try:
                self.send(subscription, item['payload'])
                return item, subscription['endpoint'], 'ok', None
            except Exception as error:
                response = getattr(error, 'response', None)
                code = getattr(response, 'status_code', None)
                return item, subscription['endpoint'], 'expired' if code in (404, 410) else 'failed', None
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(deliver, jobs))
        for item, endpoint, outcome, suppressed_status in outcomes:
            if outcome == 'ok':
                delivered += 1
                item['pending'].remove(endpoint)
            elif outcome == 'expired':
                failed += 1
                self.state['subscriptions'] = [s for s in self.state['subscriptions'] if s['endpoint'] != endpoint]
                item['pending'].remove(endpoint)
            elif outcome == 'suppressed':
                key = self._payment_outbox_key(item)
                order = self.state['orders'].get(key)
                if isinstance(order, dict) and order.get('status') == 'pending':
                    order['status'] = suppressed_status
                item['pending'].remove(endpoint)
            else:
                failed += 1
        for item in touched:
            item['attempts'] += 1
            item['next_at'] = current + min(60, 5 * 2**min(item['attempts'], 4))
        active_orders = {key for key, order in self.state['orders'].items()
                         if order['status'] == 'pending' and order['expires_at'] > current}
        self.state['outbox'] = [x for x in self.state['outbox'] if x['pending'] and x['attempts'] < 5
                               and (x['payload']['kind'] != 'payment' or self._payment_outbox_key(x) in active_orders)]
        if delivered or failed:
            self.state['last_delivery'] = {'at': current, 'delivered': delivered, 'failed': failed}
        # Keep deduplication tombstones long enough to prevent stale task snapshots replaying orders.
        if len(self.state['orders']) > 10000:
            raise ValueError('待付款提醒记录达到上限，请先导出并清理旧记录。')
        self.save()
        return self.status()

    def _send(self, subscription, payload):
        from py_vapid import Vapid
        from pywebpush import webpush
        with NoRedirectSession() as session:
            response = webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=Vapid.from_raw(self.state['private_key'].encode('ascii')),
                vapid_claims={'sub': os.environ.get('DUO_VAPID_SUBJECT', 'mailto:duo-owner@example.com')}, timeout=4, ttl=60,
                headers={'Urgency': 'high'}, requests_session=session)
            if not 200 <= response.status_code < 300:
                raise ValueError('推送服务未接受通知。')

    def handle(self, operation, params):
        if operation == 'status':
            return self.status()
        if operation == 'subscribe':
            subscription = validate_subscription(params.get('subscription'))
            self.state['subscriptions'] = [s for s in self.state['subscriptions'] if s['endpoint'] != subscription['endpoint']]
            if len(self.state['subscriptions']) >= 20:
                raise ValueError('最多支持 20 台设备订阅。')
            self.state['subscriptions'].append(subscription)
            self.save()
        elif operation == 'unsubscribe':
            endpoint = params.get('endpoint')
            self.state['subscriptions'] = [s for s in self.state['subscriptions'] if s['endpoint'] != endpoint]
            for item in self.state['outbox']:
                item['pending'] = [x for x in item['pending'] if x != endpoint]
            self.save()
        elif operation == 'test':
            self.enqueue('test', 'DUO 通知测试', '库存和待付款提醒已启用。', 'duo-test')
            self.save()
        elif operation == 'acknowledge':
            for order in self.state['orders'].values():
                if order['number'] == params.get('order_number'):
                    order['status'] = 'acknowledged'
            self.save()
        elif operation == 'observe':
            self.observe(params.get('task'))
            return self.tick()
        elif operation == 'observe_batch':
            tasks = params.get('tasks')
            if not isinstance(tasks, list):
                raise ValueError('推送任务批次无效。')
            # A malformed later item must not leave an earlier pending order
            # in memory where an autonomous tick could deliver it prematurely.
            previous = deepcopy(self.state)
            try:
                for task in tasks:
                    self.observe(task, persist=False)
                self.save()
            except Exception:
                self.state = previous
                raise
            return self.tick()
        elif operation == 'hold_delivery':
            self.delivery_enabled = False
            return self.tick()
        elif operation == 'resume_delivery':
            self.delivery_enabled = True
            try:
                return self.tick()
            except Exception:
                # The caller will retry reconciliation on error. Never leave
                # autonomous delivery enabled when that reconciliation failed.
                self.delivery_enabled = False
                raise
        elif operation != 'tick':
            raise ValueError('不支持的推送操作。')
        return self.tick()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--web-push', action='store_true')
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    service = PushService(Path(args.data_dir).resolve(),
                          delivery_enabled=os.environ.get('DUO_WEB_PUSH_DEFER_DELIVERY') != '1')
    inbox = queue.Queue(maxsize=256)
    def read():
        for line in sys.stdin:
            inbox.put(line)
        inbox.put(None)
    threading.Thread(target=read, daemon=True).start()
    while True:
        request = None
        try:
            line = inbox.get(timeout=5)
        except queue.Empty:
            try:
                service.tick()
            except Exception:
                pass
            continue
        if line is None:
            break
        try:
            if len(line) > 4_000_000:
                raise ValueError('推送输入超出限制。')
            request = json.loads(line)
            result = service.handle(request.get('operation'), request.get('params') or {})
            response = {'ok': True, 'result': result}
        except Exception as error:
            safe_errors = {'推送订阅密钥无效。', '推送订阅无效。', '推送地址无效。',
                           '只允许 Apple、Google、Mozilla 或 Microsoft 官方 HTTPS 推送地址。',
                           '推送订阅公钥无效。', '最多支持 20 台设备订阅。',
                           '待付款提醒记录达到上限，请先导出并清理旧记录。', '推送任务批次无效。',
                           '不支持的推送操作。', '推送输入超出限制。',
                           '私有设置密钥与当前系统不兼容。', '私有设置密钥已损坏。',
                           '私有设置密钥丢失，未重置现有设置。', '私有设置密钥正忙，请稍后重试。',
                           '私有设置无法解密或已损坏，未重置现有设置。',
                           '无法访问当前系统用户的私有设置密钥。'}
            message = str(error) if isinstance(error, ValueError) and str(error) in safe_errors else '推送服务暂时不可用。'
            response = {'ok': False, 'error': message[:200]}
        request_id = request.get('request_id') if isinstance(request, dict) else None
        if isinstance(request_id, str) and 0 < len(request_id) <= 128:
            response['request_id'] = request_id
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
