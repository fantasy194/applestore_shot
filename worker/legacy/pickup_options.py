"""Account-local pickup preferences; select only an exact official store match."""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from html import unescape


def pickup_calendar_now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def store_name(name):
    text = re.sub(r'^\s*apple(?:\s+store)?\s*', '', str(name or ''), flags=re.I)
    return ''.join(text.split()).casefold()


class PickupUnavailable(ValueError):
    """The official response explicitly blocks an allowed pickup choice."""


class NoPickupSlot(PickupUnavailable):
    """A valid response has no signed option meeting this store's rule."""


def validate_time_rule(rule=None):
    if rule is None:
        return {'mode': 'earliest', 'fallback': 'earliest'}
    if not isinstance(rule, dict) or set(rule) - {'mode', 'start', 'end', 'fallback'}:
        raise ValueError('自提时段规则格式无效。')
    mode, fallback = rule.get('mode', 'earliest'), rule.get('fallback', 'earliest')
    if mode not in ('earliest', 'latest', 'window') or fallback not in ('earliest', 'stop'):
        raise ValueError('自提时段规则无效。')
    result = {'mode': mode, 'fallback': fallback}
    if mode == 'window':
        start, end = rule.get('start'), rule.get('end')
        if (not isinstance(start, str) or not isinstance(end, str)
                or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', start)
                or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', end) or start >= end):
            raise ValueError('自提时间窗口须为同日 HH:MM，且结束时间晚于开始时间。')
        result.update(start=start, end=end)
    # Older latest/window settings remain readable, but all purchase entry
    # points now select the earliest official slot in the same seven-day range.
    return {'mode': 'earliest', 'fallback': 'earliest'}


def _validate_stores(stores, *, required):
    if not isinstance(stores, list) or not (1 if required else 0) <= len(stores) <= 30:
        raise ValueError('请预选 1–30 家自提门店并设置优先顺序。' if required
                         else '备用自提门店列表须为 0–30 家有效门店。')
    rows, seen = [], set()
    for row in stores:
        if not isinstance(row, dict) or set(row) - {'store_id', 'store_name', 'search'}:
            raise ValueError('预选自提门店格式无效。')
        if not isinstance(row.get('store_id'), str) or not re.fullmatch(r'R\d{1,8}', row['store_id']):
            raise ValueError('预选自提门店编号无效。')
        if row['store_id'] in seen:
            raise ValueError('预选自提门店不能重复。')
        seen.add(row['store_id'])
        clean = {'store_id': row['store_id']}
        for key in ('store_name', 'search'):
            text = row.get(key)
            if not isinstance(text, str) or not text.strip() or len(text) > 200:
                raise ValueError('请填写预选门店的名称与城市或邮编。')
            clean[key] = text.strip()
        rows.append(clean)
    return rows


def validate_preset(data, *, require_contact=True):
    pickup = data.get('pickup')
    if not isinstance(pickup, dict):
        raise ValueError('请先完整保存自提城市、门店和取货联系人。')
    mode, contact_mode = pickup.get('selection_mode', 'fixed'), pickup.get('contact_mode', 'local')
    if mode not in ('fixed', 'priority') or contact_mode not in ('local', 'official'):
        raise ValueError('自提门店或联系人模式无效。')
    result = {'pickup': {}, 'contact': {}}
    if mode == 'priority':
        rows = _validate_stores(pickup.get('stores'), required=True)
        result['pickup'].update(selection_mode='priority', stores=rows,
                                search=rows[0]['search'], store_name=rows[0]['store_name'])
    else:
        for key in ('search', 'store_name'):
            text = pickup.get(key)
            if not isinstance(text, str) or not text.strip() or len(text) > 200:
                raise ValueError('请先完整保存自提城市、门店和取货联系人。')
            result['pickup'][key] = text.strip()
        if 'stores' in pickup:
            result['pickup']['stores'] = _validate_stores(pickup['stores'], required=False)
        if 'selection_mode' in pickup:
            result['pickup']['selection_mode'] = mode
    if 'contact_mode' in pickup:
        result['pickup']['contact_mode'] = contact_mode
    if 'time_slot_rule' in pickup:
        result['pickup']['time_slot_rule'] = validate_time_rule(pickup['time_slot_rule'])
    else:
        validate_time_rule()
    saved_contact = data.get('contact', {})
    if not isinstance(saved_contact, dict):
        raise ValueError('本地取货联系人格式无效。')
    strict = require_contact and mode == 'fixed' and contact_mode == 'local'
    for key in ('lastName', 'firstName', 'emailAddress', 'fullDaytimePhone'):
        text = saved_contact.get(key, '')
        if not isinstance(text, str) or len(text) > 200 or strict and not text.strip():
            raise ValueError('请先完整保存自提城市、门店和取货联系人。')
        if text.strip():
            result['contact'][key] = text.strip()
    last4 = data['pickup'].get('id_last4', '')
    if not isinstance(last4, str) or (last4.strip() and not re.fullmatch(r'[A-Za-z0-9]{4}', last4.strip())):
        raise ValueError('取货证件号码仅填写后四位英文字母或数字，请勿填写完整证件号。')
    if last4.strip():
        result['pickup']['id_last4'] = last4.strip()
    store_id = data['pickup'].get('store_id', '') if mode == 'fixed' else ''
    if store_id:
        if not isinstance(store_id, str) or not re.fullmatch(r'R\d{1,8}', store_id):
            raise ValueError('自提门店编号格式无效，请从接口重新选择门店。')
        result['pickup']['store_id'] = store_id
    if not store_name(result['pickup']['store_name']):
        raise ValueError('请填写具体自提门店名称。')
    if result['contact'].get('fullDaytimePhone') and not re.fullmatch(r'\d{11,12}', result['contact']['fullDaytimePhone']):
        raise ValueError('请输入正确的取货手机号码。')
    if result['contact'].get('emailAddress') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['contact']['emailAddress']):
        raise ValueError('请输入正确的取货通知邮箱。')
    return result


