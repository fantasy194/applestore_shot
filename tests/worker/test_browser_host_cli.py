"""Explicit browser-host teardown precedes account credential removal."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import uuid
import main

class BrowserHostCliTests(unittest.TestCase):
    def test_remove_closes_only_derived_profile_before_vault_delete(self):
        account=str(uuid.uuid4());order=[]
        with tempfile.TemporaryDirectory() as root:
            profile=Path(root)/'expected-profile'
            with patch('sys.argv',['worker','--forget-account',account,'--data-dir',root]), \
                 patch.dict(os.environ,{'DUO_BROWSER_SOCKET':'fixture'}), \
                 patch('apple_login.browser_profile_path',return_value=profile) as derive, \
                 patch('browser_process.close_profile',side_effect=lambda path:order.append(('close',path))), \
                 patch('credentials.Vault') as vault:
                vault.return_value.delete.side_effect=lambda key:order.append(('delete',key))
                main.main()
            derive.assert_called_once_with(Path(root).resolve()/'accounts'/account/'auth.json')
            self.assertEqual(order,[('close',profile),('delete',account)])

    def test_busy_profile_does_not_delete_credential(self):
        account=str(uuid.uuid4())
        with patch('sys.argv',['worker','--forget-account',account,'--data-dir','.']), \
             patch.dict(os.environ,{'DUO_BROWSER_SOCKET':'fixture'}), \
             patch('browser_process.close_profile',side_effect=OSError('profile busy')), \
             patch('credentials.Vault') as vault:
            with self.assertRaisesRegex(OSError,'busy'):main.main()
            vault.assert_not_called()

    def test_explicit_host_shutdown_does_not_open_accounts_or_vault(self):
        with patch('sys.argv',['worker','--shutdown-browser-host']), \
             patch('browser_process.shutdown_browser_host',return_value=True) as shutdown, \
             patch('credentials.Vault') as vault:
            main.main();shutdown.assert_called_once_with();vault.assert_not_called()

    def test_local_vault_removal_without_broker_does_not_close_browser(self):
        account=str(uuid.uuid4())
        with patch('sys.argv',['worker','--forget-account',account]), \
             patch.dict(os.environ,{'DUO_BROWSER_SOCKET':''}), \
             patch('browser_process.close_profile') as close,patch('credentials.Vault') as vault:
            main.main();close.assert_not_called();vault.return_value.delete.assert_called_once_with(account)

if __name__=='__main__':unittest.main()
