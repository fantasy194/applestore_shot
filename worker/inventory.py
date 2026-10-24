"""Read-only Apple China store discovery and one-second stock monitoring.

The public inventory session has no connection to an Apple account's cookies,
browser profile, login, bag or checkout. Availability requires both the exact
store and the exact SKU in a successful structured Apple response.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
import uuid

import requests

from activity_log import ActivityLog, safe_message
from inventory_proxy import DynamicProxyPool, ProxyError, public_url
from inventory_broker import SharedClient, StoreBroker, query_running_monitor, store_query
from private_state import read_private, write_private
from monitor_schedule import default_schedule, schedule_state, validate_schedule

PICKUP_URL = 'https://www.apple.com.cn/shop/retail/pickup-message'
ACTIONS = frozenset(('inventory_settings_read', 'inventory_settings_save', 'store_search', 'inventory_check', 'inventory_monitor'))
MAX_BODY = 4 * 1024 * 1024
SECRET_FIELDS = ('extract_url', 'whitelist_url', 'username', 'password')


def iso_now(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def default_config():
    return {'interval_seconds': 1, 'targets': [], 'proxy': {'enabled': False, 'pool_size': 10, 'providers': []},
            'schedule': default_schedule()}


def sku_part(value):
    value = str(value).strip().upper()
    if value.endswith('/A'):
        value = value[:-2]
    if not re.fullmatch(r'[A-Z0-9]{5,30}', value):
        raise ValueError('请输入有效的 Apple 商品 SKU。')
    return value + '/A'


def clean_text(value, maximum=300):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', value)).strip()[:maximum]


def _number(value, default, low, high, label):
    value = default if value is None else value
    if type(value) is not int or not low <= value <= high:
        raise ValueError(label)
    return value


def validate_config(config, previous=None):
    if not isinstance(config, dict):
        raise ValueError('库存监控配置格式无效。')
    previous = previous or default_config()
    interval = _number(config.get('interval_seconds'), 1, 1, 300, '检查间隔必须为 1–300 秒。')
    targets = config.get('targets', [])
    if not isinstance(targets, list) or len(targets) > 100:
        raise ValueError('最多支持 100 个商品与门店监控目标。')
    normalized, seen, seen_ids = [], set(), set()
    previous_targets = {row.get('id'): row for row in previous.get('targets', [])}
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError('监控目标格式无效。')
        sku = sku_part(target.get('sku', ''))[:-2]
        store = target.get('store_id', '')
        if not isinstance(store, str) or not re.fullmatch(r'R\d{3,5}', store):
            raise ValueError('请选择接口返回的有效门店编号。')
        account_id = target.get('account_id') or ''
        if account_id:
            try:
                if str(uuid.UUID(account_id)) != account_id or uuid.UUID(account_id).int == 0:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('自动抢购账号标识无效。') from None
        auto = target.get('auto_purchase') is True
        if auto and not account_id:
            raise ValueError('请先选择自动抢购使用的账号。')
        identity = '|'.join((sku, store, account_id))
        if identity in seen:
            raise ValueError('同一账号、商品和门店监控重复。')
        seen.add(identity)
        identifier = target.get('id') or hashlib.sha256(identity.encode()).hexdigest()[:24]
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier) or identifier in seen_ids:
            raise ValueError('监控目标标识无效。')
        seen_ids.add(identifier)
        normalized.append({'id': identifier, 'sku': sku, 'quantity': _number(target.get('quantity'), 1, 1, 99, '监控购买数量必须为 1–99。'),
                           'store_id': store, 'store_name': clean_text(target.get('store_name'), 100) or store,
                           'location': clean_text(target.get('location'), 120), 'account_id': account_id, 'auto_purchase': auto,
                           'concurrency': _number(target.get('concurrency'), 1, 1, 10, '并发下单数必须为 1–10。'),
                           'retry_count': _number(target.get('retry_count'), previous_targets.get(identifier, {}).get('retry_count', 0),
                                                  0, 10, '下单失败重试次数必须为 0–10 的整数。')})
    proxy = config.get('proxy', {})
    if not isinstance(proxy, dict):
        raise ValueError('动态代理配置格式无效。')
    providers = proxy.get('providers', [])
    if not isinstance(providers, list) or len(providers) > 4:
        raise ValueError('最多配置 4 个库存代理供应商。')
    previous_providers = {row.get('id'): row for row in previous.get('proxy', {}).get('providers', [])}
    safe_providers, provider_ids = [], set()
    for row in providers:
        if not isinstance(row, dict):
            raise ValueError('代理供应商配置格式无效。')
        identifier = row.get('id', row.get('kind', 'generic'))
        kind = row.get('kind', 'generic')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', identifier) or identifier in provider_ids:
            raise ValueError('代理供应商标识无效或重复。')
        if kind not in ('dmdaili', 'xydaili', 'generic'):
            raise ValueError('不支持的代理供应商类型。')
        provider_ids.add(identifier)
        old = previous_providers.get(identifier, {})
        provider = {'id': identifier, 'kind': kind, 'enabled': row.get('enabled', True) is True,
                    'ttl_seconds': _number(row.get('ttl_seconds'), 180, 15, 86400, '代理有效期必须为 15–86400 秒。')}
        clear = row.get('clear_fields', [])
        if not isinstance(clear, list) or any(key not in SECRET_FIELDS for key in clear):
            raise ValueError('代理私有字段清除配置无效。')
        for name in SECRET_FIELDS:
            value = '' if name in clear else row.get(name) or old.get(name, '')
            if not isinstance(value, str) or len(value) > (4096 if name.endswith('_url') else 256) or re.search(r'[\r\n\x00]', value):
                raise ValueError('代理私有配置字段无效。')
            provider[name] = value
        for name in ('extract_url', 'whitelist_url'):
            if provider[name]:
                public_url(provider[name], resolve=False)
        if provider['enabled'] and not provider['extract_url']:
            raise ValueError('启用供应商前请填写代理提取接口。')
        safe_providers.append(provider)
    enabled = proxy.get('enabled') is True
    if enabled and not any(p['enabled'] for p in safe_providers):
        raise ValueError('请先配置并启用库存代理供应商。')
    return {'interval_seconds': interval, 'targets': normalized,
            'schedule': validate_schedule(config.get('schedule', previous.get('schedule', default_schedule()))),
            'proxy': {'enabled': enabled, 'pool_size': _number(proxy.get('pool_size'), 10, 1, 200, '代理池容量必须为 1–200。'),
                      'providers': safe_providers}}


def public_config(config):
    result = json.loads(json.dumps(config))
    for provider in result['proxy']['providers']:
        provider['secret_configured'] = bool(provider.get('extract_url'))
        for field in SECRET_FIELDS:
            provider[field + '_configured'] = bool(provider.pop(field, ''))
    return result


def load_config(settings_dir):
    config = read_private(Path(settings_dir) / 'inventory-settings.enc', None)
    return default_config() if config is None else validate_config(config)


def fulfillment_url(skus, *, store_id=None, location=None):
    if not isinstance(skus, list) or not 1 <= len(skus) <= 40:
        raise ValueError('每次库存查询需要 1–40 个 SKU。')
    query = {'pl': 'true', 'mts.0': 'regular'}
    query.update({f'parts.{index}': sku_part(sku) for index, sku in enumerate(skus)})
    if store_id:
        if not re.fullmatch(r'R\d{3,5}', store_id):
            raise ValueError('门店编号无效。')
        query['store'] = store_id
    elif isinstance(location, str) and location.strip() and len(location) <= 120:
        query['location'] = location.strip()
    else:
        raise ValueError('请填写搜索城市或门店邮编。')
    return PICKUP_URL + '?' + urlencode(query)


def parse_stores(body):
    if not isinstance(body, dict) or not isinstance(body.get('body'), dict):
        raise ValueError('官网未返回可识别的门店库存数据。')
    body = body['body']
    content = body.get('content') if isinstance(body.get('content'), dict) else {}
    pickup = content.get('pickupMessage') if isinstance(content.get('pickupMessage'), dict) else {}
    stores = body.get('stores', pickup.get('stores'))
    if not isinstance(stores, list):
        if body.get('errorMessage'):
            raise ValueError('官网未接受搜索地点，请使用有效城市或六位邮编。')
        raise ValueError('官网未返回可识别的门店库存数据。')
    return [store for store in stores if isinstance(store, dict) and re.fullmatch(r'R\d{3,5}', str(store.get('storeNumber', '')))
            and isinstance(store.get('storeName'), str)]


def stock_for_target(target, stores, checked_at=None, checked_at_ms=None):
    store = next((s for s in stores if s['storeNumber'] == target['store_id']), None)
    parts = store.get('partsAvailability', {}) if store else {}
    part = next((p for sku, p in parts.items() if isinstance(sku, str) and sku.upper() == sku_part(target['sku'])), None) if isinstance(parts, dict) else None
    display = part.get('pickupDisplay') if isinstance(part, dict) else None
    status = 'available' if display == 'available' else 'unavailable' if display in ('unavailable', 'ineligible') else 'unknown'
    quote = part.get('pickupSearchQuote') if isinstance(part, dict) else None
    if isinstance(quote, dict):
        quote = quote.get('storePickupQuote')
    if not isinstance(quote, str) and isinstance(part, dict):
        messages = part.get('messageTypes') if isinstance(part.get('messageTypes'), dict) else {}
        regular = messages.get('regular') if isinstance(messages.get('regular'), dict) else {}
        quote = regular.get('storePickupQuote')
    result = dict(target, status=status, checked_at=checked_at, checked_at_ms=checked_at_ms,
                  pickup_message=clean_text(quote) or {'available': '可到店取货', 'unavailable': '暂不可取货', 'unknown': '库存状态无法确认'}[status])
    if store:
        result['store_name'] = clean_text(store['storeName'], 100)
    if status == 'unknown':
        result['error'] = '本次查询没有该门店。' if not store else '本次查询没有该商品的明确库存状态。'
    return result


class PoolBusy(ValueError):
    pass


class PoolOwner:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, 'a+b')
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                self.file.seek(0)
                self.file.write(b'\0')
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise PoolBusy('库存查询正在使用中，单次库存检查请稍后重试。') from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


class InventoryClient:
    def __init__(self, config, settings_dir, *, session_factory=requests.Session, now=time.time, pool_factory=DynamicProxyPool):
        self.config = config
        self.root = Path(settings_dir)
        self.session_factory = session_factory
        self.now = now
        self.pool = pool_factory(config['proxy'], self.root / 'inventory-proxy-state.enc') if config['proxy']['enabled'] else None
        self.direct_next = 0
        self.group_cursor = 0
        self.round = 0
        self.activity = ActivityLog.for_settings(self.root, action='inventory_check', category='inventory')

    def _record_round(self, results, started, timings):
        self.activity.record_stock_observations(results, now_ms=int(self.now() * 1000))
        proxy = self.status()
        entries = []
        for target in results:
            details = {key: target.get(key) for key in ('sku', 'quantity', 'store_id', 'store_name', 'location',
                                                       'status', 'checked_at_ms', 'auto_purchase')}
            details.update(target_id=target.get('id'), round=self.round,
                           duration_ms=timings.get(target.get('id')), reason=target.get('error'),
                           proxy_enabled=proxy.get('enabled'), dispatchable_count=proxy.get('dispatchable_count'),
                           pool_size=proxy.get('pool_size'))
            status = re.search(r'HTTP (\d{3})', target.get('error', ''))
            details['http_status'] = int(status[1]) if status else 200 if target.get('checked_at_ms') is not None else None
            entries.append(dict(event='inventory_target_checked', message='门店商品库存检查完成。', details=details,
                                account_id=target.get('account_id'),
                                level='warning' if target.get('status') == 'unknown' else 'info'))
        counts = {status + '_count': sum(t.get('status') == status for t in results)
                  for status in ('available', 'unavailable', 'unknown')}
        entries.append(dict(event='inventory_round_end', message='库存检查轮次完成。', details={
            **counts, 'round': self.round, 'target_count': len(results),
            'duration_ms': int((time.perf_counter() - started) * 1000),
            'proxy_enabled': proxy.get('enabled'), 'dispatchable_count': proxy.get('dispatchable_count')}))
        self.activity.record_many(entries)

    def warm(self, channel=None, timeout=8, minimum=1):
        if not self.pool or minimum < 1:
            return
        if channel:
            channel.checkpoint()
        desired = min(minimum, self.config['proxy']['pool_size'], 10)
        self.pool.touch()
        deadline = time.monotonic() + timeout
        while self.pool.status()['dispatchable_count'] < desired and time.monotonic() < deadline:
            if channel:
                channel.checkpoint()
            time.sleep(.1)

    def fetch(self, url):
        deadline = time.monotonic() + 8
        lease = self.pool.acquire() if self.pool else None
        failed = True
        try:
            with self.session_factory() as session:
                # Ignore environment proxy/auth/cookie files. Only explicit anonymous
                # inventory proxy leases enter this one-request session.
                session.trust_env = False
                session.cookies.clear()
                session.headers.update({'Accept': 'application/json', 'Accept-Language': 'zh-CN,zh;q=0.9'})
                if lease:
                    session.proxies.update({'http': lease.url, 'https': lease.url})
                with session.get(url, allow_redirects=False, timeout=(3, 5), stream=True) as response:
                    if response.status_code != 200:
                        if not self.pool and response.status_code in (403, 429, 541):
                            self.direct_next = self.now() + 60
                        raise ValueError(f'库存接口 HTTP {response.status_code}；库存未知。')
                    raw, size = [], 0
                    for chunk in response.iter_content(8192):
                        if time.monotonic() > deadline:
                            raise ValueError('库存接口响应超时；库存未知。')
                        size += len(chunk)
                        if size > MAX_BODY:
                            raise ValueError('库存接口响应过大；库存未知。')
                        raw.append(chunk)
                    try:
                        stores = parse_stores(json.loads(b''.join(raw)))
                    except (ValueError, TypeError, AttributeError):
                        if not self.pool:
                            self.direct_next = self.now() + 60
                        raise ValueError('库存接口未返回有效 JSON 门店数据；库存未知。') from None
                    at = self.now()
                    failed = False
                    return stores, iso_now(at), int(at * 1000)
        except requests.Timeout:
            raise ValueError('库存查询网络请求超时；库存未知。') from None
        except requests.RequestException:
            raise ValueError('库存查询网络请求未完成；库存未知。') from None
        finally:
            if lease:
                self.pool.release(lease, failed=failed)

    @staticmethod
    def query_groups(targets):
        groups = {}
        for target in targets:
            groups.setdefault(target['store_id'], []).append(target)
        work = []
        for anchor, group in groups.items():
            skus = list(dict.fromkeys(t['sku'] for t in group))
            for offset in range(0, len(skus), 40):
                subset = skus[offset:offset + 40]
                work.append((anchor, subset, [t for t in group if t['sku'] in subset]))
        return work

    def check(self, targets, *, record_if=None):
        started = time.perf_counter()
        self.round += 1
        timings = {}
        if not targets:
            if record_if is None or record_if():
                self._record_round([], started, timings)
            return {'success': True, 'monitoring': False, 'checked_at': iso_now(), 'targets': [], 'proxy': self.status()}
        if self.pool:
            self.pool.touch()
        if not self.pool and self.now() < self.direct_next:
            results = [dict(t, status='unknown', checked_at=None, checked_at_ms=None,
                            error='直连库存查询处于保护等待，请稍后重试。') for t in targets]
        else:
            # An anchored response may contain only that store, even in one city.
            work = self.query_groups(targets)
            # A round is bounded even with many configured stores. It
            # rotates deferred work instead of queueing minutes of old samples.
            capacity = min(10, self.config['proxy']['pool_size']) if self.pool else 4
            offset = self.group_cursor % len(work)
            self.group_cursor += min(capacity, len(work))
            work = (work[offset:] + work[:offset])[:capacity]
            results_by_id = {}
            responses = []
            failures = {}
            with ThreadPoolExecutor(max_workers=len(work), thread_name_prefix='inventory-read') as executor:
                futures = {}
                for store, subset, group in work:
                    futures[executor.submit(self.fetch, fulfillment_url(subset, store_id=store))] = (group, time.perf_counter())
                for future in as_completed(futures):
                    group, sent = futures[future]
                    timings.update({target['id']: int((time.perf_counter() - sent) * 1000) for target in group})
                    try:
                        stores, at, at_ms = future.result()
                        responses.append((stores, at, at_ms))
                        for target in group:
                            results_by_id[target['id']] = stock_for_target(target, stores, at, at_ms)
                    except Exception as error:
                        message = error.code if isinstance(error, ProxyError) else str(error) if isinstance(error, ValueError) else '库存检查未完成。'
                        for target in group:
                            failures[target['id']] = message[:200]
            # A valid returned nearby store observation can satisfy other configured
            # anchors, without inferring missing stores/SKUs or replaying a request.
            for target in targets:
                current = results_by_id.get(target['id'])
                if not current or current['status'] == 'unknown':
                    for stores, at, at_ms in responses:
                        observation = stock_for_target(target, stores, at, at_ms)
                        if observation['status'] != 'unknown':
                            results_by_id[target['id']] = observation
                            break
            results = [results_by_id.get(t['id'], dict(t, status='unknown', checked_at=None, checked_at_ms=None,
                                                     error=failures.get(t['id'], '本轮未能确认该门店库存。'))) for t in targets]
        if record_if is None or record_if():
            self._record_round(results, started, timings)
        return {'success': True, 'monitoring': False, 'checked_at': iso_now(), 'targets': results, 'proxy': self.status()}

    def stores(self, sku, location):
        stores, at, at_ms = self.fetch(fulfillment_url([sku], location=location))
        result = []
        for store in stores:
            target = {'id': store['storeNumber'], 'sku': sku_part(sku)[:-2], 'store_id': store['storeNumber'], 'store_name': store['storeName']}
            stock = stock_for_target(target, stores, at, at_ms)
            address = store.get('address') if isinstance(store.get('address'), dict) else {}
            result.append({'store_id': store['storeNumber'], 'store_name': clean_text(store['storeName'], 100),
                           'location': clean_text(store.get('city') or address.get('city') or location, 120),
                           'address': clean_text(' '.join(str(address.get(k, '')) for k in ('address', 'address2'))),
                           'status': stock['status'], 'pickup_message': stock['pickup_message']})
        return {'success': True, 'stores': result, 'checked_at': at, 'scope': 'nearby', 'total': len(result),
                'note': '列表是官网接口实际返回的附近门店，并非全国门店目录。', 'proxy': self.status()}

    def status(self):
        return self.pool.status() if self.pool else {'enabled': False, 'pool_size': self.config['proxy']['pool_size'],
                                                    'dispatchable_count': 0, 'active_count': 0, 'direct_retry_at': self.direct_next or None}

    def close(self):
        if self.pool:
            self.pool.close()


def _run_inventory(action, params, channel, settings_dir, activity):
    if action not in ACTIONS:
        raise ValueError('不支持的库存操作。')
    if not isinstance(params, dict):
        raise ValueError('库存操作参数无效。')
    root = Path(settings_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if action == 'inventory_settings_read':
        return {'success': True, 'config': public_config(load_config(root))}
    if action == 'inventory_settings_save':
        previous = load_config(root)
        config = validate_config(params.get('config'), previous)
        write_private(root / 'inventory-settings.enc', config)
        return {'success': True, 'config': public_config(config), 'targets_changed': previous['targets'] != config['targets']}
    if action == 'store_search':
        params = store_query(params)
    config = load_config(root)
    try:
        with PoolOwner(root / '.inventory-pool.lock'):
            return _run_owned_inventory(action, params, channel, root, config, activity)
    except PoolBusy:
        if action == 'store_search':
            return query_running_monitor(root, params, channel)
        raise


def _run_owned_inventory(action, params, channel, root, config, activity):
    def create(current):
        client = InventoryClient(current, root)
        client.activity = activity
        if client.pool:
            client.pool.activity = activity
        return client

    def effective(current, state):
        if action != 'inventory_monitor' or not state['paused']:
            return current
        return dict(current, proxy=dict(current['proxy'], enabled=False))

    state = schedule_state(config['schedule'])
    client_config = effective(config, state)
    client = create(client_config)
    shared = SharedClient(client)
    broker = None
    try:
        if action == 'store_search':
            client.warm(channel)
            return client.stores(params['sku'], params['location'])
        targets = config['targets']
        if action == 'inventory_check':
            # Reserve enough ready exits for this round's concurrent reads,
            # rather than letting the second/third store lose a cold-start race.
            client.warm(channel, minimum=len(client.query_groups(targets)))
            return client.check(targets)
        if not targets:
            raise ValueError('请先添加至少一个库存监控目标。')
        broker = StoreBroker(root, shared, channel).start()
        last_summary, last_emitted, round_number = None, 0, 0
        last_schedule_state = None
        while True:
            channel.checkpoint()
            started = time.monotonic()
            latest = load_config(root)
            state = schedule_state(latest['schedule'])
            desired = effective(latest, state)
            if desired['proxy'] != client_config['proxy']:
                shared.replace(lambda: create(desired), channel)
            client_config = desired
            config = latest
            # Replacing a client can wait for a manual query or pool shutdown.
            # Its pre-switch clock/config snapshot cannot authorize a new round.
            latest = load_config(root)
            latest_state = schedule_state(latest['schedule'])
            if (any(latest[key] != config[key] for key in ('schedule', 'targets', 'proxy'))
                    or latest_state['paused'] != state['paused']):
                continue
            state = latest_state
            transition = (state['paused'], state['reason'])
            if transition != last_schedule_state:
                if state['paused']:
                    channel.emit('log', message='库存监控已按每日时段暂停，恢复后将重新查询库存。')
                elif last_schedule_state is not None:
                    channel.emit('log', message='库存监控已恢复，开始新的库存查询。')
                last_schedule_state = transition
            if state['paused']:
                with shared.borrow() as current:
                    proxy = current.status()
                summary = ('paused', json.dumps(config['schedule'], sort_keys=True), state['next_transition_at_ms'])
                if summary != last_summary or time.monotonic() - last_emitted >= 30:
                    channel.emit('inventory', result={'success': True, 'monitoring': True,
                                 'interval_seconds': config['interval_seconds'], 'targets': [],
                                 'checked_at': None, 'checked_at_ms': None, 'proxy': proxy,
                                 'schedule': config['schedule'], 'schedule_state': state})
                    last_summary, last_emitted = summary, time.monotonic()
                # Wall time and settings are re-evaluated after at most one second.
                channel.checkpoint()
                time.sleep(.5)
                continue
            with shared.borrow() as current:
                # Borrow acquisition and prior work may have crossed a boundary.
                # Keep this last authorization check immediately before dispatch.
                candidate = load_config(root)
                if (schedule_state(candidate['schedule'])['paused']
                        or any(candidate[key] != config[key] for key in ('schedule', 'targets', 'proxy'))):
                    continue
                def still_current():
                    candidate = load_config(root)
                    return (not schedule_state(candidate['schedule'])['paused']
                            and all(candidate[key] == config[key] for key in ('schedule', 'targets', 'proxy')))
                # A request may finish after the boundary; neither event consumers
                # nor stock history may treat its response as a fresh observation.
                result = current.check(config['targets'], record_if=still_current)
            after = load_config(root)
            after_state = schedule_state(after['schedule'])
            if after_state['paused'] or any(after[key] != config[key] for key in ('schedule', 'targets', 'proxy')):
                # Never publish stock that crossed a paused or changed authorization window.
                continue
            result['monitoring'] = True
            result['interval_seconds'] = config['interval_seconds']
            result['schedule'] = config['schedule']
            result['schedule_state'] = after_state
            round_number += 1
            proxy = result.get('proxy', {})
            summary = (tuple((target.get('id'), target.get('status'), target.get('error')) for target in result['targets']),
                       proxy.get('enabled'), proxy.get('dispatchable_count'), proxy.get('pool_size'))
            if summary != last_summary or time.monotonic() - last_emitted >= 30:
                counts = {status: sum(t.get('status') == status for t in result['targets'])
                          for status in ('available', 'unavailable', 'unknown')}
                channel.emit('log', message=(f"库存监控第 {round_number} 轮：{counts['available']} 个有货，"
                                            f"{counts['unavailable']} 个无货，{counts['unknown']} 个未知；"
                                            f"代理可调度 {proxy.get('dispatchable_count', 0)}/{proxy.get('pool_size', 0)}。"))
                last_summary, last_emitted = summary, time.monotonic()
            channel.checkpoint()
            channel.emit('inventory', result=result)
            # Nonoverlap: a slow round delays the next; no accumulated catch-up.
            deadline = started + config['interval_seconds']
            while time.monotonic() < deadline:
                channel.checkpoint()
                if schedule_state(config['schedule'])['paused'] or load_config(root) != config:
                    break
                time.sleep(min(.1, deadline - time.monotonic()))
    finally:
        if broker:
            broker.close()
        shared.close()


def run_inventory(action, params, channel, settings_dir):
    from protocol import Cancelled
    activity = getattr(channel, 'activity', None) or ActivityLog.for_settings(
        settings_dir, account_id=getattr(channel, 'account_id', ''),
        task_id=getattr(channel, 'task_id', ''), action=action, category='inventory')
    started = time.perf_counter()
    details = {key: params.get(key) for key in ('sku', 'location')} if isinstance(params, dict) else {}
    config = params.get('config') if isinstance(params, dict) else None
    if isinstance(config, dict):
        details['target_count'] = len(config['targets']) if isinstance(config.get('targets'), list) else 0
    activity.record('inventory_operation_start', '开始库存相关操作。', details=details)
    try:
        result = _run_inventory(action, params, channel, settings_dir, activity)
    except Cancelled:
        activity.record('inventory_operation_cancelled', '库存操作已取消。', level='warning',
                        details={'duration_ms': int((time.perf_counter() - started) * 1000)})
        raise
    except Exception as error:
        activity.record('inventory_operation_error', safe_message(str(error)) if isinstance(error, ValueError)
                        else '库存操作未完成。', level='error', details={
                            'error_type': type(error).__name__, 'error_code': getattr(error, 'code', None),
                            'duration_ms': int((time.perf_counter() - started) * 1000)})
        raise
    activity.record('inventory_operation_end', '库存相关操作完成。', details={
        'success': result.get('success') is not False, 'returned_store_count': result.get('total'),
        'target_count': len(result['targets']) if isinstance(result.get('targets'), list) else None,
        'duration_ms': int((time.perf_counter() - started) * 1000)})
    return result