def validate_policy(policy):
    allowed = {'selection_mode', 'stores', 'time_slot_rule', 'contact_mode',
               'search', 'store_id', 'store_name', 'id_last4'}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ValueError('本次自提偏好仅允许门店、时段与联系人使用方式，不接受联系人数据。')
    return validate_preset({'pickup': policy}, require_contact=False)['pickup']


def allowed_store(preset, name, store_id=None):
    choices = preset.get('stores', []) if preset.get('selection_mode') == 'priority' else [preset]
    return any(store_name(name) == store_name(row['store_name'])
               and (not row.get('store_id') or row['store_id'] == store_id) for row in choices)


def select_store(stores, preset):
    if not isinstance(stores, list):
        raise ValueError('官网自提门店数据不完整；未提交订单。')
    wanted = store_name(preset['store_name'])
    matches = [s for s in stores if isinstance(s, dict) and store_name(s.get('storeName')) == wanted]
    if not matches:
        names = [re.sub(r'[\x00-\x1f\x7f]', '', str(s.get('storeName') or s.get('storeId') or '未命名门店'))[:60]
                 for s in stores if isinstance(s, dict)]
        returned = '、'.join(names[:10]) + ('等' if len(names) > 10 else '')
        raise ValueError('官网本次搜索结果未包含自提门店“' + preset['store_name'] + '”'
                         + (f'（返回 {len(names)} 家：{returned}）' if names else '（返回 0 家门店）')
                         + '；这不代表门店名称错误。未改用其他门店或送货，未提交订单。')
    if len(matches) > 1:
        raise ValueError('官网返回多家同名自提门店“' + preset['store_name'] + '”，无法唯一确认；未提交订单。')
    selected = matches[0]
    if preset.get('store_id') and selected.get('storeId') != preset['store_id']:
        raise ValueError('官网自提门店编号与指定门店不一致；未改用其他门店，未提交订单。')
    if selected.get('storeDisabled') is True and re.fullmatch(r'R\d+', str(selected.get('storeId', ''))):
        raise PickupUnavailable('所选自提门店当前不可取货；未改用其他门店或送货，未提交订单。')
    if selected.get('storeDisabled') is not False or not re.fullmatch(r'R\d+', str(selected.get('storeId', ''))):
        raise ValueError('所选自提门店当前不可取货；未改用其他门店或送货，未提交订单。')
    return selected


