"""Immediate action-owned browser preparation; no cross-action session proof.

The queue wave window is local policy, never Apple session expiry.
"""
import json
import re
import time
from pathlib import Path

import duo_killer
import requests


RECENT_MS = 300_000


def _identity(service):
    from apple_login import browser_profile_path
    return {'account': service.account.strip().casefold(), 'auth_path': str(Path(service.auth).resolve()),
            'profile_path': str(browser_profile_path(service.auth).resolve())}


def record_action_session(service, *, now_ms=None):
    # This proof belongs only to the browser operation in the current action.
    service._purchase_session_proof = {**_identity(service),
        'verified_at_ms': int(time.time() * 1000) if now_ms is None else now_ms}


def _guard(service, current):
    from apple_login import browser_profile_path
    from browser_login import BrowserSessionError
    if (browser_profile_path(service.auth) / '.authx-541').exists():
        raise BrowserSessionError('APPLE_SESSION_BLOCKED',
            '商城会话需要官网登录恢复；账号和浏览器资料已保留，未开始购买。',
            status='blocked', requires_login=True, http_status=541)
    path = Path(service.root) / 'session-maintenance.json'
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
            until = previous.get('cooldown_until_ms', 0)
            if type(until) not in (int, float):
                raise ValueError()
        except (ValueError, OSError, AttributeError):
            raise ValueError('会话维护记录无法确认，请核对官网会话后重试；未开始购买。') from None
        if until > current:
            raise BrowserSessionError('PURCHASE_SESSION_COOLDOWN',
                '官网会话维护仍在冷却，请先完成官网登录恢复；未开始购买。',
                status='blocked', requires_login=True,
                http_status=previous.get('http_status'))


def session_ready(service, *, now_ms=None):
    current = int(time.time() * 1000) if now_ms is None else now_ms
    _guard(service, current)
    proof = getattr(service, '_purchase_session_proof', None)
    return (isinstance(proof, dict)
            and all(proof.get(key) == value for key, value in _identity(service).items())
            and type(proof.get('verified_at_ms')) is int
            and 0 <= current - proof['verified_at_ms'] < RECENT_MS)


def ensure_purchase_session(service):
    if service._parallel_child is True:
        return  # Parent validated before creating this isolated auth snapshot.
    owner = getattr(service, '_purchase_session_owner', None)
    if owner is not None:
        # A nested serial executor shares the current queue wave, never creates
        # a second browser or starts a refresh from a retry.
        current = int(time.time() * 1000)
        _guard(owner, current)
        proof = getattr(owner, '_purchase_session_proof', None)
        if (not isinstance(proof, dict)
                or any(proof.get(key) != value for key, value in _identity(service).items())):
            raise ValueError('父队列的购买会话核验已过期，未在子任务中刷新或开始购买。')
        return
    if session_ready(service):
        return
    from browser_login import BrowserSessionError, login_in_browser
    from browser_process import background_browser_scope
    previous_headless, previous_idle = service.headless_browser, service.idle_maintenance
    previous_auth_requested = service.authentication_requested
    previous_purchase_auth = service.purchase_auth_interaction
    service.headless_browser = True
    service.idle_maintenance = True
    try:
        service.stage('购买前刷新官网浏览器会话并核对账号，尚未加购或下单')
        with background_browser_scope():
            result = login_in_browser(service, timeout=12, maintenance=True, storefront=True)
        if (not isinstance(result, dict) or result.get('success') is not True
                or result.get('logged_in') is not True or result.get('status') not in (None, 'healthy')
                or str(result.get('apple_id', '')).strip().casefold() != service.account.strip().casefold()):
            result = result if isinstance(result, dict) else {}
            failure_status = result.get('status')
            raise BrowserSessionError(result.get('error_code', 'PURCHASE_SESSION_UNVERIFIED'),
                result.get('message', '官网购买会话尚未核验，未开始加购或下单。'),
                status=failure_status if failure_status in ('blocked', 'failed', 'requires_login') else 'requires_login',
                requires_login=(result.get('requires_login') is True or failure_status != 'failed'),
                http_status=result.get('http_status'))
        record_action_session(service)
        # Successful browser credential entry belongs to preparation, not a
        # later failed checkout round. Actual later checkout auth remains sticky.
        service.authentication_requested = previous_auth_requested
        service.purchase_auth_interaction = previous_purchase_auth
    except Exception as error:
        stamp = int(time.time() * 1000)
        http_status = getattr(error, 'http_status', None) or getattr(error, 'status', None)
        if http_status is None and isinstance(error, requests.RequestException) and error.response is not None:
            http_status = error.response.status_code
        legacy_status = re.search(r'\bHTTP\s+(403|429|541)\b', str(error))
        if http_status is None and legacy_status:
            http_status = int(legacy_status[1])
        blocked = http_status in (403, 429, 541) or getattr(error, 'status', None) == 'blocked'
        attention = getattr(error, 'requires_login', False) is True
        failure = {'success': False, 'status': 'blocked' if blocked else 'requires_login' if attention else 'failed',
                   'requires_login': blocked or attention, 'checked_at_ms': stamp,
                   'http_status': http_status if type(http_status) is int else None,
                   'error_code': 'APPLE_SESSION_BLOCKED' if blocked else getattr(error, 'code', 'PURCHASE_SESSION_CHECK_FAILED'),
                   'message': '购买前官网会话核验未通过，请在官网恢复后重试；未开始加购或下单。',
                   'cooldown_until_ms': stamp + (3_600_000 if blocked else 1_800_000 if attention else 300_000)}
        duo_killer.write_private_json(Path(service.root) / 'session-maintenance.json', failure)
        if blocked and not isinstance(error, BrowserSessionError):
            # Raw requests errors can contain private URLs. Report only the
            # confirmed response status while retaining the original cause.
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                f'官网购买会话核验返回 HTTP {http_status}，请先完成官网登录恢复；未开始加购或下单。',
                status='blocked', requires_login=True, http_status=http_status) from error
        raise
    finally:
        service.headless_browser, service.idle_maintenance = previous_headless, previous_idle
