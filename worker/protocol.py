"""One isolated task per process. Only this module writes to protocol stdout."""
import json
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager


class Cancelled(BaseException):
    pass


class PromptClosed(ValueError):
    """The process owning a prompt ended; no user reply was synthesized."""


class Channel:
    def __init__(self, account_id, task_id, output=None):
        self.account_id, self.task_id = account_id, task_id
        self.output = output or sys.stdout
        self.cancelled = threading.Event()
        self.responses = queue.Queue()
        self.commands = queue.Queue(maxsize=1024)
        self.command_error = None
        self.lock = threading.Lock()
        self.activity = None
        self._checkout_auth_lease = None

    def emit(self, kind, **payload):
        if kind == 'credential_rejected' and self._checkout_auth_lease is not None:
            payload['checkout_auth_lease'] = self._checkout_auth_lease
        if self.activity is not None:
            try:
                self.activity.observe(kind, payload)
            except Exception:
                pass  # Diagnostics never change task submission/cancellation.
        event = dict(v=1, account_id=self.account_id, task_id=self.task_id, type=kind, **payload)
        with self.lock:
            self.output.write(json.dumps(event, ensure_ascii=False) + '\n')
            self.output.flush()

    def checkpoint(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def receive(self, message):
        if (message.get('account_id') != self.account_id or message.get('task_id') != self.task_id):
            return
        if message.get('type') == 'cancel':
            self.cancelled.set()
            self.responses.put(None)
        elif message.get('type') == 'reply':
            self.responses.put(message)
        elif message.get('type') in ('queue_add', 'queue_cancel', 'queue_close'):
            try:
                self.commands.put_nowait(message)
            except queue.Full:
                self.command_error = 'QUEUE_CONTROL_FULL'
                self.cancelled.set()
                self.responses.put(None)
                self.emit('error', message='订单队列控制通道已满，未接受新命令；队列已停止，请核对已提交订单后重新操作。',
                          error_code=self.command_error)
            else:
                if self.activity is not None:
                    try:
                        self.activity.record('queue_command', '收到订单队列控制命令。', category='queue',
                                             details={'operation': message['type']})
                    except Exception:
                        pass

    def read_replies(self, source):
        try:
            for line in source:
                if len(line) > 1_048_576:
                    break
                try:
                    self.receive(json.loads(line))
                except (ValueError, AttributeError):
                    continue
        finally:
            self.cancelled.set()
            self.responses.put(None)

    def prompt(self, kind, label, *, timeout=None, _alive=None, **fields):
        self.checkpoint()
        if _alive is not None and not _alive():
            raise PromptClosed('认证步骤已结束。')
        deadline = time.monotonic() + timeout if timeout is not None else None
        request_id = str(uuid.uuid4())
        if kind == 'code' and self._checkout_auth_lease is not None:
            fields['checkout_auth_lease'] = self._checkout_auth_lease
        self.emit('prompt', prompt=dict(id=request_id, kind=kind, label=label, **fields))
        while True:
            self.checkpoint()
            if _alive is not None and not _alive():
                self.emit('prompt_closed', request_id=request_id)
                raise PromptClosed('认证步骤已结束。')
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
            polling = min(remaining, .05) if remaining is not None else .05
            try:
                response = self.responses.get(timeout=polling if _alive is not None else remaining)
            except queue.Empty:
                self.checkpoint()
                if _alive is not None and (deadline is None or time.monotonic() < deadline):
                    continue
                raise ValueError('准备等待已超时，请重新准备；未提交订单。') from None
            self.checkpoint()
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError('准备等待已超时，请重新准备；未提交订单。')
            if response and response.get('request_id') == request_id:
                return response.get('value')

    @contextmanager
    def checkout_authentication(self):
        """Private child-parent lease; credentials never appear on event output."""
        if self._checkout_auth_lease is not None:
            raise ValueError('结账认证范围不能重复进入。')
        reply = self.prompt('_checkout_authentication', '请求当前结账认证')
        if not isinstance(reply, dict) or not isinstance(reply.get('lease'), str):
            raise ValueError('结账认证授权无效。')
        lease = reply['lease']
        try:
            if str(uuid.UUID(lease)) != lease:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('结账认证授权无效。') from None
        self._checkout_auth_lease = lease
        try:
            password = reply.get('password')
            if password is not None and (not isinstance(password, str) or not password
                                         or len(password.encode('utf-8')) > 4096):
                raise ValueError('结账凭据无效。')
            yield {'password': password, 'remember': reply.get('remember') is True}
        except Exception:
            self.emit('checkout_auth_failed', checkout_auth_lease=lease)
            raise
        finally:
            self.emit('checkout_auth_release', checkout_auth_lease=lease)
            self._checkout_auth_lease = None


class LogStream:
    def __init__(self, channel):
        self.channel, self.buffer = channel, ''

    def write(self, text):
        self.buffer += text
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            if line.strip():
                self.channel.emit('log', message=line[:2000])
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False
