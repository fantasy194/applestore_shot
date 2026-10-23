"""One account-owned streaming queue of isolated, bounded pickup jobs.

Rust owns queue durability and FIFO allocation. This worker holds the existing
account lock, authenticates the parent once, and acknowledges each allocated
queue_job_id exactly once. Browser/OTP authentication is never delegated.
"""
from collections import deque
import copy
import json
from pathlib import Path
import queue
import re
import threading
import time
import uuid
from types import SimpleNamespace

from parallel_checkout import PurchaseBatch, _read, _safe_result, _write, concurrency
from protocol import Cancelled
from purchase_retry import run_purchase, retry_count


MAX_BUFFERED = 200
MAX_JOBS = 1000


def normalize_queue_pickup_policy(value):
    """Freeze only authorized public pickup fields, never contact/signing data."""
    from pickup_options import validate_policy, validate_time_rule
    fields = {'selection_mode', 'stores', 'time_slot_rule', 'contact_mode'}
    if (not isinstance(value, dict) or set(value) - fields
            or value.get('selection_mode') != 'priority'):
        raise ValueError('队列候选门店规则仅允许公开的优先门店、时间段与联系人模式。')
    safe = validate_policy(copy.deepcopy(value))
    return {'selection_mode': 'priority', 'stores': safe['stores'],
            'time_slot_rule': validate_time_rule(safe.get('time_slot_rule')),
            'contact_mode': safe.get('contact_mode', 'local')}


