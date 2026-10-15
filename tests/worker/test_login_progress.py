import contextlib
import io
from datetime import timedelta
from threading import Event
import unittest
from unittest.mock import patch

import requests
from urllib3.response import HTTPResponse

import duo_killer
from apple_login import AppleStoreLogin


class LoginProgressTests(unittest.TestCase):
    def test_request_timing_separates_header_wait_from_body_without_secrets(self):
        client = AppleStoreLogin()
        self.addCleanup(client.session.close)
        url = 'https://secure6.www.apple.com.cn/shop/checkoutx/billing?token=private'
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = b'{}'
        response.elapsed = timedelta(seconds=10)
        response.raw = HTTPResponse(headers={})
        response.request = requests.Request('POST', url).prepare()
        with patch.object(client.session, 'request', return_value=response), \
                patch.object(duo_killer, 'perf_counter', side_effect=[0, 10.2]), \
                patch.object(duo_killer.log, 'disabled', False), self.assertLogs('duo_killer', level='INFO') as captured:
            duo_killer.request_get(client.session, '计时测试', url, method='POST')
        logs = '\n'.join(captured.output)
        self.assertIn('等待响应头 10.00s', logs)
        self.assertIn('本地处理 0.20s', logs)
        self.assertNotIn('token=private', logs)
    def test_spinner_clears_on_success_failure_and_cancellation(self):
        for error in (None, ValueError('failed'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                rendered = Event()
                class Terminal(io.StringIO):
                    def isatty(self):
                        return True

                    def write(self, text):
                        result = super().write(text)
                        rendered.set()
                        return result
                terminal = Terminal()
                with patch.object(duo_killer.log, 'disabled', True), contextlib.redirect_stdout(terminal):
                    try:
                        with duo_killer.waiting('登录中'):
                            self.assertTrue(rendered.wait(1))
                            if error:
                                raise error
                    except (ValueError, KeyboardInterrupt) as caught:
                        self.assertIs(caught, error)
                self.assertIn('登录中', terminal.getvalue())
                self.assertTrue(terminal.getvalue().endswith('\r\033[2K'))

    def test_spinner_disabled_for_logs_or_redirected_output(self):
        for tty, logging_enabled in ((False, False), (True, True)):
            with self.subTest(tty=tty, logging_enabled=logging_enabled), \
                    contextlib.redirect_stdout(io.StringIO()) as output, \
                    patch.object(output, 'isatty', return_value=tty), \
                    patch.object(duo_killer.log, 'disabled', not logging_enabled), patch.object(duo_killer, 'Thread') as thread:
                with duo_killer.waiting('登录中'):
                    pass
                thread.assert_not_called()
                self.assertEqual(output.getvalue(), '')

    def test_login_request_is_quiet_without_log_and_recorded_with_log(self):
        client = AppleStoreLogin()
        self.addCleanup(client.session.close)
        client.store_request = duo_killer.request_get
        url = 'https://secure6.www.apple.com.cn/shop/signIn/idms/authx?ssi=secret-token'
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = b'{}'
        response.raw = HTTPResponse(headers={})
        response.request = requests.Request('POST', url).prepare()
        with patch.object(client.session, 'request', return_value=response), \
                patch.object(duo_killer.log, 'disabled', True), contextlib.redirect_stdout(io.StringIO()) as output:
            client.request('POST', url)
            self.assertEqual(output.getvalue(), '')
        with patch.object(client.session, 'request', return_value=response), \
                patch.object(duo_killer.log, 'disabled', False), contextlib.redirect_stdout(io.StringIO()) as output, \
                self.assertLogs('duo_killer', level='INFO') as captured:
            client.request('POST', url)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('HTTP 200', '\n'.join(captured.output))
        self.assertNotIn('secret-token', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