def search_results(node):
    """A successful HTTP/business response can still contain a search error.

    Read actual server messages (m), never client-side validation templates (b).
    A failed search must not reuse stores retained from an earlier response.
    """
    messages = node.get('m') or []
    if not isinstance(messages, list):
        messages = []
    errors = [' '.join(unescape(re.sub(r'<[^>]*>', ' ', m.get('text', ''))).split())
              for m in messages if isinstance(m, dict) and m.get('level') == 'ERROR'
              and isinstance(m.get('text'), str) and m['text'].strip()]
    if node.get('d', {}).get('isStoreSearchErrorPresent') is True or errors:
        import logging
        message = '；'.join(errors) or '官网标记门店搜索失败'
        logging.getLogger('duo_killer').warning('[自提门店] 官网搜索错误：%s', message)
        error_type = (PickupUnavailable if errors and all('你所在的地区无货可取' in text for text in errors)
                      else ValueError)
        raise error_type('官网自提门店搜索失败：' + message
                         + (' 可在本地配置将“自提城市 / 邮编”改为门店的 6 位邮编后重试。' if '邮政编码' in message else '')
                         + ' 尚未取得有效门店列表，这不代表门店名称错误；未提交订单。')
    results = node.get('searchResults')
    data = results.get('d') if isinstance(results, dict) else None
    stores = data.get('retailStores') if isinstance(data, dict) else None
    if not isinstance(stores, list):
        raise ValueError('官网未返回可识别的自提门店列表；未将搜索异常当作门店不存在，未提交订单。')
    return stores


def fresh_pickup_context(flow, preset):
    """Require this response's store list and matching physical-item evidence.

    The merged checkout model can retain old lists and signed slots. It is not
    evidence for omitting a write. Unknown response shapes use the normal path.
    """
    from duo_killer import field, model_items, normalize_sku
    import interaction
    body = getattr(flow, 'current_response_body', None)
    expected = getattr(interaction.handler, 'purchase_selection', None)
    if not isinstance(body, dict) or not expected:
        return None
    if field(body, 'checkout.fulfillment.fulfillmentOptions.d.selectFulfillmentLocation') != 'RETAIL':
        return None
    evidence = []
    try:
        for path in ('checkout.fulfillment', 'checkout.companionBar.orderDetails'):
            items = model_items(field(body, path, {}))
            if items:
                evidence.append([(normalize_sku(sku), quantity) for sku, quantity in items])
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    target = [(normalize_sku(expected[0]), expected[1])]
    if not evidence or any(items != target for items in evidence):
        return None
    pickup = field(body, 'checkout.fulfillment.pickupTab.pickup')
    if (not isinstance(pickup, dict) or not isinstance(pickup.get('storeLocator'), dict)
            or not isinstance(pickup['storeLocator'].get('d'), dict)):
        return None
    try:
        stores = search_results(pickup['storeLocator'])
        chosen = select_store(stores, preset)
    except ValueError:
        return None
    return {'body': body, 'pickup': pickup, 'stores': stores, 'chosen': chosen}


def fresh_pickup_slot_payload(context, rule=None):
    """None means reselect; {} means this response explicitly disables slots."""
    from types import SimpleNamespace
    pickup, chosen = context['pickup'], context['chosen']
    if pickup['storeLocator'].get('d', {}).get('selectStore') != chosen['storeId']:
        return None
    slot = pickup.get('timeSlot')
    if (not isinstance(slot, dict) or not isinstance(slot.get('d'), dict)
            or slot['d'].get('storeId') != chosen['storeId']):
        return None
    try:
        return time_slot_payload(SimpleNamespace(model=context['body']),
                                 'checkout.fulfillment.pickupTab.pickup.timeSlot', chosen['storeId'], rule=rule)
    except ValueError:
        return None


