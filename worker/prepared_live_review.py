"""Single-use evidence for the original, unchanged in-memory pickup Review.

Nothing here is persisted or accepted from task parameters. The final order
still uses the ordinary Review validation and one official submission.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import hashlib
import json
import re
from time import monotonic, time as wall_time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
import interaction


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        return hashlib.sha256(raw.encode('utf-8')).digest()
    except (ValueError, TypeError, OverflowError):
        return None


def _session_digest(session):
    if not isinstance(session, requests.Session):
        return None
    try:
        now = wall_time()
        cookies = sorted((c.domain, c.path, c.name, c.value, c.expires, c.secure,
                          c.is_expired(now=now), sorted(c._rest.items())) for c in session.cookies)
        headers = sorted((str(k).lower(), str(v)) for k, v in session.headers.items())
        return _digest([cookies, headers, session.trust_env, sorted(session.proxies.items())])
    except (AttributeError, TypeError, ValueError):
        return None


def _identity(handler):
    channel = getattr(handler, 'channel', None)
    account, task = getattr(channel, 'account_id', None), getattr(channel, 'task_id', None)
    if not all(isinstance(v, str) and v for v in (account, task)):
        return None
    return (account, task)


def _selected_store(handler):
    selected = getattr(handler, 'pickup_selected', None)
    if not isinstance(selected, dict) or selected.get('storeDisabled') is not False:
        return None
    store_id, name = selected.get('storeId'), selected.get('storeName')
    if (not isinstance(store_id, str) or not re.fullmatch(r'R\d{1,8}', store_id)
            or not isinstance(name, str) or not name.strip()):
        return None
    return {'storeId': store_id, 'storeName': name, 'storeDisabled': False}


def record_accepted_selection(flow):
    """Called only after the official pickup continue advanced successfully."""
    from duo_killer import field
    handler = interaction.handler
    chosen, owner = _selected_store(handler), _identity(handler)
    page = field(flow.model, 'checkout.d.page')
    section = {'PickupContact':'pickupContact', 'Billing':'billing', 'Review':'review'}.get(page)
    if (not chosen or not owner or not isinstance(flow.session, requests.Session)
            or not section or not isinstance(field(flow.model, 'checkout.' + section), dict)
            or getattr(handler, 'submitted', False) is True
            or getattr(flow, '_unverified_checkout_action', False) is True):
        flow._accepted_live_pickup_selection = None
        return
    flow._accepted_live_pickup_selection = {
        'flow': flow, 'session': flow.session, 'handler': handler, 'owner': owner,
        'selection': chosen, 'slot': deepcopy(getattr(handler, 'pickup_slot_selected', None)),
        'generation': getattr(flow, '_expired_checkout_recovery_generation', 0),
    }


def _slot_valid(slot, store_id):
    if slot is None:
        return True
    from pickup_options import pickup_calendar_now
    if not isinstance(slot, dict) or slot.get('store_id') != store_id:
        return False
    try:
        day = date.fromisoformat(slot['date'])
        start, end = time.fromisoformat(slot['start']), time.fromisoformat(slot['end'])
        now = pickup_calendar_now().astimezone(ZoneInfo('Asia/Shanghai'))
        return (now.date() <= day <= now.date() + timedelta(days=6) and start < end
                and datetime.combine(day, end, ZoneInfo('Asia/Shanghai')) > now)
    except (KeyError, ValueError, TypeError):
        return False


def _official_review_operation(flow):
    from duo_killer import field
    action = field(flow.model, 'checkout.review.placeOrder.a.continueFromReviewToProcess.url')
    token = flow.headers.get('x-aos-stk') if isinstance(flow.headers, dict) else None
    if not isinstance(action, str) or not action or not isinstance(token, str) or not token:
        return False
    try:
        base, target = urlsplit(flow.url), urlsplit(flow.apple_url(action))
        return (base.scheme == 'https' and re.fullmatch(r'secure\d*\.www\.apple\.com\.cn', base.hostname or '')
                and base.path == '/shop/checkout' and target.scheme == 'https'
                and target.hostname == base.hostname and target.port in (None, 443)
                and not target.username and not target.password
                and (target.path == '/shop/checkoutx' or target.path.startswith('/shop/checkoutx/')))
    except (ValueError, AttributeError):
        return False


def _state_digest(flow):
    return _digest({'model': flow.model, 'response': flow.current_response_body,
                    'headers': flow.headers, 'url': flow.url})


def mark_review_confirmed(flow):
    """Anchor both clocks to the original successful Review confirmation."""
    flow._prepared_review_confirmed_clocks = (wall_time(), monotonic())


def capture(flow, expected, approved, config_file_sha256, result, pickup):
    """Freeze one fully verified Review after this flow's successful selection."""
    from duo_killer import (active_checkout_review, checkout_physical_items, field,
                            normalize_sku, prepared_amount)
    from pickup_options import official_review_proof, review_pickup_selection, store_name
    flow._prepared_live_review = None
    handler = interaction.handler
    accepted = getattr(flow, '_accepted_live_pickup_selection', None)
    clocks = getattr(flow, '_prepared_review_confirmed_clocks', None)
    chosen, owner = _selected_store(handler), _identity(handler)
    if (result.get('status') != 'prepared' or result.get('review_verified') is not True
            or result.get('order_ready') is not True or not isinstance(accepted, dict)
            or accepted.get('flow') is not flow or accepted.get('session') is not flow.session
            or accepted.get('handler') is not handler or accepted.get('owner') != owner
            or accepted.get('generation') != getattr(flow, '_expired_checkout_recovery_generation', 0)
            or accepted.get('selection') != chosen or not isinstance(approved, dict)
            or accepted.get('slot') != getattr(handler, 'pickup_slot_selected', None)
            or not _slot_valid(accepted.get('slot'), chosen['storeId'] if chosen else None)
            or getattr(flow, '_unverified_checkout_action', False) is True
            or not isinstance(clocks, tuple) or len(clocks) != 2
            or not all(type(value) in (int, float) for value in clocks)
            or not 0 <= wall_time() - clocks[0] <= 300 or not 0 <= monotonic() - clocks[1] <= 300
            or not active_checkout_review(flow) or field(flow.model, 'checkout.d.page') != 'Review'
            or not _official_review_operation(flow)):
        return False
    if (approved.get('contains_delivery') is not False or approved.get('address')
            or approved.get('store_id') != chosen['storeId']
            or store_name(approved.get('store')) != store_name(chosen['storeName'])
            or approved.get('pickup_time_slot') != accepted.get('slot')
            or not review_pickup_selection(flow.model, chosen)):
        return False
    proof = getattr(handler, 'pickup_contact_proof', None)
    if (getattr(handler, 'pickup_contact_accepted', False) is not True
            or getattr(handler, 'pickup_contact_review_verified', False) is not True
            or not isinstance(proof, dict) or not proof):
        return False
    accepted_contact = all(isinstance(v, dict) and v.get('server_accepted') is True for v in proof.values())
    official_contact = official_review_proof(flow.current_response_body)
    if not accepted_contact and (official_contact is None or official_contact != proof):
        return False
    wanted = sorted((normalize_sku(sku), qty) for sku, qty in expected)
    review = field(flow.model, 'checkout.review', {})
    total = field(review, 'summary.d', {})
    total = (total.get('totalAfterTradeIn') if total.get('showTotalAfterTradeinAsTotal')
             else field(total, 'installmentTotal.value') or total.get('total'))
    try:
        if (not wanted or checkout_physical_items(review.get('fulfillmentReview', {})) != wanted
                or prepared_amount(total) is None or prepared_amount(total) != prepared_amount(approved.get('total'))
                or field(review, 'billingReview.alipayReview.d.alipayLabel') != 'ALIPAY'
                or field(review, 'placeOrder.d.enableContinue') is not True):
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    state, session = _state_digest(flow), _session_digest(flow.session)
    private = _digest([approved, chosen, proof, getattr(handler, 'pickup_identity_proof', None)])
    pickup_policy = _digest(pickup)
    if state is None or session is None or private is None or pickup_policy is None or not isinstance(config_file_sha256, str):
        return False
    flow._prepared_live_review = {
        'flow': flow, 'session': flow.session, 'handler': handler, 'owner': owner,
        'accepted': accepted,
        'state': state, 'session_state': session, 'private': private, 'selection': chosen,
        'expected': wanted, 'generation': getattr(flow, '_expired_checkout_recovery_generation', 0),
        'epoch': getattr(flow, '_checkout_activity_epoch', 0), 'config_file_sha256': config_file_sha256,
        'pickup_policy': pickup_policy,
        'created_wall': clocks[0], 'created_mono': clocks[1], 'slot': deepcopy(accepted.get('slot')),
    }
    return True


