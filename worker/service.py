"""Desktop boundary: legacy network operations remain account-local and sequential."""
import base64
import json
import logging
import re
import time
from pathlib import Path

import requests
import interaction
import duo_killer
import alipay_qr
import checkout_options
from credentials import Vault


def empty_auth(account):
    return {'account': account, 'cookies': {key: [] for key in
            ('account_session', 'server_managed', 'with_expiry', 'expiry_unknown')}, 'trust_cookies': []}


class Service:
    def __init__(self, channel, account_dir, task_dir, account, vault=None):
        self.channel, self.root, self.task_dir, self.account = channel, account_dir, task_dir, account
        self.vault = vault or Vault()
        self.password = None
        self.remember = False
        self.authentication_requested = False
        self.authentication_failed = False
        self.idle_maintenance = False
        self.headless_browser = False
        self.purchase_auth_interaction = False
        self.action = None
        self.submitted = False
        self.automatic_checkout = False
        self.prefill_checkout = False
        self.test_order = False
        self.preparing = False
        self.keep_prepared_pickup = False
        self.prepared_order = None
        self.retry_enabled = False
        self.order_details = None
        self.purchase_selection = None
        self.fulfillment_mode = 'HOME'
        self.pickup_selected = None
        self.pickup_override = None
        self.pickup_policy_override = None
        self.pickup_slot_selected = None
        self.pickup_contact_proof = None
        self.pickup_identity_proof = None
        self.pickup_contact_accepted = False
        self.pickup_contact_review_verified = False
        self._parallel_child = False
        self.settings_dir = (self.root.parent.parent / 'settings'
                             if self.root.parent.name == 'accounts' else
                             self.root if self.root.name == 'settings' else self.root / 'settings')
        self.task_created_at = None
        try:
            record = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
            triggered = record.get('schedule', {}).get('triggered_at_ms') if isinstance(record.get('schedule'), dict) else None
            created = triggered / 1000 if type(triggered) in (int, float) else record.get('created_at')
            if type(created) in (int, float) and 0 < created <= time.time():
                self.task_created_at = created
        except (OSError, ValueError, TypeError):
            pass
        self.auth = self.root / 'auth.json'
        self.checkout = self.root / 'checkout.json'
        duo_killer.ROOT, duo_killer.AUTH_PATH, duo_killer.LOG_PATH = self.root, self.auth, self.task_dir / 'requests.log'
        duo_killer.PAYMENT_ROOT = self.task_dir
        alipay_qr.ROOT = self.task_dir
        interaction.handler = self

    def enable_logging(self):
        from protocol import LogStream
        from platform_support import private_fd
        import os
        with os.fdopen(os.open(duo_killer.LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as file:
            private_fd(file.fileno())
        logger = duo_killer.log
        logger.disabled = False
        logger.propagate = False
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s %(message)s', '%H:%M:%S')
        for handler in (logging.FileHandler(duo_killer.LOG_PATH, encoding='utf-8'), logging.StreamHandler(LogStream(self.channel))):
            handler.setFormatter(formatter)
            logger.addHandler(handler)

    def ensure_auth_file(self):
        if self.auth.exists():
            data = json.loads(self.auth.read_text(encoding='utf-8'))
            if data.get('account', '').casefold() != self.account.casefold():
                raise ValueError('本地会话的账号与所选账号不同，请重新导入正确的会话。')
            if 'password' in data:
                data.pop('password')
                duo_killer.write_private_json(self.auth, data)
            return False
        duo_killer.write_private_json(self.auth, empty_auth(self.account))
        return True

    def browser_password(self, *, allow_prompt=False):
        """Provide only this task's password to the official browser UI.

        This does not enable legacy HTTP/SRP reauthentication during idle work.
        Linux/Docker still has no plaintext credential-store fallback.
        """
        from browser_login import BrowserSessionError
        self.channel.checkpoint()
        if self._parallel_child:
            raise BrowserSessionError('BROWSER_LOGIN_REQUIRES_ATTENTION',
                '并发子任务需要重新认证，请先完成当前账号的官网登录。',
                status='requires_login', requires_login=True)
        self.authentication_requested = True
        if not self.password:
            try:
                stored = self.vault.get(self.channel.account_id)
                self.password = stored if isinstance(stored, str) and stored else None
            except Exception:
                self.password = None
        if (not self.password and allow_prompt and self.headless_browser is not True
                and self.idle_maintenance is not True):
            value = self.channel.prompt('password', '输入 Apple 账号密码，将自动填入当前官网登录页面',
                                        account=self.account)
            if not isinstance(value, dict) or not isinstance(value.get('password'), str) or not value['password']:
                raise ValueError('请输入密码。')
            self.password = value['password']
            self.remember = value.get('remember') is True
        return self.password

    def credentials(self, path):
        self.purchase_auth_interaction = True
        if self._parallel_child:
            raise ValueError('并发会话需要重新登录，请先预热账号后重新发起；本次未自动登录。')
        self.authentication_requested = True
        if not self.password:
            try:
                self.password = self.vault.get(self.channel.account_id)
            except Exception:
                self.stage('系统凭据保险库不可用，请输入本次登录密码')
        if self.idle_maintenance is True:
            from browser_login import BrowserSessionError
            raise BrowserSessionError('HEADLESS_LOGIN_REQUIRES_ATTENTION',
                '空闲预热需要重新认证，请使用“官网登录恢复（保留账号）”完成；后台未提交密码或验证码。',
                status='requires_login', requires_login=True)
        if not self.password:
            if getattr(self, 'headless_browser', False) is True:
                from browser_login import BrowserSessionError
                raise BrowserSessionError('HEADLESS_LOGIN_REQUIRES_ATTENTION',
                    '后台预热缺少已保存密码，请在账号工作台完成登录后继续。',
                    status='requires_login', requires_login=True)
            value = self.channel.prompt('password', '输入 Apple 账号密码', account=self.account)
            if not isinstance(value, dict) or not isinstance(value.get('password'), str) or not value['password']:
                raise ValueError('请输入密码。')
            self.password = value['password']
            self.remember = value.get('remember') is True
        return self.account, self.password

    def text(self, label, default=''):
        kind = 'code' if '验证码' in label else 'text'
        if kind == 'code':
            self.purchase_auth_interaction = True
        if getattr(self, 'headless_browser', False) is True:
            from browser_login import BrowserSessionError
            raise BrowserSessionError('HEADLESS_LOGIN_REQUIRES_ATTENTION',
                '后台预热需要人工验证，请在账号工作台完成官网登录后继续。',
                status='requires_login', requires_login=True)
        if self._parallel_child and kind == 'code':
            raise ValueError('并发会话需要验证，请先在账号工作台完成登录，再重新发起。')
        if (self.automatic_checkout or self.prefill_checkout) and kind != 'code':
            raise ValueError('自动结账缺少必要信息：' + label + '。请先完善本地地址和联系方式。')
        label = label.replace('👉 ', '')
        fields = {}
        if kind == 'code':
            # Expose only the transitions supported by this legacy prompt.
            for hint, value, title in (
                ('（输入 s 改用短信）', 's', '改用短信验证码'),
                ('（输入 d 改用设备）', 'd', '改用设备验证码'),
            ):
                if hint in label:
                    label = label.replace(hint, '')
                    fields['actions'] = [{'value': value, 'label': title}]
        value = self.channel.prompt(kind, label, default=default, **fields)
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError('输入格式不正确。')
        return value

    def choose(self, label, options, default=None):
        if self.automatic_checkout or self.prefill_checkout:
            values = [key for key, _ in options]
            if label == '收货方式':
                selected = self.fulfillment_mode
            elif label == '配送服务':
                selected = default if default in values else values[0] if values else None
            else:
                raise ValueError(label + '无法自动确定，请先完善本地配置；未提交订单。')
            if selected not in values:
                raise ValueError(label + '没有可用的自动选项；未提交订单。')
            self.stage('自动选择：' + label)
            return selected
        return self.channel.prompt('select', label, options=[{'value': k, 'label': v} for k, v in options], default=default)

    def confirm(self, details):
        self.order_details = details
        if self.automatic_checkout:
            from decimal import Decimal
            items = details.get('items', [])
            item_sku = duo_killer.normalize_sku(items[0].get('sku', '')) if len(items) == 1 else ''
            if (len(items) != 1 or type(items[0].get('quantity')) is not int or items[0]['quantity'] < 1
                    or self.purchase_selection is not None
                    and (item_sku, items[0]['quantity']) != self.purchase_selection):
                raise ValueError(duo_killer.selection_mismatch_message(items, self.purchase_selection, '订单复核页'))
            match = re.fullmatch(r'(?:RMB|CNY|CN¥|¥|￥)?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?:元|RMB|CNY)?', str(details.get('total', '')))
            if not match:
                raise ValueError('无法确认人民币订单总额；未提交订单。')
            total = Decimal(match[1].replace(',', ''))
            if total <= Decimal('0'):
                raise ValueError('订单总额无效；未提交订单。')
            config = checkout_options.read_config(self.checkout)
            if self.fulfillment_mode == 'RETAIL':
                from pickup_options import validate_preset, store_name, allowed_store
                preset = validate_preset(config)['pickup']
                if details.get('address') or details.get('contains_delivery') is True:
                    raise ValueError('订单复核仍包含送货商品或地址，未确认全部商品自提；未提交订单。')
                if not details.get('store'):
                    raise ValueError('订单复核未返回门店，且缺少本次官网已确认门店与全部商品自提的完整依据；未提交订单。')
                actual_id = details.get('store_id') or (self.pickup_selected or {}).get('storeId')
                matches_store = (allowed_store(preset, details['store'], actual_id)
                                 if preset.get('selection_mode') == 'priority'
                                 else store_name(details['store']) == store_name(preset['store_name']))
                if not matches_store:
                    raise ValueError('官网复核自提门店与本地指定门店不一致；未提交订单。')
                if preset.get('store_id') and preset.get('selection_mode') != 'priority':
                    if actual_id != preset['store_id']:
                        raise ValueError('官网复核尚未确认指定门店编号；未提交订单。')
                if (self.pickup_selected and details.get('store_id')
                        and details['store_id'] != self.pickup_selected['storeId']):
                    raise ValueError('官网复核门店编号与已选门店不一致；未提交订单。')
                if (preset.get('selection_mode') == 'priority' or preset.get('contact_mode') == 'official') and not self.pickup_contact_review_verified:
                    raise ValueError('官网复核尚未确认本次取货联系人，未提交订单。')
            else:
                if details.get('store') or not details.get('address'):
                    raise ValueError('订单复核未确认送货地址；未提交订单。')
                street = config.get('address', {}).get('street')
                if checkout_options.local_address_ready(config) and ''.join(str(street).split()) not in ''.join(str(details['address']).split()):
                    raise ValueError('官网复核地址与本地地址不一致；未提交订单。')
            self.stage('指定自提门店、商品、数量和官网金额已核对，自动创建订单' if self.fulfillment_mode == 'RETAIL' and not self.preparing
                       else '准备完成，已核对商品、数量、地址和金额；未创建订单' if self.preparing
                       else '抛光布、数量、地址和金额已核对，自动创建订单' if self.test_order
                       else '本次所选商品、数量、官网金额和地址已核对，自动创建订单')
            return True
        return self.channel.prompt('confirm', '确认商品与金额，创建订单', details=details, account=self.account)

    def submission_path(self):
        # Keep polishing-cloth test records separate from phone orders.
        return self.root / ('test_order_submission.json' if self.test_order else 'order_submission.json')

    def wait_prepared_pickup(self, result, *, timeout):
        self.stage('自提准备就绪，等待点击下单（五分钟有效）')
        self.channel.emit('progress_result', result=result)
        accepted = self.channel.prompt('confirm', '自提准备就绪，等待下单',
            details={**self.order_details, 'operation': 'prepared_pickup',
                     'expires_at': time.time() + timeout}, account=self.account, timeout=timeout)
        if accepted is not True:
            return False
        self.preparing = False
        # Measure the purchase from the user's trigger, excluding preparation.
        self.task_created_at = time.time()
        return True

    def payment_ready(self, result):
        if self.submitted and result.get('status') == 'pending_payment' and result.get('order_number'):
            confirmed = int(time.time() * 1000)
            result.setdefault('payment_confirmed_at_ms', confirmed)
            result.setdefault('payment_deadline_ms', result['payment_confirmed_at_ms'] + 30 * 60 * 1000)
            if self.task_created_at is not None:
                # Desktop timestamps have one-second resolution. Includes queue
                # and worker startup, stops before payment QR requests.
                elapsed = max(0, int(time.time()) - int(self.task_created_at))
                result['order_confirmed_elapsed_seconds'] = elapsed
                duo_killer.log.info('订单确认总耗时：约 %s 秒（含排队和启动，不含二维码）。', elapsed)
            duo_killer.write_private_json(self.submission_path(), {
                'state': 'pending_payment', 'task_id': self.channel.task_id,
                'order_number': result['order_number'],
            })
            self.channel.emit('progress_result', result={**result, 'qr_loading': True})
            self.stage('订单已创建，正在加载支付宝付款二维码')

    def stage(self, stage, **fields):
        self.authentication_failed = self.authentication_failed or fields.get('authentication_failed', False)
        if 'browser_pid' in fields:
            self.channel.emit('stage', message=stage, **fields)
            self.channel.checkpoint()
            return
        self.channel.checkpoint()
        if fields.get('order_submitted'):
            if self.submission_path().exists():
                previous = json.loads(self.submission_path().read_text(encoding='utf-8'))
                if previous.get('state') in ('pending_payment', 'submission_unknown'):
                    history_path = self.root / ('test_order_history.json' if self.test_order else 'order_history.json')
                    history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else []
                    history.append(previous)
                    duo_killer.write_private_json(history_path, history)
            # Record an uncertain response, but do not use past order state to
            # block another explicitly requested checkout. Apple decides whether
            # that new checkout is accepted; this task still sends placeOrder once.
            duo_killer.write_private_json(self.submission_path(), {
                'state': 'submission_unknown', 'task_id': self.channel.task_id,
                'details': self.order_details,
            })
        self.submitted = self.submitted or fields.get('order_submitted', False)
        self.channel.emit('stage', message=stage, **fields)

    def persist_password(self):
        if self.remember and self.password:
            try:
                self.vault.save(self.channel.account_id, self.password)
            except Exception:
                self.stage('操作成功，但系统未保存密码；下次登录需重新输入')

    def cleanup_credentials(self, error=None):
        # A rejected authentication is not permission to delete an existing
        # account, its vault password, or its trust/address/order configuration.
        if self.auth.exists():
            data = json.loads(self.auth.read_text(encoding='utf-8'))
            data.pop('password', None)
            # Legacy failure cleanup may remove the account too. Keep the registry identity.
            data['account'] = self.account
            duo_killer.write_private_json(self.auth, data)
        self.password = None

    def should_remove_account(self, error):
        # Removal requires an explicit user action; task errors must retain it.
        return False

    def warm_start_context(self):
        """Refresh browser validation before generating a fresh sign-in entry."""
        from apple_login import AppleStoreLogin
        with requests.Session() as session:
            duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
            def sent_names():
                request = session.prepare_request(requests.Request('GET', duo_killer.BASE_URL + '/shop/bag'))
                return {part.strip().partition('=')[0] for part in request.headers.get('Cookie', '').split(';')}
            client = AppleStoreLogin(auth_path=self.auth, session=session)
            if (client.browser_profile / '.authx-541').exists():
                from browser_login import BrowserSessionError
                raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                    '浏览器校验需要处理；账号和浏览器配置已保留，未自动重置。',
                    status='blocked', requires_login=True, http_status=541)
            if 'shld_bt_ck' in sent_names():
                return False
            self.stage('预热浏览器校验，保留账号与本机设备信任，不加购、不下单')
            client.initialize_browser_context()
            if 'shld_bt_ck' not in sent_names():
                raise ValueError('预热未取得有效浏览器校验，未加购、未下单；账号未重置。')
            duo_killer.save_auth(session)
            return True

    def check_account(self, *, enrolling=False):
        if enrolling:
            self.stage('正在登录，完成验证后才会添加账号')
            duo_killer.login()
        with requests.Session() as session:
            duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
            save_session = True
            retried = False
            try:
                self.stage('正在查询当前登录人')
                try:
                    # Keep -me's account-page reauthentication and current SSI/session.
                    result = duo_killer.account_info(session)
                except (ValueError, requests.RequestException):
                    # Wrong passwords, rejected OTPs and a failed reauthentication must
                    # reach the user instead of starting another login loop.
                    if enrolling or self.authentication_requested:
                        raise
                    result = None
                if (not result or result.get('success') is not True
                        or result.get('logged_in') is not True or not result.get('apple_id')):
                    if enrolling or self.authentication_requested:
                        raise ValueError('登录后仍无法确认当前账号，请检查登录结果后重试。')
                    self.stage('未确认当前登录人，执行一次完整登录')
                    save_session = False
                    retried = True
                    duo_killer.login()
                    session.cookies.clear()
                    duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
                    save_session = True
                    result = duo_killer.account_info(session)
                if (result.get('success') is not True or result.get('logged_in') is not True
                        or not result.get('apple_id')):
                    raise ValueError('无法确认当前登录人，未启用此账号。')
                return result
            except ValueError:
                if not enrolling and (retried or self.authentication_requested):
                    self.authentication_failed = True
                raise
            finally:
                if save_session:
                    duo_killer.save_auth(session)

    def warm_login(self):
        """Refresh idle browser sessions; retain explicit manual preheat behavior."""
        self.stage('预热登录：检查账号会话和设备信任，不加购、不下单')
        if self.action == 'session_maintain':
            from apple_login import browser_profile_path
            from browser_login import BrowserSessionError, login_in_browser
            if (browser_profile_path(self.auth) / '.authx-541').exists():
                raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                    '浏览器校验需要处理；账号和浏览器配置已保留，未自动重置。',
                    status='blocked', requires_login=True, http_status=541)
            result = login_in_browser(self, timeout=12, maintenance=True)
            if (result.get('success') is not True or result.get('logged_in') is not True
                    or result.get('status') not in (None, 'healthy')):
                return result
            refreshed = True
        else:
            refreshed = self.warm_start_context()
            result = self.check_account(enrolling=False)
        empty_bag_ready = duo_killer.warm_empty_bag()
        result.update(warm_login=True, browser_context_refreshed=refreshed,
            empty_bag_ready=empty_bag_ready,
            message=('登录预热完成，空购物袋已检查；两分钟内首次加购可省去重复检查。'
                     if empty_bag_ready else '登录预热完成，购物袋非空或暂不满足快速加购条件。')
                    + '未加购、未下单；结算仍可能要求认证。')
        return result

    def warm_pickup_prepare(self, sku, count):
        """Use the same saved pickup policy and hard prepare-only boundary as manual preparation."""
        if not isinstance(sku, str) or not re.fullmatch(r'[A-Za-z0-9]{1,30}(?:/A)?', sku):
            raise ValueError('请先保存当前账号的商品SKU，再进行自提预热。')
        if type(count) is not int or not 1 <= count <= 999:
            raise ValueError('请先保存当前账号的购买数量，再进行自提预热。')
        from apple_login import browser_profile_path
        if (browser_profile_path(self.auth) / '.authx-541').exists():
            from browser_login import BrowserSessionError
            raise BrowserSessionError('APPLE_SESSION_BLOCKED',
                '浏览器校验需要处理；账号和浏览器配置已保留，未自动重置。',
                status='blocked', requires_login=True, http_status=541)
        if self.action == 'session_maintain':
            from browser_login import BrowserSessionError, login_in_browser
            checked = login_in_browser(self, timeout=12, maintenance=True)
            if (checked.get('success') is not True or checked.get('logged_in') is not True
                    or checked.get('status') not in (None, 'healthy')):
                raise BrowserSessionError(checked.get('error_code', 'SESSION_CHECK_INCOMPLETE'),
                    checked.get('message', '官网会话尚未验证，未继续自提准备。'),
                    status=checked.get('status', 'failed'), requires_login=checked.get('requires_login') is True,
                    http_status=checked.get('http_status'),
                    browser_started=checked.get('browser_started') is True,
                    browser_connected=checked.get('browser_connected') is True)
        self.stage('空闲自提预热：使用当前账号商品和自提偏好，不创建订单')
        try:
            return self.run('purchase', {'sku': sku, 'quantity': count, 'fulfillment': 'RETAIL',
                                        'prepare_only': True, 'keep_ready': False, 'concurrency': 1,
                                        'retry_count': 0})
        finally:
            # Engine ownership and purchase priority remain session-maintenance scoped.
            self.action = 'session_maintain'

    def run(self, action, params):
        self._parallel_child = params.get('_parallel_child') is True
        self.pickup_policy_override = None
        self.pickup_slot_selected = None
        self.pickup_contact_proof = None
        self.pickup_identity_proof = None
        self.pickup_contact_accepted = False
        self.pickup_contact_review_verified = False
        if action == 'purchase' and params.get('queue_stream') is True:
            if self._parallel_child:
                raise ValueError('并发子任务不能再次创建执行队列。')
            self.action = 'purchase'
            self.retry_enabled = True
            self.ensure_auth_file()
            from queued_checkout import run_queued_purchase
            return run_queued_purchase(self, params)
        if action == 'purchase':
            from purchase_retry import retry_count
            retry_count(params)  # Reject invalid policies before auth or mutations.
            concurrency = params.get('concurrency', 1)
            if type(concurrency) is not int or not 1 <= concurrency <= 10:
                raise ValueError('自提并发数必须是 1 到 10 的整数。')
        if action in ('inventory_settings_read', 'inventory_settings_save', 'store_search',
                      'inventory_check', 'inventory_monitor'):
            from inventory import run_inventory
            return run_inventory(action, params, self.channel, self.settings_dir)
        # Only the engine may defer a purchase. An older service must never
        # silently pass a scheduling request through as an immediate purchase.
        if 'scheduled_at' in params:
            raise ValueError('当前本地服务尚未登记此定时请求，请重启启动程序后重新设定；未开始购买。')
        self.action = action
        self.fulfillment_mode = params.get('fulfillment', 'HOME') if action in ('purchase', 'checkout') else 'HOME'
        if self.fulfillment_mode not in ('HOME', 'RETAIL'):
            raise ValueError('收货方式无效，未开始购买。')
        self.pickup_selected = None
        self.pickup_override = None
        if action in ('purchase', 'checkout') and self.fulfillment_mode == 'RETAIL' and 'pickup_policy' in params:
            from pickup_options import validate_policy
            policy = params['pickup_policy']
            if not isinstance(policy, dict) or 'contact' in policy:
                raise ValueError('本次自提偏好不接受联系人数据。')
            # Validate the supplied field allowlist separately, then merge with
            # saved pickup preferences. Contact always remains account-local.
            allowed = {'selection_mode','stores','time_slot_rule','contact_mode','search','store_id','store_name','id_last4'}
            if set(policy) - allowed:
                raise ValueError('本次自提偏好包含不支持的字段。')
            saved = checkout_options.read_config(self.checkout).get('pickup', {})
            self.pickup_policy_override = validate_policy({**saved, **policy})
        if action in ('purchase', 'checkout') and self.fulfillment_mode == 'RETAIL' and any(
                key in params for key in ('pickup_store_id', 'pickup_store_name', 'pickup_location')) and checkout_options.read_config(self.checkout).get('pickup', {}).get('selection_mode') != 'priority':
            store_id, name = params.get('pickup_store_id'), params.get('pickup_store_name')
            if (not isinstance(store_id, str) or not re.fullmatch(r'R\d{1,8}', store_id)
                    or not isinstance(name, str) or not name.strip() or len(name) > 200):
                raise ValueError('监控触发的自提门店编号或名称无效；未开始购买。')
            saved = checkout_options.read_config(self.checkout).get('pickup', {})
            location = params.get('pickup_location') or saved.get('search')
            if not isinstance(location, str) or not location.strip() or len(location) > 200:
                raise ValueError('请为监控门店配置城市或邮编；未开始购买。')
            self.pickup_override = {'store_id': store_id, 'store_name': name.strip(), 'search': location.strip()}
            # A different monitored store must never inherit a cached preparation.
            (self.root / 'prepared_checkout.json').unlink(missing_ok=True)
        self.preparing = params.get('prepare_only') is True
        self.keep_prepared_pickup = params.get('keep_ready') is True
        self.prepared_order = None
        if self.keep_prepared_pickup and not (action == 'purchase' and self.preparing
                and self.fulfillment_mode == 'RETAIL'):
            raise ValueError('保留准备会话仅适用于自提前准备；未开始购买。')
        self.purchase_selection = ((duo_killer.normalize_sku(params.get('sku', 'MK2M4CH')), quantity(params))
                                   if action == 'purchase' else None)
        self.test_order = action == 'purchase' and duo_killer.normalize_sku(params.get('sku', '')) == 'MLH74FE/A'
        self.automatic_checkout = action in ('purchase', 'checkout')
        config = checkout_options.read_config(self.checkout)
        if action in ('purchase', 'checkout') and self.fulfillment_mode == 'RETAIL':
            from pickup_options import validate_preset
            validate_preset(config)  # Fail before authentication or adding to the bag.
        self.prefill_checkout = action in ('purchase', 'checkout') and self.fulfillment_mode != 'RETAIL' and checkout_options.local_address_ready(config)
        if self.prefill_checkout:
            validate_address(config)
            if not self.automatic_checkout:
                self.stage('自动使用已保存的送货地址、官网默认配送和支付宝；最终提交仍需确认')
        self.retry_enabled = action in ('purchase', 'checkout', 'qr', 'orders', 'bag', 'catalog')
        self.channel.checkpoint()
        if action == 'address_read':
            return {'success': True, 'config': checkout_options.read_config(self.checkout)}
        if action == 'address_save':
            config = checkout_options.read_config(self.checkout)
            if params.get('pickup_only') is True:
                from pickup_options import validate_preset
                validated = validate_preset(params, require_contact=False)
                config['pickup'] = validated['pickup']
                if 'contact' in params:
                    config['contact'] = {**config.get('contact', {}), **validated['contact']}
            else:
                config.update(validate_address(params))
            checkout_options.save_config(self.checkout, config)
            return {'success': True, 'config': config, 'message': '自提偏好已保存。' if params.get('pickup_only') is True else '快捷地址已保存，结账时可选择使用。'}
        if action == 'import_auth':
            data = validate_import(params.get('data'), self.account)
            duo_killer.write_private_json(self.auth, data)
            return {'success': True, 'message': '会话已导入，点击检查登录以确认有效性。'}
        if action == 'forget_password':
            self.vault.delete(self.channel.account_id)
            return {'success': True, 'message': '已删除系统保存的密码。'}
        if action == 'catalog':
            from shop_catalog import query_store, query_duo
            return query_duo(duo_killer.request_get) if params.get('mode') == 'duo' else query_store(duo_killer.request_get, phone_only=params.get('mode') == 'phone')
        self.ensure_auth_file()
        if action in ('enroll', 'me'):
            if action == 'enroll' and params.get('browser_login') is True:
                from browser_login import login_in_browser
                result = login_in_browser(self)
            else:
                result = self.check_account(enrolling=action == 'enroll')
        elif action == 'login':
            if params.get('browser_login') is True:
                from browser_login import login_in_browser
                result = login_in_browser(self)
                result['message'] = '官网登录与会话核对完成，账号和浏览器资料已保留。'
            elif params.get('warm_only') is True:
                result = self.warm_login()
            else:
                if params.get('renew_device_trust') is True:
                    self.stage('重新验证设备信任，保留账号、密码和地址；不加购、不下单')
                    result = duo_killer.login(sms=params.get('sms') is True, renew_device_trust=True)
                else:
                    result = duo_killer.login(sms=params.get('sms') is True)
        elif action == 'session_maintain':
            from session_maintenance import maintain_session
            previous_idle_maintenance = self.idle_maintenance
            self.idle_maintenance = True
            previous_headless = self.headless_browser
            self.headless_browser = True
            try:
                result = maintain_session(self, warm_only=params.get('warm_only') is True,
                                          preheat_flow=params.get('preheat_flow', 'login'),
                                          sku=params.get('sku'), count=params.get('quantity'))
            finally:
                self.idle_maintenance = previous_idle_maintenance
                # Nested prepare-only purchases inherit this scope. Manual actions
                # and any later use of this service retain their original policy.
                self.headless_browser = previous_headless
        elif action == 'purchase':
            if params.get('prepare_only') is True:
                if not self.automatic_checkout:
                    raise ValueError('提前准备需要已启用自动结算配置。')
                result = duo_killer.purchase_sku(params.get('sku', 'MK2M4CH'), quantity=quantity(params), prepare_only=True)
            else:
                from purchase_retry import run_purchase
                def attempt():
                    if self.fulfillment_mode == 'RETAIL' and concurrency > 1:
                        from parallel_checkout import run_parallel_purchase
                        return run_parallel_purchase(self, params)
                    return duo_killer.purchase_sku(params.get('sku', 'MK2M4CH'), quantity=quantity(params))
                result = run_purchase(self, params, attempt)
        elif action == 'add':
            result = duo_killer.add_to_bag(params.get('sku', ''), quantity=quantity(params), set_total=True)
        elif action == 'checkout':
            result = duo_killer.pay_bag()
        elif action == 'clear_bag':
            from cart_clear import clear_bag
            with requests.Session() as session:
                duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
                try:
                    result = clear_bag(session, self.channel)
                finally:
                    duo_killer.save_auth(session)
            if result.get('removed_count') or result.get('status') == 'bag_cleared':
                for name in ('prepared_checkout.json', 'warm_empty_bag.json'):
                    (self.root / name).unlink(missing_ok=True)
        elif action == 'cancel_order':
            from order_cancel import cancel_order
            number = params.get('order_number')
            if not isinstance(number, str) or not re.fullmatch(r'W\d{6,20}', number):
                raise ValueError('订单编号无效。')
            history_path = self.root / 'order_cancellations.json'
            history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
            def before_submit():
                history[number] = 'cancellation_unknown'
                duo_killer.write_private_json(history_path, history)
                self.stage('正在取消订单；结果不明时不会重复发送取消')
            with requests.Session() as session:
                duo_killer.load_auth(session, json.loads(self.auth.read_text(encoding='utf-8')))
                try:
                    result = cancel_order(session, number, self.channel, before_submit,
                                          allow_submit=history.get(number) != 'cancellation_unknown',
                                          contact=checkout_options.read_config(self.checkout).get('contact', {}))
                finally:
                    duo_killer.save_auth(session)
            if result.get('status') in ('order_cancelled', 'order_expired'):
                history[number] = result['status']
                duo_killer.write_private_json(history_path, history)
                for name in ('order_submission.json', 'test_order_submission.json'):
                    path = self.root / name
                    if path.exists():
                        marker = json.loads(path.read_text(encoding='utf-8'))
                        if marker.get('order_number') == number:
                            marker['state'] = 'expired' if result['status'] == 'order_expired' else 'cancelled'
                            duo_killer.write_private_json(path, marker)
        elif action == 'qr':
            source_id = str(params.get('source_task', ''))
            import uuid
            if str(uuid.UUID(source_id)) != source_id:
                raise ValueError('付款任务编号无效。')
            data = json.loads((self.root / 'tasks' / source_id / 'payment.json').read_text(encoding='utf-8'))
            duo_killer.validate_payment_result(data, data.get('order_total'))
            duo_killer.payment_qr(data)
            duo_killer.write_private_json(self.task_dir / 'payment.json', data)
            result = data
        elif action in ('orders', 'bag', 'region'):
            if action == 'region':
                return {'success': True, 'region': checkout_options.current_region(self.auth)}
            with requests.Session() as session:
                duo_killer.authenticate(session, strong=True)
                try:
                    if action == 'orders':
                        result = duo_killer.order_list(session)
                        from order_access import supplement_known_orders
                        result = supplement_known_orders(session, result, self.root, verify_existing=True)
                    else:
                        flow = duo_killer.Checkout(session)
                        flow.page(duo_killer.BASE_URL + '/shop/bag', '读取购物袋')
                        result = {'success': True, 'items': [
                            {'name': duo_killer.order_text(duo_killer.field(item, 'd.name') or duo_killer.field(item, 'd.productName')),
                             'sku': duo_killer.field(item, 'd.sku'), 'quantity': duo_killer.field(item, 'itemQuantity.d.quantity'),
                             'subtotal': duo_killer.order_price(duo_killer.field(item, 'd.totalPrice'))}
                            for item in duo_killer.model_item_nodes(duo_killer.field(flow.model, 'shoppingCart.items', {}))],
                            'total': duo_killer.order_price(duo_killer.field(flow.model, 'shoppingCart.summary.d.total'))}
                finally:
                    duo_killer.save_auth(session)
        else:
            raise ValueError('不支持的操作。')
        if self.submitted and result.get('status') == 'pending_payment':
            duo_killer.write_private_json(self.submission_path(), {
                'state': 'pending_payment', 'task_id': self.channel.task_id,
                'order_number': result.get('order_number'),
            })
        self.persist_password()
        if action in ('enroll', 'login', 'me') and result.get('logged_in') is True:
            from apple_login import stored_device_trust
            result['device_trust_saved'] = bool(stored_device_trust(
                json.loads(self.auth.read_text(encoding='utf-8')), self.account))
        if result.get('qr_image'):
            path = Path(result['qr_image']).resolve()
            if path.parent != self.task_dir.resolve() or path.stat().st_size > 2_000_000:
                raise ValueError('二维码文件不在当前任务目录。')
            result['qr_data'] = 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()
        return result