def validate_target(value):
    if not isinstance(value, dict):
        raise ValueError('队列目标必须是对象。')
    target = {key: child for key, child in value.items() if key in (
        'queue_job_id', 'id', 'queue_assignment', 'sku', 'quantity', 'fulfillment',
        'pickup_store_id', 'pickup_store_name', 'pickup_location', 'concurrency',
        'retry_count', 'checked_at_ms', 'inventory_trigger', 'target_id', 'pickup_policy')}
    identity = target.get('queue_job_id', target.get('id'))
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', identity):
        raise ValueError('队列任务标识无效。')
    target['queue_job_id'] = identity
    assignment = target.get('queue_assignment', 0)
    if type(assignment) is not int or not 0 <= assignment <= 18_446_744_073_709_551_615:
        raise ValueError('队列分配代数无效。')
    target['queue_assignment'] = assignment
    sku = target.get('sku')
    if not isinstance(sku, str) or not re.fullmatch(r'[A-Za-z0-9]{5,30}(?:/[Aa])?', sku):
        raise ValueError('队列商品 SKU 无效。')
    if type(target.get('quantity')) is not int or not 1 <= target['quantity'] <= 4_294_967_295:
        raise ValueError('队列购买数量必须为正整数。')
    if target.get('fulfillment', 'RETAIL') != 'RETAIL':
        raise ValueError('自动队列仅支持指定门店自提。')
    if not isinstance(target.get('pickup_store_id'), str) or not re.fullmatch(r'R\d{1,8}', target['pickup_store_id']):
        raise ValueError('队列自提门店编号无效。')
    for key in ('pickup_store_name', 'pickup_location'):
        text = target.get(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise ValueError('队列门店名称或城市邮编无效。')
        target[key] = text.strip()
    target['fulfillment'] = 'RETAIL'
    target['concurrency'] = concurrency(target)
    target['retry_count'] = retry_count(target)
    if 'pickup_policy' in target:
        target['pickup_policy'] = normalize_queue_pickup_policy(target['pickup_policy'])
    return target


class ProcessSlots:
    """The cap counts actual subprocess lifetimes, not batches or target jobs."""
    def __init__(self, capacity=10):
        self.semaphore = threading.BoundedSemaphore(capacity)

    def acquire(self, channel):
        while True:
            channel.checkpoint()
            if self.semaphore.acquire(timeout=.05):
                break
        released = False
        lock = threading.Lock()
        def release():
            nonlocal released
            with lock:
                if not released:
                    released = True
                    self.semaphore.release()
        return release


def _private_result(value):
    if isinstance(value, dict):
        return {key: _private_result(child) for key, child in _safe_result(value).items()}
    if isinstance(value, list):
        return [_private_result(child) for child in value]
    return value


class TargetChannel:
    def __init__(self, owner, identity, task_id, stopped):
        self.owner, self.identity, self.stopped = owner, identity, stopped
        self.account_id = owner.channel.account_id
        self.task_id = task_id
        self.browser_task_id = owner.channel.task_id
        self.browser_task_dir = owner.service.task_dir
        self.base_channel = owner.channel
        self.base_service = owner.service
        self.assignment = owner.jobs[identity]['target']['queue_assignment']
        self.context = None

    def checkpoint(self):
        self.owner.channel.checkpoint()
        if self.stopped.is_set():
            raise Cancelled()

    def emit(self, kind, **payload):
        if payload.get('order_submitted') is True and self.context is not None:
            self.context.submitted = True
            self.owner.service.submitted = True
        if kind == 'progress_result':
            result = payload.get('result')
            if isinstance(result, dict):
                with self.owner.lock:
                    previous = self.owner.jobs[self.identity].get('progress') or {}
                    retained = list(previous.get('pending_orders') or [])
                    if previous.get('status') == 'pending_payment' and previous.get('order_number'):
                        retained.append({**previous, 'task_id': self.task_id})
                    if retained:
                        by_number = {order.get('order_number'):order for order in retained}
                        by_number.update({order.get('order_number'):order
                                          for order in result.get('pending_orders', [])})
                        result = {**result, 'pending_orders':list(by_number.values())}
                    self.owner.jobs[self.identity]['progress'] = copy.deepcopy(result)
                self.owner.channel.emit('queue_progress', queue_job_id=self.identity, result=result,
                                        queue_assignment=self.assignment,
                                        order_submitted=self.context.submitted is True)
        else:
            self.owner.channel.emit(kind, queue_job_id=self.identity,
                                    queue_assignment=self.assignment, **payload)

    def prompt(self, kind, label, **fields):
        # Channel has one current prompt; serialize interactions across jobs.
        while not self.owner.prompt_lock.acquire(timeout=.05):
            self.checkpoint()
        try:
            self.checkpoint()
            timeout = fields.pop('timeout', None)
            deadline = time.monotonic() + timeout if timeout is not None else None
            request_id = str(uuid.uuid4())
            self.owner.channel.emit('prompt', prompt={
                'id': request_id, 'kind': kind,
                'label': f'[队列 {self.identity[:8]}] {label}', **fields,
                'queue_job_id':self.identity, 'queue_assignment':self.assignment},
                queue_job_id=self.identity, queue_assignment=self.assignment)
            while True:
                self.checkpoint()
                if deadline is not None and time.monotonic() >= deadline:
                    raise ValueError('队列交互等待已超时，未自动继续。')
                try:
                    response = self.owner.channel.responses.get(timeout=.05)
                except queue.Empty:
                    continue
                self.checkpoint()
                if response and response.get('request_id') == request_id:
                    return response.get('value')
        finally:
            self.owner.prompt_lock.release()


def run_parent_serial(context, target):
    """Normal authenticated checkout, exclusively used when no jobs can overlap.

    All legacy module globals are process-owned. QueuedCheckout only invokes
    this executor at effective target concurrency one and route count one.
    """
    import alipay_qr
    import duo_killer as shop
    import interaction
    from service import Service
    saved_shop = {name: getattr(shop, name) for name in ('ROOT', 'AUTH_PATH', 'LOG_PATH', 'PAYMENT_ROOT')}
    saved_qr, saved_handler = alipay_qr.ROOT, interaction.handler
    nested, error = None, None
    try:
        nested = Service(context.channel, context.root, context.task_dir, context.account,
                         vault=context.parent_service.vault)
        nested.browser_task_id = context.channel.browser_task_id
        nested.browser_task_dir = context.channel.browser_task_dir
        # Only the outer per-target runner controls configured serial retries.
        params = {**target, 'concurrency': 1, 'retry_count': 0, '_parallel_child': False}
        return nested.run('purchase', params)
    except BaseException as caught:
        error = caught
        raise
    finally:
        if nested is not None:
            context.submitted = context.submitted or nested.submitted is True
            for name in ('authentication_failed', 'authentication_requested', 'purchase_auth_interaction'):
                if getattr(nested, name, False) is True:
                    setattr(context, name, True)
            try:
                nested.cleanup_credentials(error)
            finally:
                for name, value in saved_shop.items():
                    setattr(shop, name, value)
                alipay_qr.ROOT, interaction.handler = saved_qr, saved_handler
        else:
            for name, value in saved_shop.items():
                setattr(shop, name, value)
            alipay_qr.ROOT, interaction.handler = saved_qr, saved_handler


class QueuedCheckout:
    def __init__(self, service, params, *, worker_command=None, batch_factory=PurchaseBatch,
                 backoff_seconds=1, process_capacity=10, parent_executor=run_parent_serial):
        self.service, self.channel = service, service.channel
        self.params = dict(params)
        limit = params.get('queue_max_concurrency', 1)
        mode = params.get('queue_mode', 'sequential')
        if type(limit) is not int or not 1 <= limit <= 10 or mode not in ('sequential', 'parallel'):
            raise ValueError('队列模式或并发任务数无效（1–10）。')
        self.limit = limit if mode == 'parallel' else 1
        initial = params.get('queue_targets', [])
        if not isinstance(initial, list) or len(initial) > MAX_BUFFERED:
            raise ValueError('队列目标数过多或格式无效。')
        self.initial = [validate_target(target) for target in initial]
        self.commands = getattr(self.channel, 'commands', None)
        if self.commands is None:
            raise ValueError('当前业务通道不支持流式队列，请重启最新本地服务。')
        self.command = worker_command
        self.batch_factory = batch_factory
        self.parent_executor = parent_executor
        self.backoff = backoff_seconds
        self.slots = ProcessSlots(process_capacity)
        self.jobs = {}
        self.waiting = deque()
        self.running = {}
        self.completed = queue.Queue()
        self.lock = threading.RLock()
        self.prompt_lock = threading.Lock()
        self.closing = False
        self.paused = None
        self.idle_sent = False
        self.last_progress = 0

    def snapshot(self, *, finished=False):
        with self.lock:
            jobs = []
            orders = []
            attempts = []
            for identity, job in self.jobs.items():
                result = job.get('result') or job.get('progress') or {}
                jobs.append({'queue_job_id': identity, 'queue_assignment':job['target']['queue_assignment'],
                             'sku':job['target']['sku'], 'quantity':job['target']['quantity'],
                             'store_id':job['target']['pickup_store_id'],
                             'store_name':job['target']['pickup_store_name'],
                             'status': job['status'],
                             'started': job['started'], 'result': copy.deepcopy(result),
                             **({'pickup_policy':copy.deepcopy(job['target']['pickup_policy'])}
                                if 'pickup_policy' in job['target'] else {})})
                orders.extend({**order, 'queue_job_id': identity}
                              for order in result.get('pending_orders', []))
                if (not result.get('pending_orders') and result.get('status') == 'pending_payment'
                        and result.get('order_number')):
                    orders.append({**result, 'queue_job_id': identity, 'task_id': job.get('task_id')})
                attempts.extend({**attempt, 'queue_job_id': identity}
                                for attempt in result.get('attempts', []))
            return {'status': 'queue_complete' if finished else 'queue_running',
                    'success': any(job.get('result', {}).get('success') is True for job in self.jobs.values()),
                    'queue_jobs': jobs, 'pending_orders': orders, 'attempts': attempts,
                    'queue_job_ids': list(self.jobs),
                    'order_submitted': self.service.submitted is True,
                    'requires_serial_checkout': self.paused == 'authentication',
                    'pause_reason': self.paused}

    def _add(self, targets):
        if not isinstance(targets, list) or len(targets) > MAX_BUFFERED:
            raise ValueError('新增队列目标格式无效或数量过多。')
        validated = [validate_target(target) for target in targets]
        for target in validated:
            identity = target['queue_job_id']
            if identity in self.jobs:
                previous = self.jobs[identity]
                prior = previous.get('result') or {}
                if not (previous['acked'] and prior.get('status') == 'paused'
                        and prior.get('started') is False
                        and prior.get('pause_reason') in ('inventory_stale', 'authentication')
                        and target['queue_assignment'] > previous['target']['queue_assignment']):
                    continue  # Deduplicate delivery; only a fresh unstarted allocation may resume.
            if len(self.jobs) >= MAX_JOBS:
                raise ValueError('本次队列任务总数已达上限，请启动新协调任务。')
            self.jobs[identity] = {'target': target, 'status': 'queued', 'started': False,
                                   'stopped': threading.Event(), 'acked': False}
            if self.paused or self.closing or len(self.waiting) >= MAX_BUFFERED:
                self._finish(identity, {'status': 'paused', 'success': False,
                                       'started': False, 'order_submitted': False,
                                       'pause_reason': self.paused or ('closing' if self.closing else 'capacity')})
            else:
                self.waiting.append(identity)
                self.idle_sent = False

    def _finish(self, identity, result):
        with self.lock:
            job = self.jobs[identity]
            if job['acked']:
                return
            result['queue_assignment'] = job['target']['queue_assignment']
            job.update(result=result, status=result['status'], acked=True)
        self.channel.emit('queue_finished', queue_job_id=identity, result=result,
                          queue_assignment=job['target']['queue_assignment'],
                          order_submitted=result.get('order_submitted') is True)

    def _context(self, identity):
        job = self.jobs[identity]
        task_id = str(uuid.uuid4())
        job['task_id'] = task_id
        task_dir = self.service.root / 'tasks' / task_id
        task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        channel = TargetChannel(self, identity, task_id, job['stopped'])
        context = SimpleNamespace(root=self.service.root, task_dir=task_dir,
                                  auth=self.service.auth, checkout=self.service.checkout,
                                  account=self.service.account, channel=channel,
                                  parent_service=self.service,
                                  submitted=False, authentication_failed=False,
                                  authentication_requested=False, purchase_auth_interaction=False,
                                  pickup_selected=None, order_details=None, prepared_order=None)
        channel.context = context
        def stage(message, **fields):
            channel.checkpoint()
            channel.emit('stage', message=message, **fields)
        context.stage = stage
        return context

    def _recover(self, context):
        try:
            return _read(context.task_dir / 'parallel_attempts.json')
        except (OSError, ValueError, TypeError):
            with self.lock:
                return copy.deepcopy(self.jobs[context.channel.identity].get('progress') or {})

    def _run_target(self, identity, context):
        target = self.jobs[identity]['target']
        result = None
        try:
            def attempt():
                if context.parent_serial:
                    return self.parent_executor(context, target)
                batch = self.batch_factory(context, target, worker_command=self.command,
                                           skip_preflight=True, process_slots=self.slots)
                return batch.run()
            result = run_purchase(context, target, attempt, backoff_seconds=self.backoff)
        except Cancelled:
            result = {**self._recover(context), 'success': False,
                      'status': 'submission_unknown' if context.submitted else 'cancelled',
                      'retryable': False, 'message': '队列任务已停止，未自动重放。'}
        except Exception as error:
            # Match main.py's private network-error boundary.
            import requests
            message = '网络请求失败，请检查网络或系统代理。' if isinstance(error, requests.RequestException) else (
                str(error) if isinstance(error, (ValueError, OSError)) else f'队列任务未完成（{type(error).__name__}）。')
            result = {**self._recover(context), 'success': False,
                      'status': 'submission_unknown' if context.submitted else 'failed',
                      'message': message[:2000]}
            if hasattr(error, 'retry_summary'):
                result['retry_summary'] = error.retry_summary
        finally:
            if not isinstance(result, dict):
                result = {'success': False, 'status': 'submission_unknown' if context.submitted else 'failed',
                          'retryable': False, 'message': '队列任务异常中断。'}
            result.setdefault('status', 'submission_unknown' if context.submitted else
                              'succeeded' if result.get('success') is True else 'failed')
            result['started'] = True
            result['order_submitted'] = context.submitted is True
            if result['order_submitted']:
                self.service.submitted = True
            if result.get('requires_serial_checkout') or result.get('retry_summary', {}).get('stop_reason') == 'authentication':
                result['pause_reason'] = 'authentication'
            if result.get('status') == 'parallel_complete':
                unknown = any(item.get('status') == 'submission_unknown' for item in result.get('attempts', []))
                result['status'] = 'submission_unknown' if unknown else 'succeeded' if result.get('success') else 'failed'
            if result.get('status') == 'pending_payment' and result.get('order_number'):
                order = {**result, 'task_id': context.channel.task_id}
                result['pending_orders'] = [order]
            elif result.get('order_number') and not result.get('pending_orders'):
                # A verified pending order may precede a QR-read failure. Keep
                # its payment reminder while still conservatively pausing work.
                prior = self.jobs[identity].get('progress') or {}
                if prior.get('status') == 'pending_payment' and prior.get('order_number') == result['order_number']:
                    result['pending_orders'] = [{**prior,'task_id':context.channel.task_id}]
            try:
                _write(context.task_dir / 'task.json', {
                    'id': context.channel.task_id, 'account_id': context.channel.account_id,
                    'action': 'purchase', 'status': 'interrupted' if result['status'] == 'submission_unknown' else
                    'succeeded' if result.get('success') else 'cancelled' if result['status'] == 'cancelled' else 'failed',
                    'stage': '队列目标执行结束', 'created_at': int(time.time()), 'updated_at': int(time.time()),
                    'prompt': None, 'result': _private_result(result),
                    'order_submitted': result['order_submitted'], 'logs': []})
            except (OSError, ValueError, TypeError):
                result['persistence_error'] = True
                result['retryable'] = False
            self.completed.put((identity, result))

    def _launch(self, identity):
        job = self.jobs[identity]
        checked = job['target'].get('checked_at_ms')
        if (type(checked) is not int or not -1000 <= int(time.time() * 1000) - checked <= 10_000):
            self._finish(identity, {'status': 'paused', 'success': False, 'started': False,
                                    'order_submitted': False, 'pause_reason': 'inventory_stale',
                                    'retryable': False})
            return
        context = self._context(identity)
        context.parent_serial = self.limit == 1 and job['target']['concurrency'] == 1
        job.update(started=True, status='running', parent_serial=context.parent_serial)
        thread = threading.Thread(target=self._run_target, args=(identity, context), daemon=True)
        self.running[identity] = thread
        self.channel.emit('queue_started', queue_job_id=identity,
                          queue_assignment=job['target']['queue_assignment'])
        thread.start()

    def _drain_completed(self):
        while True:
            try:
                identity, result = self.completed.get_nowait()
            except queue.Empty:
                return
            self.running.pop(identity, None)
            if result.get('status') == 'submission_unknown':
                self.paused = 'submission_unknown'
            elif result.get('pause_reason') == 'authentication' and self.paused is None:
                self.paused = 'authentication'
            self._finish(identity, result)
            self.channel.emit('progress_result', result=self.snapshot())

    def _commands(self):
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            if not isinstance(command, dict):
                continue
            kind = command.get('type')
            if kind == 'queue_close':
                self.closing = True
            elif kind == 'queue_add':
                if not self.closing:
                    if 'queue_max_concurrency' in command:
                        limit = command['queue_max_concurrency']
                        if type(limit) is not int or not 1 <= limit <= 10:
                            raise ValueError('队列热更新并发任务数无效（1–10）。')
                        mode = command.get('queue_mode')
                        if mode not in (None, 'sequential', 'parallel'):
                            raise ValueError('队列热更新模式无效。')
                        self.limit = 1 if mode == 'sequential' else limit
                    self._add(command.get('queue_targets', []))
            elif kind == 'queue_cancel':
                identity = command.get('queue_job_id')
                job = self.jobs.get(identity)
                if job is None or job['acked']:
                    continue
                if ('queue_assignment' in command
                        and command['queue_assignment'] != job['target']['queue_assignment']):
                    continue
                job['stopped'].set()
                if not job['started']:
                    self.waiting = deque(item for item in self.waiting if item != identity)
                    self._finish(identity, {'status': 'cancelled', 'success': False,
                                            'started': False, 'order_submitted': False, 'retryable': False})

    def run(self):
        self.channel.checkpoint()
        self._add(self.initial)
        try:
            try:
                state = self.service.check_account(enrolling=False)
                if not isinstance(state, dict) or state.get('logged_in') is not True:
                    raise ValueError('队列下单前未确认有效登录；未开始下单。')
            except Exception as error:
                import requests
                authentication = (not isinstance(error, requests.RequestException)
                                  and (getattr(self.service, 'authentication_requested', False) is True
                                       or getattr(self.service, 'authentication_failed', False) is True
                                       or bool(re.search(r'登录|认证|验证码', str(error)))))
                self.paused = 'authentication' if authentication else 'preflight_failed'
                self.channel.emit('stage', message='账号预检未通过，尚未启动下单任务，请处理后手动恢复队列。')
            else:
                self.channel.emit('queue_ready')
            while True:
                self.channel.checkpoint()
                self._commands()
                self._drain_completed()
                if time.monotonic() - self.last_progress >= 1:
                    self.channel.emit('progress_result', result=self.snapshot())
                    self.last_progress = time.monotonic()
                if self.paused:
                    while self.waiting:
                        identity = self.waiting.popleft()
                        self._finish(identity, {'status': 'paused', 'success': False,
                                                'started': False, 'order_submitted': False,
                                                'pause_reason': self.paused, 'retryable': False})
                while (self.waiting and len(self.running) < self.limit and not self.paused
                       and not any(self.jobs[identity].get('parent_serial') for identity in self.running)):
                    self._launch(self.waiting.popleft())
                if not self.running and not self.waiting:
                    if self.closing:
                        return self.snapshot(finished=True)
                    if not self.idle_sent:
                        self.channel.emit('queue_idle')
                        self.idle_sent = True
                threading.Event().wait(.02)
        finally:
            for job in self.jobs.values():
                if not job['acked']:
                    job['stopped'].set()
            deadline = time.monotonic() + 1.4
            for thread in list(self.running.values()):
                thread.join(timeout=max(0, deadline - time.monotonic()))
            self._drain_completed()
            for identity, job in self.jobs.items():
                if not job['acked']:
                    # A running thread still owns uncertain side effects: never
                    # label it clean/cancelled or permit Rust to replay it.
                    self._finish(identity, {'status': 'submission_unknown' if job['started'] else 'cancelled',
                                            'success': False, 'started': job['started'],
                                            'order_submitted': job['started'], 'retryable': False})


def run_queued_purchase(service, params, **options):
    if params.get('_parallel_child') is True:
        raise ValueError('并发子任务不能创建自动队列。')
    return QueuedCheckout(service, params, **options).run()