def consume(flow, expected, approved, config_file_sha256, pickup):
    """Consume the receipt before use; return a public reason, never its values."""
    from duo_killer import normalize_sku
    from pickup_options import allowed_store
    receipt = getattr(flow, '_prepared_live_review', None)
    flow._prepared_live_review = None
    handler = interaction.handler
    if (getattr(handler, 'submitted', False) is True or getattr(flow, '_final_submit_attempted', False) is True
            or getattr(flow, '_expired_checkout_initialization_unverified', False) is True):
        return 'submission_unknown'
    evidence = getattr(flow, '_submission_evidence', None)
    if evidence is not None and evidence.found():
        return 'submission_unknown'
    if getattr(flow, '_unverified_checkout_action', False) is True:
        # A contact/billing POST may already have advanced; the existing fresh
        # page recovery determines its state without repeating that form.
        return 'unverified_action'
    if not isinstance(receipt, dict):
        return 'missing'
    now_wall, now_mono = wall_time(), monotonic()
    if not (0 <= now_wall - receipt['created_wall'] <= 300 and 0 <= now_mono - receipt['created_mono'] <= 300):
        return 'expired'
    if (receipt['flow'] is not flow or receipt['session'] is not flow.session or receipt['handler'] is not handler
            or receipt['owner'] != _identity(handler) or receipt['generation'] != getattr(flow, '_expired_checkout_recovery_generation', 0)
            or receipt['epoch'] != getattr(flow, '_checkout_activity_epoch', 0)
            or receipt['accepted'] is not getattr(flow, '_accepted_live_pickup_selection', None)
            or receipt['config_file_sha256'] != config_file_sha256):
        return 'scope_changed'
    selected = _selected_store(handler)
    if (selected != receipt['selection'] or not allowed_store(pickup, selected.get('storeName'), selected.get('storeId'))
            or pickup.get('selection_mode') == 'priority' and receipt['pickup_policy'] != _digest(pickup)
            and (not pickup.get('stores')
                or not allowed_store(pickup['stores'][0], selected.get('storeName'), selected.get('storeId')))):
        return 'store_changed'
    if (receipt['slot'] != getattr(handler, 'pickup_slot_selected', None)
            or not _slot_valid(receipt['slot'], selected['storeId'])):
        return 'slot_changed'
    if receipt['expected'] != sorted((normalize_sku(sku), qty) for sku, qty in expected):
        return 'goods_changed'
    proof = getattr(handler, 'pickup_contact_proof', None)
    if (receipt['state'] != _state_digest(flow) or receipt['session_state'] != _session_digest(flow.session)
            or receipt['private'] != _digest([approved, selected, proof, getattr(handler, 'pickup_identity_proof', None)])
            or not _official_review_operation(flow)):
        return 'state_changed'
    return 'ready'
