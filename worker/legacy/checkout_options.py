"""根据结账页面实时模型选择配送、自提与收货地址。"""
import json
import os
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

import requests
import interaction


def value(node, path, default=None):
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def automatic_options():
    return (getattr(interaction.handler, 'automatic_checkout', False) is True
            or getattr(interaction.handler, 'prefill_checkout', False) is True)


def local_address_ready(config):
    return (bool(config.get('address', {}).get('street'))
            and all(config.get('delivery', {}).get(k) for k in ('state', 'city', 'district'))
            and all(config.get('contact', {}).get(k) for k in ('lastName', 'firstName', 'emailAddress', 'fullDaytimePhone')))


def read_config(path):
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError('checkout.json 必须是 JSON 对象。')
    handler = interaction.handler
    policy = getattr(handler, 'pickup_policy_override', None)
    if (isinstance(policy, dict) and getattr(handler, 'checkout', None)
            and Path(path).resolve() == Path(handler.checkout).resolve()):
        config = {**config, 'pickup': {**config.get('pickup', {}), **policy}}
    override = getattr(handler, 'pickup_override', None)
    if (isinstance(override, dict) and getattr(handler, 'checkout', None)
            and Path(path).resolve() == Path(handler.checkout).resolve()
            and config.get('pickup', {}).get('selection_mode', 'fixed') != 'priority'):
        # A monitor target belongs to this attempt only. The account's saved
        # store and the shared contact/address file remain intact.
        config = {**config, 'pickup': {**config.get('pickup', {}), **override}}
    if config.get('pickup', {}).get('selection_mode') == 'priority' and 'store_id' in config['pickup']:
        config = {**config, 'pickup': {key:val for key,val in config['pickup'].items() if key != 'store_id'}}
    return config


