"""One isolated task per process. Only this module writes to protocol stdout."""
import json
import queue
import sys
import threading
import time
import uuid


class Cancelled(BaseException):
    pass


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

    def emit(self, kind, **payload):
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

    def prompt(self, kind, label, *, timeout=None, **fields):
        self.checkpoint()
        deadline = time.monotonic() + timeout if timeout is not None else None
        request_id = str(uuid.uuid4())
        self.emit('prompt', prompt=dict(id=request_id, kind=kind, label=label, **fields))
        while True:
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
            try:
                response = self.responses.get(timeout=remaining)
            except queue.Empty:
                self.checkpoint()
                raise ValueError('准备等待已超时，请重新准备；未提交订单。') from None
            self.checkpoint()
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError('准备等待已超时，请重新准备；未提交订单。')
            if response and response.get('request_id') == request_id:
                return response.get('value')


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
