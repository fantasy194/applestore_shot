"""Anonymous inventory-only proxy leases; never accepts Apple account cookies.

Capacity is fixed. Failed leases are discarded and a demand-driven background
thread refills small batches at least one second apart. No login or checkout
transport imports this module.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import hashlib
import ipaddress
import math
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote

import requests

from activity_log import ActivityLog
from private_state import read_private, write_private

MAX_PROVIDER_BODY = 128 * 1024


class ProxyError(ValueError):
    """Only a fixed safe code may cross the private provider boundary."""
    def __init__(self, code='proxy_network'):
        self.code = code if re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code) else 'proxy_network'
        super().__init__(self.code)


def public_ip(value):
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def public_url(value, *, resolve=True):
    """Provider settings cannot turn the inventory service into a LAN fetcher."""
    if not isinstance(value, str) or len(value) > 4096 or re.search(r'[\r\n\x00]', value):
        raise ProxyError('provider_configuration')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
        if parsed.hostname.lower() in ('localhost', 'localhost.localdomain'):
            raise ValueError()
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if resolve:
                addresses = {row[4][0] for row in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
                if not addresses or not all(public_ip(address) for address in addresses):
                    raise ValueError()
        else:
            if not public_ip(parsed.hostname):
                raise ValueError()
        return parsed
    except (ValueError, OSError):
        raise ProxyError('provider_configuration') from None


def _json_get(url, *, session_factory=requests.Session, maximum=MAX_PROVIDER_BODY):
    public_url(url)
    deadline = time.monotonic() + 12
    try:
        with session_factory() as session:
            session.trust_env = False
            with session.get(url, headers={'Accept': 'application/json'}, timeout=(4, 8),
                             allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ProxyError('provider_http_auth' if response.status_code in (401, 403) else 'provider_http')
                chunks, size = [], 0
                for chunk in response.iter_content(8192):
                    if time.monotonic() > deadline:
                        raise ProxyError('provider_timeout')
                    size += len(chunk)
                    if size > maximum:
                        raise ProxyError('provider_response_size')
                    chunks.append(chunk)
                import json
                result = json.loads(b''.join(chunks))
                if not isinstance(result, (dict, list)):
                    raise ProxyError('provider_response')
                return result
    except ProxyError:
        raise
    except Exception:
        raise ProxyError('provider_network') from None


def expires_at(value, now, ttl=180):
    if value is None:
        return now + ttl
    try:
        if type(value) in (int, float):
            result = float(value) / (1000 if value > 10_000_000_000 else 1)
        else:
            raw = str(value)
            # DM publishes China wall time with unpadded fields, especially
            # "0:..." after midnight. Parse the complete vendor form strictly;
            # datetime validates the actual calendar instead of normalizing it.
            legacy = re.fullmatch(r'(\d{4})([-/])(\d{1,2})\2(\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})', raw)
            if legacy:
                fields = [int(legacy.group(index)) for index in (1, 3, 4, 5, 6, 7)]
                parsed = datetime(*fields, tzinfo=timezone(timedelta(hours=8)))
            else:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            result = parsed.timestamp()
        if not now + 2 < result <= now + 86400:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise ProxyError('provider_expiry') from None


@dataclass
class Lease:
    url: str
    expires_at: float
    provider_id: str
    slot: int = -1
    acquired_at: float = 0
    status: str = 'active'
    busy: bool = False


class Provider:
    def __init__(self, config, request_json=_json_get, now=time.time):
        self.config = dict(config)
        self.id = config['id']
        self.kind = config.get('kind', 'generic')
        self.request_json = request_json
        self.now = now
        self.whitelisted = False
        self.on_event = None
        self.fingerprint = hashlib.sha256(str(sorted(config.items())).encode()).hexdigest()

    def ensure_whitelist(self):
        url = self.config.get('whitelist_url')
        if not url and self.kind == 'xydaili':
            parsed = public_url(self.config.get('extract_url', ''), resolve=False)
            original = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if original.get('OrderNumber'):
                url = urlunsplit(parsed._replace(query=urlencode({'OrderNumber': original['OrderNumber'], 'action': 'addwhite'})))
        if not url or self.whitelisted:
            return
        # XY's provider discovers the real source itself: no externally guessed IP.
        parsed = public_url(url, resolve=False)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if self.kind == 'xydaili':
            query.pop('ip', None)
            query['action'] = 'addwhite'
        result = self.request_json(urlunsplit(parsed._replace(query=urlencode(query))))
        code = str(result.get('code', result.get('status', ''))) if isinstance(result, dict) else ''
        if code not in ('0', '200', '1007') or isinstance(result, dict) and result.get('success') is False:
            raise ProxyError('provider_whitelist')
        self.whitelisted = True
        if self.on_event:
            self.on_event('whitelist_ready', {'provider_id': self.id})

    def extract(self, count):
        self.ensure_whitelist()
        parsed = public_url(self.config.get('extract_url', ''), resolve=False)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if self.kind == 'dmdaili':
            query.update(getnum=str(count), httptype='1', geshi='2')
        elif self.kind == 'xydaili':
            query.update(qty=str(min(count, 10)), split='json', protocol='1')
            query.setdefault('action', 'GetIP')
        else:
            query['count'] = str(count)
        result = self.request_json(urlunsplit(parsed._replace(query=urlencode(query))))
        if isinstance(result, dict):
            if result.get('success') is False:
                raise ProxyError('provider_response')
            code = str(result.get('code', result.get('status', '200')))
            if code not in ('0', '200'):
                raise ProxyError('provider_response')
            rows = result.get('data', result.get('proxies'))
            if isinstance(rows, dict):
                rows = rows.get('proxy_list', rows.get('list'))
        else:
            rows = result
        if not isinstance(rows, list) or not 1 <= len(rows) <= count:
            raise ProxyError('provider_response')
        leases, seen = [], set()
        for row in rows:
            if isinstance(row, str):
                candidate = urlsplit(row if '://' in row else 'http://' + row)
                row = {'ip': candidate.hostname, 'port': candidate.port,
                       'username': candidate.username or '', 'password': candidate.password or ''}
            if not isinstance(row, dict) or not public_ip(row.get('ip', '')):
                raise ProxyError('provider_endpoint')
            try:
                port = int(row.get('port'))
            except (TypeError, ValueError):
                raise ProxyError('provider_endpoint') from None
            if not 1 <= port <= 65535:
                raise ProxyError('provider_endpoint')
            user = row.get('userName', row.get('username', self.config.get('username', '')))
            password = row.get('pwd', row.get('password', self.config.get('password', '')))
            if not isinstance(user, str) or not isinstance(password, str) or len(user + password) > 512 or re.search(r'[\r\n\x00]', user + password) or bool(user) != bool(password):
                raise ProxyError('provider_endpoint')
            host = '[' + row['ip'] + ']' if ':' in row['ip'] else row['ip']
            endpoint = 'http://' + ((quote(user, safe='') + ':' + quote(password, safe='') + '@') if user else '') + host + ':' + str(port)
            if endpoint in seen:
                raise ProxyError('provider_duplicate')
            seen.add(endpoint)
            expiry = row.get('endtime', row.get('expire', row.get('expires_at')))
            if expiry is None and self.kind != 'generic':
                raise ProxyError('provider_expiry')
            leases.append(Lease(endpoint, expires_at(expiry, self.now(), self.config.get('ttl_seconds', 180)), self.id,
                                acquired_at=self.now()))
        return leases


class DynamicProxyPool:
    def __init__(self, config, state_path, *, provider_factory=Provider, now=time.time):
        self.config = config
        self.state_path = state_path
        self.activity = ActivityLog.for_settings(Path(state_path).parent, action='inventory_check', category='proxy')
        self.now = now
        self.pool_size = config.get('pool_size', 10)
        self.enabled = config.get('enabled', False)
        self.providers = [provider_factory(row) for row in config.get('providers', []) if row.get('enabled', True)]
        for provider in self.providers:
            provider.on_event = lambda event, details: self._event(event, details)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.worker = None
        self.cursor = 0
        self.provider_cursor = 0
        self.demand_until = 0
        self.last_error = None
        self.state = read_private(state_path, {'version': 1, 'leases': [], 'events': [], 'next_extract_at': 0})
        if not isinstance(self.state, dict) or self.state.get('version') != 1:
            raise ValueError('代理池持久化状态无效。')
        self.leases = []
        valid_providers = {p.id for p in self.providers}
        fingerprints = {p.id: p.fingerprint for p in self.providers}
        old_fingerprints = self.state.get('provider_fingerprints', {})
        for raw in self.state.get('leases', []):
            try:
                lease = Lease(**raw)
                lease.busy = False
                if lease.provider_id in valid_providers and public_ip(urlsplit(lease.url).hostname or ''):
                    if old_fingerprints.get(lease.provider_id) != fingerprints.get(lease.provider_id) and lease.status == 'active':
                        lease.status = 'retired'
                    self.leases.append(lease)
            except (TypeError, ValueError):
                raise ValueError('代理池持久化状态无效。') from None
        self._reconcile()
        self.state['provider_fingerprints'] = fingerprints

    def _event(self, event, details, *, level='info'):
        self.activity.record('proxy_' + event, '库存代理池状态已更新。',
                             details=details, level=level, category='proxy')

    def _reconcile(self):
        for lease in self.leases:
            if lease.expires_at <= self.now() and lease.status != 'discarded':
                if lease.status not in ('expired', 'retired'):
                    self._event('lease_expired', {'provider_id': lease.provider_id, 'slot': lease.slot,
                                                  'expires_at': lease.expires_at})
                lease.status = 'expired'
            if lease.slot >= self.pool_size and lease.status == 'active':
                lease.status = 'retired'
        self.leases = [p for p in self.leases if p.expires_at > self.now() - 86400][-4096:]

    def _save(self):
        self.state['leases'] = [dict(asdict(p), busy=False) for p in self.leases]
        self.state['events'] = [event for event in self.state.get('events', []) if event.get('at', 0) > self.now() - 86400][-4096:]
        write_private(self.state_path, self.state)

    def _needed(self):
        active = {p.slot: p for p in self.leases if p.status == 'active' and p.expires_at > self.now()}
        # A near-expiry lease remains usable until the replacement succeeds.
        return [slot for slot in range(self.pool_size) if slot not in active] + [
            slot for slot in range(self.pool_size) if slot in active and active[slot].expires_at - self.now() <= 10]

    def touch(self):
        if not self.enabled or not self.providers or self.closed.is_set():
            return
        with self.lock:
            self.demand_until = self.now() + 20
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._maintain, name='inventory-proxy-refill', daemon=True)
                self.worker.start()

    def _maintain(self):
        while not self.closed.is_set():
            with self.lock:
                self._reconcile()
                if self.now() > self.demand_until:
                    return
                slots = self._needed()
                ready = self.now() >= self.state.get('next_extract_at', 0)
                if slots and ready:
                    provider = self.providers[self.provider_cursor % len(self.providers)]
                    self.provider_cursor += 1
                    slots = slots[:max(1, math.ceil(self.pool_size / 5))]
                    self.state['next_extract_at'] = self.now() + 1
                    self.state.setdefault('events', []).append({'provider_id': provider.id, 'at': self.now(), 'requested': len(slots), 'state': 'reserved'})
                    self._save()  # Reserve paid work durably before contacting the vendor.
                    self._event('refill_reserved', {'provider_id': provider.id, 'requested_count': len(slots),
                                                    'pool_size': self.pool_size})
                else:
                    provider = None
            if provider:
                try:
                    leases = provider.extract(len(slots))
                    with self.lock:
                        if self.closed.is_set():
                            return
                        accepted = 0
                        for slot, lease in zip(slots, leases):
                            host = urlsplit(lease.url).hostname
                            if any(p.expires_at > self.now() and (p.url == lease.url or urlsplit(p.url).hostname == host)
                                   and p.status in ('active', 'discarded') for p in self.leases):
                                continue
                            for old in self.leases:
                                if old.slot == slot and old.status == 'active':
                                    old.status = 'retired'
                            lease.slot = slot
                            self.leases.append(lease)
                            accepted += 1
                        self.last_error = None
                        self.state['events'][-1]['state'] = 'completed'
                        self._save()
                        self._event('refill_complete', {'provider_id': provider.id, 'requested_count': len(slots),
                                                       'accepted_count': accepted, 'pool_size': self.pool_size})
                except Exception as error:
                    with self.lock:
                        self.last_error = error.code if isinstance(error, ProxyError) else 'provider_network'
                        self.state['events'][-1]['state'] = self.last_error
                        # Provider failures are paced, never tight-looped or leaked.
                        self.state['next_extract_at'] = max(self.state['next_extract_at'], self.now() + 5)
                        self._save()
                        self._event('refill_failed', {'provider_id': provider.id, 'error_code': self.last_error,
                                                     'requested_count': len(slots)}, level='warning')
            self.closed.wait(.1)

    def acquire(self):
        self.touch()
        with self.lock:
            self._reconcile()
            candidates = [p for p in self.leases if p.status == 'active' and not p.busy and p.expires_at > self.now() + 8]
            if not candidates:
                raise ProxyError('proxy_pool_empty')
            lease = candidates[self.cursor % len(candidates)]
            self.cursor += 1
            lease.busy = True
            return lease

    def release(self, lease, failed=False):
        with self.lock:
            lease.busy = False
            if failed:
                lease.status = 'discarded'
                self._event('lease_discarded', {'provider_id': lease.provider_id, 'slot': lease.slot,
                                                'expires_at': lease.expires_at}, level='warning')
            self._save()
        self.touch()

    def status(self):
        with self.lock:
            self._reconcile()
            active = [p for p in self.leases if p.status == 'active' and p.expires_at > self.now()]
            return {'enabled': self.enabled, 'pool_size': self.pool_size, 'active_count': len(active),
                    'dispatchable_count': sum(not p.busy and p.expires_at > self.now() + 8 for p in active),
                    'discarded_count': sum(p.status == 'discarded' for p in self.leases),
                    'refill_count': len(self._needed()), 'maintaining': bool(self.worker and self.worker.is_alive()),
                    'max_acquire_per_request': max(1, math.ceil(self.pool_size / 5)),
                    'next_extract_at': self.state.get('next_extract_at'), 'last_error': self.last_error,
                    'requested_last_hour': sum(e.get('requested', 0) for e in self.state.get('events', []) if e.get('at', 0) > self.now() - 3600)}

    def close(self):
        self.closed.set()
        if self.worker:
            self.worker.join(timeout=.2)