def time_slot_payload(flow, path, selected_store, choose_slot=None, *, rule=None):
    """Build the official dateTimeSlots form from this store's live signed options."""
    from duo_killer import field
    import interaction
    if interaction.handler is not None:
        interaction.handler.pickup_slot_selected = None
    slot = field(flow.model, path)
    if slot is None:
        return {}  # Some products/stores do not require a pickup window.
    if not isinstance(slot, dict) or not isinstance(slot.get('d'), dict):
        raise ValueError('官网自提时段数据不完整；未继续结账。')
    if slot['d'].get('storeId') is not None and slot['d']['storeId'] != selected_store:
        raise ValueError('官网自提时段与所选门店不匹配；未继续结账。')
    if slot['d'].get('enableTimeSlotWindows') is False:
        return {}
    if slot['d'].get('enableTimeSlotWindows') is not True or slot['d'].get('storeId') != selected_store:
        raise ValueError('官网自提时段与所选门店不匹配；未继续结账。')
    data = field(slot, 'dateTimeSlots.d')
    if not isinstance(data, dict) or data.get('storeSelected') != selected_store:
        raise ValueError('官网自提日期与所选门店不匹配；未继续结账。')
    dates, windows = data.get('pickUpDates'), data.get('timeSlotWindows')
    if not isinstance(dates, list) or not isinstance(windows, list):
        raise ValueError('官网未返回可用自提日期和时段；未继续结账。')
    candidates = []
    now = pickup_calendar_now().astimezone(ZoneInfo('Asia/Shanghai'))
    first_day, last_day = now.date(), now.date() + timedelta(days=6)
    for day in dates:
        if not isinstance(day, dict) or day.get('disabled') is True:
            continue
        iso, number = day.get('date'), day.get('dayOfMonth')
        try:
            valid_date = date.fromisoformat(iso)
            if str(valid_date.day) != str(number):
                continue
        except (TypeError, ValueError):
            continue
        for group in windows:
            options = group.get(str(number)) if isinstance(group, dict) else None
            if not isinstance(options, list):
                continue
            for option in options:
                if (not isinstance(option, dict) or option.get('enabled') is False
                        or option.get('disabled') is True or option.get('isRestricted')):
                    continue
                match = re.fullmatch(r'(\d{1,2})-(\d{2}:\d{2})-(\d{2}:\d{2})', str(option.get('timeSlotValue', '')))
                if (not match or str(int(match[1])) != str(number)
                        or not ('00:00' <= match[2] < match[3] <= '23:59')
                        or any(not isinstance(option.get(key), str) or not option[key]
                               for key in ('SlotId', 'signKey', 'timeZone', 'checkInStart', 'checkInEnd'))):
                    continue
                try:
                    time.fromisoformat(match[2]); time.fromisoformat(match[3])
                except ValueError:
                    continue
                try:
                    official_zone = ZoneInfo(option['timeZone'])
                    start_at = datetime.combine(valid_date, time.fromisoformat(match[2]), official_zone)
                    end_at = datetime.combine(valid_date, time.fromisoformat(match[3]), official_zone)
                except (ValueError, ZoneInfoNotFoundError):
                    continue
                shanghai_day = start_at.astimezone(ZoneInfo('Asia/Shanghai')).date()
                if not first_day <= shanghai_day <= last_day or end_at <= now:
                    continue
                candidates.append((start_at, match[2], day, option))
    candidates.sort(key=lambda c: c[0])
    if not candidates:
        raise NoPickupSlot('所选门店今天起七天内暂无完整可预约时段；未继续结账，未提交订单。')
    validate_time_rule(rule)  # Accept legacy input only after validating it.
    _, _, day, chosen = candidates[0]
    iso = day['date']
    prefix = path + '.dateTimeSlots.'
    fields = {'date': iso, 'dayRadio': str(day['dayOfMonth']),
              'timeSlotType': chosen.get('timeSlotType') or '', 'timeSlotValue': chosen['timeSlotValue'],
              'startTime': chosen['checkInStart'], 'endTime': chosen['checkInEnd'],
              'signKey': chosen['signKey'], 'timeZone': chosen['timeZone'], 'timeSlotId': chosen['SlotId'],
              'displayStartTime': chosen.get('displayStart') or '', 'displayEndTime': chosen.get('displayEnd') or '',
              'isRecommended': 'true' if chosen.get('recommendationLabel') else 'false',
              'isRestricted': 'false'}
    interaction.report_stage('选择自提时段：' + iso + ' ' + str(chosen.get('Label') or chosen['timeSlotValue']))
    if interaction.handler is not None:
        interaction.handler.pickup_slot_selected = {'store_id': selected_store, 'date': iso,
            'start': re.fullmatch(r'\d{1,2}-(\d{2}:\d{2})-(\d{2}:\d{2})', chosen['timeSlotValue'])[1],
            'end': re.fullmatch(r'\d{1,2}-(\d{2}:\d{2})-(\d{2}:\d{2})', chosen['timeSlotValue'])[2],
            'label': str(chosen.get('Label') or chosen['timeSlotValue'])}
    return {prefix + k: v for k, v in fields.items()}


