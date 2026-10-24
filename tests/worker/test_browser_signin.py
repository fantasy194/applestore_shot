"""Synthetic form regressions; selectors are not a live Apple DOM assertion."""
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from browser_input import BrowserInputInterrupted, replace_field, type_keyboard_text, validate_keyboard_text
from browser_signin import BrowserSigninAssistant, step_browser_signin
from browser_login import BrowserSessionError


class Field:
    def __init__(self, frame, kind, value='', *, visible=True, autocomplete='', name=''):
        self.frame, self.kind, self.value = frame, kind, value
        self.visible, self.autocomplete, self.name = visible, autocomplete, name
        self.clicks = 0
        self.on_click = None

    def click(self, **_):
        self.clicks += 1
        self.frame.page.focus = self
        if self.on_click:
            self.on_click()

    def input_value(self, **_):
        return self.value

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return True

    def evaluate(self, script, original=None):
        if 'Boolean(element.value)' in script:
            return bool(self.value)
        if original is not None:
            return self is original and ('activeElement' not in script or self.frame.page.focus is self)
        return self.frame.page.focus is self

    def element_handle(self, **_):
        return self

    def locator(self, _selector):
        return Locator([field for field in self.frame.fields if field.kind == 'button'])

    def dispose(self):
        pass


class Locator:
    def __init__(self, values):
        self.values = values

    def count(self):
        return len(self.values)

    def nth(self, index):
        return self.values[index]

    def or_(self, other):
        return Locator(list(dict.fromkeys(self.values + other.values)))

    def and_(self, other):
        return Locator([value for value in self.values if value in other.values])

    def inner_text(self, **_):
        return self.values[0] if self.values else ''

    def all_inner_texts(self):
        return self.values


class Frame:
    def __init__(self, page, url):
        self.page, self.url = page, url
        self.fields, self.body, self.alerts = [], '', []
        self.detached = False

    def is_detached(self):
        return self.detached

    def locator(self, selector):
        if selector == 'body':
            return Locator([self.body])
        if selector.startswith('iframe'):
            return Locator([])
        if selector == 'input[type="password"]':
            return Locator([field for field in self.fields if field.kind == 'password'])
        if 'one-time-code' in selector:
            return Locator([field for field in self.fields if field.autocomplete == 'one-time-code'])
        if 'username' in selector:
            return Locator([field for field in self.fields if field.kind == 'account'])
        if 'current-password' in selector:
            return Locator([field for field in self.fields if field.kind == 'password'])
        if '#sign-in' in selector:
            return Locator([field for field in self.fields if field.kind == 'button'])
        return Locator([])

    def get_by_role(self, role, name=None):
        if role == 'alert':
            return Locator(self.alerts)
        kind = 'button' if role == 'button' else 'account'
        return Locator([field for field in self.fields if field.kind == kind and name.search(field.name)])

    def get_by_label(self, name):
        return Locator([field for field in self.fields if name.search(field.name)])

    def get_by_text(self, name):
        return Locator([field for field in self.fields if field.kind == 'identity' and name.search(field.value)])


class Page:
    def __init__(self, url='https://secure.www.apple.com.cn/shop/signIn'):
        self.url, self.focus = url, None
        self.main_frame = Frame(self, url)
        self.auth = Frame(self, 'https://idmsa.apple.com.cn/appleauth/auth/authorize')
        self.frames = [self.main_frame, self.auth]
        self.listeners, self.events = {}, []
        self.closed = False
        self.after_type = None
        self.keyboard = SimpleNamespace(type=self.type, press=self.press)

    def is_closed(self):
        return self.closed

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, *args):
        for callback in self.listeners.get(event, [])[:]:
            callback(*args)

    def type(self, char, *, delay):
        self.events.append((char, delay))
        if self.focus:
            self.focus.value += char
        if self.after_type:
            self.after_type()

    def press(self, key, **_):
        if key == 'Backspace':
            self.focus.value = ''


