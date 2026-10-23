"""Private loopback store discovery using the monitor's one inventory client.

This is not a general HTTP proxy. The only query accepts an Apple SKU and a
city/postcode; no URL, cookies, account state or provider configuration is sent.
"""
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
import uuid

import requests

from private_state import read_private, write_private
from protocol import Cancelled
from activity_log import ActivityLog
from inventory_proxy import ProxyError

REGISTRATION = 'inventory-query-broker.enc'
MAX_BODY = 8192
MAX_RESPONSE = 512 * 1024
MAX_REJECT_DRAIN = 64 * 1024
PUBLIC_ERRORS = {
    'broker_unavailable': '运行中的库存查询服务暂不可用，请稍后重试。',
    'broker_busy': '库存查询服务正在切换或繁忙，请稍后重试。',
    'broker_cancelled': '本次共享门店查询已取消；自动监控未因此停止。',
    'proxy_pool_empty': '库存代理池暂无可调度连接，请等待代理池就绪后重试。',
    'proxy_network': '库存代理连接未完成；尚未确认门店库存。',
    'inventory_network': '官网库存查询网络请求未完成，请稍后重试。',
    'inventory_timeout': '官网库存查询超时，请稍后重试。',
    'inventory_challenge': '官网限制或验证了库存请求，本次库存未知，请稍后重试。',
    'inventory_http': '官网库存接口返回异常状态，本次库存未知。',
    'inventory_response': '官网未返回有效门店库存数据，本次库存未知。',
    'inventory_query': '官网门店查询未完成，请稍后重试。',
}
for _code in ('provider_configuration', 'provider_network', 'provider_timeout', 'provider_http',
              'provider_http_auth', 'provider_response', 'provider_response_size', 'provider_expiry',
              'provider_endpoint', 'provider_duplicate', 'provider_whitelist'):
    PUBLIC_ERRORS[_code] = '库存代理供应商暂未提供有效连接，请检查代理池状态后重试。'


class BrokerUnavailable(ValueError):
    def __init__(self, message, code='broker_unavailable'):
        self.code = code if code in PUBLIC_ERRORS else 'broker_unavailable'
        super().__init__(message)


def _public_error(error):
    if isinstance(error, ProxyError) and error.code in PUBLIC_ERRORS:
        code = error.code
    elif isinstance(error, requests.Timeout):
        code = 'inventory_timeout'
    elif isinstance(error, requests.RequestException):
        code = 'inventory_network'
    elif isinstance(error, ValueError):
        text = str(error)  # Classify internally; never reflect vendor/URL text.
        status = re.search(r'HTTP (\d{3})', text)
        code = ('inventory_challenge' if status and status[1] in ('403', '429', '541')
                else 'inventory_http' if status else 'inventory_timeout' if '超时' in text
                else 'inventory_response' if 'JSON' in text or '门店数据' in text
                else 'inventory_network' if '网络请求' in text else 'inventory_query')
    else:
        code = 'inventory_query'
    return code, PUBLIC_ERRORS[code]


def store_query(params):
    if not isinstance(params, dict):
        raise ValueError('门店查询参数无效。')
    sku = params.get('sku')
    location = params.get('location')
    if not isinstance(sku, str) or not re.fullmatch(r'[A-Z0-9]{5,30}(?:/A)?', sku.strip().upper()):
        raise ValueError('请输入有效的 Apple 商品 SKU。')
    if (not isinstance(location, str) or not location.strip() or len(location) > 120
            or re.search(r'[\r\n\x00]', location)):
        raise ValueError('请填写有效的搜索城市或门店邮编。')
    return {'sku': sku.strip().upper(), 'location': location.strip()}


