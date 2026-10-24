"""One isolated task per process. Only this module writes to protocol stdout."""
import json
import hashlib
import os
from pathlib import Path
import re
import stat
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


CHECKOUT_BROWSER_REQUEST = 'checkout-browser-request.json'
CHECKOUT_BROWSER_RESPONSE = 'checkout-browser-response.json'
CHECKOUT_BROWSER_BYTES = 4_000_000


_CHECKOUT_BROWSER_ERRORS = {
    'APPLE_SESSION_BLOCKED': '官网拒绝当前结账认证，未重新开始购买。',
    'CHECKOUT_BROWSER_ACCOUNT_UNVERIFIED': '官网登录账号尚未核验，未继续结账。',
    'CHECKOUT_BROWSER_ACCOUNT_MISMATCH': '官网登录账号与当前账号不一致，未继续结账。',
    'BROWSER_LOGIN_AUTHENTICATION_FAILED': '官网未接受当前结账凭据。',
    'BROWSER_LOGIN_REJECTED': '官网未完成当前结账认证。',
    'BROWSER_LOGIN_ACCOUNT_MISMATCH': '官网登录账号与当前账号不一致，已停止认证。',
    'BROWSER_LOGIN_SECURITY_CHALLENGE': '官网要求安全验证，当前自动认证无法继续。',
    'BROWSER_LOGIN_CODE_INVALID': '请输入六位数字验证码，本次未向官网提交。',
    'BROWSER_LOGIN_INPUT_INTERRUPTED': '官网登录页面或输入焦点已变化，当前认证操作已停止。',
    'BROWSER_PROXY_UNSUPPORTED': '官网浏览器不支持当前需要账号密码的系统代理。',
    'CHECKOUT_BROWSER_DOCUMENT_INVALID': '官网未返回可继续的结账页面。',
    'CHECKOUT_BROWSER_NAVIGATION_FAILED': '当前结账登录页面未正常加载。',
    'CHECKOUT_BROWSER_NETWORK_FAILED': '当前结账认证请求未完成。',
    'CHECKOUT_BROWSER_NETWORK_TIMEOUT': '当前结账认证请求超时，未自动重发。',
    'CHECKOUT_BROWSER_TIMEOUT': '等待当前结账认证超时。',
    'CHECKOUT_BROWSER_CLOSED': '当前结账认证窗口已结束。',
    'BROWSER_CONNECTION_INTERRUPTED': '当前结账浏览器连接已中断。',
    'BROWSER_CONTEXT_MISSING': '当前结账浏览器窗口不可用。',
    'BROWSER_LAUNCH_FAILED': '当前结账浏览器启动失败。',
    'BROWSER_NOT_FOUND': '当前结账浏览器不可用。',
    'BROWSER_PROFILE_ACCESS_FAILED': '当前账号浏览器资料暂不可用。',
    'BROWSER_NAVIGATION_HTTP_ERROR': '官网未正常返回当前认证页面。',
    'BROWSER_NAVIGATION_TIMEOUT': '官网认证页面加载超时。',
    'HEADLESS_LOGIN_REQUIRES_ATTENTION': '官网要求完成当前认证验证。',
    'BROWSER_UNEXPECTED_PAGE': '官网未返回当前认证所需页面。',
}


def checkout_browser_diagnostics(fields):
    if not isinstance(fields, dict) or not isinstance(fields.get('error_code'), str) or fields['error_code'] not in _CHECKOUT_BROWSER_ERRORS:
        return {}
    result = {'error_code': fields['error_code']}
    if type(fields.get('http_status')) is int and 100 <= fields['http_status'] <= 599:
        result['http_status'] = fields['http_status']
    return result


def checkout_browser_error(error):
    from browser_login import BrowserSessionError
    result = {'error': 'CHECKOUT_BROWSER_RECOVERY_FAILED'}
    if isinstance(error, BrowserSessionError) and isinstance(error.code, str) and error.code in _CHECKOUT_BROWSER_ERRORS:
        result['error_code'] = error.code
        if type(error.http_status) is int and 100 <= error.http_status <= 599:
            result['http_status'] = error.http_status
        result['status'] = error.status if error.status in ('failed', 'blocked', 'requires_login') else 'failed'
        result['requires_login'] = error.requires_login is True
    return result


def checkout_browser_raise(reply):
    code = reply.get('error_code') if isinstance(reply, dict) else None
    if isinstance(code, str) and code in _CHECKOUT_BROWSER_ERRORS:
        from browser_login import BrowserSessionError
        status = reply.get('http_status')
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        raise BrowserSessionError(code, _CHECKOUT_BROWSER_ERRORS[code],
            status=reply.get('status') if reply.get('status') in ('failed', 'blocked', 'requires_login') else 'failed',
            requires_login=reply.get('requires_login') is True, http_status=status)
    raise ValueError('当前结账浏览器认证未完成。')


def checkout_private_write(directory, name, value):
    from platform_support import private_fd
    if name not in (CHECKOUT_BROWSER_REQUEST, CHECKOUT_BROWSER_RESPONSE):
        raise ValueError('结账认证文件标识无效。')
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('结账认证目录无效。')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(raw) > CHECKOUT_BROWSER_BYTES:
        raise ValueError('结账认证数据超出限制。')
    path = directory / name
    with open(path, 'xb') as output:
        try:
            private_fd(output.fileno())
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            output.close()
            path.unlink(missing_ok=True)
            raise
    return path, hashlib.sha256(raw).hexdigest()


def checkout_private_read(directory, name, digest):
    if name not in (CHECKOUT_BROWSER_REQUEST, CHECKOUT_BROWSER_RESPONSE) or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('结账认证文件标识无效。')
    directory = Path(directory)
    path = directory / name
    if directory.is_symlink() or path.is_symlink():
        raise ValueError('结账认证文件无效。')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > CHECKOUT_BROWSER_BYTES
                or os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.geteuid())):
            raise ValueError('结账认证文件无效或过大。')
        raw = source.read(CHECKOUT_BROWSER_BYTES + 1)
    if len(raw) > CHECKOUT_BROWSER_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('结账认证文件校验不匹配。')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('结账认证文件内容无效。')
    return value


def checkout_private_remove(path, digest):
    # Only remove the exact file this process created, never a replacement.
    try:
        path = Path(path)
        checkout_private_read(path.parent, path.name, digest)
        path.unlink()
    except (OSError, ValueError):
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

    def checkout_browser_recovery(self, digest):
        lease = self._checkout_auth_lease
        if lease is None:
            raise ValueError('浏览器结账恢复缺少当前认证授权。')
        reply = self.prompt('_checkout_browser_recovery', '恢复当前结账认证',
                            checkout_auth_lease=lease, payload_sha256=digest)
        if isinstance(reply, dict) and reply.get('error'):
            checkout_browser_raise(reply)
        if (not isinstance(reply, dict) or reply.get('lease') != lease
                or reply.get('basename') != CHECKOUT_BROWSER_RESPONSE
                or not isinstance(reply.get('sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', reply['sha256'])):
            raise ValueError('当前结账浏览器认证未完成。')
        return reply

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