class BrowserInputTests(unittest.TestCase):
    def test_controls_and_surrogates_rejected_before_browser_event(self):
        for text in ('a\nb', '\x00', '\x7f', '\x85', '\ud800', ''):
            with self.subTest(text=repr(text)), self.assertRaises(BrowserInputInterrupted):
                validate_keyboard_text(text)

    def test_replacement_uses_click_and_individual_bounded_keyboard_events(self):
        page = Page()
        field = Field(page.auth, 'password', 'old')
        replace_field(page, page.auth, field, 'secret-fixture', lambda: None)
        self.assertEqual(field.value, 'secret-fixture')
        self.assertEqual(field.clicks, 1)
        self.assertEqual(''.join(char for char, _ in page.events), field.value)
        self.assertTrue(all(100 <= delay <= 200 for _, delay in page.events))
        self.assertTrue(all(not callbacks for callbacks in page.listeners.values()))

    def test_same_url_reload_interrupts_after_first_character(self):
        page = Page()
        field = Field(page.auth, 'password')
        page.after_type = lambda: page.emit('framenavigated', page.auth)
        with self.assertRaises(BrowserInputInterrupted):
            replace_field(page, page.auth, field, 'secret-fixture', lambda: None)
        self.assertEqual(field.value, 's')
        self.assertTrue(all(not callbacks for callbacks in page.listeners.values()))

    def test_focus_change_or_field_intervention_stops_remaining_password(self):
        for intervention in ('focus', 'value'):
            page = Page()
            field = Field(page.auth, 'password')
            def intervene():
                if intervention == 'focus':
                    page.focus = Field(page.auth, 'other')
                else:
                    field.value += 'manual'
            page.after_type = intervene
            with self.subTest(intervention=intervention), self.assertRaises(BrowserInputInterrupted):
                replace_field(page, page.auth, field, 'secret-fixture', lambda: None)
            self.assertEqual(len(page.events), 1)

    def test_general_keyboard_allows_same_document_otp_autofocus(self):
        page = Page()
        first, second = Field(page.auth, 'otp'), Field(page.auth, 'otp')
        page.focus = first
        page.after_type = lambda: setattr(page, 'focus', second)
        type_keyboard_text(page, '12')
        self.assertEqual((first.value, second.value), ('1', '2'))

    def test_general_keyboard_any_frame_reload_or_close_stops(self):
        for event in ('framenavigated', 'framedetached', 'close'):
            page = Page()
            page.after_type = lambda: page.emit(event, page.auth) if event != 'close' else page.emit(event)
            with self.subTest(event=event), self.assertRaises(BrowserInputInterrupted):
                type_keyboard_text(page, 'private-fixture')
            self.assertEqual(len(page.events), 1)

    def test_deadline_and_cancellation_do_not_type(self):
        page = Page()
        with self.assertRaises(BrowserInputInterrupted):
            type_keyboard_text(page, 'fixture', deadline=0)
        class Cancelled(BaseException):
            pass
        def cancel():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            type_keyboard_text(page, 'fixture', cancel)
        self.assertEqual(page.events, [])

    def test_browser_exception_does_not_expose_input(self):
        page = Page()
        page.keyboard.type = Mock(side_effect=RuntimeError('private-fixture'))
        with self.assertRaises(BrowserInputInterrupted) as raised:
            type_keyboard_text(page, 'private-fixture')
        self.assertNotIn('private-fixture', str(raised.exception))


class BrowserSigninTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()
        self.service = SimpleNamespace(account='owner@example.test', channel=SimpleNamespace(checkpoint=Mock()),
                                       browser_password=Mock(return_value='private-fixture'), stage=Mock(), reject_password=Mock())
        self.assistant = BrowserSigninAssistant(self.service, headless=True)
        self.network = {'pending': {}, 'error': None}

    def form(self, *, account='', password=False, visible=True):
        account = Field(self.page.auth, 'account', account, visible=visible)
        button = Field(self.page.auth, 'button', name='Continue')
        self.page.auth.fields = [account, button]
        if password:
            self.page.auth.fields.append(Field(self.page.auth, 'password', name='Password'))
        return account, button

    def test_account_first_step_types_and_clicks_only_once(self):
        account, button = self.form()
        self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
        self.assertEqual(account.value, self.service.account)
        self.assertEqual(self.assistant.step(self.page, self.network), 'waiting')
        self.assertEqual(button.clicks, 1)
        self.service.browser_password.assert_not_called()

    def test_disabled_continue_can_become_enabled_after_account_input(self):
        account, button = self.form()
        button.is_enabled = lambda: account.value == self.service.account
        self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
        self.assertEqual(button.clicks, 1)

    def test_password_provider_navigation_cannot_use_stale_identity(self):
        account, button = self.form(account=self.service.account, password=True)
        def changed_identity(**_):
            account.value = 'other@example.test'
            return 'private-fixture'
        self.service.browser_password.side_effect = changed_identity
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_ACCOUNT_MISMATCH')
        self.assertEqual(self.page.events, [])
        self.assertEqual(button.clicks, 0)

    def test_same_url_reload_while_password_provider_waits_stops_before_typing(self):
        self.form(account=self.service.account, password=True)
        def reload(**_):
            self.page.emit('framenavigated', self.page.auth)
            return 'private-fixture'
        self.service.browser_password.side_effect = reload
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_INPUT_INTERRUPTED')
        self.assertEqual(self.page.events, [])

    def test_replaced_password_element_while_provider_waits_stops_before_typing(self):
        self.form(account=self.service.account, password=True)
        password = self.page.auth.fields[-1]
        password.evaluate = lambda _script, original=None: False
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_INPUT_INTERRUPTED')
        self.assertEqual(self.page.events, [])

    def test_replacement_iframe_never_repeats_account_or_password_submission(self):
        for stage in ('account', 'password'):
            with self.subTest(stage=stage):
                self.setUp()
                self.form(account=self.service.account if stage == 'password' else '', password=stage == 'password')
                self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
                replacement = Frame(self.page, self.page.auth.url)
                replacement.fields = [Field(replacement, 'account', self.service.account),
                                      Field(replacement, 'button', name='Continue')]
                if stage == 'password':
                    replacement.fields.append(Field(replacement, 'password', name='Password'))
                self.page.frames = [self.page.main_frame, replacement]
                count = len(self.page.events)
                self.assertEqual(self.assistant.step(self.page, self.network), 'waiting')
                self.assertEqual(len(self.page.events), count)
                self.assertEqual(replacement.fields[1].clicks, 0)

    def test_hidden_matching_account_binds_password_step_without_profile_guess(self):
        _, button = self.form(account=self.service.account, password=True, visible=False)
        self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
        self.service.browser_password.assert_called_once_with(allow_prompt=False)
        self.assertEqual(button.clicks, 1)
        self.assertEqual(self.assistant.step(self.page, self.network), 'waiting')
        self.assertNotIn('private-fixture', str(self.service.stage.call_args_list))

    def test_prefilled_password_for_matching_account_continues_without_provider_or_prompt(self):
        for headless in (False, True):
            with self.subTest(headless=headless):
                self.setUp()
                self.assistant = BrowserSigninAssistant(self.service, headless=headless)
                _, button = self.form(account=self.service.account, password=True)
                self.page.auth.fields[-1].value = 'browser-filled-fixture'
                self.service.browser_password.return_value = None
                self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
                self.assertEqual(button.clicks, 1)
                self.assertEqual(self.page.events, [])
                self.service.browser_password.assert_not_called()
                self.assertNotIn('browser-filled-fixture', str(self.service.stage.call_args_list))

    def test_prefilled_password_for_another_account_cannot_continue(self):
        _, button = self.form(account='another@example.test', password=True)
        self.page.auth.fields[-1].value = 'browser-filled-fixture'
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_ACCOUNT_MISMATCH')
        self.assertEqual(button.clicks, 0)
        self.assertEqual(self.page.events, [])
        self.service.browser_password.assert_not_called()

    def test_password_without_identity_never_reads_secret(self):
        self.page.auth.fields = [Field(self.page.auth, 'password', name='Password'),
                                 Field(self.page.auth, 'button', name='Sign in')]
        self.assertEqual(self.assistant.step(self.page, self.network), 'unrecognized')
        self.service.browser_password.assert_not_called()

    def test_different_account_stops_before_any_input_or_password_read(self):
        _, button = self.form(account='another@example.test', password=True)
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_ACCOUNT_MISMATCH')
        self.assertEqual(self.page.events, [])
        self.assertEqual(button.clicks, 0)
        self.service.browser_password.assert_not_called()

    def test_otp_and_challenges_never_type_or_submit(self):
        for challenge in ('otp', 'captcha'):
            self.form(account=self.service.account, password=True)
            if challenge == 'otp':
                self.page.auth.fields.append(Field(self.page.auth, 'otp', autocomplete='one-time-code'))
            else:
                self.page.auth.body = 'Please verify you are human'
            with self.subTest(challenge=challenge):
                self.assertEqual(self.assistant.step(self.page, self.network), 'verification')
                self.assertEqual(self.page.events, [])
                self.service.browser_password.assert_not_called()

    def test_external_iframe_and_nonlogin_top_level_are_ignored(self):
        for location in ('iframe', 'top'):
            self.form(account=self.service.account, password=True)
            if location == 'iframe':
                self.page.auth.url = 'https://idmsa.apple.com.cn.attacker.test/auth'
            else:
                self.page.url = 'https://www.apple.com.cn/shop/bag'
            with self.subTest(location=location):
                self.assertEqual(self.assistant.step(self.page, self.network), 'unrecognized')
                self.assertEqual(self.page.events, [])

    def test_pending_and_error_network_prevent_actions(self):
        self.form(account=self.service.account, password=True)
        for network in ({'pending': {'request': 1}}, {'error': 'HTTP 541'}):
            self.assertEqual(self.assistant.step(self.page, network), 'waiting')
        self.service.browser_password.assert_not_called()

    def test_missing_saved_password_returns_attention_without_submit(self):
        _, button = self.form(account=self.service.account, password=True)
        self.service.browser_password.return_value = None
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
        self.assertEqual(button.clicks, 0)

    def test_failed_password_is_never_retried(self):
        _, button = self.form(account=self.service.account, password=True)
        self.page.auth.alerts = ['Incorrect password']
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_AUTHENTICATION_FAILED')
        self.service.reject_password.assert_called_once()
        self.assertEqual(button.clicks, 0)
        with self.assertRaises(BrowserSessionError):
            self.assistant.step(self.page, {'http_status': 401})
        self.service.browser_password.assert_not_called()

    def test_interrupted_fill_never_resumes_after_navigation(self):
        self.form(account=self.service.account, password=True)
        self.page.after_type = lambda: self.page.emit('framenavigated', self.page.auth)
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'BROWSER_LOGIN_INPUT_INTERRUPTED')
        with self.assertRaises(BrowserSessionError):
            self.assistant.step(self.page, self.network)
        self.assertEqual(len(self.page.events), 1)

    def test_uncertain_button_click_is_reserved_before_failure(self):
        _, button = self.form()
        button.on_click = Mock(side_effect=RuntimeError('private-fixture'))
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertNotIn('private-fixture', str(raised.exception))
        with self.assertRaises(BrowserSessionError):
            self.assistant.step(self.page, self.network)
        self.assertEqual(button.clicks, 1)

    def test_explicit_existing_credentials_resumes_only_unsubmitted_password_step(self):
        _, button = self.form(account=self.service.account, password=True)
        self.assistant.submitted.add('account')
        self.assistant.interrupted = True
        self.assistant.deadline = 0
        self.assertTrue(self.assistant.request_existing_credentials())
        self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
        self.service.browser_password.assert_called_once_with(allow_prompt=False)
        self.assertEqual(button.clicks, 1)
        self.assertFalse(self.assistant.request_existing_credentials())
        self.assertEqual(self.assistant.step(self.page, self.network), 'waiting')
        self.assertEqual(button.clicks, 1)

    def test_explicit_existing_credentials_never_retries_failed_authentication(self):
        self.form(account=self.service.account, password=True)
        with self.assertRaises(BrowserSessionError):
            self.assistant.step(self.page, {'authentication_failed': True})
        self.assertFalse(self.assistant.request_existing_credentials())
        self.service.browser_password.assert_not_called()

    def test_explicit_resume_replaces_our_partial_password_instead_of_submitting_it(self):
        _, button = self.form(account=self.service.account, password=True)
        password = self.page.auth.fields[-1]
        self.page.after_type = lambda: setattr(self.page, 'focus', None)
        with self.assertRaises(BrowserSessionError):
            self.assistant.step(self.page, self.network)
        self.assertTrue(self.assistant.password_input_incomplete)
        self.assertEqual(password.value, 'p')
        self.assertEqual(button.clicks, 0)
        self.page.after_type = None
        self.assertTrue(self.assistant.request_existing_credentials())
        self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
        self.assertEqual(password.value, 'private-fixture')
        self.assertEqual(button.clicks, 1)
        self.assertFalse(self.assistant.request_existing_credentials())

    def test_explicit_resume_replaces_our_partial_account_and_preserves_selected_identity(self):
        account, button = self.form()
        self.page.after_type = lambda: setattr(self.page, 'focus', None)
        with self.assertRaises(BrowserSessionError):
            self.assistant.step(self.page, self.network)
        self.assertTrue(self.assistant.account_input_incomplete)
        self.assertEqual(button.clicks, 0)
        self.page.after_type = None
        self.assertTrue(self.assistant.request_existing_credentials())
        self.assertEqual(self.assistant.step(self.page, self.network), 'acted')
        self.assertEqual(account.value, self.service.account)
        self.assertEqual(button.clicks, 1)

    def test_remote_manual_input_and_assistant_do_not_interleave(self):
        lock = threading.Lock()
        remote = SimpleNamespace(interaction_lock=lock)
        assistant = Mock()
        lock.acquire()
        self.assertEqual(step_browser_signin(assistant, self.page, self.network, remote), 'waiting')
        assistant.step.assert_not_called()
        lock.release()
        assistant.step.side_effect = RuntimeError('fixture')
        with self.assertRaises(RuntimeError):
            step_browser_signin(assistant, self.page, self.network, remote)
        self.assertFalse(lock.locked())

    def test_waiting_disabled_prefilled_form_does_not_toggle_interaction_status(self):
        for password_step in (False, True):
            with self.subTest(password_step=password_step):
                self.setUp()
                _, button = self.form(account=self.service.account, password=password_step)
                if password_step:
                    self.page.auth.fields[-1].value = 'browser-prefilled-fixture'
                button.is_enabled = lambda: False
                self.assistant.on_interaction = Mock()
                self.assertEqual(self.assistant.step(self.page, self.network), 'waiting')
                self.assistant.on_interaction.assert_not_called()

    def test_existing_credentials_missing_does_not_open_new_password_prompt(self):
        self.form(account=self.service.account, password=True)
        self.service.browser_password.return_value = None
        self.assistant.headless = False
        self.assertTrue(self.assistant.request_existing_credentials())
        with self.assertRaises(BrowserSessionError) as raised:
            self.assistant.step(self.page, self.network)
        self.assertEqual(raised.exception.code, 'HEADLESS_LOGIN_REQUIRES_ATTENTION')
        self.service.browser_password.assert_called_once_with(allow_prompt=False)
        self.assertNotIn('password', self.assistant.submitted)


if __name__ == '__main__':
    unittest.main()
