"""Keyboard events with bounded lifetime; never expose supplied text in errors."""
from contextlib import contextmanager
import time


class BrowserInputInterrupted(ValueError):
    REASONS = {'document_changed', 'frame_detached', 'page_closed', 'deadline',
               'focus_changed', 'element_replaced', 'value_changed', 'field_missing', 'invalid_text', 'cdp_error'}
    PHASES = {'recognition', 'before_input', 'focus', 'clear', 'type', 'verify', 'submit', 'navigation', 'input'}
    ERRORS = {'timeout', 'detached_context', 'target_closed', 'protocol_error', 'other'}

    def __init__(self, message, *, reason='cdp_error', phase='input', browser_error=None):
        super().__init__(message)
        self.reason = reason if reason in self.REASONS else 'cdp_error'
        self.phase = phase if phase in self.PHASES else 'input'
        self.browser_error = browser_error if browser_error in self.ERRORS else None


def browser_error_category(error):
    """Classify privately; never return exception text, URLs or supplied values."""
    if type(error).__name__ == 'TimeoutError':
        return 'timeout'
    message = str(error)
    if any(fragment in message for fragment in ('Execution context was destroyed', 'Cannot find context', 'Frame was detached', 'Element is not attached')):
        return 'detached_context'
    if any(fragment in message for fragment in ('Target closed', 'Target page, context or browser has been closed', 'Browser has been closed')):
        return 'target_closed'
    if 'Protocol error' in message:
        return 'protocol_error'
    return 'other'


def click_control(locator, *, timeout=1500):
    """Normal mouse events without an artificial interaction delay."""
    locator.click(timeout=timeout)


def validate_keyboard_text(text):
    if not isinstance(text, str) or not text or len(text) > 4096:
        raise BrowserInputInterrupted('浏览器输入格式无效。')
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
           for char in text):
        raise BrowserInputInterrupted('浏览器输入包含不支持的字符。')
    return text


@contextmanager
def _navigation_guard(page, checkpoint, deadline, *, frame=None):
    invalidated = False
    changed_frames = set()
    documents = {}

    def invalidate(*args):
        nonlocal invalidated
        invalidated = True

    def changed(changed_frame):
        if frame is None or changed_frame == frame or changed_frame == page.main_frame:
            changed_frames.add(changed_frame)

    def detached(changed_frame):
        if frame is None or changed_frame == frame or changed_frame == page.main_frame:
            invalidate()

    listeners = [('framenavigated', changed), ('framedetached', detached),
                 ('close', invalidate), ('crash', invalidate)]
    try:
        for event, callback in listeners:
            page.on(event, callback)
        tracked = [page.main_frame, frame] if frame is not None else list(page.frames)
        for current in tracked:
            if current is not None and current not in documents:
                documents[current] = current.evaluate_handle('document')

        def check():
            checkpoint()
            if invalidated or page.is_closed():
                raise BrowserInputInterrupted('页面已变化，已停止剩余输入。', reason='page_closed' if page.is_closed() else 'frame_detached', phase='navigation')
            current_deadline = deadline() if callable(deadline) else deadline
            if current_deadline is not None and time.monotonic() >= current_deadline:
                raise BrowserInputInterrupted('输入已超时，已停止剩余输入。', reason='deadline', phase='input')
            if frame is not None and frame.is_detached():
                raise BrowserInputInterrupted('登录页面已变化，已停止剩余输入。', reason='frame_detached', phase='navigation')
            for current in list(changed_frames):
                original = documents.get(current)
                try:
                    unchanged = original is not None and original.evaluate('(original) => original === document') is True
                except Exception:
                    unchanged = False
                if not unchanged:
                    raise BrowserInputInterrupted('登录文档已变化，已停止剩余输入。', reason='document_changed', phase='navigation')
                changed_frames.discard(current)
        yield check
    finally:
        for document in documents.values():
            try:
                document.dispose()
            except Exception:
                pass
        for event, callback in listeners:
            try:
                page.remove_listener(event, callback)
            except Exception:
                pass


