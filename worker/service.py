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


def automatic_policies(config):
    limits = {'MK2M4CH/A': '15999.00', 'MK2P4CH/A': '17999.00', 'MJT84CH/A': '9999.00'}
    entries = config.get('products', [config])
    if not isinstance(entries, list) or not entries:
        raise ValueError('自动锁单配置缺少商品范围；未开始购买。')
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('自动锁单商品配置无效；未开始购买。')
        sku = duo_killer.normalize_sku(entry.get('sku', ''))
        if (sku not in limits or sku in result or type(entry.get('quantity')) is not int
                or entry['quantity'] != 1 or entry.get('max_total') != limits[sku]):
            raise ValueError('自动锁单配置与本次授权不一致，请先确认配置。')
        result[sku] = entry
    return result


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
        self.action = None
        self.submitted = False
        self.automatic_checkout = False
        self.prefill_checkout = False
        self.test_order = False
        self.preparing = False
        self.retry_enabled = False
        self.order_details = None
        self.task_created_at = None
        try:
            created = json.loads((task_dir / 'task.json').read_text(encoding='utf-8')).get('created_at')
            if type(created) in (int, float) and 0 < created <= time.time():
                self.task_created_at = created
        except (OSError, ValueError, TypeError):
            pass
        policy_path = self.root / 'automation.json'
        self.order_policy = json.loads(policy_path.read_text(encoding='utf-8')) if policy_path.exists() else {}
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

    def credentials(self, path):
        self.authentication_requested = True
        if not self.password:
            try:
                self.password = self.vault.get(self.channel.account_id)
            except Exception:
                self.stage('系统凭据保险库不可用，请输入本次登录密码')
        if not self.password:
            value = self.channel.prompt('password', '输入 Apple 账号密码', account=self.account)
            if not isinstance(value, dict) or not isinstance(value.get('password'), str) or not value['password']:
                raise ValueError('请输入密码。')
            self.password = value['password']
            self.remember = value.get('remember') is True
        return self.account, self.password

    def text(self, label, default=''):
        kind = 'code' if '验证码' in label else 'text'
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
                selected = 'HOME'
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
            policies = automatic_policies(self.order_policy)
            legacy_cloth = self.test_order and 'products' not in self.order_policy
            item_sku = duo_killer.normalize_sku(items[0].get('sku', '')) if len(items) == 1 else ''
            policy = self.order_policy if legacy_cloth else policies.get(item_sku)
            expected_sku = 'MLH74FE' if legacy_cloth else item_sku
            # Authorize only the exact item/quantity approved by the user.
            if (policy is None or len(items) != 1 or item_sku != duo_killer.normalize_sku(expected_sku)
                    or type(items[0].get('quantity')) is not int or items[0]['quantity'] != policy['quantity']):
                raise ValueError('购物袋含额外商品或型号/数量不符合自动锁单预设；未提交订单。')
            match = re.fullmatch(r'(?:RMB|CNY|CN¥|¥|￥)?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?:元|RMB|CNY)?', str(details.get('total', '')))
            if not match:
                raise ValueError('无法确认人民币订单总额；未提交订单。')
            total = Decimal(match[1].replace(',', ''))
            if total <= Decimal('0'):
                raise ValueError('订单总额无效；未提交订单。')
            if not legacy_cloth and total > Decimal(policy['max_total']):
                raise ValueError('订单总额超过自动锁单上限 ' + policy['max_total'] + ' 元；未提交订单。')
            if details.get('store') or not details.get('address'):
                raise ValueError('订单复核未确认送货地址；未提交订单。')
            config = checkout_options.read_config(self.checkout)
            street = config.get('address', {}).get('street')
            if checkout_options.local_address_ready(config) and ''.join(str(street).split()) not in ''.join(str(details['address']).split()):
                raise ValueError('官网复核地址与本地地址不一致；未提交订单。')
            self.stage('准备完成，已核对商品、数量、地址和金额；未创建订单' if self.preparing
                       else '抛光布、数量、地址和金额已核对，自动创建订单' if self.test_order
                       else '商品、数量、地址和金额上限已核对，自动创建订单')
            return True
        return self.channel.prompt('confirm', '确认商品与金额，创建订单', details=details, account=self.account)

    def submission_path(self):
        # Keep polishing-cloth test records separate from phone orders.
        return self.root / ('test_order_submission.json' if self.test_order else 'order_submission.json')

    def payment_ready(self, result):
        if self.submitted and result.get('status') == 'pending_payment' and result.get('order_number'):
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
            if 'shld_bt_ck' in sent_names():
                return False
            client = AppleStoreLogin(auth_path=self.auth, session=session)
            if (client.browser_profile / '.authx-541').exists():
                raise ValueError('浏览器校验需要处理；账号和浏览器配置已保留，未自动重置。')
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

    def run(self, action, params):
        self.action = action
        self.preparing = params.get('prepare_only') is True
        self.test_order = action == 'purchase' and duo_killer.normalize_sku(params.get('sku', '')) == 'MLH74FE/A'
        self.automatic_checkout = action in ('purchase', 'checkout') and self.order_policy.get('enabled') is True
        config = checkout_options.read_config(self.checkout)
        self.prefill_checkout = action in ('purchase', 'checkout') and checkout_options.local_address_ready(config)
        if self.prefill_checkout:
            validate_address(config)
            if not self.automatic_checkout:
                self.stage('自动使用已保存的送货地址、官网默认配送和支付宝；最终提交仍需确认')
        self.retry_enabled = self.order_policy.get('enabled') is True and action in ('purchase', 'checkout', 'qr', 'orders', 'bag', 'catalog')
        if self.automatic_checkout:
            policies = automatic_policies(self.order_policy)
            if 'products' in self.order_policy and not self.prefill_checkout:
                raise ValueError('请先完整保存本账号的本地送货地址；未加购、未提交订单。')
            allowed = set(policies)
            if 'products' not in self.order_policy:
                allowed.add('MLH74FE/A')
            if action == 'purchase' and (duo_killer.normalize_sku(params.get('sku', '')) not in allowed or quantity(params) != 1):
                raise ValueError('商品或数量不在自动锁单授权范围内；未开始购买。')
        self.channel.checkpoint()
        if action == 'address_read':
            return {'success': True, 'config': checkout_options.read_config(self.checkout)}
        if action == 'address_save':
            config = checkout_options.read_config(self.checkout)
            config.update(validate_address(params))
            checkout_options.save_config(self.checkout, config)
            return {'success': True, 'config': config, 'message': '快捷地址已保存，结账时可选择使用。'}
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
            if params.get('warm_only') is True:
                self.stage('预热登录：检查账号会话和设备信任，不加购、不下单')
                refreshed = self.warm_start_context()
                result = self.check_account(enrolling=False)
                empty_bag_ready = duo_killer.warm_empty_bag()
                result.update(warm_login=True, browser_context_refreshed=refreshed,
                    empty_bag_ready=empty_bag_ready,
                    message=('登录预热完成，空购物袋已检查；两分钟内首次加购可省去重复检查。'
                             if empty_bag_ready else '登录预热完成，购物袋非空或暂不满足快速加购条件。')
                            + '未加购、未下单；结算仍可能要求认证。')
            else:
                if params.get('renew_device_trust') is True:
                    self.stage('重新验证设备信任，保留账号、密码和地址；不加购、不下单')
                    result = duo_killer.login(sms=params.get('sms') is True, renew_device_trust=True)
                else:
                    result = duo_killer.login(sms=params.get('sms') is True)
        elif action == 'purchase':
            if params.get('prepare_only') is True:
                if not self.automatic_checkout:
                    raise ValueError('提前准备需要已启用自动结算配置。')
                result = duo_killer.purchase_sku(params.get('sku', 'MK2M4CH'), quantity=quantity(params), prepare_only=True)
            else:
                result = duo_killer.purchase_sku(params.get('sku', 'MK2M4CH'), quantity=quantity(params))
        elif action == 'add':
            result = duo_killer.add_to_bag(params.get('sku', ''), quantity=quantity(params), set_total=True)
        elif action == 'checkout':
            result = duo_killer.pay_bag()
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
                                          allow_submit=history.get(number) != 'cancellation_unknown')
                finally:
                    duo_killer.save_auth(session)
            if result.get('status') == 'order_cancelled':
                history[number] = 'order_cancelled'
                duo_killer.write_private_json(history_path, history)
                for name in ('order_submission.json', 'test_order_submission.json'):
                    path = self.root / name
                    if path.exists():
                        marker = json.loads(path.read_text(encoding='utf-8'))
                        if marker.get('order_number') == number:
                            marker['state'] = 'cancelled'
                            duo_killer.write_private_json(path, marker)
        elif action == 'qr':
            source_id = str(params.get('source_task', ''))
            import uuid
            if str(uuid.UUID(source_id)) != source_id:
                raise ValueError('付款任务编号无效。')
            data = json.loads((self.root / 'tasks' / source_id / 'payment.json').read_text(encoding='utf-8'))
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
    if type(value) is not int or not 1 <= value <= 999:
        raise ValueError('数量须为 1–999 的整数；实际限购数量以官网为准。')
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
