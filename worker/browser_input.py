"""Keyboard events with bounded lifetime; never expose supplied text in errors."""
from contextlib import contextmanager
import random
import time


class BrowserInputInterrupted(ValueError):
    pass


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

    def invalidate(*args):
        nonlocal invalidated
        invalidated = True

    def changed(changed_frame):
        if frame is None or changed_frame == frame or changed_frame == page.main_frame:
            invalidate()

    listeners = [('framenavigated', changed), ('framedetached', changed),
                 ('close', invalidate), ('crash', invalidate)]
    try:
        for event, callback in listeners:
            page.on(event, callback)

        def check():
            checkpoint()
            if invalidated or page.is_closed() or deadline is not None and time.monotonic() >= deadline:
                raise BrowserInputInterrupted('页面已变化或输入已超时，已停止剩余输入。')
            if frame is not None and frame.is_detached():
                raise BrowserInputInterrupted('登录页面已变化，已停止剩余输入。')
        yield check
    finally:
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
            for char in text:
                check()
                page.keyboard.type(char, delay=random.SystemRandom().randint(20, 99))
                check()
    except BrowserInputInterrupted:
        raise
    except Exception:
        raise BrowserInputInterrupted('浏览器输入中断，请确认当前页面后重试。') from None


def replace_field(page, frame, locator, text, checkpoint, *, deadline=None):
    """Replace only a focused, unchanged field using mouse and keyboard events."""
    validate_keyboard_text(text)
    original = None
    try:
        with _navigation_guard(page, checkpoint, deadline, frame=frame) as check:
            check()
            original = locator.element_handle(timeout=500)
            if original is None:
                raise BrowserInputInterrupted('登录输入字段已变化，已停止输入。')
            locator.click(timeout=1500)
            check()

            def focused():
                return locator.evaluate('(element, original) => element === original && element === element.ownerDocument.activeElement', original) is True

            if not focused():
                raise BrowserInputInterrupted('登录输入焦点已变化，已停止输入。')
            page.keyboard.press('ControlOrMeta+A')
            check()
            if not focused():
                raise BrowserInputInterrupted('登录输入焦点已变化，已停止输入。')
            page.keyboard.press('Backspace')
            prefix = ''
            for char in text:
                check()
                if not focused() or locator.input_value(timeout=500) != prefix:
                    raise BrowserInputInterrupted('登录输入已变化，已停止剩余输入。')
                page.keyboard.type(char, delay=random.SystemRandom().randint(20, 99))
                prefix += char
                check()
                if not focused() or locator.input_value(timeout=500) != prefix:
                    raise BrowserInputInterrupted('登录输入已变化，已停止剩余输入。')
    except BrowserInputInterrupted:
        raise
    except Exception:
        raise BrowserInputInterrupted('浏览器登录输入中断，请确认页面后重试。') from None
    finally:
        if original is not None:
            try:
                original.dispose()
            except Exception:
                pass
