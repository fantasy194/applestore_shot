import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import interaction
import duo_killer
from protocol import Channel, Cancelled
from service import Service, empty_auth, validate_address, validate_import
from platform_support import browser_candidates


class ChannelTests(unittest.TestCase):
    def test_wrong_account_and_stale_prompt_are_ignored(self):
        output=io.StringIO(); channel=Channel('a','t',output)
        ready=threading.Event()
        original_emit=channel.emit
        def emit(*args, **kwargs):
            original_emit(*args, **kwargs)
            ready.set()
        channel.emit=emit
        received=[]
        thread=threading.Thread(target=lambda:received.append(channel.prompt('code','验证码')),daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        request=json.loads(output.getvalue())['prompt']['id']
        channel.receive(dict(type='reply',account_id='other',task_id='t',request_id=request,value='wrong'))
        channel.receive(dict(type='reply',account_id='a',task_id='t',request_id='stale',value='wrong'))
        channel.receive(dict(type='reply',account_id='a',task_id='t',request_id=request,value='012345'))
        thread.join(2)
        self.assertEqual(received,['012345'])
        self.assertNotIn('012345',output.getvalue())

    def test_cancel_unblocks_input(self):
        channel=Channel('a','t',io.StringIO()); channel.receive(dict(type='cancel',account_id='a',task_id='t'))
        with self.assertRaises(Cancelled):channel.prompt('code','验证码')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.globals = {key: getattr(duo_killer, key) for key in ('ROOT','AUTH_PATH','LOG_PATH','PAYMENT_ROOT')}
        import alipay_qr
        self.qr_root = alipay_qr.ROOT

    def tearDown(self):
        interaction.handler=None
        for key, value in self.globals.items():
            setattr(duo_killer, key, value)
        import alipay_qr
        alipay_qr.ROOT = self.qr_root

    def test_import_strips_password_and_other_domains(self):
        data=empty_auth('a@example.test');data['password']='not-to-persist'
        data['cookies']['account_session']=[dict(name='one',value='fixture',domain='.apple.com.cn'),dict(name='two',value='no',domain='evil.test')]
        cleaned=validate_import(data,'a@example.test')
        self.assertNotIn('password',cleaned)
        self.assertEqual(len(cleaned['cookies']['account_session']),1)
        with self.assertRaises(ValueError):validate_import(data,'b@example.test')

    def test_code_prompt_exposes_only_available_switch_actions(self):
        from unittest.mock import Mock
        cases = [
            ('👉 设备六位验证码（输入 s 改用短信）：', '设备六位验证码：', 's',
             {'actions': [{'value': 's', 'label': '改用短信验证码'}]}),
            ('👉 短信六位验证码（输入 d 改用设备）：', '短信六位验证码：', 'd',
             {'actions': [{'value': 'd', 'label': '改用设备验证码'}]}),
            ('👉 设备六位验证码：', '设备六位验证码：', '012345', {}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            channel = Mock()
            service = Service(channel, Path(directory), Path(directory), 'fixture', Mock())
            for original, label, answer, fields in cases:
                with self.subTest(label=original):
                    channel.prompt.reset_mock()
                    channel.prompt.return_value = answer
                    self.assertEqual(service.text(original), answer)
                    channel.prompt.assert_called_once_with('code', label, default='', **fields)

    def test_password_failure_retains_vault_and_identity(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);vault=Mock();service=Service(Channel('a','t',io.StringIO()),root,root,'a@example.test',vault)
            service.ensure_auth_file();service.cleanup_credentials(ValueError('401 -20101'))
            vault.delete.assert_not_called()
            data=json.loads(service.auth.read_text());self.assertEqual(data['account'],'a@example.test');self.assertNotIn('password',data)

    def test_expiry_is_recorded_separately_from_cancellation_without_resetting_account(self):
        from unittest.mock import Mock
        for status, marker_state in [('order_expired', 'expired'), ('order_cancelled', 'cancelled')]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); vault = Mock()
                service = Service(Channel('a', 't', io.StringIO()), root, root, 'a@example.test', vault)
                service.ensure_auth_file()
                original = service.auth.read_bytes()
                marker = root / 'order_submission.json'
                marker.write_text(json.dumps({'state': 'submitted', 'order_number': 'W1234567890'}))
                with patch('order_cancel.cancel_order', return_value={
                        'success': True, 'status': status, 'order_number': 'W1234567890'}), \
                        patch.object(duo_killer, 'save_auth'):
                    service.run('cancel_order', {'order_number': 'W1234567890'})
                self.assertEqual(json.loads(marker.read_text())['state'], marker_state)
                self.assertEqual(json.loads((root / 'order_cancellations.json').read_text())['W1234567890'], status)
                self.assertEqual(service.auth.read_bytes(), original)
                vault.delete.assert_not_called()

    def test_windows_and_macos_browser_locations(self):
        win=browser_candidates('win32',{'PROGRAMFILES':'C:/Program Files','LOCALAPPDATA':'C:/Local'})
        self.assertTrue(any(str(p).endswith('chrome.exe') for p in win))
        self.assertTrue(any(str(p).endswith('msedge.exe') for p in win))
        self.assertTrue(any(str(p).endswith('Google Chrome') for p in browser_candidates('darwin')))

    def test_confirmation_requires_a_true_boolean(self):
        from unittest.mock import Mock
        channel=Mock();channel.prompt.return_value=''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);service=Service(channel,root,root,'fixture',Mock())
            model={'checkout':{'review':{'fulfillmentReview':{},'summary':{'d':{'total':'RMB 1'}}}}}
            with patch.object(duo_killer,'model_item_nodes',return_value=[{'d':{'name':'测试','sku':'TEST/A','totalPrice':'RMB 1'},'itemQuantity':{'d':{'quantity':1}}}]):
                with self.assertRaisesRegex(ValueError,'已取消'):duo_killer.confirm_order(model)


class ProcessIsolationTests(unittest.TestCase):
    def test_worker_protocol_uses_utf8_with_non_utf8_system_encoding(self):
        root = Path(__file__).resolve().parents[2] / 'worker'
        account_id, task_id = str(uuid.UUID(int=0)), str(uuid.uuid4())
        address = {
            'contact': {'lastName': '测', 'firstName': '试', 'emailAddress': 'fixture@example.test', 'fullDaytimePhone': '13800000000'},
            'delivery': {'state': '四川', 'city': '成都', 'district': '锦江区'},
            'address': {'street': '测试路 1 号'},
        }
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, str(root / 'main.py'), '--data-dir', directory, '--account-id', account_id, '--task-id', task_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8',
                env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'},
            )
            try:
                request = dict(account_id=account_id, task_id=task_id, account='', action='address_save', params=address)
                process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
                process.stdin.flush()
                event = json.loads(process.stdout.readline())
                self.assertEqual(event['type'], 'result', event)
                self.assertEqual(event['result']['config'], address)
                self.assertEqual(event['result']['message'], '快捷地址已保存，结账时可选择使用。')
                self.assertEqual(process.wait(15), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def test_two_concurrent_workers_write_only_their_account(self):
        root=Path(__file__).resolve().parents[2]/'worker'
        with tempfile.TemporaryDirectory() as directory:
            processes=[];accounts=[]
            try:
                for index in range(2):
                    account_id,task_id=str(uuid.uuid4()),str(uuid.uuid4());accounts.append(account_id)
                    process=subprocess.Popen([sys.executable,str(root/'main.py'),'--data-dir',directory,'--account-id',account_id,'--task-id',task_id],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
                    data={'contact':{'lastName':f'测试{index}','firstName':'人','emailAddress':f'a{index}@example.test','fullDaytimePhone':'13800000000'},'delivery':{'state':'四川','city':'成都','district':'锦江'},'address':{'street':f'测试地址{index}'}}
                    process.stdin.write(json.dumps(dict(account_id=account_id,task_id=task_id,account=f'a{index}@example.test',action='address_save',params=data))+'\n');process.stdin.flush()
                    processes.append(process)
                for index,process in enumerate(processes):
                    event=json.loads(process.stdout.readline());self.assertEqual(event['type'],'result',event)
                    self.assertEqual(event['account_id'],accounts[index]);self.assertEqual(process.wait(15),0)
                    data=json.loads((Path(directory)/'accounts'/accounts[index]/'checkout.json').read_text())
                    self.assertEqual(data['address']['street'],f'测试地址{index}')
                self.assertFalse((Path(directory)/'auth.json').exists())
            finally:
                for process in processes:
                    if process.poll() is None:process.kill()
                    process.wait(5)
                    for pipe in (process.stdin,process.stdout,process.stderr):pipe.close()




class AccountRemovalTests(unittest.TestCase):
    def test_forget_mode_only_deletes_the_requested_vault_entry(self):
        import main
        account = str(uuid.uuid4())
        with patch.object(sys, 'argv', ['worker', '--forget-account', account]), patch('credentials.Vault') as vault:
            main.main()
            vault.return_value.delete.assert_called_once_with(account)

    def test_forget_mode_rejects_paths_and_local_scope(self):
        import main
        for account in ('../outside', str(uuid.UUID(int=0))):
            with patch.object(sys, 'argv', ['worker', '--forget-account', account]), patch('credentials.Vault') as vault, patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    main.main()
                vault.assert_not_called()

    def test_vault_deletion_does_not_hide_a_remaining_password(self):
        from credentials import Vault
        from keyring.errors import PasswordDeleteError
        from unittest.mock import Mock
        vault = Vault.__new__(Vault)
        vault.backend = Mock()
        vault.backend.delete_password.side_effect = PasswordDeleteError('fixture failure')
        vault.backend.get_password.return_value = 'fixture-password'
        with self.assertRaises(PasswordDeleteError):
            vault.delete('fixture-id')
        vault.backend.get_password.return_value = None
        vault.delete('fixture-id')


if __name__ == '__main__':
    unittest.main()
