"""Idle login or pickup preheat, with a hard no-order preparation boundary."""
import json
import time
import re
import requests

import duo_killer


AUTHENTICATION_PHASES=frozenset(('password_authentication','authentication_context','store_exchange',
                               'store_redirect','prepared_renewal'))
AUTHENTICATION_REASONS=frozenset(('unsupported_signin_scope','redirect_missing','redirect_host_mismatch',
    'redirect_scope_mismatch','redirect_method_invalid','redirect_args_invalid','current_signin_required',
    'checkout_response_unverified','configuration_changed','authorization_changed','goods_changed',
    'renewal_recovery_failed'))


class MaintenanceFailure(ValueError):
    """A bounded current preparation failure, separate from historical auth flags."""
    def __init__(self, code, message, *, reason, requires_login=False):
        if (code not in ('PREPARED_RENEWAL_UNVERIFIED','PREPARED_RENEWAL_AUTHENTICATION_FAILED')
                or not isinstance(reason,str) or reason not in AUTHENTICATION_REASONS or type(requires_login) is not bool):
            raise ValueError('维护失败诊断类型无效。')
        super().__init__(message)
        self.code=code;self.reason=reason;self.phase='prepared_renewal'
        self.requires_login=requires_login;self.status='requires_login' if requires_login else 'failed'


def _causes(error):
    seen=set()
    for _ in range(8):
        if error is None or id(error) in seen:return
        seen.add(id(error));yield error
        error=getattr(error,'__cause__',None)


def authentication_failure_diagnostics(error):
    """Export bounded protocol facts, never exception text or private payloads."""
    result={}
    for cause in _causes(error):
        phase=getattr(cause,'phase',None)
        if isinstance(phase,str) and phase in AUTHENTICATION_PHASES:
            result.setdefault('authentication_phase',phase)
        reason=getattr(cause,'reason',None)
        if isinstance(reason,str) and reason in AUTHENTICATION_REASONS:
            result.setdefault('authentication_reason',reason)
        status=getattr(cause,'http_status',None)
        if type(status) is not int:
            status=getattr(cause,'status',None)
        if type(status) is not int and isinstance(cause,requests.RequestException) and cause.response is not None:
            status=cause.response.status_code
        if type(status) is int and 100<=status<=599:
            result.setdefault('http_status',status)
    return result


def record_maintenance_result(service, result, *, cooling=False, warm_only=False):
    activity = getattr(getattr(service, 'channel', None), 'activity', None)
    if activity is not None:
        details = {key: result[key] for key in ('status', 'error_code', 'http_status', 'authentication_phase', 'authentication_reason', 'checked_at_ms', 'preheat_flow', 'preparation_stage', 'order_ready', 'review_verified', 'browser_mode')
                   if key in result}
        details.update(operation='session_maintain', transport='shared_session'
                       if warm_only and result.get('preheat_flow') == 'pickup_prepare' else 'official_browser',
                       reason='cooldown' if cooling else 'checked')
        activity.record('session_maintenance_result', '官网会话维护结果已记录。',
                        category='authentication', details=details,
                        level='info' if result.get('success') else 'warning')


