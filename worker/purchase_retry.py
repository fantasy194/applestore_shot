"""Opt-in serial retries of complete, positively unsubmitted purchase rounds.

The caller retains its account lock. A round callable must finish its cleanup
before returning or raising (including all parallel child processes). Submission
evidence is sticky; an uncertain order is never replayed by this runner.
"""
import json
from pathlib import Path
import re
import time


def retry_count(params):
    count = params.get('retry_count', 0)
    if type(count) is not int or not 0 <= count <= 10:
        raise ValueError('下单失败重试次数必须为 0–10 的整数。')
    if count and params.get('prepare_only') is True:
        raise ValueError('提前准备不支持失败自动重试，请将重试次数设为 0。')
    if count and params.get('_parallel_child') is True:
        raise ValueError('并发子任务不能嵌套自动重试，子任务重试次数必须为 0。')
    return count


def _nodes(result):
    if isinstance(result, dict):
        yield result
        for key in ('result', 'attempts', 'pending_orders'):
            child = result.get(key)
            if isinstance(child, dict):
                yield from _nodes(child)
            elif isinstance(child, list):
                for item in child:
                    yield from _nodes(item)


def _submitted_result(result):
    return any(node.get('order_submitted') is True or node.get('submitted') is True
               or node.get('status') in ('pending_payment', 'submission_unknown', 'unknown_submission',
                                         'verification_required', 'order_created', 'paid')
               or bool(node.get('order_number')) or bool(node.get('pending_orders'))
               for node in _nodes(result))


def _auth_required(service, result, error):
    if (getattr(service, 'authentication_failed', False) is True
            or getattr(service, 'authentication_requested', False) is True
            or getattr(service, 'purchase_auth_interaction', False) is True):
        return True
    for node in _nodes(result):
        if (node.get('auth_failed') is True or node.get('authentication_failed') is True
                or node.get('requires_serial_checkout') is True
                or node.get('error_code') in ('CHECKOUT_REAUTH_REQUIRED', 'PARALLEL_SESSION_REAUTH_REQUIRED')
                or node.get('status') in ('auth_failed', 'authentication_failed', 'login_required')):
            return True
    # Older child workers only provide their fixed guard message, without a code.
    text = str(error) if error else ''
    text += ' '.join(str(node.get('error', '')) for node in _nodes(result))
    return bool(re.search(r'需要重新登录|需要验证|未确认有效登录|未识别为已登录|仍要求登录|重新认证未通过|请先.*完成登录', text))


def _read(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return b'__unreadable_private_record__'


class SubmissionEvidence:
    def __init__(self, service):
        self.service = service
        self.root = Path(service.root)
        self.task_dir = Path(service.task_dir)
        self.task_id = service.channel.task_id
        self.account_id = service.channel.account_id
        self.root_markers = [self.root / name for name in ('order_submission.json', 'test_order_submission.json')]
        # An old unrelated order must not prevent the owner's new purchase or
        # cause its clean pre-submission failure to be mistaken for submission.
        self.baseline = {path: _read(path) for path in self.root_markers}

    def _mark_submitted(self):
        self.service.submitted = True
        return True

    def found(self, result=None):
        if self.service.submitted is True or _submitted_result(result):
            return self._mark_submitted()
        for path in self.root_markers:
            raw = _read(path)
            if raw is None:
                continue
            try:
                record = json.loads(raw)
                if isinstance(record, dict) and record.get('task_id') == self.task_id:
                    if record.get('state') in ('pending_payment', 'submission_unknown') or _submitted_result(record):
                        return self._mark_submitted()
                elif raw != self.baseline[path]:
                    # This isolated account operation changed a durable record
                    # without the expected task identity: do not infer safe replay.
                    return self._mark_submitted()
            except (ValueError, TypeError):
                if raw != self.baseline[path]:
                    return self._mark_submitted()
        files = [self.task_dir / name for name in ('payment.json', 'order_submission.json',
                                                   'test_order_submission.json', 'parallel_attempts.json')]
        # Harvest even a child whose IPC or parent's manifest write was lost.
        sandbox = self.task_dir / 'parallel'
        if sandbox.exists():
            for account in sandbox.glob('*/accounts/' + str(self.account_id)):
                files.extend(account / name for name in ('order_submission.json', 'test_order_submission.json'))
                files.extend(account.glob('tasks/*/payment.json'))
        for path in files:
            if path in self.root_markers:
                continue  # Already checked identity; never reinterpret old history.
            raw = _read(path)
            if raw is None:
                continue
            if path.name == 'payment.json':
                return self._mark_submitted()  # Includes payment verification_required/unknown.
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                return self._mark_submitted()
            if not isinstance(record, dict):
                return self._mark_submitted()
            if record.get('state') in ('pending_payment', 'submission_unknown') or _submitted_result(record):
                return self._mark_submitted()
        return False


def _summary(configured, made, reason):
    return {'retry_count': configured, 'attempts_made': made,
            'retries_used': max(0, made - 1), 'stop_reason': reason}


def _failure_progress(service, summary):
    reason = summary['stop_reason']
    suffix = {'submitted': '发现提交或待付款记录，已停止自动重试，请先核对订单。',
              'authentication': '本次认证未完成，当前下单尝试已结束。',
              'non_retryable': '当前失败需要人工处理，已停止自动重试。',
              'exhausted': '失败重试次数已用完。'}[reason]
    service.channel.emit('progress_result', result={'success': False, 'status': 'purchase_retry',
                         'message': f"已执行 {summary['attempts_made']} 次下单，{suffix}", 'retry_summary': summary})


def _wait(channel, seconds):
    deadline = time.monotonic() + seconds
    while True:
        channel.checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.1, remaining))


