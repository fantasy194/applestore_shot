"""Experimental minute-scale store query on the original authenticated Checkout.

This does not reserve inventory or guarantee a longer server-side checkout TTL.
It has no relationship to the anonymous inventory monitor or its proxy pool.
"""
from contextlib import contextmanager
from time import monotonic
from urllib.parse import urljoin, urlsplit
import re


LOCATOR='checkout.fulfillment.pickupTab.pickup.storeLocator'


@contextmanager
def _direct(session):
    trust_env,proxies=session.trust_env,session.proxies
    session.trust_env=False
    session.proxies={}
    try:
        yield
    finally:
        session.trust_env=trust_env
        session.proxies=proxies


def _metadata(state,reason,*,stage=None,interval_seconds=300):
    return {'state':state,'reason':reason,'experimental':True,'stage_proven':stage,
            'order_ready':False,'interval_seconds':interval_seconds}


def _scope(flow,action):
    try:
        source=urlsplit(flow.url);target=urlsplit(urljoin(flow.url,action))
        return (source.scheme==target.scheme=='https'
            and source.hostname==target.hostname
            and re.fullmatch(r'secure\d*\.www\.apple\.com\.cn',source.hostname or '') is not None
            and not source.username and not source.password and not target.username and not target.password
            and source.port in (None,443) and target.port in (None,443)
            and source.path in ('/shop/checkout','/shop/checkout/start')
            and target.path=='/shop/checkout')
    except (ValueError,TypeError):
        return False