def _safe_result(result):
    if not isinstance(result, dict) or result.get('success') is not True or not isinstance(result.get('stores'), list):
        raise ValueError('官网未返回有效门店数据。')
    stores = []
    for row in result['stores'][:200]:
        if not isinstance(row, dict) or not re.fullmatch(r'R\d{3,5}', str(row.get('store_id', ''))):
            continue
        stores.append({key: row[key][:300] for key in ('store_id', 'store_name', 'location', 'address', 'status', 'pickup_message')
                       if isinstance(row.get(key), str)})
    proxy = result.get('proxy') if isinstance(result.get('proxy'), dict) else {}
    safe_proxy = {key: proxy[key] for key in ('enabled', 'pool_size', 'active_count', 'dispatchable_count',
                                            'discarded_count', 'refill_count', 'maintaining')
                  if type(proxy.get(key)) in (bool, int)}
    return {'success': True, 'stores': stores, 'total': len(stores), 'scope': 'nearby',
            'checked_at': result.get('checked_at') if isinstance(result.get('checked_at'), str) else None,
            'note': '列表是官网接口实际返回的附近门店，并非全国门店目录。', 'proxy': safe_proxy}


def _close_client(client):
    client.close()
    # DynamicProxyPool.close signals its maintenance thread. Join that same
    # owner before a replacement can load/write its persisted lease state.
    worker = getattr(getattr(client, 'pool', None), 'worker', None)
    if isinstance(worker, threading.Thread) and worker is not threading.current_thread():
        worker.join()


class SharedClient:
    def __init__(self, client):
        self.client = client
        self.condition = threading.Condition()
        self.active = 0
        self.switching = False
        self.stopping = False

    @contextmanager
    def borrow(self):
        with self.condition:
            if self.stopping or self.switching or self.client is None:
                raise BrokerUnavailable('库存查询服务正在切换或停止，请稍后重试。')
            self.active += 1
            client = self.client
        try:
            yield client
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def replace(self, factory, channel):
        with self.condition:
            self.switching = True
            try:
                while self.active:
                    channel.checkpoint()
                    self.condition.wait(.1)
                previous, self.client = self.client, None
            except BaseException:
                self.switching = False
                self.condition.notify_all()
                raise
        try:
            _close_client(previous)
            channel.checkpoint()
            replacement = factory()
            with self.condition:
                self.client = replacement
        finally:
            with self.condition:
                self.switching = False
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.stopping = True
            while self.active:
                self.condition.wait(.1)
            client, self.client = self.client, None
        if client is not None:
            _close_client(client)


class RequestChannel:
    def __init__(self, owner, cancelled):
        self.owner, self.cancelled = owner, cancelled

    def checkpoint(self):
        self.owner.channel.checkpoint()
        if self.cancelled.is_set() or self.owner.stopping.is_set():
            raise Cancelled()