def fulfillment_error(node):
    """Actual validation messages only; never echo template errors or input values."""
    errors = []
    def visit(value):
        if not isinstance(value, dict):
            return
        messages = value.get('m')
        if isinstance(messages, list):
            errors.extend(' '.join(unescape(re.sub(r'<[^>]*>', ' ', m['text'])).split())[:300]
                          for m in messages if isinstance(m, dict) and m.get('level') == 'ERROR'
                          and isinstance(m.get('text'), str) and m['text'].strip())
        for key, child in value.items():
            if key not in ('d', 'b', 'a', 'm'):
                visit(child)
    visit(node)
    return '；'.join(dict.fromkeys(errors))


def identity_payload(flow, path, config):
    """Use only the last four characters required by the official self-pickup form."""
    from duo_killer import field
    import interaction
    if interaction.handler is not None:
        interaction.handler.pickup_identity_proof = None
    node = field(flow.model, path)
    if node is None or field(node, 'd.enabled') is False:
        return {}
    if not isinstance(node, dict) or field(node, 'd.enabled') is not True:
        raise ValueError('官网取货证件校验字段不完整，未提交联系人。')
    official = config.get('pickup', {}).get('contact_mode', 'local') == 'official'
    existing = field(node, 'd.nationalIdSelf')
    binding = field(node, 'b.nationalIdSelf', {})
    bound_mask = (isinstance(existing, str) and len(existing) == 4 and set(existing) == {'•'}
                  and binding.get('masked') is True and isinstance(binding.get('submit'), dict))
    if official and bound_mask:
        if interaction.handler is not None:
            interaction.handler.pickup_identity_proof = {'masked': existing, 'bound': True}
        return {}
    last4 = config.get('pickup', {}).get('id_last4', '')
    if official and isinstance(existing, str) and re.fullmatch(r'[A-Za-z0-9]{4}', existing):
        last4 = existing
    if not last4:
        existing = field(node, 'd.nationalIdSelf')
        binding = field(node, 'b.nationalIdSelf', {})
        if (isinstance(existing, str) and len(existing) == 4 and set(existing) == {'•'}
                and binding.get('masked') is True and isinstance(binding.get('submit'), dict)):
            return {}  # Official binding omits an unchanged, masked saved value.
        if official and interaction.handler is not None:
            last4 = interaction.handler.channel.prompt('text', '官网缺少取货人证件号码后四位，请补充（不要填写完整证件号）', account=interaction.handler.account)
        else:
            raise ValueError('官网要求取货人证件号码后四位；请在“本地配置 → 自提门店与时段偏好”的本地联系人中填写。无需完整证件号，未提交联系人或订单。')
    if not isinstance(last4, str) or not re.fullmatch(r'[A-Za-z0-9]{4}', last4):
        raise ValueError('取货证件号码后四位格式无效；请检查本地自提预设，未提交联系人或订单。')
    handler = interaction.handler
    if handler is not None:
        handler.pickup_identity_proof = {'value': last4}
    return {path + '.nationalIdSelf': last4}


CONTACT_FIELDS = {'lastName': '取货人姓氏', 'firstName': '取货人名字',
                  'emailAddress': '取货通知邮箱', 'fullDaytimePhone': '取货手机号码'}


def normalize_contact_value(key, value):
    """Compare form values with Review display values, without suffix matches."""
    from duo_killer import order_text
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    text = order_text(value)
    if not text or re.search(r'[*•…]', text):
        return None
    if key == 'fullDaytimePhone':
        if not re.fullmatch(r'[+0-9\s()\-]+', text):
            return None
        number = re.sub(r'[\s()\-]', '', text)
        international = re.fullmatch(r'(?:\+86|0086)(1[3-9][0-9]{9})', number)
        if international:
            return international[1]
        return number if re.fullmatch(r'[0-9]{11,12}', number) else None
    if key == 'emailAddress':
        return text.casefold() if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', text) else None
    return ''.join(text.split())


