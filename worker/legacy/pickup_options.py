"""Account-local pickup preferences; select only an exact official store match."""
import re
from html import unescape


def store_name(name):
    text = re.sub(r'^\s*apple(?:\s+store)?\s*', '', str(name or ''), flags=re.I)
    return ''.join(text.split()).casefold()


def validate_preset(data):
    result = {'pickup': {}, 'contact': {}}
    for group, keys in (('pickup', ('search', 'store_name')),
                        ('contact', ('lastName', 'firstName', 'emailAddress', 'fullDaytimePhone'))):
        node = data.get(group)
        if not isinstance(node, dict):
            raise ValueError('请先完整保存自提城市、门店和取货联系人。')
        for key in keys:
            text = node.get(key)
            if not isinstance(text, str) or not text.strip() or len(text) > 200:
                raise ValueError('请先完整保存自提城市、门店和取货联系人。')
            result[group][key] = text.strip()
    last4 = data['pickup'].get('id_last4', '')
    if not isinstance(last4, str) or (last4.strip() and not re.fullmatch(r'[A-Za-z0-9]{4}', last4.strip())):
        raise ValueError('取货证件号码仅填写后四位英文字母或数字，请勿填写完整证件号。')
    if last4.strip():
        result['pickup']['id_last4'] = last4.strip()
    if not store_name(result['pickup']['store_name']):
        raise ValueError('请填写具体自提门店名称。')
    if not re.fullmatch(r'\d{11,12}', result['contact']['fullDaytimePhone']):
        raise ValueError('请输入正确的取货手机号码。')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', result['contact']['emailAddress']):
        raise ValueError('请输入正确的取货通知邮箱。')
    return result


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
    if selected.get('storeDisabled') is not False or not isinstance(selected.get('storeId'), str) or not selected['storeId']:
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
        raise ValueError('官网自提门店搜索失败：' + message
                         + (' 可在本地配置将“自提城市 / 邮编”改为门店的 6 位邮编后重试。' if '邮政编码' in message else '')
                         + ' 尚未取得有效门店列表，这不代表门店名称错误；未提交订单。')
    results = node.get('searchResults')
    data = results.get('d') if isinstance(results, dict) else None
    stores = data.get('retailStores') if isinstance(data, dict) else None
    if not isinstance(stores, list):
        raise ValueError('官网未返回可识别的自提门店列表；未将搜索异常当作门店不存在，未提交订单。')
    return stores


def time_slot_payload(flow, path, selected_store, choose_slot=None):
    """Build the official dateTimeSlots form from this store's live signed options."""
    from duo_killer import field
    import interaction
    slot = field(flow.model, path)
    if slot is None:
        return {}  # Some products/stores do not require a pickup window.
    if not isinstance(slot, dict) or not isinstance(slot.get('d'), dict):
        raise ValueError('官网自提时段数据不完整；未继续结账。')
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
    from datetime import date, time
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
                candidates.append((iso, match[2], day, option))
    candidates.sort(key=lambda c: (c[0], c[1]))
    if not candidates:
        raise ValueError('所选门店当前没有可自动选择的完整自提时段；未继续结账，未提交订单。')
    index = 0 if choose_slot is None else choose_slot('选择自提日期与时段', [
        (i, c[0] + ' ' + str(c[3].get('Label') or c[3]['timeSlotValue'])) for i, c in enumerate(candidates)], 0)
    iso, _, day, chosen = candidates[index]
    prefix = path + '.dateTimeSlots.'
    fields = {'date': iso, 'dayRadio': str(day['dayOfMonth']),
              'timeSlotType': chosen.get('timeSlotType') or '', 'timeSlotValue': chosen['timeSlotValue'],
              'startTime': chosen['checkInStart'], 'endTime': chosen['checkInEnd'],
              'signKey': chosen['signKey'], 'timeZone': chosen['timeZone'], 'timeSlotId': chosen['SlotId'],
              'displayStartTime': chosen.get('displayStart') or '', 'displayEndTime': chosen.get('displayEnd') or '',
              'isRecommended': 'true' if chosen.get('recommendationLabel') else 'false',
              'isRestricted': 'false'}
    interaction.report_stage('选择自提时段：' + iso + ' ' + str(chosen.get('Label') or chosen['timeSlotValue']))
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
    node = field(flow.model, path)
    if node is None or field(node, 'd.enabled') is False:
        return {}
    if not isinstance(node, dict) or field(node, 'd.enabled') is not True:
        raise ValueError('官网取货证件校验字段不完整，未提交联系人。')
    last4 = config.get('pickup', {}).get('id_last4', '')
    if not last4:
        existing = field(node, 'd.nationalIdSelf')
        binding = field(node, 'b.nationalIdSelf', {})
        if (isinstance(existing, str) and len(existing) == 4 and set(existing) == {'•'}
                and binding.get('masked') is True and isinstance(binding.get('submit'), dict)):
            return {}  # Official binding omits an unchanged, masked saved value.
        raise ValueError('官网要求取货人证件号码后四位；请在“本地配置 → 自提门店与联系人”填写。无需完整证件号，未提交联系人或订单。')
    if not isinstance(last4, str) or not re.fullmatch(r'[A-Za-z0-9]{4}', last4):
        raise ValueError('取货证件号码后四位格式无效；请检查本地自提预设，未提交联系人或订单。')
    return {path + '.nationalIdSelf': last4}


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