def run_purchase(service, params, attempt, *, backoff_seconds=1):
    configured = retry_count(params)
    evidence = SubmissionEvidence(service)
    for made in range(1, configured + 2):
        service.channel.checkpoint()
        if made > 1:
            # Only these per-round review caches are reset. Authentication,
            # submitted state and durable records are never cleared by retries.
            service.pickup_selected = None
            service.order_details = None
            service.prepared_order = None
        service.stage(f'下单第 {made}/{configured + 1} 次尝试；已使用 {made - 1}/{configured} 次失败重试')
        result, error = None, None
        try:
            result = attempt()
        except Exception as caught:
            error = caught
        finally:
            # Also harvest on Cancelled/KeyboardInterrupt without catching them.
            # A cancellation must not erase a marker written just before IPC.
            submitted = evidence.found(result)
        # Cancelled inherits BaseException and is deliberately never caught.
        service.channel.checkpoint()
        authentication = _auth_required(service, result, error)
        failed = error is not None or isinstance(result, dict) and (
            result.get('success') is False or result.get('status') in ('failed', 'error', 'purchase_failed'))
        non_retryable = getattr(error, 'retryable', True) is False or any(node.get('retryable') is False or node.get('status') == 'cancelled'
                            for node in _nodes(result))
        reason = ('submitted' if submitted else 'authentication' if authentication and failed
                  else 'non_retryable' if non_retryable and failed
                  else 'success' if not failed else 'exhausted' if made > configured else None)
        if reason is not None:
            summary = _summary(configured, made, reason)
            if error is not None:
                # Preserve the last exception's type and message. Protocol's
                # existing network-error boundary hides requests' private URLs.
                try:
                    error.retry_summary = summary
                except (AttributeError, TypeError):
                    pass
                _failure_progress(service, summary)
                service.stage(f'下单尝试已停止；共执行 {made} 次，使用 {made - 1}/{configured} 次失败重试')
                raise error
            if isinstance(result, dict):
                result = {**result, 'retry_summary': summary}
            return result
        service.stage(f'第 {made} 次下单失败且未发现提交记录；等待后串行重试 {made}/{configured}')
        _wait(service.channel, backoff_seconds)
        # Recheck cancellation and any delayed durable evidence before a new
        # round, even after the preceding parallel batch completed its cleanup.
        if evidence.found(result):
            summary = _summary(configured, made, 'submitted')
            if error is not None:
                error.retry_summary = summary
                _failure_progress(service, summary)
                raise error
            return {**result, 'retry_summary': summary}