def quantity(params):
    value = params.get('quantity', 1)
    if type(value) is not int or value < 1:
        raise ValueError('数量须为正整数；实际限购数量以官网为准。')
    return value


def validate_address(data):
    expected = {'contact': ['lastName', 'firstName', 'emailAddress', 'fullDaytimePhone'],
                'delivery': ['state', 'city', 'district'], 'address': ['street']}
    result = {}
    for group, keys in expected.items():
        values = data.get(group, {})
        result[group] = {}
        for key in keys:
            value = values.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError('请完整填写联系人、省市区和详细地址。')
            result[group][key] = value.strip()
    if not re.fullmatch(r'\d{11,12}', result['contact']['fullDaytimePhone']):
        raise ValueError('请输入正确的手机号码。')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['contact']['emailAddress']):
        raise ValueError('请输入正确的通知邮箱。')
    return result


def validate_import(data, account):
    if not isinstance(data, dict) or str(data.get('account', '')).casefold() != account.casefold():
        raise ValueError('导入文件的 account 必须与当前账号一致。')
    clean = empty_auth(account)
    groups = data.get('cookies', {})
    for group in clean['cookies']:
        rows = groups.get(group, [])
        if not isinstance(rows, list):
            raise ValueError('Cookie 分组格式不正确。')
        for cookie in rows:
            if not isinstance(cookie, dict) or not all(isinstance(cookie.get(k), str) for k in ('name', 'value', 'domain')):
                raise ValueError('Cookie 格式不正确。')
            domain = cookie['domain'].lstrip('.')
            if domain == 'apple.com.cn' or domain.endswith('.apple.com.cn'):
                clean['cookies'][group].append(cookie)
    clean['headers'] = {k: v for k, v in data.get('headers', {}).items()
                        if k.lower() in ('user-agent', 'accept-language') and isinstance(v, str)}
    from apple_login import stored_device_trust, TRUST_COOKIE_NAME
    clean['trust_account'] = account
    clean['trust_cookies'] = stored_device_trust(data, account)
    # Keep trust in one authoritative list, including when the source revoked it.
    for group, rows in clean['cookies'].items():
        clean['cookies'][group] = [c for c in rows if not TRUST_COOKIE_NAME.fullmatch(c['name'])]
    return clean
