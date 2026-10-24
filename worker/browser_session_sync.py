"""Account-local three-way session bridge; checkpoints contain hashes, not cookies.

An HTTP snapshot is not automatically newer than a persistent browser. Replace
only a changed HTTP key whose browser value is still the last observed value.
Verified browser export/probe pairs provide an explicit common source instead.
"""
import hashlib
import json
import math
import re
import time

SESSION_NAMES = frozenset(('as_ltn_cn', 'as_cn', 'as_disa', 'as_rec', 'myacinfo', 'acn01'))
MERCHANT = re.compile(r'(?:apple\.com\.cn|www\.apple\.com\.cn|secure\d*\.www\.apple\.com\.cn)\Z')


def _rows(rows):
    selected = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, domain, path = row.get('name'), row.get('domain'), row.get('path', '/')
        if (name not in SESSION_NAMES or not isinstance(domain, str)
                or not MERCHANT.fullmatch(domain.lstrip('.').lower())
                or not isinstance(path, str) or not path.startswith('/')
                or any(ord(c) < 32 for c in domain + path)):
            continue
        expiry = row.get('expires')
        if expiry == -1:
            expiry = None
        if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time()):
            continue
        value = row.get('value')
        if not isinstance(value, str) or not value or len(value) > 65536:
            continue
        # CDP exposes integral expiry seconds as floats; requests stores ints.
        # Their JSON spelling is not a cookie rotation. Match at HTTP precision.
        clean = dict(row, name=name, domain=domain.lower(), path=path,
                     expires=int(expiry) if expiry is not None else None)
        selected[json.dumps([name, clean['domain'], path], separators=(',', ':'))] = clean
    return selected


def _digest(row):
    if row is None:
        return None
    # Attributes are retained separately; legacy HTTP snapshots did not preserve
    # HttpOnly/SameSite, so their absence cannot assert that Chrome changed.
    core = {key: row.get(key) for key in ('name', 'domain', 'path', 'value', 'expires')}
    core['secure'] = row.get('secure', True) is True
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _entry(source, browser):
    result = {key: source[key] for key in ('name', 'value', 'domain', 'path')}
    result['secure'] = source.get('secure', True) is True
    for name in ('httpOnly', 'sameSite'):
        value = source.get(name, (browser or {}).get(name))
        if name == 'httpOnly' and type(value) is bool:
            result[name] = value
        elif name == 'sameSite' and value in ('Strict', 'Lax', 'None'):
            result[name] = value
    if source['expires'] is not None:
        result['expires'] = source['expires']
    return result


def sync_browser_session(context, service, *, verified_export=None):
    """Synchronize exact merchant session keys without launching a browser.

    Returns only bounded counts. Read/sync failure preserves the original state;
    independent browser changes remain pending until explicitly verified.
    """
    report = {'applied': 0, 'removed': 0, 'conflicts': 0}
    if getattr(service, '_http_session_rejected', False) is True and verified_export is None:
        return {**report, 'skipped_rejected': True}
    try:
        saved = json.loads(service.auth.read_text(encoding='utf-8'))
        account = service.account.strip().casefold()
        if (not account or str(saved.get('account', '')).strip().casefold() != account
                or not isinstance(saved.get('cookies'), dict)):
            return report
        source = _rows([row for rows in saved['cookies'].values() if isinstance(rows, list) for row in rows])
        browser = _rows(context.cookies())
        exported = _rows(verified_export) if isinstance(verified_export, list) else None
        path = service.auth.parent / '.browser-session-sync.json'
        previous = {}
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if previous.get('v') != 1 or previous.get('account') != account:
            previous = {}
        old_source, old_browser = previous.get('source', {}), previous.get('browser', {})
        if not isinstance(old_source, dict) or not isinstance(old_browser, dict):
            old_source, old_browser = {}, {}
        initial = not previous
        next_source, next_browser = dict(old_source), dict(old_browser)
        keys = set(source) | set(old_source)
        if exported is not None:
            keys |= set(exported)
        for key in keys:
            incoming, current = source.get(key), browser.get(key)
            source_hash, browser_hash = _digest(incoming), _digest(current)
            verified = (exported is not None and browser_hash == _digest(exported.get(key))
                        and (key in exported or incoming is not None))
            if exported is not None and key in exported and not verified:
                report['conflicts'] += 1
                continue
            changed = source_hash != old_source.get(key)
            if initial and not verified:
                # First contact cannot determine which independently persisted
                # session is newer. Preserve every existing browser value.
                may_apply = current is None and incoming is not None
            else:
                may_apply = verified or changed and browser_hash == old_browser.get(key)
            if may_apply and source_hash != browser_hash:
                if incoming is not None:
                    context.add_cookies([_entry(incoming, current)])
                    browser_hash = source_hash
                    report['applied'] += 1
                elif current is not None:
                    context.clear_cookies(name=current['name'], domain=current['domain'], path=current['path'])
                    browser_hash = None
                    report['removed'] += 1
            elif changed and not initial and not verified and source_hash != browser_hash:
                # Do not consume the update, or advance the browser baseline:
                # either would let a later poll overwrite its independent value.
                report['conflicts'] += 1
                continue
            if initial or verified or may_apply or source_hash == browser_hash:
                if source_hash is None:
                    next_source.pop(key, None)
                else:
                    next_source[key] = source_hash
                if browser_hash is None:
                    next_browser.pop(key, None)
                else:
                    next_browser[key] = browser_hash
        if initial:
            for key, row in browser.items():
                next_browser.setdefault(key, _digest(row))
        from duo_killer import write_private_json
        write_private_json(path, {'v': 1, 'account': account, 'source': next_source, 'browser': next_browser})
    except Exception:
        # Browser/IO failure must not replace auth.json or commit a false baseline.
        # Already applied exact keys are harmless to retry from the old checkpoint.
        report['failed'] = True
    return report