def _contact_valid(key, value):
    return normalize_contact_value(key, value) is not None


def _review_mask_matches(key, submitted, displayed):
    """Review redaction is evidence only after this run's accepted form POST."""
    from duo_killer import order_text
    expected = normalize_contact_value(key, submitted)
    if expected is None or not isinstance(displayed, str) or len(displayed) > 200:
        return False
    text = order_text(displayed)
    if not re.search(r'[*•…]', text):
        return False
    if key == 'emailAddress':
        if text.count('@') != 1 or re.search(r'\s', text):
            return False
        local, domain = text.casefold().split('@')
        wanted, expected_domain = expected.split('@')
        # Require the entire domain and both visible ends of the local part.
        if (domain != expected_domain or len(local) < 3
                or local[0] in '*•…' or local[-1] in '*•…'):
            return False
        displayed, submitted, wildcard = local, wanted, r'[^@\s]+'
    elif key == 'fullDaytimePhone':
        if not re.fullmatch(r'[+0-9\s()\-*•…]+', text):
            return False
        number = re.sub(r'[\s()\-]', '', text)
        if re.fullmatch(r'1[3-9][0-9]{9}', expected):
            if number.startswith('+86'):
                number = number[3:]
            elif number.startswith('0086'):
                number = number[4:]
        if (not re.fullmatch(r'[0-9*•…]+', number)
                or not re.search(r'[0-9]{4}$', number)):
            return False
        displayed, submitted, wildcard = number, expected, r'[0-9]+'
    else:
        return False
    # Apple uses a fixed mask length for phone numbers: eight bullets can
    # represent seven hidden digits. Match a run, never a numeric suffix alone.
    pattern = ''.join(wildcard if re.fullmatch(r'[*•…]+', part) else re.escape(part)
                      for part in re.split(r'([*•…]+)', displayed))
    return re.fullmatch(pattern, submitted) is not None


def resolve_contact(flow, path, config):
    """Prefer current official values/bindings. A masked value is never posted."""
    from duo_killer import field
    import interaction
    node = flow.require(path)
    data, bindings = node.get('d', {}), node.get('b', {})
    if not isinstance(data, dict) or not isinstance(bindings, dict):
        raise ValueError('官网取货联系人字段不完整；未提交联系人。')
    official = config.get('pickup', {}).get('contact_mode', 'local') == 'official'
    payload, proof = {}, {}
    for key, label in CONTACT_FIELDS.items():
        current, binding = data.get(key), bindings.get(key, {})
        masked = (official and isinstance(current, str) and bool(re.search(r'[*•…]', current))
                  and isinstance(binding, dict) and binding.get('masked') is True
                  and isinstance(binding.get('submit'), dict))
        if masked:
            proof[key] = {'masked': current, 'bound': True}
            continue
        selected = current if official and _contact_valid(key, current) else config.get('contact', {}).get(key)
        if not _contact_valid(key, selected):
            handler = interaction.handler
            if handler is None:
                from checkout_options import ask
                selected = ask('官网缺少' + label + '，请补充')
            else:
                selected = handler.channel.prompt('text', '官网和本地均缺少' + label + '，请补充', account=handler.account)
            if not _contact_valid(key, selected):
                raise ValueError(label + '格式无效，未提交联系人。')
        selected = selected.strip()
        if key == 'fullDaytimePhone':
            # The form field expects a number; Review may display that number
            # using punctuation or an explicit China country calling code.
            selected = normalize_contact_value(key, selected)
        payload[path + '.' + key] = selected
        proof[key] = {'value': selected}
    if interaction.handler is not None:
        interaction.handler.pickup_contact_proof = proof
    return payload