def refresh_prepared_inventory(flow,expected,config):
    """One original-session search per configured interval, before store selection.

    A successful incremental response can omit checkout.d.page. In that case
    stage_proven records only the prior freshly verified Fulfillment scope plus
    this request's new locator. The raw response is never amended or fabricated.
    Errors propagate once; no authentication, bag rebuild or query replay occurs.
    """
    from duo_killer import field,checkout_physical_items,normalize_sku
    from checkout_options import read_config
    from pickup_options import validate_preset,query_pickup_stores,search_results,PickupUnavailable,store_name
    from session_maintenance import MaintenanceFailure
    import interaction

    interval=getattr(interaction.handler,'prepared_inventory_interval_minutes',5)
    if type(interval) is not int or not 1<=interval<=60:
        raise ValueError('准备内库存查询间隔必须是 1–60 的整数分钟。')
    interval_seconds=interval*60
    def metadata(state,reason,*,stage=None):
        return _metadata(state,reason,stage=stage,interval_seconds=interval_seconds)
    def skipped(reason):return metadata('skipped',reason)
    body=getattr(flow,'current_response_body',None)
    if field(body,'checkout.d.page')!='Fulfillment':return skipped('not_fulfillment')
    if (getattr(interaction.handler,'submitted',False) is True
            or getattr(flow,'_final_submit_attempted',False) is True):return skipped('submission_started')
    if field(body,'checkout.fulfillment.fulfillmentOptions.d.selectFulfillmentLocation')!='RETAIL':
        return skipped('not_pickup')
    node=field(body,LOCATOR)
    if not isinstance(node,dict) or not isinstance(node.get('d'),dict):return skipped('no_current_locator')
    if (node['d'].get('selectStore') not in ('',None)
            or field(flow.model,LOCATOR+'.d.selectStore') not in ('',None)):
        return skipped('store_already_selected')
    try:
        wanted=sorted((normalize_sku(sku),quantity) for sku,quantity in expected)
        if not wanted or any(type(quantity) is not int or quantity<1 for _,quantity in wanted):
            return skipped('no_current_item_evidence')
        found=False
        for path in ('checkout.fulfillment','checkout.companionBar.orderDetails'):
            items=checkout_physical_items(field(body,path,{}))
            if items and items!=wanted:return skipped('item_evidence_mismatch')
            found=found or bool(items)
        if not found:
            # A real current bag check may have been needed because the fresh
            # Fulfillment document omitted products. Only its same-flow,
            # same-body, short-lived private receipt can supply that evidence.
            from duo_killer import prepared_items_verified_for
            if not prepared_items_verified_for(flow,expected):return skipped('no_current_item_evidence')
    except (ValueError,TypeError,KeyError):return skipped('no_current_item_evidence')
    action=field(node,'a.search.url')
    if (not isinstance(action,str) or not action or action!=field(flow.model,LOCATOR+'.a.search.url')
            or not _scope(flow,action)):return skipped('no_current_search_action')
    raw_headers=field(body,'meta.h',{})
    tokens=[value for key,value in raw_headers.items() if isinstance(key,str) and key.lower()=='x-aos-stk'] if isinstance(raw_headers,dict) else []
    token=tokens[0] if len(tokens)==1 else None
    if not isinstance(token,str) or not token or token!=flow.headers.get('x-aos-stk'):
        return skipped('no_current_stk')
    session=getattr(flow,'session',None)
    if not hasattr(session,'trust_env') or not hasattr(session,'proxies'):return skipped('no_authenticated_session')
    settings=read_config(config) if not isinstance(config,dict) else config
    policy=validate_preset(settings,require_contact=False)['pickup']
    preferred=(policy['stores'][0] if policy.get('selection_mode')=='priority' else policy)
    stamp=monotonic();previous=getattr(flow,'_prepared_inventory_queried_at',None)
    if type(previous) not in (int,float):
        # A newly created waiting checkout without a prior actual search still
        # waits one full configured interval before its first experimental query.
        flow._prepared_inventory_queried_at=stamp
        return skipped('minimum_interval')
    if stamp-previous<interval_seconds:return skipped('minimum_interval')
    channel=getattr(interaction.handler,'channel',None)
    pending=getattr(channel,'prepared_claim_waiting',None)
    if callable(pending) and pending() is True:return skipped('purchase_waiting')
    flow._prepared_inventory_queried_at=stamp
    with _direct(session):
        query_pickup_stores(flow,preferred['search'])
    current=getattr(flow,'current_response_body',None)
    fresh=field(current,LOCATOR)
    page=field(current,'checkout.d.page')
    if (current is body or not isinstance(fresh,dict) or page not in (None,'Fulfillment')
            or not _scope(flow,action)):
        raise MaintenanceFailure('PREPARED_RENEWAL_UNVERIFIED',
            '本次门店查询未返回可核验的原结算结果，已停止续期；未提交订单。',reason='checkout_response_unverified')
    for path in ('checkout.fulfillment','checkout.companionBar.orderDetails'):
        items=checkout_physical_items(field(current,path,{}))
        if items and items!=wanted:
            raise MaintenanceFailure('PREPARED_RENEWAL_UNVERIFIED',
                '本次门店查询商品与原准备不一致，已停止续期；未提交订单。',reason='goods_changed')
    try:
        stores=search_results(fresh)
    except PickupUnavailable:
        return metadata('unavailable','no_available_authorized_store',stage='Fulfillment')
    if preferred.get('store_id') and any(isinstance(row,dict)
            and row.get('storeId')==preferred['store_id']
            and store_name(row.get('storeName'))!=store_name(preferred['store_name']) for row in stores):
        raise MaintenanceFailure('PREPARED_RENEWAL_UNVERIFIED',
            '本次门店查询身份与原准备不一致，已停止续期；未提交订单。',reason='checkout_response_unverified')
    matches=[row for row in stores if isinstance(row,dict)
             and store_name(row.get('storeName'))==store_name(preferred['store_name'])
             and (not preferred.get('store_id') or row.get('storeId')==preferred['store_id'])]
    if not matches or len(matches)==1 and matches[0].get('storeDisabled') is True:
        return metadata('unavailable','no_available_authorized_store',stage='Fulfillment')
    if (len(matches)!=1 or matches[0].get('storeDisabled') is not False
            or re.fullmatch(r'R\d{1,8}',str(matches[0].get('storeId',''))) is None):
        raise MaintenanceFailure('PREPARED_RENEWAL_UNVERIFIED',
            '本次门店查询状态尚未确认，已停止续期；未提交订单。',reason='checkout_response_unverified')
    return metadata('available','query_completed',stage='Fulfillment')
