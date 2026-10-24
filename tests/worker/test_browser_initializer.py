"""The legacy context initializer shares automatic, task-bound app OTP."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import apple_login
import interaction


class BrowserInitializerTests(unittest.TestCase):
    def test_background_initializer_uses_app_otp_and_releases_only_its_task_page(self):
        with tempfile.TemporaryDirectory() as directory:
            auth=Path(directory)/'auth.json'
            service=SimpleNamespace(account='initializer@example.test',auth=auth,
                headless_browser=True,channel=SimpleNamespace(checkpoint=Mock(),prompt=Mock(return_value='012345')),
                reject_password=Mock(),stage=Mock())
            page=Mock(url='about:blank')
            old=Mock()
            context=Mock(pages=[old])
            def new_page():
                context.pages.append(page)
                return page
            context.new_page.side_effect=new_page
            def navigate(*args,**kwargs):
                page.url='https://secure8.www.apple.com.cn/shop/signIn'
                return Mock(status=200)
            page.goto.side_effect=navigate
            page.evaluate.return_value='fixture-browser'
            context.cookies.return_value=[{'name':'shld_bt_ck','value':'synthetic',
                'domain':'.apple.com.cn','path':'/','secure':True,'expires':-1}]
            browser=Mock(contexts=[context])
            process=Mock(persistent=True,pid=None)
            process.poll.return_value=None
            state={'pending':{},'error':None,'refresh':Mock()}
            assistant=Mock()
            def step(used,controlled,network,remote):
                self.assertIs(used,assistant)
                self.assertIs(controlled,page)
                code=service.channel.prompt('code','fixture',account=service.account)
                self.assertEqual(code,'012345')
                page.url='https://www.apple.com.cn/'
                return 'acted'
            with patch.object(interaction,'handler',service), \
                    patch('apple_login.find_browser',return_value=Path('fixture-chrome')), \
                    patch('requests.utils.get_environ_proxies',return_value={}), \
                    patch('browser_process.launch_browser',return_value=process), \
                    patch('browser_cdp.connect_browser',return_value=browser), \
                    patch('playwright.sync_api.sync_playwright',return_value=nullcontext(Mock())), \
                    patch('browser_login.observe_login_network',return_value=state) as observer, \
                    patch('browser_login.pending_auth_page',side_effect=lambda current:'/shop/signIn' in current.url), \
                    patch('browser_login.trust_browser_if_offered',return_value=False), \
                    patch('browser_signin.BrowserSigninAssistant',return_value=assistant) as factory, \
                    patch('browser_signin.step_browser_signin',side_effect=step):
                self.assertTrue(apple_login.AppleStoreLogin(auth_path=auth).initialize_browser_context())
            factory.assert_called_once_with(service,headless=True,automatic=True)
            observer.assert_called_once_with(context,service,page=page)
            service.channel.prompt.assert_called_once()
            page.close.assert_called_once()
            old.close.assert_not_called()
            browser.new_browser_cdp_session.assert_not_called()
            process.release.assert_called_once()
            process.terminate.assert_not_called()
            process.kill.assert_not_called()


if __name__=='__main__':
    unittest.main()