def verify_contact_review(model, proof, identity=None):
    """Compare the accepted contact with the actual current Review, not config."""
    from duo_killer import field, order_text
    node = field(model, 'checkout.review.pickupContactReview')
    if (not isinstance(node, dict) or not isinstance(proof, dict)
            or not {'emailAddress','fullDaytimePhone'} <= proof.keys()
            or not ('fullName' in proof or {'firstName','lastName'} <= proof.keys())):
        raise ValueError('官网复核未提供可核对的取货联系人；未提交订单。')
    values = {}
    def visit(current):
        if not isinstance(current, dict):
            return
        data = current.get('d', {})
        if isinstance(data, dict):
            for key in (*CONTACT_FIELDS, 'fullName', 'nationalIdSelf'):
                if isinstance(data.get(key), str) and data[key].strip():
                    values.setdefault(key, set()).add(data[key])
        for key, child in current.items():
            if key not in ('d', 'a', 'b', 'm', 'p'):
                visit(child)
    visit(node)
    def reject(key, reason, *, missing=False):
        import logging
        safe_key = key if key in (*CONTACT_FIELDS, 'fullName') else 'unknown'
        label = CONTACT_FIELDS.get(safe_key, '取货人姓名' if safe_key == 'fullName' else '取货人信息')
        logging.getLogger('duo_killer').warning('[自提联系人复核] 字段=%s，原因=%s；未记录联系人字段值。', safe_key, reason)
        message = '官网复核缺少完整且一致的取货联系人' if missing else '官网复核取货联系人与本次确认的信息不一致'
        raise ValueError(message + '（' + label + ' / ' + safe_key + '）；未提交订单。')
    for key, record in proof.items():
        actual = values.get(key)
        if not isinstance(record, dict):
            reject(key, '本次联系人证明不完整', missing=True)
        masked = 'value' not in record and record.get('bound') is True and isinstance(record.get('masked'), str)
        if not actual and key in ('firstName', 'lastName') and values.get('fullName'):
            parts = [proof[k] for k in ('lastName', 'firstName')]
            if any(not isinstance(part, dict) for part in parts):
                reject(key, '本次联系人姓名证明不完整', missing=True)
            expected = ''.join(part.get('value', part.get('masked', '')) for part in parts)
            masked = any('masked' in part for part in parts) and all(
                'value' in part or part.get('bound') is True for part in parts)
            actual, wanted = values['fullName'], expected
        else:
            wanted = record.get('value', record.get('masked', ''))
        if not actual:
            reject(key, '复核页缺少该字段', missing=True)
        def normalize(value):
            if not masked:
                return normalize_contact_value(key, value)
            text = order_text(value)
            return text.casefold() if key == 'emailAddress' else ''.join(text.split())
        expected = normalize(wanted)
        def matches(value):
            observed = normalize(value)
            if expected is not None and observed == expected:
                return True
            return (not masked and record.get('server_accepted') is True
                    and isinstance(record.get('value'), str)
                    and _review_mask_matches(key, wanted, value))
        if expected is None or not all(matches(value) for value in actual):
            reject(key, '复核值无法证明与本次确认相同')
        if any(value != wanted for value in actual):
            import logging
            logging.getLogger('duo_killer').info('[自提联系人复核] 字段=%s，已核对本次确认值与官网显示一致。', key)
    if identity and values.get('nationalIdSelf'):
        wanted = identity.get('value', identity.get('masked'))
        if {order_text(value) for value in values['nationalIdSelf']} != {order_text(wanted)}:
            raise ValueError('官网复核取货证件信息与本次确认的信息不一致；未提交订单。')
    return True


def official_review_proof(model):
    """Only a fresh Review's explicit, complete saved identity can stand alone."""
    from duo_killer import field
    node = field(model, 'checkout.review.pickupContactReview')
    rows = {}
    def visit(current):
        if not isinstance(current, dict):
            return
        data, bindings = current.get('d', {}), current.get('b', {})
        if isinstance(data, dict) and isinstance(bindings, dict):
            for key in (*CONTACT_FIELDS, 'fullName'):
                value = data.get(key)
                if _contact_valid(key, value):
                    rows.setdefault(key, []).append({'value':value.strip()})
                elif (key in ('emailAddress','fullDaytimePhone') and isinstance(value, str)
                      and re.search(r'[*•…]',value) and isinstance(bindings.get(key),dict)
                      and bindings[key].get('masked') is True and isinstance(bindings[key].get('submit'),dict)):
                    rows.setdefault(key, []).append({'masked':value,'bound':True})
        for key, child in current.items():
            if key not in ('a','b','d','m','p'):
                visit(child)
    visit(node)
    proof = {key:values[0] for key,values in rows.items() if len(values)==1}
    if not {'emailAddress','fullDaytimePhone'} <= proof.keys():
        return None
    if {'firstName','lastName'} <= proof.keys():
        proof.pop('fullName',None)
    elif 'fullName' in proof:
        proof.pop('firstName',None); proof.pop('lastName',None)
    else:
        return None
    return proof


