"""Immediate action-owned account verification; no cross-action session proof.

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
    # This proof belongs only to the current action account verification.
    service._purchase_session_proof = {**_identity(service),
        'verified_at_ms': int(time.time() * 1000) if now_ms is None else now_ms}


def verify_account_session(service, *, force_browser=False):
    """Use the current Session and official authentication entry once.

    A maintenance record is scheduling metadata, never a purchase veto.
    A healthy account read never submits credentials. An actual account login
    response uses the same protocol and saved trust as first enrollment; only
    an explicit merchant authx rejection delegates to the original browser.
    """
    service.channel.checkpoint()
    if Path(duo_killer.AUTH_PATH).resolve() != Path(service.auth).resolve():
        raise ValueError('当前会话存储与任务账号归属不一致，未发起官网请求。')
    service.ensure_auth_file()
    previous_failed = getattr(service, 'authentication_failed', False)
    if not force_browser:
        result = _read_or_restore(service)
    else:
        result = _restore_browser(service)
    # A current merchant rejection resolved by the bounded browser takeover is
    # no longer an authentication failure for the purchase that follows it.
    # Do not erase a failure that predated this restoration scope.
    service.authentication_failed = previous_failed
    return result


def _read_or_restore(service):
    from browser_login import BrowserSessionError
    auth=json.loads(Path(service.auth).read_text(encoding='utf-8'))
    cookies=auth.get('cookies') if isinstance(auth,dict) else None
    previous_auth = getattr(service, '_account_auth_active', False)
    service._account_auth_active = True
    try:
        if isinstance(cookies,dict) and not any(isinstance(rows,list) and rows for rows in cookies.values()):
            # There is no account session to authenticate in place. Reuse the
            # first-login routine, including its existing device-trust export.
            service.stage('尚无商城会话，使用首次登录流程和已保存设备信任自动登录')
            try:
                duo_killer.login()
            except Exception as error:
                if _browser_recovery_required(error, service):
                    return _restore_after_rejection(service)
                raise
            auth=json.loads(Path(service.auth).read_text(encoding='utf-8'))
            authenticate_current=False
        else:
            authenticate_current=True
        with requests.Session() as session:
            duo_killer.load_auth(session,auth)
            try:
                # account_info only requests credentials after this exact GET
                # returned an official account authentication entry. It retains
                # that response's Session, SSI/STK, dynamic URL and saved trust.
                result=duo_killer.account_info(session,reauthenticate=authenticate_current)
            except Exception as error:
                if not _browser_recovery_required(error, service):
                    raise
                # Persist current response rotations/deletions before browser
                # handoff. Never revive the pre-request HTTP snapshot.
                duo_killer.save_auth(session)
                return _restore_after_rejection(service)
            if isinstance(result,dict) and result.get('success') is True and result.get('logged_in') is True:
                if str(result.get('apple_id','')).strip().casefold()!=service.account.strip().casefold():
                    raise BrowserSessionError('APPLE_ACCOUNT_MISMATCH',
                        '官网当前登录账号与任务账号不一致，未开始购买。',status='failed',requires_login=False)
                duo_killer.save_auth(session)
                return {**result,'status':'healthy'}
    finally:
        service._account_auth_active = previous_auth
    raise BrowserSessionError('PURCHASE_SESSION_UNVERIFIED',
        '官网当前会话暂时无法核验，已保留登录状态；未重新登录或开始购买。',
        status='failed',requires_login=False)


def _browser_recovery_required(error, service):
    """A current Shop block or known-password context rejection permits one handoff."""
    from apple_login import LoginError, recoverable_password_rejection
    seen=set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        if isinstance(error,LoginError) and error.status==541 and error.phase=='store_exchange':
            return True
        if recoverable_password_rejection(error, service):
            return True
        error=getattr(error,'__cause__',None)
    return False


def _restore_after_rejection(service):
    previous = getattr(service, '_http_session_rejected', False)
    service._http_session_rejected = True
    try:
        return _restore_browser(service)
    finally:
        service._http_session_rejected = previous


def _restore_browser(service):
    from browser_login import BrowserSessionError, login_in_browser
    service.stage('正在使用已保存凭据自动恢复官网会话')
    result = login_in_browser(service, timeout=120, maintenance=True, storefront=True,
                              recover=True, automatic=True)
    if (not isinstance(result, dict) or result.get('success') is not True
            or result.get('logged_in') is not True or result.get('status') not in (None, 'healthy')
            or str(result.get('apple_id', '')).strip().casefold() != service.account.strip().casefold()):
        result = result if isinstance(result, dict) else {}
        failure_status = result.get('status')
        raise BrowserSessionError(result.get('error_code', 'PURCHASE_SESSION_UNVERIFIED'),
            result.get('message', '官网自动登录未完成，未开始加购或下单。'),
            status=failure_status if failure_status in ('blocked', 'failed', 'requires_login') else 'requires_login',
            requires_login=result.get('requires_login') is True or failure_status == 'requires_login',
            http_status=result.get('http_status'))
    return result


def session_ready(service, *, now_ms=None):
    current = int(time.time() * 1000) if now_ms is None else now_ms
    proof = getattr(service, '_purchase_session_proof', None)
    return (isinstance(proof, dict)
            and all(proof.get(key) == value for key, value in _identity(service).items())
            and type(proof.get('verified_at_ms')) is int
            and 0 <= current - proof['verified_at_ms'] < RECENT_MS)


def action_session_ready(service):
    """Current account owner proof, including its exclusive queue executor."""
    if session_ready(service):return True
    owner=getattr(service,'_purchase_session_owner',None)
    if owner is None:return False
    proof=getattr(owner,'_purchase_session_proof',None)
    # This target and its configured retries already passed owner admission.
    # A wave refresh only happens after active targets have drained.
    return (isinstance(proof,dict)
        and all(proof.get(key)==value for key,value in _identity(service).items()))


def ensure_purchase_session(service):
    if service._parallel_child is True:
        return  # Parent validated before creating this isolated auth snapshot.
    owner = getattr(service, '_purchase_session_owner', None)
    if owner is not None:
        # A nested serial executor shares the current queue wave, never creates
        # a second browser or starts a refresh from a retry.
        proof = getattr(owner, '_purchase_session_proof', None)
        if (not isinstance(proof, dict)
                or any(proof.get(key) != value for key, value in _identity(service).items())):
            raise ValueError('父队列的购买会话核验已过期，未在子任务中刷新或开始购买。')
        return
    if session_ready(service):
        return
    from browser_login import BrowserSessionError
    from browser_process import background_browser_scope
    previous_headless, previous_idle = service.headless_browser, service.idle_maintenance
    previous_auth_requested = service.authentication_requested
    previous_purchase_auth = service.purchase_auth_interaction
    service.headless_browser = True
    service.idle_maintenance = True
    try:
        service.stage('购买前快速核对官网账号会话，尚未加购或下单')
        with background_browser_scope():
            verify_account_session(service)
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
                   'message': '本次官网自动会话恢复未完成，未开始加购或下单；后续购买可立即重新核验。',
                   'cooldown_until_ms': stamp + 600_000}
        duo_killer.write_private_json(Path(service.root) / 'session-maintenance.json', failure)
        if blocked and not isinstance(error, BrowserSessionError):
            # Raw requests errors can contain private URLs. Report only the
            # confirmed response status while retaining the original cause.
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                f'官网购买会话核验返回 HTTP {http_status}，自动恢复未完成；未开始加购或下单。',
                status='blocked', requires_login=True, http_status=http_status) from error
        raise
    finally:
        service.headless_browser, service.idle_maintenance = previous_headless, previous_idle