class _Server(ThreadingHTTPServer):
    # server_close must join the *whole* handler, including its terminal log.
    # A released SharedClient reference or a delivered HTTP reply is earlier
    # than handler completion and cannot serve as the shutdown barrier.
    daemon_threads = False
    block_on_close = True

    def __init__(self, broker):
        self.broker = broker
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(('127.0.0.1', 0), _Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # No request headers, credentials or traceback in stderr.


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *_args):
        pass

    def _reply(self, status, payload):
        # On Windows closing a TCP connection with an unread request body can
        # reset it before the rejection response reaches the caller. Discard a
        # small, advertised body without parsing it; huge/slow bodies still hit
        # strict byte/time bounds and never become accepted business input.
        remaining = getattr(self, '_unread_body', None)
        if status >= 400 and remaining is not None and 0 < remaining <= MAX_REJECT_DRAIN:
            deadline = time.monotonic() + .2
            try:
                while remaining and time.monotonic() < deadline:
                    self.connection.settimeout(max(.001, deadline - time.monotonic()))
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            self._unread_body = remaining
            self.connection.settimeout(2)
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        broker = self.server.broker
        request_id = None
        size = self.headers.get('Content-Length', '')
        self._unread_body = (int(size) if size.isdigit() and len(size) <= 7
                             and not self.headers.get('Transfer-Encoding') else None)
        try:
            if (self.client_address[0] != '127.0.0.1'
                    or self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port)
                    or self.headers.get('Origin') is not None
                    or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + broker.token)):
                self._reply(403, {'error': 'broker_auth'})
                return
            if self.path not in ('/stores', '/cancel') or self.headers.get('Transfer-Encoding'):
                self._reply(400, {'error': 'broker_input'})
                return
            if not size.isdigit() or not 1 <= int(size) <= MAX_BODY or self.headers.get_content_type() != 'application/json':
                self._reply(400, {'error': 'broker_input'})
                return
            body = self.rfile.read(int(size))
            self._unread_body = max(0, int(size) - len(body))
            params = json.loads(body)
            request_id = params.get('request_id') if isinstance(params, dict) else None
            if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
                raise ValueError()
            if self.path == '/cancel':
                with broker.lock:
                    event = broker.pending.get(request_id)
                    if event:
                        event.set()
                    broker.cancelled[request_id] = time.monotonic() + 45
                    broker.cancelled = dict(list(broker.cancelled.items())[-256:])
                self._reply(200, {'cancelled': True})
                return
            query = store_query(params)
            with broker.lock:
                broker.cancelled = {key: expiry for key, expiry in broker.cancelled.items() if expiry > time.monotonic()}
                if request_id in broker.cancelled:
                    self._reply(409, {'error': 'broker_cancelled'})
                    return
                if broker.stopping.is_set() or len(broker.pending) >= 2 or request_id in broker.pending:
                    self._reply(503, {'error': 'broker_busy'})
                    return
                cancelled = threading.Event()
                broker.pending[request_id] = cancelled
            channel = RequestChannel(broker, cancelled)
            identity = lambda key: params.get(key, '') if isinstance(params.get(key, ''), str) and re.fullmatch(r'[A-Za-z0-9_-]{0,100}', params.get(key, '')) else ''
            activity = ActivityLog.for_settings(broker.path.parent, account_id=identity('query_account_id'),
                                               task_id=identity('query_task_id') or request_id,
                                               action='store_search', category='inventory')
            started = time.perf_counter()
            activity.record('inventory_shared_query_start', '使用运行中监控的匿名库存连接查询门店。',
                            details={**query, 'transport': 'shared_monitor'})
            try:
                channel.checkpoint()
                with broker.shared.borrow() as client:
                    client.warm(channel, timeout=6)
                    channel.checkpoint()
                    result = client.stores(query['sku'], query['location'])
                    channel.checkpoint()
                self._reply(200, _safe_result(result))
                activity.record('inventory_shared_query_end', '共享门店查询完成。', details={
                    **query, 'transport': 'shared_monitor', 'returned_store_count': len(result['stores']),
                    'duration_ms': int((time.perf_counter() - started) * 1000)})
            except BrokerUnavailable:
                activity.record('inventory_shared_query_waiting', PUBLIC_ERRORS['broker_busy'],
                                details={**query, 'error_code': 'broker_busy'}, level='warning')
                self._reply(503, {'error': 'broker_busy'})
            except Cancelled:
                activity.record('inventory_shared_query_cancelled', PUBLIC_ERRORS['broker_cancelled'],
                                details={**query, 'error_code': 'broker_cancelled'}, level='warning')
                self._reply(409, {'error': 'broker_cancelled', 'error_code': 'broker_cancelled'})
            except Exception as error:
                code, message = _public_error(error)
                activity.record('inventory_shared_query_error', message, details={
                    **query, 'transport': 'shared_monitor', 'error_code': code,
                    'duration_ms': int((time.perf_counter() - started) * 1000)}, level='error')
                self._reply(502, {'error': 'broker_query', 'error_code': code})
            finally:
                with broker.lock:
                    broker.pending.pop(request_id, None)
        except (ValueError, TypeError, OSError):
            try:
                self._reply(400, {'error': 'broker_input'})
            except OSError:
                pass