def review_pickup_selection(model, selected_store):
    """Some Review models omit the store. Bind pickup-only groups to this run's
    server-confirmed store selection; never infer a store from the local preset.
    """
    from duo_killer import field, model_items, order_text
    review = field(model, 'checkout.review', {})
    if not isinstance(review, dict):
        return None
    fulfillment = review.get('fulfillmentReview', {})
    if not isinstance(fulfillment, dict):
        return None
    groups = [node for key, node in fulfillment.items()
              if key.startswith('reviewGroup-') and isinstance(node, dict) and model_items(node)]
    if (not groups or field(review, 'summary.d.pickupSummary') is not True
            or not isinstance(review.get('pickupContactReview'), dict)):
        return None
    for group in groups:
        data = group.get('d', {})
        if not isinstance(data, dict) or not isinstance(data.get('pickup'), dict) or data.get('delivers'):
            return None
    # Reject any item outside the verified pickup groups, not just the first group.
    if sorted(item for group in groups for item in model_items(group)) != model_items(fulfillment):
        return None
    if field(review, 'shippingReview.address.d.street'):
        return None
    if (not isinstance(selected_store, dict) or selected_store.get('storeDisabled') is not False
            or not re.fullmatch(r'R\d+', str(selected_store.get('storeId', '')))
            or not store_name(selected_store.get('storeName'))):
        return None
    for group in groups:
        pickup = group['d']['pickup']
        if (pickup.get('storeId') and pickup['storeId'] != selected_store['storeId']
                or any(pickup.get(k) and store_name(pickup[k]) != store_name(selected_store['storeName'])
                       for k in ('storeName', 'store_name'))):
            return None
    return {'store': order_text(selected_store['storeName']), 'store_id': selected_store['storeId'],
            'source': 'current_checkout_store_selection_and_pickup_only_review'}


def prepared_pickup_store_confirmed(model, details):
    """Re-prove the prepared store from a freshly read, unmerged Review model.

    The caller must perform the GET after waiting. Previous task selection is
    deliberately not consulted; the usual final review still checks items,
    amount and contact details before placing the order.
    """
    from duo_killer import field, model_items
    if not isinstance(details, dict):
        return False
    name, store_id = details.get('store'), details.get('store_id')
    if (not isinstance(name, str) or not store_name(name)
            or not isinstance(store_id, str) or not re.fullmatch(r'R\d+', store_id)):
        return False

    def check_identity(node, *, require_name):
        if not isinstance(node, dict):
            return False
        actual_id = node.get('storeId')
        names = [node[key] for key in ('store_name', 'storeName') if node.get(key)]
        if (actual_id and actual_id != store_id
                or any(not isinstance(actual, str) or store_name(actual) != store_name(name) for actual in names)):
            raise ValueError('准备后的官网自提门店与已确认门店不一致，未提交订单。')
        return actual_id == store_id and (bool(names) or not require_name)

    companion = field(model, 'checkout.companionBar.orderDetails.fulfillmentCompanionBar.pickupSummary.d')
    summary_confirmed = check_identity(companion, require_name=True)
    fulfillment = field(model, 'checkout.review.fulfillmentReview', {})
    if not isinstance(fulfillment, dict):
        return False
    group_confirmations = []
    for key, group in fulfillment.items():
        if key.startswith('reviewGroup-') and isinstance(group, dict):
            identity = check_identity(field(group, 'd.pickup'), require_name=False)
            try:
                items = model_items(group)
            except (ValueError, TypeError, KeyError, OverflowError):
                return False
            if items:
                group_confirmations.append(identity)
    try:
        selected = {'storeId': store_id, 'storeName': name, 'storeDisabled': False}
        pure_pickup = review_pickup_selection(model, selected)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    # Missing group store IDs are allowed only when the fresh companion header
    # itself supplies both identity fields; incomplete pickup structure is not.
    return pure_pickup is not None and (summary_confirmed or bool(group_confirmations) and all(group_confirmations))