def config_fingerprint(path):
    """Hash effective purchase settings, independent of template/override source."""
    import hashlib
    from copy import deepcopy
    config = deepcopy(read_config(path))
    if isinstance(config.get('pickup'), dict):
        from pickup_options import validate_preset
        validated = validate_preset(config, require_contact=False)
        pickup = validated['pickup']
        pickup.setdefault('selection_mode', 'fixed')
        pickup.setdefault('contact_mode', 'local')
        pickup['time_slot_rule'] = {'mode':'earliest', 'fallback':'earliest'}
        if pickup['selection_mode'] == 'fixed':
            pickup.pop('stores', None)  # Backup choices do not authorize fixed pickup.
        config['pickup'] = pickup
        if isinstance(config.get('contact'), dict):
            config['contact'] = {**config['contact'], **validated['contact']}
    canonical = {'config':config, 'pickup_slot_policy':{'calendar_days':7,
                 'timezone':'Asia/Shanghai', 'mode':'earliest'}}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def save_config(path, config):
    fd, name = tempfile.mkstemp(prefix='.checkout-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(config, file, ensure_ascii=False, indent=2)
            file.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ask(label, default='', pattern=None):
    if interaction.handler is not None:
        while True:
            answer = interaction.handler.text(label, default=default).strip()
            if answer and (pattern is None or re.fullmatch(pattern, answer)):
                return answer
            interaction.report_stage('输入为空或格式不正确，请重新输入')
            default = ''
    while True:
        if default and sys.stdin.isatty() and sys.stdout.isatty():
            from prompt_toolkit import prompt
            answer = prompt('👉 ' + label + '：', default=default).strip()
        else:
            answer = input(f'👉 {label}' + (f' [{default}]' if default else '') + '：').strip() or default
        if answer and (pattern is None or re.fullmatch(pattern, answer)):
            return answer
        default = ''  # 用户清空预填内容后，不再偷偷恢复旧默认值。
        print('输入为空或格式不正确，请重新输入。')


def choose(label, options, default=None):
    if not options:
        raise ValueError(label + '：没有可选项，已停止。')
    if interaction.handler is not None:
        selected = interaction.handler.choose(label, options, default)
        if selected not in [key for key, _ in options]:
            raise ValueError('所选选项已失效，请重新开始当前操作。')
        return selected
    if sys.stdin.isatty() and sys.stdout.isatty():
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts import choice

        keys = KeyBindings()

        @keys.add('c-d')
        def cancel(event):
            event.app.exit(exception=EOFError())

        return choice('👉 ' + label + '（↑↓ 选择，回车确认，Ctrl+C/D 取消）',
                      options=options, default=default, key_bindings=keys)
    print('\n' + label)
    for index, (key, title) in enumerate(options, 1):
        print(f'{index}. {title}')
    default_index = next((i for i, (key, _) in enumerate(options, 1) if key == default), None)
    while True:
        answer = input('👉 请选择序号' + (f'（回车选 {default_index}）' if default_index else '') + '：').strip()
        if not answer and default_index:
            return options[default_index - 1][0]
        if answer.isdecimal() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print('请输入有效序号。')


def contact(config):
    saved = config.get('contact', {})
    result = {}
    for key, label, pattern in (
        ('lastName', '收件/取货人姓氏', None), ('firstName', '名字', None),
        ('emailAddress', '通知邮箱', r'[^\s@]+@[^\s@]+\.[^\s@]+'),
        ('fullDaytimePhone', '手机号码', r'\d{11,12}'),
    ):
        old = saved.get(key, '')
        result[key] = old if old and (pattern is None or re.fullmatch(pattern, old)) else ask(label, pattern=pattern)
    return result


def current_region(auth_path):
    """读取官网会话地区，不提交定位同意，不修改登录配置。"""
    from duo_killer import load_auth
    try:
        with requests.Session() as session:
            if auth_path.exists():
                load_auth(session, json.loads(auth_path.read_text(encoding='utf-8')))
            response = session.get('https://www.apple.com.cn/shop/address/location/update',
                                   timeout=(3, 5), allow_redirects=False)
            response.raise_for_status()
            data = response.json()
            content = value(data, 'body.content', {})
            if str(value(data, 'head.status')) != '200' or content.get('defaultLocation') is True:
                return {}
            address = content.get('address', {})
            return {key: address[key].strip() for key in ('state', 'city', 'district')
                    if isinstance(address.get(key), str) and address[key].strip()}
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def configure_address(path, auth_path=None):
    config = read_config(path)
    # 主动配置时每一项均可修改；结账时只补充缺项。
    old = config.get('contact', {})
    config['contact'] = {
        'lastName': ask('收件/取货人姓氏', old.get('lastName', '')),
        'firstName': ask('名字', old.get('firstName', '')),
        'emailAddress': ask('通知邮箱', old.get('emailAddress', ''), r'[^\s@]+@[^\s@]+\.[^\s@]+'),
        'fullDaytimePhone': ask('手机号码', old.get('fullDaytimePhone', ''), r'\d{11,12}'),
    }
    saved_region = config.get('delivery', {})
    remote_region = current_region(auth_path or path.parent / 'auth.json')
    if remote_region.get('state'):
        old_region = remote_region
        print('默认地区来自 Apple 当前会话：' + ' / '.join(remote_region.values()) + '。可直接删除、修改；回车确认。')
    else:
        old_region = saved_region
        print('官网未返回当前地区，使用本地记录。' if old_region.get('state')
              else '官网未返回当前地区，请手动填写；可能尚未登录或没有地区记录。')
    defaults = dict(old_region)
    region = {}
    for key, label in (('state', '省/直辖市'), ('city', '城市'), ('district', '区/县')):
        region[key] = ask(label, defaults.get(key, ''))
        if region[key].rstrip('省市') != defaults.get(key, '').rstrip('省市'):
            for child in ('city', 'district') if key == 'state' else ('district',) if key == 'city' else ():
                defaults.pop(child, None)
    config['delivery'] = region
    same_region = all(region[k].rstrip('省市') == saved_region.get(k, '').rstrip('省市') for k in region)
    street_default = config.get('address', {}).get('street', '') if same_region else ''
    config['address'] = {'street': ask('详细地址（街道、楼栋、门牌）', street_default)}
    save_config(path, config)
    print(f'快捷地址已保存到 {path}，结账时可选择使用。')
    return {'success': True, 'config_file': str(path)}


def region_data(flow, path, wanted=None, *, prefer_current=False):
    wanted = wanted or {}
    if prefer_current:
        current = flow.require(path)['d']
        if all(current.get(key) for key in ('state', 'city', 'district')):
            wanted = {key: current[key] for key in ('state', 'city', 'district')}
    selected = {}
    for key, label in (('state', '省份'), ('city', '城市'), ('district', '区县')):
        node = flow.require(path)
        data = node['d']
        options = [(o['value'], o.get('label', o['value'])) for o in (data.get(key + 'Options') or []) if o.get('value')]
        # Municipality pages can omit the city selector while returning the
        # fixed city explicitly. Selectstate responses may omit the submitted
        # state, leaving its initially empty value in the merged model. The
        # state was selected from official options; an explicit matching city
        # confirms this municipality even if state was not echoed back.
        if (key == 'city' and not options
                and selected.get('state') in ('北京', '上海', '天津', '重庆')
                and data.get('state') in (None, '', selected['state'])
                and data.get('city') == selected['state']):
            options = [(data['city'], data['city'])]
        desired = wanted.get(key) or data.get(key)
        matches = list(dict.fromkeys(k for k, title in options if desired in (k, title)))
        if not matches and desired:
            # 优先精确匹配；当前省市范围内唯一的区/县名称才兼容后缀差异。
            suffix = r'[区县]$' if key == 'district' else r'[省市]$'
            normalized = re.sub(suffix, '', str(desired).strip())
            matches = list(dict.fromkeys(k for k, title in options
                                        if normalized in (re.sub(suffix, '', str(k).strip()),
                                                          re.sub(suffix, '', str(title).strip()))))
        if len(matches) == 1 and matches[0] != desired:
            print(f'{label}自动匹配：{desired} → {matches[0]}')
        elif len(matches) != 1 and desired:
            print(f'配置中的{label}“{desired}”无法唯一匹配官网选项，请确认。')
        chosen = matches[0] if len(matches) == 1 else choose(label, options, data.get(key))
        selected[key] = chosen
        payload = {path + '.' + k: data.get(k, '') for k in ('state', 'city', 'district', 'provinceCityDistrict')}
        payload.update({path + '.' + k: v for k, v in selected.items()})
        payload[path + '.countryCode'] = 'CN'
        # Fresh checkout HTML may retain the selected province/city but omit
        # the dependent lists. Re-select that server-provided value once to
        # hydrate its children, even when the selection itself is unchanged.
        fixed_city = (chosen in ('北京', '上海', '天津', '重庆')
                      and data.get('city') == chosen)
        missing_children = (key == 'state' and not data.get('cityOptions')
                            and not (fixed_city and data.get('districtOptions'))
                            or key == 'city' and not data.get('districtOptions'))
        if (chosen != data.get(key) or missing_children) and 'Select' + key in node.get('a', {}):
            # 改省/市时不携带上一地区的下级选择。
            for child in ('city', 'district') if key == 'state' else ('district',) if key == 'city' else ():
                payload[path + '.' + child] = ''
            payload[path + '.provinceCityDistrict'] = ''
            flow.action(path, 'Select' + key, payload)
    return {**{path + '.' + key: val for key, val in selected.items()},
            path + '.countryCode': 'CN', path + '.provinceCityDistrict': ' '.join(selected.values())}


def shipping_options(flow, groups_path):
    payload = {}
    for group in flow.require(groups_path).get('c', []):
        options_path = groups_path + '.' + group + '.shipmentOptionsGroups'
        for option in flow.require(options_path).get('c', []):
            path = options_path + '.' + option + '.shippingOptions'
            node = flow.require(path)['d']
            options = [(o['value'], o.get('label') or o.get('title') or
                        ' / '.join(str(o[k]) for k in ('deliveryQuote', 'deliveryTimeSlot', 'shippingCost') if o.get(k)) or o['value'])
                       for o in node.get('options', []) if o.get('value') and not o.get('disabled')]
            selected = choose('配送服务', options, node.get('selectShippingOption')) if len(options) > 1 else options[0][0] if options else None
            if selected is None:
                raise ValueError('当前商品没有可用配送服务。')
            payload[path + '.selectShippingOption'] = selected
    return payload


def invoice_data(flow, path):
    invoice = value(flow.model, path + '.eFapiaoSelector')
    if not invoice:
        return {}  # 混合自提的发票已在取货联系人页面填写。
    if value(invoice, 'd.selectFapiao') != 'e_personal_fdf' and not (automatic_options() and invoice.get('ePersonalFapiao')):
        raise ValueError('当前仅支持个人电子发票，请先在官网选择。')
    return {path + '.eFapiaoSelector.selectFapiao': 'e_personal_fdf',
            path + '.eFapiaoSelector.ePersonalFapiao.invoiceHeader': value(invoice, 'ePersonalFapiao.d.invoiceHeader', '')}


def verification(flow, path):
    node = value(flow.model, path + '.verificationModule.d', {})
    if node.get('authRequired') is True and not node.get('verificationToken'):
        raise ValueError('当前联系人需要手机验证，请先在官网完成验证；未继续结账。')
    return {path + '.verificationModule.verificationToken': node.get('verificationToken', '')} if node else {}


def pickup_contact(flow, config, config_path):
    path = 'checkout.pickupContact'
    address = path + '.selfPickupContact.selfContact.address'
    flow.require(address)
    payload = verification(flow, address)
    from pickup_options import identity_payload, fulfillment_error, resolve_contact
    payload.update(identity_payload(flow, path + '.selfPickupContact.nationalIdSelf', config))
    if config.get('pickup', {}).get('contact_mode', 'local') == 'official' or config.get('pickup', {}).get('selection_mode') == 'priority':
        payload.update(resolve_contact(flow, address, config))
    else:
        details = contact(config)
        config['contact'] = details
        if not any(isinstance(getattr(interaction.handler, key, None), dict)
                   for key in ('pickup_policy_override', 'pickup_override')):
            save_config(config_path, config)
        payload.update({address + '.' + key: val for key, val in details.items()})
        if interaction.handler is not None:
            interaction.handler.pickup_contact_proof = {key: {'value': val} for key, val in details.items()}
    payload.update(invoice_data(flow, path))
    actions = flow.require(path).get('a', {})
    action = next((a for a in ('continueFromPickupContactToShipping', 'continueFromPickupContactToBilling') if a in actions), None)
    if not action:
        raise ValueError('当前取货联系人页面没有支持的继续操作。')
    if interaction.handler is not None:
        interaction.handler.pickup_contact_accepted = False
        flow._pending_pickup_contact_submission = {
            'handler': interaction.handler, 'source_page': value(flow.model, 'checkout.d.page'), 'action': action,
            'store': deepcopy(getattr(interaction.handler, 'pickup_selected', None)),
            'proof': deepcopy(getattr(interaction.handler, 'pickup_contact_proof', None)),
            'payload': {key: val for key, val in payload.items() if key.startswith(address + '.')}}
    flow.action(path, action, payload)
    expected = 'Shipping' if action.endswith('Shipping') else 'Billing'
    if value(flow.model, 'checkout.d.page') != expected:
        error = fulfillment_error(value(flow.model, path, {}))
        if not error:
            # The POST can finish while its partial response still shows the old page.
            flow.page(flow.apple_url('/shop/checkout'), '核对取货联系人是否已完成', fresh=True)
            error = fulfillment_error(value(flow.model, path, {}))
        if value(flow.model, 'checkout.d.page') != expected:
            raise ValueError('取货联系人未通过服务端校验：' + (error or '官网仍停留在联系人步骤，请核对页面提示')
                             + '；未继续结账，不重复发送联系人表单。')
    flow.require('checkout.shipping' if action.endswith('Shipping') else 'checkout.billing')
    confirm_pickup_contact_submission(flow)


def confirm_pickup_contact_submission(flow, *, fresh=False):
    """Finalize only this flow's actual form after an official step advance.

    Recovery calls with fresh=True after its GET; no POST is performed here.
    Private pending fields never become UI state or persistent contact proof.
    """
    handler = interaction.handler
    pending = getattr(flow, '_pending_pickup_contact_submission', None)
    if (handler is None or not isinstance(pending, dict) or pending.get('handler') is not handler
            or pending.get('source_page') != 'PickupContact'
            or pending.get('store') != getattr(handler, 'pickup_selected', None)):
        return False
    page = value(flow.model, 'checkout.d.page')
    allowed = (('Shipping', 'Billing', 'Review') if pending.get('action') == 'continueFromPickupContactToShipping'
               else ('Billing', 'Review') if pending.get('action') == 'continueFromPickupContactToBilling' else ())
    if page not in allowed or not isinstance(value(flow.model, 'checkout.' + page.lower()), dict):
        return False
    if fresh and value(flow.current_response_body, 'checkout.d.page') != page:
        return False
    proof = getattr(handler, 'pickup_contact_proof', None)
    if not isinstance(proof, dict) or not proof or proof != pending.get('proof'):
        return False
    prefix = 'checkout.pickupContact.selfPickupContact.selfContact.address.'
    for key, record in proof.items():
        if not isinstance(record, dict):
            return False
        if isinstance(record.get('value'), str):
            if pending['payload'].get(prefix + key) != record['value']:
                return False
        elif record.get('bound') is not True or not isinstance(record.get('masked'), str):
            return False
    for record in proof.values():
        record['server_accepted'] = True
    handler.pickup_contact_accepted = True
    del flow._pending_pickup_contact_submission
    return True


def prepare_checkout_details(flow, config_path, auth_path=None):
    """Advance only actual current contact/address forms; never select inventory.

    Fulfillment may expose no legal route to contact before stock is available.
    Local completeness is then reported separately from official acceptance.
    Billing/Review are retained and no payment or order action is called here.
    """
    from pickup_options import normalize_contact_value, prepared_contact_review_proof
    config = read_config(config_path)
    local_contact = all(normalize_contact_value(key, config.get('contact', {}).get(key)) is not None
                        for key in ('firstName', 'lastName', 'emailAddress', 'fullDaytimePhone'))
    accepted_contact = accepted_address = False
    for _ in range(2):
        page = value(flow.current_response_body, 'checkout.d.page')
        if page not in ('PickupContact', 'Shipping'):
            break
        if value(flow.model, 'checkout.d.page') != page:
            break
        section = 'checkout.' + ('pickupContact' if page == 'PickupContact' else 'shipping')
        names = (('continueFromPickupContactToShipping', 'continueFromPickupContactToBilling')
                 if page == 'PickupContact' else ('continueFromShippingToBilling',))
        # The merged model must expose the very action supplied by this response.
        action = next((name for name in names
                       if isinstance(value(flow.current_response_body, section + '.a.' + name + '.url'), str)
                       and value(flow.current_response_body, section + '.a.' + name + '.url')
                       and value(flow.current_response_body, section + '.a.' + name + '.url')
                       == value(flow.model, section + '.a.' + name + '.url')), None)
        if action is None:
            break
        if next((name for name in names if name in value(flow.model, section + '.a', {})), None) != action:
            break
        if page == 'PickupContact':
            pickup_contact(flow, config, config_path)
            accepted_contact = True
        else:
            shipping(flow, config_path, auth_path)
            accepted_address = True
    stage = value(flow.model, 'checkout.d.page')
    if stage == 'Review':
        proof = getattr(interaction.handler, 'pickup_contact_proof', None)
        accepted_contact = prepared_contact_review_proof(flow.current_response_body, proof) is not None
    return {'status': 'waiting_stock' if stage == 'Fulfillment' else 'details_accepted'
            if accepted_contact or accepted_address else 'unchanged',
            'stage': stage if stage in ('Fulfillment', 'PickupContact', 'Shipping', 'Billing', 'Review') else 'unknown',
            'local_contact_ready': local_contact, 'local_address_ready': local_address_ready(config),
            'contact_accepted': accepted_contact, 'address_accepted': accepted_address}


def priority_fulfillment(flow, config, config_path, payload):
    """Try allowed stores in order only while still choosing fulfillment."""
    from pickup_options import (validate_preset, search_results, store_name, time_slot_payload, NoPickupSlot,
                                PickupUnavailable, fulfillment_error, fresh_pickup_context, fresh_pickup_slot_payload,
                                pickup_search_payload, query_pickup_stores)
    policy = validate_preset(config)['pickup']
    path = 'checkout.fulfillment'
    locator = path + '.pickupTab.pickup.storeLocator'
    slot_path = path + '.pickupTab.pickup.timeSlot'
    handler = interaction.handler
    rejected = []
    current_search = None
    for preferred in policy['stores']:
        if getattr(handler, 'submitted', False) is True or value(flow.model, 'checkout.d.page') != 'Fulfillment':
            raise ValueError('已离开自提选择阶段，不能自动切换其他门店。')
        flow.require(locator)
        store_data = pickup_search_payload(flow,preferred['search'])
        fresh_context = fresh_pickup_context(flow, preferred)
        query = tuple(sorted(store_data.items()))
        if fresh_context is not None:
            stores = fresh_context['stores']
        else:
            current_body = getattr(flow, 'current_response_body', None)
            if current_search is not None and current_search[0] == query and current_search[1] is current_body:
                fresh_locator = current_search[2]
            else:
                previous_body = current_body
                query_pickup_stores(flow,preferred['search'])
                current_body = getattr(flow, 'current_response_body', None)
                fresh_locator = value(current_body, locator)
                if current_body is previous_body or not isinstance(fresh_locator, dict):
                    raise ValueError('官网本次门店搜索未返回可核对的新列表，未复用旧门店结果，未提交订单。')
                current_search = (query, current_body, fresh_locator)
            try:
                stores = search_results(fresh_locator)
            except PickupUnavailable:
                rejected.append(preferred['store_name'])
                continue
            # The search itself may have returned complete item/store/slot proof.
            fresh_context = fresh_pickup_context(flow, preferred)
        matches = [store for store in stores if isinstance(store, dict) and store.get('storeId') == preferred['store_id']]
        if len(matches) > 1:
            raise ValueError('官网返回重复门店编号，无法确认优先门店；未提交订单。')
        if not matches or matches[0].get('storeDisabled') is True:
            rejected.append(preferred['store_name'])
            continue
        chosen = matches[0]
        if chosen.get('storeDisabled') is not False:
            raise ValueError('官网尚未明确确认预选门店的可取货状态，未当作无货换店，未提交订单。')
        if store_name(chosen.get('storeName')) != store_name(preferred['store_name']):
            raise ValueError('官网门店编号与预选门店名称不一致；未提交订单。')
        store_data[locator + '.selectStore'] = chosen['storeId']
        required_slot = value(flow.model, slot_path + '.d.enableTimeSlotWindows') is True
        slots = fresh_pickup_slot_payload(fresh_context, policy.get('time_slot_rule')) if fresh_context is not None else None
        if slots is None:
            flow.require(path + '.pickupTab.pickup').pop('timeSlot', None)
            flow.require(locator).setdefault('d', {})['selectStore'] = ''
            flow.action(locator, 'select', store_data)
            if value(flow.model, locator + '.d.selectStore') != chosen['storeId']:
                raise ValueError('服务端未确认所选优先自提门店，已停止。')
            if required_slot and value(flow.model, slot_path) is None:
                flow.page(flow.apple_url('/shop/checkout'), '重新核对优先门店自提时段', fresh=True)
                if value(flow.model, slot_path) is None:
                    raise ValueError('官网尚未返回当前门店时段，未继续结账，未提交订单。')
            try:
                slots = time_slot_payload(flow, slot_path, chosen['storeId'], rule=policy.get('time_slot_rule'))
            except NoPickupSlot:
                rejected.append(preferred['store_name'])
                handler.pickup_selected = None
                handler.pickup_slot_selected = None
                continue
        if value(flow.model, path + '.pickupTab.deliveryForced.shipmentGroups'):
            raise ValueError('自提订单中仍包含必须送货的商品，未自动改为配送，未提交订单。')
        handler.pickup_selected = chosen
        interaction.report_stage('按预设顺序选择自提门店：' + chosen['storeName'])
        payload = {**payload, **store_data, **slots}
        flow.action(path, 'continueFromFulfillmentToPickupContact', payload)
        def advanced():
            page = value(flow.model, 'checkout.d.page')
            key = {'PickupContact':'pickupContact','Billing':'billing','Review':'review'}.get(page)
            return bool(key and isinstance(value(flow.model, 'checkout.' + key), dict))
        if not advanced():
            error = fulfillment_error(value(flow.model, path, {}))
            if not error:
                flow.page(flow.apple_url('/shop/checkout'), '核对优先门店自提选择是否已完成', fresh=True)
                error = fulfillment_error(value(flow.model, path, {}))
            if not advanced():
                raise ValueError('优先门店自提选择未通过官网校验：' + (error or '尚未进入下一步骤')
                                 + '；未提交订单，不自动切换门店重发。')
        return
    raise PickupUnavailable('预选门店目前均不可取货或没有符合规则的有效时段；未改选列表外门店，未提交订单。')


def home_delivery(flow, config):
    path = 'checkout.fulfillment'
    selector = path + '.fulfillmentOptions'
    payload = {selector + '.selectFulfillmentLocation': 'HOME'}
    if flow.require(selector)['d'].get('selectFulfillmentLocation') != 'HOME':
        flow.action(selector, 'selectFulfillmentLocationAction', payload)
    if flow.require(selector)['d'].get('selectFulfillmentLocation') != 'HOME':
        raise ValueError('服务端未确认切换为送货，已停止。')
    tab = path + '.deliveryTab.delivery'
    payload.update(region_data(flow, tab + '.deliveryLocation.address.provinceCityDistrictTabsForCheckout', config.get('delivery'), prefer_current=not automatic_options()))
    payload.update(shipping_options(flow, tab + '.shipmentGroups'))
    flow.action(path, 'continueFromFulfillmentToShipping', payload)
    flow.require('checkout.shipping')


def fulfillment(flow, config_path):
    if interaction.handler is not None:
        interaction.handler.pickup_selection_scope = None
        interaction.handler.pickup_identity_scope = None
        interaction.handler.pickup_slot_selected = None
        interaction.handler.pickup_contact_proof = None
        interaction.handler.pickup_identity_proof = None
        interaction.handler.pickup_contact_accepted = False
        interaction.handler.pickup_contact_review_verified = False
    config = read_config(config_path)
    path = 'checkout.fulfillment'
    node = flow.require(path + '.fulfillmentOptions')['d']
    options = [(o['value'], o.get('title', o['value'])) for o in node.get('options', [])
               if o.get('value') in ('HOME', 'RETAIL') and not o.get('disabled')]
    mode = choose('收货方式', options, 'HOME')
    if mode != node.get('selectFulfillmentLocation'):
        flow.action(path + '.fulfillmentOptions', 'selectFulfillmentLocationAction',
                    {path + '.fulfillmentOptions.selectFulfillmentLocation': mode})
    payload = {path + '.fulfillmentOptions.selectFulfillmentLocation': mode}
    if mode == 'HOME':
        home_delivery(flow, config)
        return
    if automatic_options() and config.get('pickup', {}).get('selection_mode') == 'priority':
        priority_fulfillment(flow, config, config_path, payload)
        return
    locator = path + '.pickupTab.pickup.storeLocator'
    node = flow.require(locator)
    if automatic_options():
        from pickup_options import validate_preset
        search = validate_preset(config)['pickup']['search']
    else:
        search = ask('自提门店所在城市/地区', config.get('pickup', {}).get('search') or value(node, 'd.searchInput', '') or config.get('delivery', {}).get('city', ''))
    # The default visible subset can omit the explicitly requested store.
    # Ask for all results in this search area; selection remains exact and
    # a disabled/missing target never falls back to another store or HOME.
    from pickup_options import (search_results, fresh_pickup_context, fresh_pickup_slot_payload,
                                pickup_search_payload, query_pickup_stores)
    store_data = pickup_search_payload(flow,search,show_all=automatic_options())
    previous_body = getattr(flow, 'current_response_body', None)
    fresh_context = fresh_pickup_context(flow, config['pickup']) if automatic_options() else None
    if fresh_context is None:
        query_pickup_stores(flow,search,show_all=automatic_options())
        stores = search_results(flow.require(locator))
        fresh_context = fresh_pickup_context(flow, config['pickup']) if automatic_options() else None
    else:
        stores = fresh_context['stores']
        import logging
        logging.getLogger('duo_killer').info('[自提门店] 本次响应已核对商品和可取门店，跳过重复搜索。')
    if automatic_options():
        import logging
        from duo_killer import order_text
        summary = [{'name': order_text(s.get('storeName')), 'id': order_text(s.get('storeId')),
                    'disabled': s.get('storeDisabled')} for s in stores[:30] if isinstance(s, dict)] if isinstance(stores, list) else []
        logging.getLogger('duo_killer').info('[自提门店] 搜索区域=%s，全部结果=%s，返回数量=%s，门店=%s',
            order_text(search), store_data[locator + '.showAllStores'], len(stores) if isinstance(stores, list) else '格式异常', summary)
        # A disabled store can still establish identity for a legacy named
        # preset. It supplies no availability, selection, slot or Review proof.
        current_body = getattr(flow, 'current_response_body', None)
        identity_stores = fresh_context['stores'] if fresh_context is not None else None
        if identity_stores is None and isinstance(current_body, dict) and current_body is not previous_body:
            node = value(current_body, locator)
            if isinstance(node, dict):
                identity_stores = search_results(node)
        from pickup_options import store_name
        matches = [row for row in identity_stores or [] if isinstance(row, dict)
                   and store_name(row.get('storeName')) == store_name(config['pickup']['store_name'])]
        if (len(matches) == 1 and isinstance(matches[0].get('storeId'), str)
                and re.fullmatch(r'R\d{1,8}', matches[0]['storeId'])
                and config.get('pickup', {}).get('store_id', matches[0]['storeId']) == matches[0]['storeId']):
            interaction.handler.pickup_identity_scope = {'search': search,
                'store_name': config['pickup']['store_name'], 'store_id': matches[0]['storeId'],
                'config_fingerprint': config_fingerprint(config_path)}
    if automatic_options():
        from pickup_options import select_store
        chosen = select_store(stores, config['pickup'])
        selected = chosen['storeId']
    else:
        selected = None
    choices = []
    for store in stores:
        lines = value(store, 'availability.lineItemAvailability', [])
        status = '；'.join(str(line.get('partName', '商品')) + '：' + str(line.get('availabilityQuote', '未知')) for line in lines)
        title = str(store.get('storeName', store['storeId'])) + ' — ' + status
        if store.get('storeDisabled') is True:
            print('不可选：' + title)
        else:
            choices.append((store['storeId'], title))
    no_stores = not choices
    delivery_available = any(o.get('value') == 'HOME' and not o.get('disabled')
                             for o in flow.require(path + '.fulfillmentOptions')['d'].get('options', []))
    if delivery_available:
        choices.append(('HOME', '为我送货（不选门店，改为配送）'))
        if no_stores:
            print('当前没有可选自提门店，可以改为送货。')
    if not automatic_options():
        selected = choose('选择自提门店或改为送货（取货状态由官网返回）', choices,
                          'HOME' if no_stores and delivery_available else None)
    if selected == 'HOME':
        home_delivery(flow, config)
        return
    store_data[locator + '.selectStore'] = selected
    slot_path = path + '.pickupTab.pickup.timeSlot'
    required_slot = value(flow.model, slot_path + '.d.enableTimeSlotWindows') is True
    fresh_slots = fresh_pickup_slot_payload(fresh_context, config.get('pickup', {}).get('time_slot_rule')) if fresh_context is not None else None
    if fresh_slots is None:
        if automatic_options():
            # The fresh response did not prove a usable slot state. Never let
            # an older merged slot survive the new selection response.
            flow.require(path + '.pickupTab.pickup').pop('timeSlot', None)
        flow.action(locator, 'select', store_data)
        if automatic_options() and required_slot and value(flow.model, slot_path) is None:
            # An omitted increment is not proof that a previously required
            # appointment disappeared. Recover its live state without replay.
            flow.page(flow.apple_url('/shop/checkout'), '重新核对自提时段', fresh=True)
            if value(flow.model, slot_path) is None:
                raise ValueError('官网尚未返回当前自提时段，请重新准备；未继续结账，未提交订单。')
    else:
        logging.getLogger('duo_killer').info('[自提门店] 本次响应已确认相同门店和时段，跳过重复选择。')
    if value(flow.model, locator + '.d.selectStore') != selected:
        raise ValueError('服务端未确认所选自提门店，已停止。')
    if automatic_options():
        interaction.handler.pickup_selected = chosen
        # Bind legacy name-only presets to the exact effective query/name used
        # for this run's unique official selection, independently of raw rows.
        identity_scope = getattr(interaction.handler, 'pickup_identity_scope', None)
        interaction.handler.pickup_selection_scope = (dict(identity_scope)
            if isinstance(identity_scope, dict) and identity_scope.get('store_id') == chosen['storeId'] else None)
        interaction.report_stage('自动选择自提门店：' + chosen['storeName'])
    payload.update(store_data)
    forced = path + '.pickupTab.deliveryForced'
    if value(flow.model, forced + '.shipmentGroups'):
        if automatic_options():
            raise ValueError('自提订单中仍包含必须送货的商品，未自动改为配送，未提交订单。')
        print('此订单包含必须配送的商品，继续配置送货地区和配送服务。')
        payload.update(region_data(flow, forced + '.deliveryLocation.address.provinceCityDistrictTabsForCheckout', config.get('delivery'), prefer_current=True))
        payload.update(shipping_options(flow, forced + '.shipmentGroups'))
    from pickup_options import time_slot_payload, fulfillment_error
    payload.update(fresh_slots if fresh_slots is not None else time_slot_payload(
        flow, path + '.pickupTab.pickup.timeSlot', selected, None if automatic_options() else choose,
        rule=config.get('pickup', {}).get('time_slot_rule')))
    flow.action(path, 'continueFromFulfillmentToPickupContact', payload)
    def advanced():
        node = {'PickupContact': 'pickupContact', 'Billing': 'billing', 'Review': 'review'}.get(value(flow.model, 'checkout.d.page'))
        return bool(node and isinstance(value(flow.model, 'checkout.' + node), dict))
    if not advanced():
        error = fulfillment_error(value(flow.model, path, {}))
        if not error:
            # A partial response may not include the next step. Read, never replay.
            flow.page(flow.apple_url('/shop/checkout'), '核对自提配送是否已完成', fresh=True)
            error = fulfillment_error(value(flow.model, path, {}))
        if not advanced():
            raise ValueError('自提配送未通过官网校验：' + (error or '尚未进入取货联系人步骤，请核对门店和取货时段')
                             + '；未提交订单，不重复发送继续请求。')
    if not automatic_options() and isinstance(value(flow.model, 'checkout.pickupContact'), dict):
        pickup_contact(flow, config, config_path)


def shipping(flow, config_path, auth_path=None):
    if value(flow.model, 'checkout.d.page') == 'Billing':
        flow.require('checkout.billing')
        return  # 纯自提没有收货地址步骤。
    path = 'checkout.shipping'
    node = flow.require(path)
    selector = node['addressSelector']['d']
    saved = [a for a in selector.get('radioAddresses', [])
             if a.get('isSavedAddress') and a.get('addressLine1') and a.get('value')
             and a['value'] != 'newAddr' and not a.get('disabled')]
    current = next((a for a in saved if a['value'] == selector.get('selectAddress')), None)
    defaults = [a for a in saved if a.get('isDefaultAddr') is True]
    if current is None and len(defaults) == 1:
        current = defaults[0]
    if current is None and len(saved) == 1:
        current = saved[0]
    can_add = any(a.get('value') == 'newAddr' and not a.get('disabled')
                  for a in selector.get('radioAddresses', []))
    if automatic_options():
        config = read_config(config_path)
        if local_address_ready(config):
            if not can_add:
                raise ValueError('官网未提供本地地址填写入口；未改用其他地址，未提交订单。')
            selected = 'newAddr'
            interaction.report_stage('自动使用本地地址和联系方式')
        elif saved and current:
            # Only an explicit default or a unique saved address is safe.
            selected = 'saved'
            interaction.report_stage('本地地址不完整，自动使用官网默认地址')
        else:
            raise ValueError('没有完整本地地址或唯一官网默认地址，请先保存地址；未提交订单。')
    elif saved:
        choices = [('saved', '使用官网已有的地址')]
        if can_add:
            choices.append(('newAddr', '使用本地快捷地址进行配置'))
        else:
            print('官网当前未提供可用的新增地址入口，只能使用已有地址。')
        selected = choose('选择送货地址来源', choices, 'saved')
    elif can_add:
        selected = 'newAddr'
        print('官网没有可用的已保存地址，使用本地快捷地址。')
    else:
        raise ValueError('官网没有可用的收货地址，也未提供新增地址入口，已停止。')
    if selected == 'saved':
        selected = current['value'] if current else choose('官网有多个地址且未指定默认，请选择',
                    [(a['value'], str(a.get('provinceCityDistrict', '')) + ' ' + str(a['addressLine1'])) for a in saved])
        current = next(a for a in saved if a['value'] == selected)
        print('使用官网已保存地址：' + str(current.get('provinceCityDistrict', '')) + ' ' + str(current['addressLine1']))
    payload = {path + '.addressSelector.selectAddress': selected}
    if selected == 'newAddr':
        config = read_config(config_path)
        if not config.get('address', {}).get('street') or not config.get('delivery'):
            if auth_path is None:
                configure_address(config_path)
            else:
                configure_address(config_path, auth_path)
            config = read_config(config_path)
        details = contact(config)
        config['contact'] = details
        save_config(config_path, config)
        address = path + '.addressSelector.newAddress.address'
        flow.require(address)
        payload.update(region_data(flow, address + '.stateCitySelectorForCheckout', config.get('delivery')))
        payload.update({address + '.' + k: details[k] for k in ('lastName', 'firstName')})
        payload.update({address + '.street': config['address']['street'], address + '.street2': config.get('address', {}).get('street2', ''),
                        address + '.copyContact.fullDaytimePhone': details['fullDaytimePhone'],
                        path + '.addressSelector.newAddress.saveToAddressBook': 'false' if automatic_options() else 'true'})
        email = details['emailAddress']
        print('收货地址：' + ' '.join(config['delivery'].values()) + ' ' + config['address']['street'])
    else:
        details = {}
        email = value(flow.model, path + '.addressNotification.address.d.emailAddress', '')
    payload.update(invoice_data(flow, path))
    if value(flow.model, path + '.addressNotification.address') is not None:
        optional = value(flow.model, path + '.addressNotification.address.b.emailAddress.optional') is True
        payload[path + '.addressNotification.address.emailAddress'] = email if email or optional else ask('通知邮箱', pattern=r'[^\s@]+@[^\s@]+\.[^\s@]+')
    phone_path = path + '.addressContactPhone.address'
    phone = value(flow.model, phone_path)
    if phone:
        payload.update(verification(flow, phone_path))
        if value(phone, 'b.mobilePhone.submit') is True:
            number = (details.get('fullDaytimePhone') if selected == 'newAddr' else value(phone, 'd.mobilePhone', '')) or ''
            if not re.fullmatch(r'\d{11,12}', number):
                number = ask('收货手机号码', pattern=r'\d{11,12}')
            payload[phone_path + '.mobilePhone'] = number
    flow.action(path, 'continueFromShippingToBilling', payload)
    if value(flow.model, 'checkout.d.page') != 'Billing':
        raise ValueError('收货地址或联系方式未通过服务端校验，请检查后重试；未提交订单。')
    flow.require('checkout.billing')