def type_keyboard_text(page, text, checkpoint=lambda: None, *, deadline=None):
    """Type into current focus, allowing same-document OTP autofocus changes."""
    validate_keyboard_text(text)
    try:
        with _navigation_guard(page, checkpoint, deadline) as check:
            page.bring_to_front()
            for char in text:
                check()
                if page.main_frame.evaluate('document.hasFocus()') is not True:
                    raise BrowserInputInterrupted('输入窗口焦点已变化，已停止输入。', reason='focus_changed', phase='type')
                page.keyboard.type(char)
                check()
                if page.main_frame.evaluate('document.hasFocus()') is not True:
                    raise BrowserInputInterrupted('输入窗口焦点已变化，已停止输入。', reason='focus_changed', phase='type')
    except BrowserInputInterrupted:
        raise
    except Exception as error:
        raise BrowserInputInterrupted('浏览器输入中断，请确认当前页面后重试。', browser_error=browser_error_category(error)) from None


def replace_field(page, frame, locator, text, checkpoint, *, deadline=None, rebind_before_input=None):
    """Replace only a focused, unchanged field using mouse and keyboard events."""
    validate_keyboard_text(text)
    original = None
    phase = 'before_input'
    try:
        with _navigation_guard(page, checkpoint, deadline, frame=frame) as check:
            check()
            page.bring_to_front()
            check()
            original = locator.element_handle(timeout=500)
            if original is None:
                raise BrowserInputInterrupted('登录输入字段已变化，已停止输入。', reason='field_missing', phase='before_input')
            phase = 'focus'
            click_control(locator)
            check()

            def require_field(phase, expected=None):
                # Use the captured handle, not a Locator that may auto-wait for
                # a replacement. One CDP read checks identity/liveness, focus
                # and the expected prefix together without returning the value.
                reason = original.evaluate('''(element, expected) => {
                    const __duo_input_state = true;
                    if (!element.isConnected || element.ownerDocument !== document) return 'element_replaced';
                    if (element !== document.activeElement || !document.hasFocus()) return 'focus_changed';
                    if (expected !== null && element.value !== expected) return 'value_changed';
                    return null;
                }''', expected)
                if reason in ('element_replaced', 'focus_changed', 'value_changed'):
                    raise BrowserInputInterrupted('登录输入目标或内容已变化，已停止输入。', reason=reason, phase=phase)

            if locator.evaluate('(element, original) => element === original && element.isConnected && element.ownerDocument === document', original) is not True:
                if rebind_before_input is None:
                    raise BrowserInputInterrupted('登录输入字段已变化，已停止输入。', reason='element_replaced', phase='before_input')
                replacement = rebind_before_input()
                if replacement is None:
                    raise BrowserInputInterrupted('登录输入字段已变化，已停止输入。', reason='element_replaced', phase='before_input')
                original.dispose()
                locator = replacement
                original = locator.element_handle(timeout=500)
                if original is None:
                    raise BrowserInputInterrupted('登录输入字段已变化，已停止输入。', reason='field_missing', phase='before_input')
                click_control(locator)
                check()

            require_field('focus')
            phase = 'clear'
            page.keyboard.press('ControlOrMeta+A')
            check()
            require_field('clear')
            page.keyboard.press('Backspace')
            prefix = ''
            phase = 'type'
            for char in text:
                check()
                require_field('type', prefix)
                page.keyboard.type(char)
                prefix += char
                check()
                require_field('type', prefix)
    except BrowserInputInterrupted:
        raise
    except Exception as error:
        from browser_login import BrowserSessionError
        if isinstance(error, BrowserSessionError):
            raise
        raise BrowserInputInterrupted('浏览器登录输入中断，请确认页面后重试。', reason='cdp_error', phase=phase,
                                      browser_error=browser_error_category(error)) from None
    finally:
        if original is not None:
            try:
                original.dispose()
            except Exception:
                pass