class StoreBroker:
    def __init__(self, settings_dir, shared, channel):
        self.path = Path(settings_dir) / REGISTRATION
        self.shared, self.channel = shared, channel
        self.token = secrets.token_hex(32)
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        self.cancelled = {}
        self.server = _Server(self)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, name='inventory-store-broker', daemon=True)

    def start(self):
        # Caller already holds PoolOwner. Any former registration is ephemeral
        # and cannot belong to a different live owner of this workspace.
        self.path.unlink(missing_ok=True)
        try:
            write_private(self.path, {'version': 1, 'port': self.server.server_port,
                                      'token': self.token, 'pid': os.getpid()})
            self.thread.start()
        except BaseException:
            self.server.server_close()
            self.path.unlink(missing_ok=True)
            raise
        return self

    def close(self):
        self.stopping.set()
        with self.lock:
            for event in self.pending.values():
                event.set()
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()
        try:
            registered = read_private(self.path, {})
            if isinstance(registered, dict) and registered.get('token') == self.token:
                self.path.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass


def _registration(settings_dir, channel, deadline):
    path = Path(settings_dir) / REGISTRATION
    while True:
        if channel:
            channel.checkpoint()
        try:
            value = read_private(path, None)
        except (ValueError, OSError):
            raise BrokerUnavailable('运行中的库存查询服务登记已失效，请稍后重试。') from None
        if isinstance(value, dict):
            if (value.get('version') != 1 or type(value.get('port')) is not int or not 1 <= value['port'] <= 65535
                    or not isinstance(value.get('token'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['token'])):
                raise BrokerUnavailable('运行中的库存查询服务登记无效，请稍后重试。')
            return value
        if time.monotonic() >= deadline:
            raise BrokerUnavailable('自动监控正在启动或退出，门店查询暂时不可用，请稍后重试。')
        time.sleep(.1)


def _post(registration, path, payload, *, timeout):
    # Literal loopback only; ignore environment proxies, cookies and netrc auth.
    with requests.Session() as session:
        session.trust_env = False
        response = session.post('http://127.0.0.1:' + str(registration['port']) + path,
                                headers={'Authorization': 'Bearer ' + registration['token'], 'Content-Type': 'application/json'},
                                json=payload, timeout=timeout, allow_redirects=False, stream=True)
        with response:
            raw, size = [], 0
            for chunk in response.iter_content(8192):
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise BrokerUnavailable('运行中的库存查询服务响应无效。')
                raw.append(chunk)
            return response.status_code, json.loads(b''.join(raw))


def query_running_monitor(settings_dir, params, channel, *, timeout=35, readiness_timeout=4):
    query = store_query(params)
    deadline = time.monotonic() + timeout
    registration = _registration(settings_dir, channel, min(deadline, time.monotonic() + readiness_timeout))
    request_id = uuid.uuid4().hex
    context = {key: getattr(channel, attribute, '') for key, attribute in (
        ('query_account_id', 'account_id'), ('query_task_id', 'task_id'))}
    completed = queue.Queue(maxsize=1)
    stopped = threading.Event()

    def request():
        try:
            while not stopped.is_set():
                status, result = _post(registration, '/stores', dict(query, **context, request_id=request_id), timeout=(1, 25))
                if status == 200:
                    completed.put((True, _safe_result(result)))
                    return
                if status != 503 or time.monotonic() >= deadline:
                    code = result.get('error_code') if isinstance(result, dict) else None
                    code = code if code in PUBLIC_ERRORS else 'broker_unavailable'
                    raise BrokerUnavailable(PUBLIC_ERRORS[code], code)
                stopped.wait(.2)  # Busy/switching requests did not start a query.
        except Exception as error:
            code = error.code if isinstance(error, BrokerUnavailable) else 'broker_unavailable'
            completed.put((False, {'code': code, 'message': PUBLIC_ERRORS[code]}))

    worker = threading.Thread(target=request, name='inventory-broker-request', daemon=True)
    worker.start()
    cancel_remote = True
    try:
        while time.monotonic() < deadline:
            if channel:
                channel.checkpoint()
            try:
                success, result = completed.get(timeout=.1)
            except queue.Empty:
                continue
            if not success:
                raise BrokerUnavailable(result['message'], result['code'])
            cancel_remote = False
            return result
        raise BrokerUnavailable('门店查询等待超时，自动监控仍在运行，请稍后重试。')
    finally:
        stopped.set()
        if cancel_remote:
            try:
                _post(registration, '/cancel', {'request_id': request_id}, timeout=(.3, .5))
            except Exception:
                pass
