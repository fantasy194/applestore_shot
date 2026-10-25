import argparse
import json
import os
import sys
import threading
import uuid
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'legacy'))
from protocol import Channel, LogStream, Cancelled


class AccountLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')

    def __enter__(self):
        self.file.seek(0)
        if os.name == 'nt':
            import msvcrt
            self.file.write(b'\0')
            self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *args):
        self.file.close()


def main():
    if '--activity-log' in sys.argv[1:]:
        from activity_log import main as activity_main
        activity_main()
        return
    # The persistent push worker never opens an Apple account or checkout.
    if '--web-push' in sys.argv[1:]:
        from push_service import main as push_main
        push_main()
        return
    if '--shutdown-browser-host' in sys.argv[1:]:
        from browser_process import shutdown_browser_host
        shutdown_browser_host()
        return
    # Local removal only: reuse the OS vault without starting a login task.
    if '--forget-account' in sys.argv[1:]:
        parser = argparse.ArgumentParser()
        parser.add_argument('--forget-account', required=True, type=uuid.UUID)
        parser.add_argument('--data-dir')
        args = parser.parse_args()
        if args.forget_account.int == 0:
            parser.error('不能移除通用预设。')
        if os.environ.get('DUO_BROWSER_SOCKET'):
            if not args.data_dir:
                parser.error('移除浏览器账号需要应用数据目录。')
            from apple_login import browser_profile_path
            from browser_process import close_profile
            auth=Path(args.data_dir).resolve()/'accounts'/str(args.forget_account)/'auth.json'
            close_profile(browser_profile_path(auth))
        from credentials import Vault
        Vault().delete(str(args.forget_account))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--account-id', required=True, type=uuid.UUID)
    parser.add_argument('--task-id', required=True, type=uuid.UUID)
    args = parser.parse_args()
    channel = Channel(str(args.account_id), str(args.task_id))
    from activity_log import ActivityLog, safe_details
    data_dir = Path(args.data_dir).resolve()
    channel.activity = ActivityLog(data_dir, account_id=channel.account_id, task_id=channel.task_id)
    service = None
    error = None
    try:
        raw = sys.stdin.readline(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('任务输入过大。')
        request = json.loads(raw)
        if request.get('account_id') != channel.account_id or request.get('task_id') != channel.task_id:
            raise ValueError('任务与账号标识不匹配。')
        credential = request.pop('credential', None)
        credential_remember = request.pop('credential_remember', False) is True
        credential_verified = request.pop('credential_verified', False) is True
        credential_rejected = request.pop('credential_rejected', False) is True
        params = request.get('params', {})
        child = isinstance(params, dict) and params.get('_parallel_child') is True
        # Parallel workers keep account/checkout files in isolated sandboxes,
        # while their business diagnostics stay queryable in the owner database.
        activity_root = Path(os.environ.get('DUO_ACTIVITY_DATA_DIR', str(data_dir))) if child else data_dir
        if not child:
            os.environ['DUO_ACTIVITY_DATA_DIR'] = str(data_dir)
        channel.activity = ActivityLog(activity_root, account_id=channel.account_id, task_id=channel.task_id,
                                       action=request.get('action', 'operation'))
        channel.activity.record('operation_start', '开始操作。', details=safe_details(request.get('params')))
        account = request.get('account', '')
        local = args.account_id.int == 0
        inventory_action = request.get('action') in (
            'inventory_settings_read', 'inventory_settings_save', 'store_search',
            'inventory_check', 'inventory_monitor')
        if local and request.get('action') not in ('catalog', 'address_read', 'address_save') and not inventory_action:
            raise ValueError('此操作需要选择并登录 Apple 账号。')
        if not local and (not isinstance(account, str) or not account.strip()):
            raise ValueError('账号不能为空。')
        data_dir = Path(args.data_dir).resolve()
        root = data_dir / 'settings' if local else data_dir / 'accounts' / channel.account_id
        task_dir = root / 'tasks' / channel.task_id
        task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        threading.Thread(target=channel.read_replies, args=(sys.stdin,), daemon=True).start()
        # Anonymous monitoring must not keep the settings/account execution lock:
        # the owner can still edit targets and discover stores while it runs.
        task_lock = (AccountLock(root / '.inventory-monitor.lock')
                     if request.get('action') == 'inventory_monitor' else
                     nullcontext() if inventory_action else AccountLock(root / '.worker.lock'))
        with task_lock, redirect_stdout(LogStream(channel)), redirect_stderr(LogStream(channel)):
            from service import Service
            service = Service(channel, root, task_dir, account)
            service.password_rejected = credential_rejected
            legacy_remember = params.pop('_remember_password', False) is True
            if isinstance(credential, str) and credential and len(credential.encode('utf-8')) <= 4096 and not child:
                service.password = credential
                service.remember = credential_remember or legacy_remember
                service._credential_source = 'service_cache'
                if credential_verified:
                    service._password_authenticated = credential
            credential = None
            service.enable_logging()
            result = service.run(request['action'], request.get('params', {}))
            service.finish_prepared_handoff(result)
            channel.emit('result', result=result)
    except Cancelled:
        if service:
            service.finish_prepared_handoff(cancelled=True)
        channel.emit('cancelled', message='任务已取消。',
                     order_submitted=bool(service and service.submitted))
    except Exception as exc:
        error = exc
        if service:
            service.finish_prepared_handoff(error=exc)
        # Never include requests' exception URLs, headers, or a traceback with locals.
        import requests
        message = '网络请求失败，请检查网络或系统代理。' if isinstance(exc, requests.RequestException) else str(exc)
        if not isinstance(exc, (ValueError, OSError)):
            message = f'操作未完成（{type(exc).__name__}），请查看任务阶段。'
        if service and service.submitted:
            message += ' 订单提交结果可能未确认，请查询订单后再决定下一步。'
        from protocol import checkout_browser_error, checkout_browser_diagnostics
        diagnostic = checkout_browser_diagnostics(checkout_browser_error(exc))
        channel.emit('error', message=message[:2000], order_submitted=bool(service and service.submitted),
                     account_invalid=bool(service and service.should_remove_account(exc)), **diagnostic)
    finally:
        if service:
            try:
                service.cleanup_credentials(error)
            except Exception as cleanup_error:
                channel.activity.record('credential_cleanup_error', '任务本地会话清理未完成。', level='warning',
                                        details={'error_type': type(cleanup_error).__name__})


if __name__ == '__main__':
    # Frozen Windows workers may ignore PYTHONIOENCODING; IPC always uses UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    main()
