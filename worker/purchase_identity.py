"""Private, runtime-keyed proof of an actually verified HTTP account session.

No account result, maintenance status or file timestamp can establish identity.
Only an official account response issues proof. HTTP lineage can rotate its
cookie fingerprint without extending the original five minute lifetime.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urljoin, urlsplit

TTL_MS = 300_000
_PROOF_FILE = '.purchase-identity.json'
_ATTRIBUTE = '_duo_purchase_identity_lineage'
_MERCHANT = re.compile(r'(?:www|secure\d*\.www)\.apple\.com\.cn\Z', re.I)
_SOURCES = frozenset(('account_http', 'account_browser'))
_PROOF_LOCK = threading.RLock()


@dataclass
class _Context:
    path: Path
    key: bytes
    binding: dict


@dataclass
class _Lineage:
    context: _Context
    proof: dict
    observed: str
    user_agent: str
    token: object = None


@dataclass
class _Request:
    lineage: _Lineage
    url: str


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _context(service=None, auth_path=None):
    if service is None:
        import interaction
        service=interaction.handler
    key=getattr(service, '_identity_proof_key', None)
    if (not isinstance(key,str) or re.fullmatch(r'[0-9a-fA-F]{64}',key) is None
            or getattr(service,'_parallel_child',False) is True):
        return None
    account=getattr(service,'account',None)
    account_id=getattr(getattr(service,'channel',None),'account_id',None)
    auth=getattr(service,'auth',None);root=getattr(service,'root',None)
    if (not isinstance(account,str) or not account.strip() or len(account)>320
            or not isinstance(account_id,str) or not 1<=len(account_id)<=100
            or not isinstance(auth,(str,Path)) or not isinstance(root,(str,Path))):
        return None
    import duo_killer
    from apple_login import browser_profile_path
    destination=Path(auth_path) if auth_path is not None else Path(duo_killer.AUTH_PATH)
    if destination.resolve()!=Path(auth).resolve() or Path(auth).parent.resolve()!=Path(root).resolve():
        return None
    binding={'account_id':account_id,
        'account_sha256':hashlib.sha256(account.strip().casefold().encode()).hexdigest(),
        'auth_path':str(destination.resolve()),'profile_path':str(browser_profile_path(destination).resolve())}
    return _Context(Path(root)/_PROOF_FILE,bytes.fromhex(key),binding)


def _read(path, maximum=2_097_152):
    with Path(path).open('rb') as stream:
        value=stream.read(maximum+1)
    if len(value)>maximum:raise ValueError('Private identity document exceeds its bound')
    return json.loads(value)


def _snapshot(auth):
    if not isinstance(auth,dict) or not isinstance(auth.get('cookies'),dict):return None
    headers=auth.get('headers',{})
    if not isinstance(headers,dict):return None
    agent=next((v for k,v in headers.items() if isinstance(k,str) and k.lower()=='user-agent'),None)
    if not isinstance(agent,str) or not agent or len(agent)>4096:return None
    selected={};logged_in=False
    for group in auth['cookies'].values():
        if not isinstance(group,list):return None
        for item in group:
            if not isinstance(item,dict):return None
            domain=item.get('domain');name=item.get('name');path=item.get('path','/')
            value=item.get('value');expiry=item.get('expires')
            if not isinstance(domain,str) or not (domain.lstrip('.').lower()=='apple.com.cn' or domain.lower().endswith('.apple.com.cn')):continue
            if (not isinstance(name,str) or not name or not isinstance(value,str)
                    or not isinstance(path,str) or not path.startswith('/') or len(value)>65536):return None
            if expiry==-1:expiry=None
            if expiry is not None:
                if type(expiry) not in (int,float) or not math.isfinite(expiry):return None
                expiry=int(expiry)
            key=(name,domain.lower(),path)
            if key in selected:return None
            selected[key]={'name':name,'domain':domain.lower(),'path':path,'value':value,'expires':expiry,
                'secure':item.get('secure',True) is True,'httpOnly':item.get('httpOnly') is True,
                'sameSite':item.get('sameSite') if item.get('sameSite') in ('Strict','Lax','None') else None}
            if (name=='as_ltn_cn' and value and domain.lstrip('.').lower()=='apple.com.cn'
                    and path=='/' and (expiry is None or expiry>time.time())):logged_in=True
    if not logged_in:return None
    cookies=[selected[key] for key in sorted(selected)]
    digest=hashlib.sha256(_json({'cookies':cookies,'user_agent':agent})).hexdigest()
    return digest,hashlib.sha256(agent.encode()).hexdigest()


def _session_auth(session):
    rows=[]
    for item in session.cookies:
        rows.append({'name':item.name,'value':item.value,'domain':item.domain,'path':item.path,
            'expires':item.expires,'secure':item.secure,'httpOnly':item.has_nonstandard_attr('HttpOnly'),
            'sameSite':item.get_nonstandard_attr('SameSite')})
    return {'headers':dict(session.headers),'cookies':{'session':rows}}


def _auth_matches(context, auth):
    account=auth.get('account') if isinstance(auth,dict) else None
    return (isinstance(account,str) and hashlib.sha256(account.strip().casefold().encode()).hexdigest()
        ==context.binding['account_sha256'])


def _seal(context, proof):
    data={key:value for key,value in proof.items() if key!='mac'}
    return {**data,'mac':hmac.new(context.key,_json(data),hashlib.sha256).hexdigest()}


def _valid(context, proof, *, now_ms=None, monotonic_ns=None):
    if not isinstance(proof,dict):return False
    signature=proof.get('mac')
    if not isinstance(signature,str) or not hmac.compare_digest(signature,_seal(context,proof)['mac']):return False
    if (proof.get('v')!=1 or proof.get('source') not in _SOURCES or proof.get('ttl_ms')!=TTL_MS
            or any(proof.get(key)!=value for key,value in context.binding.items())):return False
    wall=int(time.time()*1000) if now_ms is None else now_ms
    monotonic=time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    issued=proof.get('verified_at_ms');tick=proof.get('verified_monotonic_ns')
    return (type(wall) is int and type(monotonic) is int and type(issued) is int and type(tick) is int
        and 0<=wall-issued<TTL_MS and 0<=monotonic-tick<TTL_MS*1_000_000)


def _write(context, proof):
    from duo_killer import write_private_json
    with _PROOF_LOCK:
        write_private_json(context.path,proof)


def _discard_matching(context, proof):
    with _PROOF_LOCK:
        current=_read(context.path,65_536)
        if (_valid(context,current) and isinstance(proof,dict)
                and hmac.compare_digest(current.get('mac',''),proof.get('mac',''))):
            context.path.unlink(missing_ok=True)


def identity_ready(service, *, now_ms=None, monotonic_ns=None):
    try:
        context=_context(service)
        if context is None:return False
        proof=_read(context.path,65_536)
        auth=_read(context.binding['auth_path']);snapshot=_snapshot(auth)
        if not _valid(context,proof,now_ms=now_ms,monotonic_ns=monotonic_ns):return False
        ready=(_auth_matches(context,auth) and snapshot is not None
            and proof.get('snapshot_sha256')==snapshot[0] and proof.get('user_agent_sha256')==snapshot[1])
        if not ready:_discard_matching(context,proof)
        return ready
    except (OSError,ValueError,TypeError,AttributeError):return False


def record_verified_identity(service, result, session=None, source='account_http'):
    try:
        context=_context(service)
        if context is None:return False
        if (session is None or source not in _SOURCES or not isinstance(result,dict) or result.get('success') is not True
                or result.get('logged_in') is not True or result.get('scope')!='account'
                or not isinstance(result.get('apple_id'),str)
                or hashlib.sha256(result['apple_id'].strip().casefold().encode()).hexdigest()!=context.binding['account_sha256']):
            invalidate_identity(service,session=session)
            return False
        auth=_session_auth(session)
        snapshot=_snapshot(auth)
        if snapshot is None:return False
        proof=_seal(context,{'v':1,**context.binding,'source':source,'generation':secrets.token_hex(16),
            'verified_at_ms':int(time.time()*1000),'verified_monotonic_ns':time.monotonic_ns(),
            'ttl_ms':TTL_MS,'snapshot_sha256':snapshot[0],'user_agent_sha256':snapshot[1]})
        _write(context,proof)
        if session is not None:setattr(session,_ATTRIBUTE,_Lineage(context,proof,snapshot[0],snapshot[1]))
        return True
    except (OSError,ValueError,TypeError,AttributeError):return False


def attach_session_identity(session, data, *, auth_path=None, service=None):
    try:
        setattr(session,_ATTRIBUTE,None)
        context=_context(service,auth_path)
        if context is None:return False
        proof=_read(context.path,65_536);snapshot=_snapshot(data)
        actual=_snapshot(_session_auth(session))
        if (not _auth_matches(context,data) or snapshot is None or actual!=snapshot or not _valid(context,proof)
                or proof.get('snapshot_sha256')!=snapshot[0] or proof.get('user_agent_sha256')!=snapshot[1]):return False
        setattr(session,_ATTRIBUTE,_Lineage(context,proof,snapshot[0],snapshot[1]))
        return True
    except (OSError,ValueError,TypeError,AttributeError):return False


def invalidate_identity(service=None, session=None):
    try:
        lineage=getattr(session,_ATTRIBUTE,None) if session is not None else None
        # A public/untracked Session cannot revoke the current owner's proof.
        # Explicit service invalidation denotes a current authentication event.
        explicit=service is not None or session is None
        context=_context(service) if explicit else None
        if isinstance(lineage,_Lineage):
            setattr(session,_ATTRIBUTE,None)
            if not explicit:
                # A late response belongs only to the generation it loaded.
                # It must not revoke a newer account confirmation's proof.
                _discard_matching(lineage.context,lineage.proof)
                return
        if context is not None:
            with _PROOF_LOCK:context.path.unlink(missing_ok=True)
    except (OSError,ValueError,TypeError,AttributeError):pass


def _official(url):
    parsed=urlsplit(url)
    return (parsed.scheme=='https' and _MERCHANT.fullmatch(parsed.hostname or '') is not None
        and not parsed.username and not parsed.password and parsed.port in (None,443)
        and not parsed.path.lower().startswith('/shop/signin'))


def begin_session_request(session, url):
    try:
        lineage=getattr(session,_ATTRIBUTE,None)
        if not isinstance(lineage,_Lineage):return None
        snapshot=_snapshot(_session_auth(session))
        if (lineage.token is not None or not _valid(lineage.context,lineage.proof) or snapshot!=(lineage.observed,lineage.user_agent)
                or not _official(url)):
            invalidate_identity(session=session);return None
        token=_Request(lineage,url);lineage.token=token
        return token
    except (OSError,ValueError,TypeError,AttributeError):
        invalidate_identity(session=session);return None


def complete_session_request(session, response, token):
    if not isinstance(token,_Request):return False
    try:
        lineage=getattr(session,_ATTRIBUTE,None)
        if lineage is not token.lineage:return False
        if lineage.token is not token:
            invalidate_identity(session=session);return False
        replies=[*response.history,response]
        for reply in replies:
            if (type(reply.status_code) is not int or not 200<=reply.status_code<400
                    or not isinstance(reply.url,str) or not _official(reply.url)
                    or reply.headers.get('Location') and not _official(urljoin(reply.url,reply.headers['Location']))):
                invalidate_identity(session=session);return False
        snapshot=_snapshot(_session_auth(session))
        if snapshot is None or snapshot[1]!=lineage.user_agent:
            invalidate_identity(session=session);return False
        lineage.observed=snapshot[0];lineage.token=None
        return True
    except (OSError,ValueError,TypeError,AttributeError):
        invalidate_identity(session=session);return False


def advance_session_identity(session, *, auth_path=None, service=None):
    with _PROOF_LOCK:
        return _advance_session_identity(session,auth_path=auth_path,service=service)


def _advance_session_identity(session, *, auth_path=None, service=None):
    try:
        context=_context(service,auth_path)
        if context is None:return False
        auth=_read(context.binding['auth_path']);snapshot=_snapshot(auth)
        actual=_snapshot(_session_auth(session))
        lineage=getattr(session,_ATTRIBUTE,None)
        stored=_read(context.path,65_536)
        if not isinstance(lineage,_Lineage):
            if (_auth_matches(context,auth) and snapshot is not None and _valid(context,stored)
                    and stored.get('snapshot_sha256')==snapshot[0]):return True
            return False
        if (not _auth_matches(context,auth) or snapshot is None or snapshot!=actual or snapshot!=(lineage.observed,lineage.user_agent)
                or lineage.token is not None or lineage.context.binding!=context.binding
                or not _valid(context,stored) or not _valid(context,lineage.proof)
                or not hmac.compare_digest(stored.get('mac',''),lineage.proof.get('mac',''))):
            invalidate_identity(session=session);return False
        advanced=_seal(context,{**stored,'snapshot_sha256':snapshot[0]})
        _write(context,advanced);lineage.proof=advanced
        return True
    except (OSError,ValueError,TypeError,AttributeError):
        invalidate_identity(service,session=session);return False
