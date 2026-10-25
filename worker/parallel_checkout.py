"""Bounded, intentional pickup attempts with separate worker processes and files.

The parent's account lock remains held throughout the batch. A snapshot isolates
local state; it does not assert that Apple's server-side shopping bags are
independent. Each normal checkout still verifies its goods, quantity and store.
No failed/uncertain attempt is restarted and child cookies never replace the
parent's session.
"""
from collections import deque
import copy
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from protocol import (Cancelled, PromptClosed, CHECKOUT_BROWSER_REQUEST, CHECKOUT_BROWSER_RESPONSE,
                      checkout_private_read, checkout_private_write, checkout_private_remove, checkout_browser_error, checkout_browser_diagnostics)
from platform_support import private_fd


MAX_CONCURRENCY = 10
MAX_FILE_BYTES = 4_000_000
ORDER_NUMBER = re.compile(r'W\d{6,20}')
AUTH_GUARD = '并发会话需要重新登录'
CHECKOUT_AUTH_MESSAGE = 'Apple 要求本次结账认证，原会话认证未完成；本次未创建订单。'
SESSION_AUTH_MESSAGE = '当前购买会话尚未完成认证；本次未自动重放购买。'


# A queue can own multiple PurchaseBatch threads for the same account. Keep
# authentication ownership here, across batches, while purchases stay parallel.
_AUTHENTICATION_LOCK = threading.RLock()
_AUTHENTICATION_LEASES = {}


class _CheckoutLeaseChannel:
    """Keep browser OTP on its queue target and revoke it with its child."""
    def __init__(self, channel, alive):
        self.channel, self.alive = channel, alive

    def __getattr__(self, name):
        return getattr(self.channel, name)

    def checkpoint(self):
        self.channel.checkpoint()
        if not self.alive():
            raise PromptClosed('当前结账认证已结束。')

    def prompt(self, kind, label, **fields):
        prior = fields.pop('_alive', None)
        fields['_alive'] = lambda: self.alive() and (prior is None or prior())
        return self.channel.prompt(kind, label, **fields)


def concurrency(params):
    value = params.get('concurrency', 1)
    if type(value) is not int or not 1 <= value <= MAX_CONCURRENCY:
        raise ValueError('并发下单数必须为 1–10 的整数。')
    return value


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temporary, 'x', encoding='utf-8') as target:
            private_fd(target.fileno())
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('并发任务文件无效或过大。')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('并发任务文件必须是对象。')
    return value


def _worker_command():
    return [sys.executable] if getattr(sys, 'frozen', False) else [
        sys.executable, str(Path(__file__).with_name('main.py'))]


def _safe_result(result):
    # Task snapshots follow Engine.update's treatment of private payment data.
    return {key: value for key, value in result.items()
            if key not in ('qr_data', 'payment_url', 'qr_url', 'qr_image')}


def _pending(result):
    return (isinstance(result, dict) and result.get('status') == 'pending_payment'
            and isinstance(result.get('order_number'), str)
            and ORDER_NUMBER.fullmatch(result['order_number']) is not None)


