"""根据结账页面实时模型选择配送、自提与收货地址。"""
import json
import os
import re
import sys
import tempfile

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
    return config


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
    details = contact(config)
    config['contact'] = details
    save_config(config_path, config)
    payload.update({address + '.' + key: val for key, val in details.items()})
    payload.update(invoice_data(flow, path))
    actions = flow.require(path).get('a', {})
    action = next((a for a in ('continueFromPickupContactToShipping', 'continueFromPickupContactToBilling') if a in actions), None)
    if not action:
        raise ValueError('当前取货联系人页面没有支持的继续操作。')
    flow.action(path, action, payload)
    expected = 'Shipping' if action.endswith('Shipping') else 'Billing'
    if value(flow.model, 'checkout.d.page') != expected:
        raise ValueError('取货联系人未通过服务端校验，请检查姓名、手机及邮箱；未继续结账。')
    flow.require('checkout.shipping' if action.endswith('Shipping') else 'checkout.billing')


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
    locator = path + '.pickupTab.pickup.storeLocator'
    node = flow.require(locator)
    search = ask('自提门店所在城市/地区', value(node, 'd.searchInput', '') or config.get('delivery', {}).get('city', ''))
    region = locator + '.address.stateCitySelectorForCheckout'
    store_data = {locator + '.showAllStores': 'false', locator + '.selectStore': '', locator + '.searchInput': search}
    store_data.update({region + '.' + k: value(flow.model, region + '.d.' + k, '')
                      for k in ('state', 'city', 'district', 'provinceCityDistrict', 'countryCode')})
    flow.action(locator, 'search', store_data)
    stores = value(flow.model, locator + '.searchResults.d.retailStores', [])
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
    selected = choose('选择自提门店或改为送货（取货状态由官网返回）', choices,
                      'HOME' if no_stores and delivery_available else None)
    if selected == 'HOME':
        home_delivery(flow, config)
        return
    store_data[locator + '.selectStore'] = selected
    flow.action(locator, 'select', store_data)
    if value(flow.model, locator + '.d.selectStore') != selected:
        raise ValueError('服务端未确认所选自提门店，已停止。')
    payload.update(store_data)
    forced = path + '.pickupTab.deliveryForced'
    if value(flow.model, forced + '.shipmentGroups'):
        print('此订单包含必须配送的商品，继续配置送货地区和配送服务。')
        payload.update(region_data(flow, forced + '.deliveryLocation.address.provinceCityDistrictTabsForCheckout', config.get('delivery'), prefer_current=True))
        payload.update(shipping_options(flow, forced + '.shipmentGroups'))
    flow.action(path, 'continueFromFulfillmentToPickupContact', payload)
    flow.require('checkout.pickupContact')
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