def maintain_session(service, *, now=time.time, warm_only=False, preheat_flow='login', sku=None, count=None,
                     interval_minutes=5):
    if type(interval_minutes) is not int or not 1 <= interval_minutes <= 60:
        raise ValueError('空闲后预热间隔必须是 1–60 的整数分钟。')
    interval_ms = interval_minutes * 60_000
    if preheat_flow not in ('login', 'pickup_prepare', 'browser_login'):
        raise ValueError('预热流程无效。')
    browser_mode = 'background' if getattr(service, 'headless_browser', False) is True else 'visible'
    path = service.root / 'session-maintenance.json'
    stamp = int(now() * 1000)
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            previous = {}
        previous = previous if isinstance(previous, dict) else {}
        checked_at = previous.get('checked_at_ms', 0)
        if (previous.get('status') not in ('healthy', 'prepared', 'waiting_stock')
                and type(checked_at) in (int, float) and checked_at > 0
                and 0 <= stamp - checked_at < interval_ms):
            result = {**previous, 'preheat_flow': preheat_flow, 'browser_mode': browser_mode, 'cooldown_until_ms': checked_at + interval_ms, 'message': '等待下一次空闲会话维护；购买可立即自动核验。'}
            record_maintenance_result(service, result, cooling=True, warm_only=warm_only)
            return result
    from browser_login import BrowserSessionError
    from purchase_session import verify_account_session
    try:
        if preheat_flow == 'pickup_prepare':
            prepared = service.warm_pickup_prepare(sku, count)
            if isinstance(getattr(service,'_prepared_claim',None),dict) or prepared.get('status')=='preparation_expired':
                return prepared
            # Only an explicitly verified Review is a complete preparation. Stock-out
            # may preserve a useful Fulfillment session but is never healthy/order-ready.
            full = (prepared.get('success') is True and prepared.get('status') == 'prepared'
                    and prepared.get('preparation_stage') == 'review'
                    and prepared.get('order_ready') is True and prepared.get('review_verified') is True
                    and prepared.get('order_submitted') is not True)
            partial = (prepared.get('success') is True and prepared.get('status') == 'waiting_stock'
                       and prepared.get('preparation_stage') == 'fulfillment'
                       and prepared.get('order_ready') is False and prepared.get('review_verified') is False
                       and prepared.get('order_submitted') is not True)
            result = {'success': full or partial,
                      'status': 'prepared' if full else 'waiting_stock' if partial else 'failed',
                      'requires_login': False, 'warm_login': False,
                      'order_ready': full, 'review_verified': full,
                      'message': prepared.get('message', '自提预热未完成；未创建订单。'),
                      **{key: prepared[key] for key in ('preparation_stage', 'blocked_reason',
                          'error_code', 'pickup_store', 'pickup_store_id', 'pickup_time_slot') if key in prepared}}
        else:
            if preheat_flow == 'browser_login':
                checked = verify_account_session(service)
            else:
                checked = service.warm_login() if warm_only else verify_account_session(service)
            status = checked.get('status', 'healthy' if checked.get('logged_in') is True else 'requires_login')
            status = status if status in ('healthy', 'requires_login', 'blocked', 'failed') else 'failed'
            result = {'success': status == 'healthy', 'status': status,
                      'requires_login': status in ('requires_login', 'blocked'),
                      'message': checked.get('message', '官网会话检查完成。'),
                      'warm_login': warm_only and status == 'healthy',
                      **{key: checked[key] for key in ('error_code', 'http_status', 'browser_started', 'browser_connected')
                         if key in checked}}
    except Exception as error:
        if isinstance(getattr(service,'_prepared_claim',None),dict):
            raise
        diagnostics=authentication_failure_diagnostics(error)
        causes=list(_causes(error))
        explicit=next((cause for cause in causes if isinstance(cause,(BrowserSessionError,MaintenanceFailure))),None)
        from apple_login import LoginError
        current_auth_failure=any(isinstance(cause,LoginError) and cause.status in (400,401,409)
            and cause.phase in ('password_authentication','authentication_context','store_exchange') for cause in causes)
        http_status = diagnostics.get('http_status')
        if http_status is None and isinstance(error, requests.RequestException) and error.response is not None:
            http_status = error.response.status_code
        legacy_status = re.search(r'\bHTTP\s+(403|429|541)\b', str(error))
        if http_status is None and legacy_status:
            http_status = int(legacy_status[1])
        if http_status in (403, 429, 541):
            result = {'success': False, 'status': 'blocked', 'requires_login': True,
                      'http_status': http_status, 'error_code': 'APPLE_SESSION_BLOCKED',
                      'message': '官网限制了本次自动会话恢复，将在下一空闲周期重试。'}
        elif isinstance(explicit,MaintenanceFailure):
            result={'success':False,'status':explicit.status,'requires_login':explicit.requires_login,
                    'error_code':explicit.code,'message':str(explicit)}
        elif isinstance(explicit, BrowserSessionError):
            result = {'success': False, 'status': explicit.status, 'requires_login': explicit.requires_login,
                      'error_code': explicit.code, 'message': str(explicit),
                      'browser_started': explicit.browser_started, 'browser_connected': explicit.browser_connected}
        elif diagnostics.get('authentication_phase')=='store_redirect':
            result={'success':False,'status':'failed','requires_login':False,
                    'error_code':'STORE_LOGIN_REDIRECT_INVALID',
                    'message':'官网认证已返回，但商城跳转未通过当前流程核验；账号和凭据已保留。'}
        elif current_auth_failure:
            result = {'success': False, 'status': 'requires_login', 'requires_login': True,
                      'error_code': 'WARM_LOGIN_REQUIRES_ATTENTION',
                      'message': '本次自动认证未完成，将在下一空闲周期重试；未创建订单。'}
        else:
            network = any(isinstance(cause,requests.RequestException) for cause in causes)
            local_io = any(isinstance(cause,OSError) for cause in causes)
            result = {'success': False, 'status': 'failed', 'requires_login': False,
                      'error_code': 'SESSION_NETWORK_FAILED' if network else
                                    'BROWSER_LOCAL_IO_FAILED' if local_io else 'SESSION_CHECK_INCOMPLETE',
                      'message': ('会话检查的网络请求未完成；请检查网络，未判定登录过期。' if network else
                                  '无法访问本地浏览器资料或文件；会话状态尚未检查。' if local_io else
                                  '会话检查未完成，请查看任务诊断；未判定登录过期或重置账号。')}
        result.update(diagnostics)
    result['browser_mode'] = browser_mode
    result['preheat_flow'] = preheat_flow
    result['checked_at_ms'] = stamp
    result['cooldown_until_ms'] = (0 if result['status'] in ('healthy', 'prepared', 'waiting_stock') else stamp +
        interval_ms)
    duo_killer.write_private_json(path, result)
    record_maintenance_result(service, result, warm_only=warm_only)
    return result
