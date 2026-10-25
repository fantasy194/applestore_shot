"""An in-memory checkout lease owned by one serial account queue, never disk."""
from contextlib import contextmanager
import time

import requests


def owner_for(service):
    owner = getattr(service, '_live_checkout_owner', None)
    return owner if owner is not None and getattr(owner, '_live_checkout_enabled', False) is True else None


def discard(owner):
    entry = getattr(owner, '_live_checkout', None)
    owner._live_checkout = None
    if isinstance(entry, dict):
        entry['flow'].session.close()


@contextmanager
def purchase_flow(service, selection, config_hash):
    owner = owner_for(service)
    entry = getattr(owner, '_live_checkout', None) if owner is not None else None
    flow = None
    if isinstance(entry, dict):
        valid = (entry.get('selection') == selection and entry.get('config_hash') == config_hash
                 and 0 <= time.monotonic() - entry.get('created', 0) < 300
                 and entry.get('account_id') == service.channel.account_id)
        if valid:
            flow = entry['flow']
            owner._live_checkout = None  # Consume before any new request.
        else:
            discard(owner)
    session = flow.session if flow is not None else requests.Session()
    try:
        yield session, flow
    finally:
        kept = getattr(owner, '_live_checkout', None) if owner is not None else None
        if not isinstance(kept, dict) or kept['flow'].session is not session:
            session.close()


def retain_unavailable(service, flow, selection, config_hash, error):
    from pickup_options import PickupUnavailable
    from duo_killer import field
    owner = owner_for(service)
    if (owner is None or not isinstance(error, PickupUnavailable)
            or getattr(service, 'submitted', False) is True
            or getattr(service, 'authentication_failed', False) is True
            or getattr(service, 'purchase_auth_interaction', False) is True
            or getattr(flow, '_continuation_attempted', False) is True
            or field(flow.model, 'checkout.d.page') != 'Fulfillment'):
        return False
    # The old assignment remains failed. This lease can only be consumed by a
    # newly authorized, separately acknowledged target through purchase_sku.
    owner._live_checkout = {'flow': flow, 'selection': selection, 'config_hash': config_hash,
                            'created': time.monotonic(), 'account_id': service.channel.account_id}
    return True