class _WindowsJob:
    """Closing the parent-owned handle kills workers if the parent crashes."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('ReadOperationCount', 'WriteOperationCount',
                         'OtherOperationCount', 'ReadTransferCount',
                         'WriteTransferCount', 'OtherTransferCount')]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError('无法创建并发进程保护，未开始下单。')

    def add(self, child):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):
            child.kill()
            child.wait(timeout=2)
            raise OSError('无法保护并发子进程，已停止本次尝试。')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class PurchaseBatch:
    def __init__(self, service, params, *, worker_command=None, skip_preflight=False,
                 process_slots=None):
        self.service, self.params = service, copy.deepcopy(params)
        self.credential_owner = vars(service).get('parent_service') or service
        if 'pickup_policy' in self.params and 'queue_job_id' in self.params:
            # Lazy import avoids the queue/coordinator import cycle.
            from queued_checkout import normalize_queue_pickup_policy
            self.params['pickup_policy'] = normalize_queue_pickup_policy(self.params['pickup_policy'])
        self.channel = service.channel
        self.count = concurrency(params)
        self.command = list(worker_command or _worker_command())
        # Internal coordinator options, never taken from request params.
        self.skip_preflight = skip_preflight
        self.process_slots = process_slots
        self.batch_id = str(uuid.uuid4())
        self.events = queue.Queue(maxsize=512)
        self.attempts = []
        self.children = {}
        self.pending_prompts = deque()
        self.stopping = threading.Event()
        self.job = None
        self.created = int(time.time())
        self.finished = False
        self.manifest = service.task_dir / 'parallel_attempts.json'

    def summary(self, *, finished=False, private=False):
        attempts = []
        pending_orders = []
        for item in self.attempts:
            entry = {key: value for key, value in item.items()
                     if key not in ('root', 'terminal') and not key.startswith('_')}
            result = entry.get('result')
            if isinstance(result, dict):
                if _pending(result):
                    pending_orders.append({**result, 'task_id': item['task_id'],
                                           'attempt': item['attempt']})
                if private:
                    entry['result'] = _safe_result(result)
            attempts.append(entry)
        result = {**(pending_orders[0] if pending_orders else {}),
                  'success': bool(pending_orders),
                  'status': 'parallel_complete' if finished else 'parallel_running',
                  'batch_id': self.batch_id, 'concurrency': self.count,
                  'attempts': attempts,
                  'pending_orders': [_safe_result(x) for x in pending_orders]
                  if private else pending_orders}
        # Service.run only accepts a top-level image from its own task_dir.
        # Child images belong to separately authenticated source tasks; the
        # aggregate already carries qr_data and per-attempt source task IDs.
        result.pop('qr_image', None)
        auth_failures = [item for item in self.attempts if item.get('requires_serial_checkout')]
        if auth_failures:
            result['requires_serial_checkout'] = True
            result['retryable'] = False
            if not pending_orders:
                result['error_code'] = ('CHECKOUT_REAUTH_REQUIRED'
                                        if any(item.get('error_code') == 'CHECKOUT_REAUTH_REQUIRED'
                                               for item in auth_failures)
                                        else 'PARALLEL_SESSION_REAUTH_REQUIRED')
            result['message'] = (CHECKOUT_AUTH_MESSAGE
                                 if any(item.get('error_code') == 'CHECKOUT_REAUTH_REQUIRED'
                                        for item in auth_failures) else SESSION_AUTH_MESSAGE)
        return _safe_result(result) if private else result

    def persist(self):
        _write(self.manifest, self.summary(finished=self.finished, private=True))

    def _prepare(self):
        # A bounded allowlist: never clone browser profiles, stale bags,
        # prepared checkout, old orders, histories, passwords or other tasks.
        auth = _read(self.service.auth)
        config = _read(self.service.checkout)
        for attempt in range(1, self.count + 1):
            task_id = str(uuid.uuid4())
            sandbox = self.service.task_dir / 'parallel' / task_id
            root = sandbox / 'accounts' / self.channel.account_id
            _write(root / 'auth.json', {key: value for key, value in auth.items()
                                      if key != 'password'})
            _write(root / 'checkout.json', config)
            item = {'id': task_id, 'task_id': task_id, 'attempt': attempt,
                    'status': 'queued', 'order_submitted': False,
                    'root': str(sandbox), 'terminal': False}
            self.attempts.append(item)
            self._artifact(item)
        self.persist()  # Durable intent before starting any side effect.

    def _artifact(self, item):
        destination = self.service.root / 'tasks' / item['task_id']
        source_root = Path(item['root']) / 'accounts' / self.channel.account_id
        source = source_root / 'tasks' / item['task_id']
        if _pending(item.get('result')) and not (destination / 'payment.json').exists():
            _write(destination / 'payment.json', item['result'])
        if source.exists():
            payment = source / 'payment.json'
            if payment.exists():
                data = _read(payment)
                if _pending(data):
                    if not _pending(item.get('result')):
                        item['result'] = data
                        item['status'] = 'pending_payment'
                    item['order_submitted'] = True
                    self.service.submitted = True
                    # Payment is private authenticated data, kept only in this
                    # account's task directory, never in logs or notifications.
                    image = data.get('qr_image')
                    if isinstance(image, str):
                        image = Path(image)
                        if (image.resolve().parent == source.resolve()
                                and image.is_file() and not image.is_symlink()
                                and image.stat().st_size <= 2_000_000):
                            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                            target = destination / 'payment.png'
                            with open(target, 'wb') as out:
                                private_fd(out.fileno())
                                out.write(image.read_bytes())
                            data['qr_image'] = str(target)
                        else:
                            data.pop('qr_image', None)
                    _write(destination / 'payment.json', data)
        for name in ('order_submission.json', 'test_order_submission.json'):
            marker = source_root / name
            if marker.exists():
                saved = _read(marker)
                _write(destination / name, saved)
                if saved.get('state') in ('pending_payment', 'submission_unknown'):
                    item['order_submitted'] = True
                    self.service.submitted = True
        result = item.get('result')
        status = item['status']
        disk_status = ('succeeded' if status == 'pending_payment' else
                       'interrupted' if status == 'submission_unknown' else
                       status if status in ('failed', 'cancelled') else 'running')
        _write(destination / 'task.json', {
            'id': item['task_id'], 'account_id': self.channel.account_id,
            'action': 'purchase', 'status': disk_status,
            'stage': f"并发尝试 {item['attempt']}/{self.count}：{status}",
            'created_at': self.created, 'updated_at': int(time.time()),
            'prompt': None, 'result': _safe_result(result) if isinstance(result, dict) else None,
            'order_submitted': item['order_submitted'], 'logs': [],
        })

    def _launch(self, item):
        command = [*self.command, '--data-dir', item['root'],
                   '--account-id', self.channel.account_id,
                   '--task-id', item['task_id']]
        release = self.process_slots.acquire(self.channel) if self.process_slots else lambda: None
        try:
            self.channel.checkpoint()
            child = subprocess.Popen(command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding='utf-8', bufsize=1,
                                     creationflags=0x08000000 if os.name == 'nt' else 0,
                                     start_new_session=os.name != 'nt')
            self.job.add(child)
        except BaseException:
            release()
            raise
        # Release on actual process exit, independently of batch event handling.
        # Waiting until batch cleanup would deadlock two multi-route batches
        # whose combined requested routes exceed the shared process limit.
        def release_on_exit():
            try:
                child.wait()
            finally:
                release()
        threading.Thread(target=release_on_exit, daemon=True).start()
        self.children[item['id']] = child
        params = {**self.params, 'concurrency': 1, 'retry_count': 0, '_parallel_child': True,
                  'batch_id': self.batch_id, 'batch_size': self.count,
                  'attempt_index': item['attempt']}
        child.stdin.write(json.dumps({
            'account_id': self.channel.account_id, 'task_id': item['task_id'],
            'account': self.service.account, 'action': 'purchase', 'params': params}) + '\n')
        child.stdin.flush()
        item['status'] = 'running'

        done = threading.Event()
        item['_collector_done'] = done
        def queue_event(event):
            while not self.stopping.is_set():
                try:
                    self.events.put((item, event), timeout=.05)
                    return
                except queue.Full:
                    continue

        def collect():
            try:
                while True:
                    raw = child.stdout.readline(MAX_FILE_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_FILE_BYTES:
                        queue_event({'type': 'invalid'})
                        break
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        queue_event({'type': 'invalid'})
                        break
                    # Register ownership before handing the event to the main
                    # loop: its consumer may run only after this child has exited.
                    self._register_browser_request(item, event)
                    if not self._private_authentication_event(item, event):
                        queue_event(event)
            except (OSError, UnicodeError, ValueError):
                queue_event({'type': 'invalid'})
            except Cancelled:
                pass
            finally:
                # Drain private events in stdout order: explicit password
                # invalidation must finish before a released/EOF lease is reused.
                self._release_authentication(item)
                queue_event({'type': '_eof'})
                try:
                    child.stdout.close()
                except (OSError, ValueError):
                    pass
                done.set()
        threading.Thread(target=collect, daemon=True).start()

    def _register_browser_request(self, item, event):
        if (not isinstance(event, dict) or event.get('v') != 1
                or event.get('account_id') != self.channel.account_id
                or event.get('task_id') != item['task_id'] or event.get('type') != 'prompt'):
            return
        prompt = event.get('prompt')
        if (not isinstance(prompt, dict) or prompt.get('kind') != '_checkout_browser_recovery'
                or not isinstance(prompt.get('id'), str) or not 1 <= len(prompt['id']) <= 200):
            return
        with _AUTHENTICATION_LOCK:
            if not self._owns_authentication(item, prompt.get('checkout_auth_lease')):
                return
            directory = Path(item['root']) / 'accounts' / self.channel.account_id / 'tasks' / item['task_id']
            registered = (directory / CHECKOUT_BROWSER_REQUEST, prompt.get('payload_sha256'))
            previous = item.get('_checkout_browser_request')
            if previous is not None and previous != registered:
                return
            try:
                checkout_private_read(directory, CHECKOUT_BROWSER_REQUEST, prompt.get('payload_sha256'))
            except (OSError, ValueError):
                return
            item['_checkout_browser_request'] = registered

    def _release_authentication(self, item):
        with _AUTHENTICATION_LOCK:
            owned = _AUTHENTICATION_LEASES.get(self.channel.account_id)
            if owned is not None and owned[0] is self and owned[1] == item['id']:
                _AUTHENTICATION_LEASES.pop(self.channel.account_id, None)
            item.pop('_checkout_auth_lease', None)
            self._clear_browser_recovery(item)

    def _clear_browser_recovery(self, item):
        for key in ('_checkout_browser_file', '_checkout_browser_request'):
            created = item.pop(key, None)
            if created is not None:
                checkout_private_remove(*created)

    def _owns_authentication(self, item, lease):
        with _AUTHENTICATION_LOCK:
            owned = _AUTHENTICATION_LEASES.get(self.channel.account_id)
            return (isinstance(lease, str) and owned is not None and owned[0] is self
                    and owned[1] == item['id'] and owned[2] == lease)

    def _private_authentication_event(self, item, event):
        if (not isinstance(event, dict) or event.get('v') != 1
                or event.get('account_id') != self.channel.account_id
                or event.get('task_id') != item['task_id']):
            return False
        kind = event.get('type')
        if kind not in ('checkout_auth_release', 'credential_rejected', 'credential_authenticated', 'checkout_auth_failed', 'checkout_browser_consumed'):
            return False
        with _AUTHENTICATION_LOCK:
            if self._owns_authentication(item, event.get('checkout_auth_lease')):
                if kind == 'credential_rejected':
                    self.service.authentication_failed = True
                    self.credential_owner.password_submitted()
                    self.credential_owner.reject_password()
                elif kind == 'credential_authenticated':
                    self.credential_owner.password_submitted()
                    self.credential_owner.confirm_password_authenticated()
                elif kind == 'checkout_auth_failed':
                    self.service.authentication_failed = True
                elif kind == 'checkout_browser_consumed':
                    self._clear_browser_recovery(item)
                else:
                    self._release_authentication(item)
        return True

    def _send_reply(self, item, prompt, value):
        child = self.children[item['id']]
        try:
            child.stdin.write(json.dumps({
                'type': 'reply', 'account_id': self.channel.account_id,
                'task_id': item['task_id'], 'request_id': prompt.get('id'), 'value': value}) + '\n')
            child.stdin.flush()
            return True
        except (OSError, ValueError):
            # One dead IPC consumer must not abort unrelated purchase routes.
            if child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass
            try:
                child.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                pass
            item.update(status='submission_unknown' if item['order_submitted'] else 'failed',
                        terminal=True, retryable=False, error='当前购买子任务已结束，未自动重发。')
            return False

    def _reply(self, item, prompt):
        if item.get('terminal'):
            return
        child = self.children[item['id']]
        if not isinstance(prompt, dict) or not isinstance(prompt.get('id'), str) or not 1 <= len(prompt['id']) <= 200:
            raise ValueError('并发子任务交互标识无效。')
        kind = prompt.get('kind')
        if kind == '_checkout_authentication':
            owner = self.credential_owner
            if (owner.channel.account_id != self.channel.account_id
                    or not isinstance(owner.account, str) or not isinstance(self.service.account, str)
                    or owner.account.strip().casefold() != self.service.account.strip().casefold()):
                raise ValueError('结账认证的账号归属不一致。')
            with _AUTHENTICATION_LOCK:
                if child.poll() is not None:
                    return
                if item.get('_checkout_auth_granted') is True:
                    self._stop_attempt(item)
                    item.update(status='failed', terminal=True, retryable=False,
                                error='同一购买尝试不能重复申请结账认证。')
                    return
                if self.channel.account_id in _AUTHENTICATION_LEASES:
                    self.pending_prompts.append((item, prompt))
                    return
                lease = str(uuid.uuid4())
                _AUTHENTICATION_LEASES[self.channel.account_id] = (self, item['id'], lease)
                item['_checkout_auth_lease'] = lease
                item['_checkout_auth_granted'] = True
                item['_authentication_scope'] = 'checkout'
            try:
                # Reading the owner's credential is not a failed authentication
                # in that parent's purchase round. The child's private outcome
                # sets authentication_failed only when authentication fails.
                previous_authentication = getattr(owner, 'authentication_requested', False)
                try:
                    password = owner.browser_password(allow_prompt=False)
                finally:
                    owner.authentication_requested = previous_authentication
                if not isinstance(password, str) or not password:
                    password = None
                self._send_reply(item, prompt, {'lease': lease, 'password': password,
                                              'remember': getattr(self.credential_owner, 'remember', False) is True,
                                              'verified': bool(password) and owner.has_verified_password() is True})
            except BaseException:
                self._release_authentication(item)
                raise
            return
        if kind == '_checkout_browser_recovery':
            lease = prompt.get('checkout_auth_lease')
            alive = lambda: child.poll() is None and self._owns_authentication(item, lease)
            if (not alive() or item['order_submitted'] or item.get('_checkout_browser_recovery_used') is True):
                self._send_reply(item, prompt, {'error': 'CHECKOUT_BROWSER_RECOVERY_DENIED'})
                return
            item['_checkout_browser_recovery_used'] = True
            owner = self.credential_owner
            if (owner.channel.account_id != self.channel.account_id
                    or owner.account.strip().casefold() != self.service.account.strip().casefold()
                    or getattr(owner, '_parallel_child', False) is True):
                self._send_reply(item, prompt, {'error': 'CHECKOUT_BROWSER_RECOVERY_OWNER'})
                return
            directory = Path(item['root']) / 'accounts' / self.channel.account_id / 'tasks' / item['task_id']
            try:
                payload = checkout_private_read(directory, CHECKOUT_BROWSER_REQUEST, prompt.get('payload_sha256'))
                item['_checkout_browser_request'] = (directory / CHECKOUT_BROWSER_REQUEST, prompt['payload_sha256'])
                # A shadow routes only this target's OTP while preserving the
                # original owner account/profile. Credential rejection and cache
                # invalidation always reach the original service.
                shadow = copy.copy(owner)
                shadow.channel = _CheckoutLeaseChannel(self.channel, alive)
                shadow.submitted = False
                shadow._parallel_child = False
                shadow._checkout_auth_active = False
                shadow.browser_password = owner.browser_password
                shadow.reject_password = owner.reject_password
                shadow.password_submitted = owner.password_submitted
                shadow.has_verified_password = owner.has_verified_password
                shadow.begin_password_authentication = owner.begin_password_authentication
                shadow.password_submission_interrupted = owner.password_submission_interrupted
                shadow.confirm_password_authenticated = owner.confirm_password_authenticated
                previous_requested = owner.authentication_requested
                with shadow.checkout_authentication():
                    result = shadow.recover_checkout_authentication(payload)
                owner.authentication_requested = previous_requested
                if not alive():
                    raise PromptClosed('当前结账认证已结束。')
                path, digest = checkout_private_write(directory, CHECKOUT_BROWSER_RESPONSE, result)
                item['_checkout_browser_file'] = (path, digest)
                if not self._send_reply(item, prompt, {'lease': lease, 'basename': CHECKOUT_BROWSER_RESPONSE, 'sha256': digest}):
                    self._clear_browser_recovery(item)
            except Cancelled:
                self._clear_browser_recovery(item)
                raise
            except Exception as error:
                self._clear_browser_recovery(item)
                self._send_reply(item, prompt, checkout_browser_error(error))
            return
        checkout_code = kind == 'code' and self._owns_authentication(item, prompt.get('checkout_auth_lease'))
        if kind == 'password' or kind == 'code' and not checkout_code:
            self._stop_attempt(item)
            item.update(status='submission_unknown' if item['order_submitted'] else 'failed',
                        error=SESSION_AUTH_MESSAGE, terminal=True, retryable=False)
            if not item['order_submitted']:
                item.update(error_code='PARALLEL_VERIFICATION_REQUIRED', requires_serial_checkout=True)
            return
        if kind not in ('confirm', 'select', 'text', 'code'):
            raise ValueError('并发子任务返回了不支持的交互。')
        fields = {key: value for key, value in prompt.items()
                  if key not in ('id', 'kind', 'label', 'checkout_auth_lease')}
        if checkout_code:
            fields['_alive'] = lambda: child.poll() is None and self._owns_authentication(item, prompt.get('checkout_auth_lease'))
        try:
            value = self.channel.prompt(kind, f"[尝试 {item['attempt']}/{self.count}] {prompt.get('label', '')}", **fields)
        except PromptClosed:
            self._stop_attempt(item)
            item.update(status='submission_unknown' if item['order_submitted'] else 'failed',
                        error='结账认证已结束，验证码提示已撤回。', terminal=True, retryable=False)
            return
        self._send_reply(item, prompt, value)

    def _event(self, item, event):
        if not isinstance(event, dict):
            raise ValueError('并发子任务返回了无效协议。')
        kind = event.get('type')
        if kind == '_eof':
            if not item['terminal']:
                item.update(status='pending_payment' if _pending(item.get('result')) else
                            'submission_unknown' if item['order_submitted'] else 'failed',
                            error='子进程意外结束；请核对官网订单。' if item['order_submitted']
                            else '子进程意外结束，未自动重试。', terminal=True)
            self._artifact(item)
            return
        if (event.get('v') != 1
                or event.get('account_id') != self.channel.account_id
                or event.get('task_id') != item['task_id']):
            self._stop_attempt(item)
            item.update(status='submission_unknown' if item['order_submitted'] else 'failed',
                        error='并发子任务的协议标识无效，已停止。', terminal=True)
            return
        if self._private_authentication_event(item, event):
            return
        if event.get('order_submitted') is True:
            item['order_submitted'] = True
            self.service.submitted = True
            self.channel.emit('stage', message=f"尝试 {item['attempt']}/{self.count} 正在提交订单",
                              order_submitted=True)
        if kind == 'prompt':
            self.pending_prompts.append((item, event.get('prompt', {})))
        elif kind == 'log':
            if '[结账认证]' in str(event.get('message', '')):
                item['_authentication_scope'] = 'checkout'
            self.channel.emit('log', message=f"[尝试 {item['attempt']}/{self.count}] {str(event.get('message', ''))[:1800]}")
        elif kind == 'stage':
            self.channel.emit('stage', message=f"[尝试 {item['attempt']}/{self.count}] {str(event.get('message', ''))[:1800]}",
                              order_submitted=bool(event.get('order_submitted')))
        elif kind in ('progress_result', 'result'):
            result = event.get('result')
            if not isinstance(result, dict):
                raise ValueError('并发子任务返回了无效结果。')
            item['result'] = result
            item.update(checkout_browser_diagnostics(result))
            if _pending(result):
                item['status'] = 'pending_payment'
                item['order_submitted'] = True
                self.service.submitted = True
            elif kind == 'result':
                item['status'] = 'submission_unknown' if item['order_submitted'] else 'failed'
            if kind == 'result':
                item['terminal'] = True
            self._artifact(item)
            self.channel.emit('progress_result', result=self.summary())
        elif kind in ('error', 'cancelled'):
            item.update(status='pending_payment' if _pending(item.get('result')) else
                        'submission_unknown' if item['order_submitted'] else
                        'cancelled' if kind == 'cancelled' else 'failed',
                        error=str(event.get('message', '本次尝试未完成。'))[:2000], terminal=True)
            diagnostic = checkout_browser_diagnostics(event)
            item.update(diagnostic)
            guarded_auth = AUTH_GUARD in item['error'] or '并发会话需要验证' in item['error']
            if kind == 'error' and guarded_auth and not item['order_submitted'] and not diagnostic:
                checkout_auth = item.get('_authentication_scope') == 'checkout'
                item.update(error_code='CHECKOUT_REAUTH_REQUIRED' if checkout_auth
                            else 'PARALLEL_SESSION_REAUTH_REQUIRED',
                            error=CHECKOUT_AUTH_MESSAGE if checkout_auth else SESSION_AUTH_MESSAGE,
                            requires_serial_checkout=True, retryable=False)
            self._artifact(item)

    def _stop_attempt(self, item):
        child = self.children.get(item['id'])
        if child is not None and child.poll() is None:
            try:
                child.stdin.write(json.dumps({'type': 'cancel', 'account_id': self.channel.account_id,
                                              'task_id': item['task_id']}) + '\n')
                child.stdin.flush()
            except (OSError, ValueError):
                pass

    def _stop(self):
        self.stopping.set()
        for item in self.attempts:
            self._stop_attempt(item)
        deadline = time.monotonic() + 0.7
        for child in self.children.values():
            try:
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    child.kill()
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        if self.job:
            self.job.close()
        for child in self.children.values():
            try:
                child.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
            if child.stdin:
                try:
                    child.stdin.close()
                except (OSError, ValueError):
                    pass  # A dead OTP consumer may already have broken its pipe.
        # Private stdout is drained before ownership ends. In particular a
        # preceding credential rejection cannot be skipped by process cleanup.
        for item in self.attempts:
            done = item.get('_collector_done')
            if done is not None:
                done.wait(timeout=1)

    def run(self):
        self.channel.checkpoint()
        # Refresh/login once under the parent lock before making snapshots.
        if not self.skip_preflight:
            self.service.stage('并发下单前核对当前账号；各尝试将独立复核商品、数量和门店')
            state = self.service.check_account(enrolling=False)
            if not isinstance(state, dict) or state.get('logged_in') is not True:
                raise ValueError('并发下单前未确认有效登录；请先预热账号。')
        self._prepare()
        try:
            self.job = _WindowsJob()
            for item in self.attempts:
                self.channel.checkpoint()
                self._launch(item)
            self.persist()
            while any(not item['terminal'] for item in self.attempts):
                self.channel.checkpoint()
                try:
                    item, event = self.events.get(timeout=0.1)
                except queue.Empty:
                    pass
                else:
                    self._event(item, event)
                    self.persist()
                if self.pending_prompts:
                    self._reply(*self.pending_prompts.popleft())
            # Read durable child evidence before constructing the return value;
            # finally cleanup runs after a return expression has been evaluated.
            for item in self.attempts:
                self._artifact(item)
                if item['order_submitted'] and not _pending(item.get('result')):
                    item['status'] = 'submission_unknown'
            self.finished = True
            return self.summary(finished=True)
        finally:
            self._stop()
            for item in self.attempts:
                self._artifact(item)  # Harvest a marker even if its IPC event was lost.
                if not item['terminal']:
                    item.update(status='submission_unknown' if item['order_submitted'] else 'cancelled',
                                error='批次已停止，未自动重新提交。', terminal=True)
                elif item['order_submitted'] and item['status'] not in ('pending_payment', 'submission_unknown'):
                    item['status'] = 'submission_unknown'
                self._artifact(item)
            self.persist()


def run_parallel_purchase(service, params, *, worker_command=None, preflight_complete=False):
    if params.get('fulfillment', 'HOME') != 'RETAIL' or params.get('prepare_only') is True:
        raise ValueError('并发下单仅适用于正式自提购买。')
    if params.get('_parallel_child') is True:
        raise ValueError('并发子任务不能再次创建并发批次。')
    # This keyword is an internal caller contract, never read from JSON params.
    return PurchaseBatch(service, params, worker_command=worker_command,
                         skip_preflight=preflight_complete).run()
