#!/usr/bin/env python3

# 找一下Apple 的bug 😁
"""-k SKU 登录、加购并确认下单；-l 登录；-a [SKU] 加购；-p 确认下单；-purl [URL] 获取付款码。"""
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from html import unescape
from pathlib import Path
from time import perf_counter, sleep, time
from threading import Event, Thread
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

from alipay_qr import get_payment_qr
import interaction
from interaction import text_input as input
from platform_support import private_fd

BASE_URL = "https://www.apple.com.cn"
PART = "mlh74fe/A"  # 与最新成功 HAR 和用户提供的商品页面一致。
DEFAULT_PURCHASE_SKU = "mlh74fe"  #"MK2U4CH"
TIMEOUT = 30
ROOT = Path(__file__).resolve().parent
AUTH_PATH = ROOT / "auth.json"
ACCOUNT_SESSION_COOKIE_NAMES = {"as_ltn_cn", "as_dc", "shld_bt_m"}  # 用户指定的账户会话分组。
# 普通商城请求中已观察到 Set-Cookie 更新；不保证服务器每次都续期。
SERVER_MANAGED_COOKIE_NAMES = {"dssid2", "dssf", "as_atb", "as_pcts"}
LOG_PATH = ROOT / "duo_killer.log"
PAYMENT_ROOT = None
log = logging.getLogger("duo_killer")
log.disabled = True


def resolve_auth_path(value):
    """将命令行指定的认证文件解析为绝对路径，不提前创建文件。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if path.exists() and not path.is_file():
        raise ValueError(f"认证配置路径不是文件：{path}")
    return path


def setup_logging():
    log.disabled = False
    with os.fdopen(os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        private_fd(file.fileno())
    file_handler = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
    terminal_handler = logging.StreamHandler()
    terminal_handler.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[file_handler, terminal_handler])
    log.setLevel(logging.DEBUG)
    log.info("日志：%s（每次运行覆盖）", LOG_PATH)
    log.info("环境：Python %s，requests %s，代理配置项=%s", sys.version.split()[0],
             requests.__version__, sorted(requests.utils.get_environ_proxies(BASE_URL)))


def safe_url(url):
    parsed = urlsplit(url)
    public = {"product", "purchaseOption", "step", "acpart", "igt", "add-to-cart", "_a", "_m", "_s"}
    query = urlencode([(key, value if key in public else "[REDACTED]")
                       for key, value in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, query, ""))


@contextmanager
def waiting(label):
    if not sys.stdout.isatty() or not log.disabled:
        yield
        return
    stopped = Event()

    def animate():
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        index = 0
        while not stopped.is_set():
            sys.stdout.write(f"\r{frames[index % len(frames)]} {label}")
            sys.stdout.flush()
            index += 1
            stopped.wait(0.1)

    worker = Thread(target=animate, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()


def request_get(session, stage, url, method="GET", **kwargs):
    interaction.report_stage(stage)
    requested_url = requests.Request(method, url, params=kwargs.get("params")).prepare().url
    log.info("[%s] 开始 %s %s", stage, method, safe_url(requested_url))
    started = perf_counter()
    try:
        with waiting(stage):
            from checkout_retry import request as retry_request
            path = urlsplit(url).path.rstrip('/')
            read_only = (method.upper() == 'GET' and path in (
                '/shop/bag', '/cn/shop/bag/flyout', '/shop/order/list', '/shop/checkout',
            )) or (method.upper() == 'POST' and stage == 'checkStatus')
            response = retry_request(session, method, url, read_only=read_only, stage=stage, **kwargs)
    except requests.RequestException as error:
        log.error("[%s] %.2fs 网络异常：%s", stage, perf_counter() - started, type(error).__name__)
        raise
    for reply in [*response.history, response]:
        record_cookie_lifetimes(session, reply)
        request = reply.request
        headers = {k: safe_url(v) if k.lower() == "referer" else v for k, v in request.headers.items()
                   if k.lower() in {"accept", "accept-language", "user-agent", "referer",
                                    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"}}
        cookie_names = [c.strip().partition("=")[0]
                        for c in request.headers.get("Cookie", "").split(";") if c.strip()]
        detail = {"url": safe_url(request.url), "request_headers": headers,
                  "sent_cookie_names": cookie_names,
                  "duplicate_cookie_names": sorted({n for n in cookie_names if cookie_names.count(n) > 1}),
                  "response_headers": {k: v for k, v in reply.headers.items() if k.lower() in {
                      "content-type", "content-length", "date", "server", "retry-after",
                      "x-request-id", "x-nxid", "x-apple-request-uuid", "x-shred", "server-timing"}},
                  "updated_cookies": [{"name": c.name, "domain": c.domain, "path": c.path,
                                       "expires": c.expires} for c in reply.cookies]}
        if "Location" in reply.headers:
            detail["location"] = safe_url(reply.headers["Location"])
        if reply.status_code >= 400:
            title = re.search(r"<title[^>]*>(.*?)</title>", reply.text, re.S | re.I)
            title = title[1].strip() if title else None
            detail.update(body_bytes=len(reply.content), body_sha256=hashlib.sha256(reply.content).hexdigest(),
                          error_page=title if title in {"Page Not Found - Apple", "Access Denied"}
                          else "其他页面" if title else "无 HTML 标题")
        log.info("[%s] HTTP %s %s", stage, reply.status_code, safe_url(request.url))
        log.debug("[%s] 请求详情：%s", stage, json.dumps(detail, ensure_ascii=False))
    total_time = perf_counter() - started
    replies = [*response.history, response]
    if all(isinstance(getattr(reply, 'elapsed', None), timedelta) for reply in replies):
        headers_time = sum(reply.elapsed.total_seconds() for reply in replies)
        log.info("[%s] 耗时拆分：连接/发送/等待响应头 %.2fs；下载响应体及本地处理 %.2fs", stage,
                 headers_time, max(0, total_time - headers_time))
        if (method.upper() == 'POST' and headers_time >= 5 and stage in {
                'continueFromShippingToBilling', 'continueFromBillingToReview',
                '交换商城会话凭证', 'continueFromReviewToProcess'}):
            log.warning('[%s] 慢响应：响应头阶段 %.2fs，响应体及本地处理 %.2fs；'
                        '写请求仅发送一次，不因慢响应自动重发。',
                        stage, headers_time, max(0, total_time - headers_time))
    log.info("[%s] 完成，耗时 %.2fs，跳转 %s 次", stage, total_time, len(response.history))
    remove_imported_cookie_duplicates(session)
    return response


def remove_imported_cookie_duplicates(session):
    # 这些 Cookie 最初从 cURL 导入时缺少 Domain，暂记为 www。
    # 成功结账的 Set-Cookie 已确认它们属于 .apple.com.cn；保留服务器版本。
    names = {"as_ltn_cn", "as_loc", "as_uct", "rtsid", "as_pcts"}
    server_names = {c.name for c in session.cookies
                    if c.domain == ".apple.com.cn" and c.path == "/" and c.name in names}
    for cookie in list(session.cookies):
        if cookie.domain == "www.apple.com.cn" and cookie.path == "/" and cookie.name in server_names:
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
            log.debug("[认证] 已移除旧导入副本：%s，保留服务器下发的域作用域。", cookie.name)


def format_duration(seconds):
    days, seconds = divmod(abs(int(seconds)), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return "".join(f"{n}{unit}" for n, unit in
                   [(days, "天"), (hours, "小时"), (minutes, "分"), (seconds, "秒")] if n) or "0秒"


def record_cookie_lifetimes(session, reply):
    # Max-Age 优先；否则用响应的服务器时间计算，不能用保存文件时的时间。
    try:
        issued = parsedate_to_datetime(reply.headers.get("Date", "")).timestamp()
    except (ValueError, TypeError, OverflowError):
        issued = None
    max_ages = {}
    for header in reply.raw.headers.getlist("Set-Cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            if re.fullmatch(r"-?\d+", morsel["max-age"]):
                url = urlsplit(reply.url)
                domain = (morsel["domain"] or url.hostname).lstrip(".").lower()
                path = morsel["path"] or url.path.rpartition("/")[0] or "/"
                max_ages[name, domain, path] = int(morsel["max-age"])
    current = {(c.name, c.domain, c.path): c for c in session.cookies}
    for c in reply.cookies:
        target = current.get((c.name, c.domain, c.path))
        if target is None or (target.value, target.expires) != (c.value, c.expires):
            continue
        seconds = max_ages.get((c.name, c.domain.lstrip("."), c.path))
        if seconds is None and issued is not None and c.expires is not None:
            seconds = c.expires - issued
        target.set_nonstandard_attr("expires_after", format_duration(max(0, seconds))
                                    if seconds is not None else "未知（缺少下发记录）")


def expiry_details(expires, now):
    if expires is None:
        return {}
    remaining = int(expires) - int(now.timestamp())
    return {"expires_at": datetime.fromtimestamp(expires, now.tzinfo).strftime("%Y-%m-%d %H:%M:%S %z"),
            "expires_in": ("剩余 " if remaining > 0 else "已过期 ") + format_duration(remaining)}


def save_auth(session):
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    data = {"headers": {k: v for k, v in session.headers.items()
                        if k.lower() in ("user-agent", "accept-language")},
            "expiry_checked_at": now.strftime("%Y-%m-%d %H:%M:%S %z"),
            "cookies": {"account_session": [], "server_managed": [], "with_expiry": [], "expiry_unknown": []}}
    for c in session.cookies:
        if c.domain.lstrip(".") == "apple.com.cn" or c.domain.endswith(".apple.com.cn"):
            group = ("account_session" if c.name in ACCOUNT_SESSION_COOKIE_NAMES else
                     "server_managed" if c.name in SERVER_MANAGED_COOKIE_NAMES else
                     "with_expiry" if c.expires is not None else "expiry_unknown")
            data["cookies"][group].append({"name": c.name, "value": c.value, "domain": c.domain,
                                          "path": c.path, "secure": c.secure, "expires": c.expires,
                                          **expiry_details(c.expires, now),
                                          **({"expires_after": c.get_nonstandard_attr(
                                              "expires_after", "未知（缺少下发记录）")}
                                             if c.expires is not None else {})})
    existing = json.loads(AUTH_PATH.read_text(encoding="utf-8")) if AUTH_PATH.exists() else {}
    if not isinstance(existing, dict):
        raise ValueError(f"{AUTH_PATH} 必须是 JSON 对象，未覆盖原文件。")
    data = {**existing, **data}  # 保留账号、密码、设备信任及其他配置。
    write_private_json(AUTH_PATH, data)


def load_auth(session, data):
    session.cookies.clear()
    session.headers.update({k: v for k, v in data.get("headers", {}).items()
                            if k.lower() in ("user-agent", "accept-language")})
    cookies = data["cookies"]
    # expiry_unknown 仅表示未记录期限，不代表永久有效。
    for c in (cookies["account_session"] + cookies["server_managed"] +
              cookies["with_expiry"] + cookies["expiry_unknown"]):
        domain = c["domain"]
        if domain.lstrip(".") != "apple.com.cn" and not domain.endswith(".apple.com.cn"):
            continue
        expires = c.get("expires")
        expires = int(expires) if expires is not None else None
        session.cookies.set_cookie(Cookie(
            0, c["name"], c["value"], None, False, domain, domain.startswith("."),
            domain.startswith("."), c.get("path", "/"), True, c.get("secure", True),
            expires, expires is None, None, None,
            {"expires_after": c["expires_after"]} if "expires_after" in c else {}, False))
    remove_imported_cookie_duplicates(session)


def ensure_auth_file():
    if interaction.handler is not None:
        return interaction.handler.ensure_auth_file()
    existing = None
    if AUTH_PATH.exists():
        try:
            existing = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except ValueError:
            return False  # 无效文件仍交给配置校验，不覆盖用户内容。
        if not isinstance(existing, dict):
            return False
        if (isinstance(existing.get("account"), str) and existing["account"].strip()
                and isinstance(existing.get("password"), str) and existing["password"]):
            return False
    try:
        account = ""
        while not account:
            account = input("👉 Apple 账号（请重新输入）：" if existing is not None
                            else "👉 首次使用，Apple 账号：").strip()
        password = ""
        while not password:
            password = input(f"👉 密码（明文显示，保存到 {AUTH_PATH}）：")
    except EOFError:
        raise ValueError("账号或密码输入已取消，请在终端重新运行。") from None
    template = existing if existing is not None else {"cookies": {
        "account_session": [], "server_managed": [], "with_expiry": [], "expiry_unknown": []},
        "trust_cookies": []}
    previous_account = template.get("trust_account", template.get("account", ""))
    if previous_account and previous_account.strip().casefold() != account.casefold():
        template["trust_cookies"] = []
        template.pop("trust_account", None)
    template.update(account=account, password=password)
    AUTH_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(AUTH_PATH, os.O_WRONLY | (os.O_TRUNC if existing is not None else os.O_CREAT | os.O_EXCL), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        private_fd(file.fileno())
        json.dump(template, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return True


def ensure_strong_login(session, sms=False):
    """依据 Cookie 的 expires 和发送范围预检；未知期限仍交给服务器判断。"""
    def missing():
        prepared = session.prepare_request(requests.Request("GET", BASE_URL + "/shop/bag"))
        cookies = dict(part.strip().split("=", 1) for part in
                       prepared.headers.get("Cookie", "").split(";") if "=" in part)
        return [name for name in ("as_ltn_cn", "shld_bt_ck") if not cookies.get(name)]

    invalid = missing()
    from apple_login import browser_profile_path
    rejected = browser_profile_path(AUTH_PATH) / ".authx-541"
    if not invalid and not rejected.exists():
        return False
    if rejected.exists():
        print("登录预检：上次商城回调返回 541，先重建浏览器上下文并执行完整登录。")
    else:
        print("登录预检：" + "、".join(invalid) + " 已过期、缺失或不可发送，先执行完整登录。")
    login(sms=sms)
    # 完整登录用独立 Session，必须重新加载，不能让旧会话覆盖新 Cookie。
    load_auth(session, json.loads(AUTH_PATH.read_text(encoding="utf-8")))
    if missing() or rejected.exists():
        interaction.report_stage("完整登录后仍无有效会话", authentication_failed=True)
        raise ValueError("完整登录后仍缺少有效会话，已停止后续操作；未重复尝试登录。")
    return True


def authenticate(session, *, strong=False):
    created = ensure_auth_file()
    if created:
        login()
    try:
        load_auth(session, json.loads(AUTH_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError as error:
        raise ValueError(f"缺少认证文件：{AUTH_PATH}，请保存认证后重试。") from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"{AUTH_PATH} 格式不正确，请更新文件。") from error
    if strong and not created:
        ensure_strong_login(session)
    if not session.cookies:
        raise ValueError(f"{AUTH_PATH} 尚无会话 Cookie，请填写 account 和 password 后先运行 python3 duo_killer.py -l。")


def login(sms=False, *, renew_device_trust=False):
    ensure_auth_file()
    # 仅执行登录时加载 SRP；商城会话统一从当前认证 JSON 读取和保存。
    from apple_login import AppleStoreLogin, LoginError, load_credentials

    client = None
    try:
        account, password = load_credentials(AUTH_PATH)
        client = AppleStoreLogin(auth_path=AUTH_PATH)
        client.renew_device_trust = renew_device_trust
        client.trust_persistence_ready = not renew_device_trust
        config = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        old_auth = {"as_ltn_cn", "as_cn", "as_disa", "as_rec", "myacinfo", "acn01"}
        if isinstance(config.get("cookies"), dict):
            load_auth(client.session, config)
            # 仅移除旧认证：菜单需要显示登录入口，账号凭证需由本次认证取得。
            # 保留其余浏览器 Cookie，避免把 shld 等会话上下文一并清掉。
            for cookie in list(client.session.cookies):
                if cookie.name in old_auth or cookie.domain.lstrip(".") == "idmsa.apple.com.cn":
                    client.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        else:
            client.session.headers.update({key: value for key, value in config.get("headers", {}).items()
                                           if key.lower() in ("user-agent", "accept-language")})
        client.store_request = request_get
        if client.initialize_browser_context() is True:
            # 初始化上下文单独保存，认证尚未成功时保留已有登录凭证。
            with requests.Session() as initialized:
                if isinstance(config.get("cookies"), dict):
                    load_auth(initialized, config)
                for cookie in list(initialized.cookies):
                    if (cookie.domain.lstrip(".") in ("apple.com.cn", "www.apple.com.cn")
                            and cookie.name not in old_auth):
                        initialized.cookies.clear(cookie.domain, cookie.path, cookie.name)
                initialized.headers.update(client.session.headers)
                for cookie in client.session.cookies:
                    initialized.cookies.set_cookie(cookie)
                save_auth(initialized)
        client.login(account, password, sms)
        save_auth(client.session)
        return {"success": True, "logged_in": True, "scope": "store_menu", "auth_file": str(AUTH_PATH),
                "device_trust_renewal_requested": renew_device_trust}
    except LoginError as error:
        raise ValueError(str(error)) from error
    except EOFError:
        raise ValueError("验证码输入已取消，请在终端重新运行 -l。") from None
    finally:
        if client is not None:
            client.session.close()


def bag_items(session, stage="读取购物袋", part=PART):
    response = request_get(session, stage, BASE_URL + "/shop/bag")
    response.raise_for_status()
    match = re.search(r'<script[^>]*id="init_data"[^>]*>(.*?)</script>', response.text, re.S)
    log.info("[%s] init_data=%s", stage, bool(match))
    if not match:
        raise ValueError(f"未获取到购物袋数据，请检查 {AUTH_PATH} 或加 -log 查看详情。")
    try:
        cart = json.loads(match.group(1))["shoppingCart"]
        items = [
            {"item_id": key, "part": item["d"]["sku"],
             "quantity": int(item["itemQuantity"]["d"]["quantity"])}
            for key, item in cart.get("items", {}).items() if key.startswith("item-")
        ]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        log.error("[%s] 购物袋解析失败：%s", stage, type(error).__name__)
        raise ValueError("购物袋数据解析失败，可加 -log 查看详情。") from error
    log.info("[%s] 商品行数=%s，目标 SKU=%s，目标数量=%s", stage, len(items), part,
             sum(i["quantity"] for i in items if i["part"] == part))
    return items


def check_login(session, api_key=None, *, page=None):
    """检查菜单登录标志；不代表账户页或结账认证已通过。"""
    if api_key is None:
        # -k 已读取购物袋，直接复用其中的公开配置；独立 -c 保持原有行为。
        if page is None:
            with requests.Session() as public:
                page = request_get(public, "读取商城配置", BASE_URL + "/shop/bag")
                page.raise_for_status()
        match = re.search(r'<meta\b[^>]*\bname=["\']globalnav-store-key["\'][^>]*\bcontent=["\']([^"\']+)',
                          page.text, re.I)
        if not match:
            raise ValueError("未取得商城公开 API key，无法判断登录状态。")
        api_key = match[1]
    response = request_get(session, "检查商城登录", BASE_URL + "/cn/shop/bag/flyout",
                           params={"apikey": api_key, "l": BASE_URL + "/"})
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("links"), list):
        raise ValueError("登录检查未返回菜单数据，登录状态未知。")
    states = {link.get("type") for link in result["links"] if isinstance(link, dict)} & {"signIn", "signOut"}
    if len(states) != 1:
        raise ValueError("菜单未提供明确的登录标志，登录状态未知。")
    logged_in = "signOut" in states
    return {"success": True, "logged_in": logged_in, "scope": "store_menu",
            "message": "商城菜单识别为已登录；账户页和结账可能仍要求认证。" if logged_in else "商城菜单识别为未登录。"}


def account_info(session, *, reauthenticate=True):
    flow = Checkout(session)
    flow.page("https://secure.www.apple.com.cn/shop/account/home", "读取账户")
    if "signIn" in flow.model or urlsplit(flow.url).path.startswith("/shop/signIn"):
        if not reauthenticate:
            return {"success": True, "logged_in": False, "scope": "account",
                    "reauthentication_required": True}
        from apple_login import AppleStoreLogin, LoginError, load_credentials

        if urlsplit(flow.url).path != "/shop/signIn/account":
            raise ValueError("账户页返回了未支持的认证入口，已停止。")
        log.info("[账户认证] 使用账户页返回的登录入口，在当前会话完成重新认证。")
        client = AppleStoreLogin(auth_path=AUTH_PATH, session=session)
        client.store_request = request_get
        try:
            account, password = load_credentials(AUTH_PATH)
            response = client.login(account, password, signin_url=flow.url, signin_response=flow.page_response)
        except LoginError as error:
            raise ValueError(str(error)) from error
        except EOFError:
            raise ValueError("账户验证码输入已取消，请在终端重新运行 -me。") from None
        flow.read_page(response, "账户重新认证")
        if urlsplit(response.url).path != "/shop/account/home" or "home" not in flow.model:
            interaction.report_stage("账户重新认证后仍无法确认登录", authentication_failed=True)
            raise ValueError("账户重新认证后仍未进入账户页，已停止；未重复登录。")
    home = flow.require("home")
    if not field(home, "d.signOutUrl"):
        raise ValueError("账户页未提供明确的登录信息，状态未知。")
    apple_id = field(home, "customerAccount.d.appleId") or field(home, "devices.d.appleID")
    if not isinstance(apple_id, str) or not apple_id:
        flow.action("home.devices", "fetchDevices")
        apple_id = field(flow.model, "home.devices.d.appleID")
    if not isinstance(apple_id, str) or not apple_id:
        raise ValueError("账户已打开，但未取得 Apple ID 信息。")
    return {"success": True, "logged_in": True, "scope": "account",
            "name": field(flow.model, "home.d.firstName"), "apple_id": apple_id,
            "email": apple_id if "@" in apple_id else None}


def order_list(session):
    flow = Checkout(session)
    flow.page(BASE_URL + "/cn/shop/goto/order/list", "读取订单列表")
    if urlsplit(flow.url).path.startswith("/shop/signIn"):
        if urlsplit(flow.url).path != "/shop/signIn/orders":
            raise ValueError("订单页返回了未支持的认证入口，已停止。")
        from apple_login import AppleStoreLogin, LoginError, load_credentials

        client = AppleStoreLogin(auth_path=AUTH_PATH, session=session)
        client.store_request = request_get
        try:
            account, password = load_credentials(AUTH_PATH)
            reply = client.login(account, password, signin_url=flow.url, signin_response=flow.page_response)
        except LoginError as error:
            raise ValueError("订单认证失败：" + str(error)) from error
        except EOFError:
            raise ValueError("订单验证码输入已取消，请重新运行 -o。") from None
        flow.read_page(reply, "订单重新认证")
        if urlsplit(flow.url).path.startswith("/shop/signIn"):
            interaction.report_stage("订单重新认证后仍要求登录", authentication_failed=True)
    if urlsplit(flow.url).path != "/shop/order/list":
        raise ValueError("未进入订单列表，未将当前页面当作空订单。")
    listing = flow.require("orderList")
    orders = []
    for key, order in listing.items():
        if not key.startswith("order-") or not isinstance(order, dict):
            continue
        number = order_text(field(order, "d.webOrderNumber"))
        items = []
        for child in order.get("c", []):
            data = field(order, child + ".d", {})
            name = order_text(data.get("productShortName"))
            quantity = data.get("quantity")
            if not name or type(quantity) is not int or quantity < 1:
                raise ValueError("订单商品数据不完整，未返回不准确的订单列表。")
            detail_url = data.get("orderDetailUrl")
            items.append({"name": name, "quantity": quantity,
                          "delivery_info": order_text(data.get("deliveryDate")) or None,
                          "detail_url": flow.apple_url(detail_url) if detail_url else None})
        if not number or not items:
            raise ValueError("订单缺少编号或商品数据，已停止。")
        orders.append({"order_number": number, "items": items})
    has_items = field(listing, "d.hasItems")
    has_more = field(listing, "d.moreOrdersAvailable")
    if type(has_items) is not bool or has_items != bool(orders) or type(has_more) is not bool:
        raise ValueError("订单列表状态与内容不一致，未返回空订单。")
    return {"success": True, "count": len(orders), "has_more": has_more, "orders": orders,
            "message": "这是当前页订单；还有历史订单，可在官网订单列表继续查看。" if has_more
                       else "当前账号暂无订单。" if not orders else "订单列表读取完成。"}


def format_order_table(result):
    orders = result.get("orders", [])
    if not orders:
        return result.get("message") or "当前账号暂无订单。"
    rows = [["订单号", "商品", "数量", "状态 / 配送信息"]]
    for order in orders:
        for index, item in enumerate(order["items"]):
            rows.append([order_text(order["order_number"]) if index == 0 else "",
                         order_text(item["name"]), str(item["quantity"]),
                         order_text(item.get("delivery_info")) or "未知"])

    def width(value):
        return sum(0 if unicodedata.combining(c) else
                   2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in value)

    widths = [max(width(row[i]) for row in rows) for i in range(4)]
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [f"我的订单 · {len(orders)} 个订单", "", border]
    for index, row in enumerate(rows):
        lines.append("| " + " | ".join(value + " " * (w - width(value))
                                       for value, w in zip(row, widths)) + " |")
        if index == 0:
            lines.append(border)
    lines.append(border)
    if result.get("has_more"):
        lines.append("以上为当前页订单，还有历史订单可在官网继续查看。")
    return "\n".join(lines)


def normalize_sku(part):
    part = part.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{7}(?:/A)?", part):
        raise ValueError("SKU 格式不正确，例如 mjyh4ch 或 MJYH4CH/A。")
    if not part.endswith("/A"):
        part += "/A"
    return part


def cart_item(flow, part):
    matches = [(key, item) for key, item in flow.require("shoppingCart").get("items", {}).items()
               if key.startswith("item-") and str(field(item, "d.sku", "")).upper() == part]
    if len(matches) > 1:
        raise ValueError("同一 SKU 对应多个商品行，可能有不同刻字或配置；请在官网确认，未自动合并数量。")
    return matches[0] if matches else (None, None)


def add_to_bag(part=PART, quantity=1, *, set_total=False, flow=None):
    part = normalize_sku(part)
    if type(quantity) is not int or quantity < 1:
        raise ValueError("加购数量必须是正整数。")
    standalone = flow is None
    with (requests.Session() if standalone else nullcontext(flow.session)) as session:
        if standalone:
            authenticate(session, strong=True)
            flow = Checkout(session)
        try:
            if standalone:
                flow.page(BASE_URL + "/shop/bag", "加购前购物袋")
            item_id, item = cart_item(flow, part)
            before = int(field(item, "itemQuantity.d.quantity", 0))
            # -k 表示本次要买的总件数；-a 保持追加件数的含义。
            target = quantity if set_total else before + quantity
            if item is None:
                # 商品尚未入袋时只加一件，随后读取真实 item ID、数量限制和更新 action。
                add_error = _add_one_to_bag(session, part, page=flow.page_response)
                flow.page(BASE_URL + "/shop/bag", "首次加购后购物袋")
                item_id, item = cart_item(flow, part)
                if item is None or int(field(item, "itemQuantity.d.quantity", 0)) != 1:
                    raise ValueError(add_error + "首次加购未确认数量为一件，请先查袋，不要直接重复加购。")
            current = int(field(item, "itemQuantity.d.quantity"))
            maximum = field(item, "itemQuantity.d.maxQuantity")
            if maximum is not None and target > int(maximum):
                raise ValueError(f"{part} 目标数量 {target} 超过页面允许的最大数量 {maximum}；"
                                 f"当前购物袋已有 {current} 件，未继续修改数量或下单。")
            if current != target:
                module = f"shoppingCart.items.{item_id}.itemQuantity"
                print(f"更新购物袋：{part}，数量 {current} → {target}")
                # 复用当前页面令牌和 Session；action 会处理响应中的 meta.l 和新令牌。
                flow.action(module, "updateQuantity", {module + ".quantity": str(target)})
            item_id, item = cart_item(flow, part)
            if item is None or int(field(item, "itemQuantity.d.quantity", 0)) != target:
                raise ValueError("服务器返回数量与目标不一致，已停止；请先查袋，不要直接重复加购。")
            line_total = order_price(field(item, "d.totalPrice"))
            bag_total = order_price(field(flow.model, "shoppingCart.summary.d.total"))
            print(f"购物袋已确认：{part} × {target}，小计 {line_total}，购物袋总金额 {bag_total}")
            return {"success": True, "part": part, "added_quantity": max(0, target - before),
                    "item_ids": [item_id], "items": [{"item_id": item_id, "part": part, "quantity": target}],
                    "line_total": line_total, "bag_total": bag_total}
        except requests.RequestException:
            raise ValueError("购物袋请求未能确认结果，已停止；请先运行 -bag 核对数量，不要直接重跑 -k 或 -a。") from None
        finally:
            if standalone:
                save_auth(session)


def _add_one_to_bag(session, part, *, page=None):
    product_url = BASE_URL + "/shop/product/" + part
    session.headers.update({"Accept": "*/*", "Referer": product_url})
    candidate = urlsplit(getattr(page, 'url', '') or '')
    if (page is not None and page.status_code == 200 and candidate.scheme == 'https'
            and candidate.hostname == 'www.apple.com.cn' and candidate.path == '/shop/bag'):
        product_page = page
    else:
        product_page = request_get(session, "商品页", product_url)
        product_page.raise_for_status()
    key = re.search(r'<meta\b[^>]*\bname=["\']globalnav-store-key["\'][^>]*\bcontent=["\']([^"\']+)',
                    product_page.text, re.I)
    if not key:
        if product_page is page:
            return _add_one_to_bag(session, part)
        raise ValueError("商品页未提供加购 API key，可加 -log 查看详情。")
    if product_page is page:
        source = '预热读取' if getattr(page, 'duo_public_config_prewarmed', False) is True else '当前购物袋'
        log.info('[加购配置] 复用%s的公开 API key，跳过重复商品页；仍读取新加购令牌并核对购物袋。', source)
    beacon = request_get(session, "加购令牌", BASE_URL + "/shop/beacon/atb")
    beacon.raise_for_status()
    cookie_header = session.prepare_request(requests.Request("GET", product_url)).headers.get("Cookie", "")
    token = next(("".join(c.strip().split("=", 1)[1].split("|")[2:])
                  for c in cookie_header.split(";") if c.strip().startswith("as_atb=")), "")
    issued = next(("".join(c.value.split("|")[2:]) for c in beacon.cookies if c.name == "as_atb"), None)
    log.info("[加购令牌] 已取得=%s，长度=%s，与本次 beacon 一致=%s，as_atb 数量=%s",
             bool(token), len(token), token == issued if issued is not None else "本次未下发",
             sum(c.name == "as_atb" for c in session.cookies))
    if not token:
        raise ValueError(f"当前会话未取得 as_atb 加购令牌，请检查 {AUTH_PATH} 或加 -log 查看详情。")
    # 官网 acStore.addItem 使用的中国商城 JSON 接口。
    params = {"product": part, "apikey": key[1], "atbtoken": token}
    # 加购只发送一次；导航或网络出错后查袋确认，不自动重试。
    add_error = ""
    try:
        response = request_get(session, "执行加购", BASE_URL + "/cn/shop/bag/add", params=params)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Unexpected add-to-bag response")
        code = result.get("errorCode", result.get("responseCode", "unknown"))
        log.info("[加购结果] addedToBag=%s，responseCode=%s，bagQuantity=%s",
                 result.get("addedToBag"), code, result.get("bagQuantity"))
        if result.get("addedToBag") is not True:
            add_error = f"加购接口未确认成功（{code}）。"
    except ValueError:
        add_error = "加购接口返回了无法识别的 JSON 响应。"
    except requests.RequestException as error:
        add_error = (f"加购接口返回 HTTP {error.response.status_code}。"
                     if error.response is not None else "加购请求发生网络异常。")
    return add_error


def field(node, path, default=None):
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def write_private_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.duo_killer-')
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            private_fd(file.fileno())
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Checkout:
    """单次执行页面 action；准备步骤的状态核对与恢复由上层控制。"""

    def __init__(self, session):
        self.session = session
        self.model = {}
        self.headers = {}
        self.url = BASE_URL + "/shop/bag"
        self.page_response = None

    def apple_url(self, target):
        url = urljoin(self.url, target)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or
                not (host == "www.apple.com.cn" or re.fullmatch(r"secure\d*\.www\.apple\.com\.cn", host))):
            raise ValueError("结账返回了非预期的商城地址，已停止。")
        return url

    def update(self, body):
        # meta.l 指定整块替换，其余响应是局部更新；保留未重发的 x-as-actk。
        for path in body.get("meta", {}).get("l", []):
            keys = path.lstrip("/").split(".")
            parent = field(self.model, ".".join(keys[:-1])) if len(keys) > 1 else self.model
            if isinstance(parent, dict):
                parent.pop(keys[-1], None)

        def merge(target, source):
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = value
        merge(self.model, body)
        meta = body.get("meta", {})
        self.headers.update({k.lower(): v for k, v in meta.get("h", {}).items()
                             if k.lower() in {"x-aos-stk", "x-as-actk", "x-aos-model-page", "modelversion", "syntax"}})
        if meta.get("page", {}).get("htmlUrl"):
            self.url = self.apple_url(meta["page"]["htmlUrl"])

    def page(self, url, stage, *, fresh=False):
        # 显式处理跳转，商城令牌不会跟随跳转发送到支付平台。
        for _ in range(8):
            url = self.apple_url(url)
            headers = {"Referer": self.url}
            if fresh:
                headers.update({'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
            response = request_get(self.session, stage, url, allow_redirects=False, headers=headers)
            response.raise_for_status()
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            self.read_page(response, stage)
            return
        raise ValueError("结账页面重定向次数过多。")

    def read_page(self, response, stage):
        self.page_response = response
        self.url = self.apple_url(response.url)
        match = re.search(r'<script[^>]*id=["\']init_data["\'][^>]*>(.*?)</script>', response.text, re.S)
        if not match:
            raise ValueError(f"{stage}没有返回页面数据，请更新 {AUTH_PATH} 或加 -log 检查。")
        self.model = {}
        self.headers = {}
        self.update(json.loads(match[1]))
        if urlsplit(response.url).path.startswith("/shop/signIn"):
            self.url = response.url  # 保留实际登录入口的 ssi/up，不用模型中的规范 URL 替换。
        # start 文档加载后，官网将地址栏切换到当前结账步骤。
        if urlsplit(self.url).path == "/shop/checkout/start" and field(self.model, "checkout.fulfillment"):
            self.url = self.apple_url("/shop/checkout?_s=Fulfillment-init")

    def action(self, module, name, data=None):
        from checkout_recovery import CheckoutActionError
        url = field(self.model, f"{module}.a.{name}.url")
        if not url or not self.headers.get("x-aos-stk"):
            raise ValueError(f"当前页面没有可执行的 {name}，可能需要更新 {AUTH_PATH}。")
        headers = {**self.headers, "Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded",
                   "Origin": urlunsplit((*urlsplit(self.url)[:2], "", "", "")),
                   "Referer": self.url, "X-Requested-With": "Fetch"}
        response = request_get(self.session, name, self.apple_url(url), method="POST",
                               data=data or {}, headers=headers, allow_redirects=False)
        if response.status_code != 200:
            retry_after = 0
            hint = response.headers.get('Retry-After')
            if hint:
                try:
                    retry_after = max(0, float(hint))
                except ValueError:
                    try:
                        retry_after = max(0, parsedate_to_datetime(hint).timestamp() - time())
                    except (ValueError, TypeError, OverflowError):
                        retry_after = 30
            raise CheckoutActionError(f"{name} 返回 HTTP {response.status_code}，已停止。",
                recoverable=response.status_code in (408, 429, 500, 502, 503, 504), retry_after=retry_after)
        try:
            result = response.json()
            status = str(result["head"]["status"])
        except (ValueError, KeyError, TypeError) as error:
            raise CheckoutActionError(f"{name} 未返回预期 JSON，已停止。", recoverable=True) from error
        log.info("[%s] 业务状态=%s", name, status)
        if status == "302":
            target = field(result, 'head.data.url')
            if not isinstance(target, str) or not target:
                raise CheckoutActionError(f"{name} 返回的跳转数据不完整。", recoverable=True)
            self.page(target, name + " 跳转")
        elif status == "200":
            body = result.get('body', {})
            if not isinstance(body, dict):
                raise CheckoutActionError(f"{name} 返回的页面数据格式异常。", recoverable=True)
            self.update(body)
        else:
            raise CheckoutActionError(f"{name} 返回业务状态 {status}，已停止。",
                recoverable=status in ('408', '429', '500', '502', '503', '504'))

    def require(self, path):
        node = field(self.model, path)
        if not isinstance(node, dict):
            raise ValueError(f"未进入 {path}；请在官网核对登录、配送或地址信息，再加 -log 查看阶段。")
        return node


def reauthenticate_checkout(flow):
    if not urlsplit(flow.url).path.startswith("/shop/signIn"):
        return False
    from apple_login import AppleStoreLogin, LoginError, load_credentials

    log.info("[结账认证] 当前结账要求重新认证，保留 Session 和原登录入口继续。")
    prepared = flow.session.prepare_request(requests.Request('GET', flow.url))
    cookie_header = prepared.headers.get('Cookie', '')
    sent = {part.strip().partition('=')[0] for part in cookie_header.split(';')} if isinstance(cookie_header, str) else set()
    log.info('[结账认证] 登录入口可发送凭证：商城=%s，Apple账号=%s；由官网跳转触发认证。',
             '有' if 'as_ltn_cn' in sent else '无', '有' if 'myacinfo' in sent else '无')
    client = AppleStoreLogin(auth_path=AUTH_PATH, session=flow.session)
    client.store_request = request_get
    client.store_referrer = flow.url
    try:
        account, password = load_credentials(AUTH_PATH)
        response = client.login(account, password, signin_url=flow.url, signin_response=flow.page_response)
    except LoginError as error:
        raise ValueError("结账重新认证失败：" + str(error)) from error
    except EOFError:
        raise ValueError("结账验证码输入已取消，请在终端重新运行 -p。") from None
    flow.read_page(response, "结账重新认证")
    if urlsplit(flow.url).path.startswith("/shop/signIn"):
        interaction.report_stage("结账重新认证后仍要求登录", authentication_failed=True)
    if not any(isinstance(field(flow.model, 'checkout.' + step), dict)
               for step in ('fulfillment', 'shipping', 'billing', 'review')):
        raise ValueError("结账重新认证后仍未进入配送页面，已停止；未重复提交 checkout。")
    # Persist renewed cookies before the long shipping/payment stages. A task
    # interruption must not lose a successful authentication.
    save_auth(flow.session)
    log.info("[结账认证] 已恢复到有效结算步骤。")
    return True


def checkout_delivery(flow):
    from checkout_options import fulfillment
    fulfillment(flow, ROOT / "checkout.json")


def checkout_shipping(flow):
    from checkout_options import shipping
    shipping(flow, ROOT / "checkout.json", AUTH_PATH)


def model_item_nodes(model):
    if isinstance(model, dict):
        if field(model, "d.sku") and field(model, "itemQuantity.d.quantity") is not None:
            yield model
        else:
            for value in model.values():
                yield from model_item_nodes(value)
    elif isinstance(model, list):
        for value in model:
            yield from model_item_nodes(value)


def model_items(model):
    """提取购物袋/复核页中的实物商品，用于下单前核对 SKU 和数量。"""
    return sorted((item["d"]["sku"], int(item["itemQuantity"]["d"]["quantity"]))
                  for item in model_item_nodes(model))


def order_text(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = unescape(re.sub(r"<[^>]+>", "", str(value)))
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())


def order_price(value):
    text = order_text(value)
    if not re.fullmatch(r"(?:RMB|CNY|CN¥|¥|￥|\$|USD)?\s*\d[\d,]*(?:\.\d{1,2})?\s*(?:元|RMB|CNY|USD)?", text):
        raise ValueError("订单复核页缺少可识别的金额，已停止，未提交订单。")
    return text


def confirm_order(model):
    review = field(model, "checkout.review", {})
    rows = []
    for item in model_item_nodes(review.get("fulfillmentReview", {})):
        quantity = int(field(item, "itemQuantity.d.quantity"))
        if quantity <= 0:
            raise ValueError("订单商品数量无效，未提交订单。")
        name = order_text(field(item, "d.name") or field(item, "d.productName") or field(item, "d.sku"))
        # 与官网商品组件及复核页 summary 使用相同字段；totalPrice 是该行小计。
        rows.append(f"{len(rows) + 1}. {name} × {quantity}  小计 {order_price(field(item, 'd.totalPrice'))}")
    if not rows:
        raise ValueError("订单复核页没有商品，未提交订单。")
    summary = field(review, "summary.d", {})
    total = order_price(summary.get("totalAfterTradeIn") if summary.get("showTotalAfterTradeinAsTotal")
                        else field(summary, "installmentTotal.value") or summary.get("total"))
    print("\n即将结算整个购物袋：")
    print("\n".join(rows))
    details = field(model, "checkout.companionBar.orderDetails", {})
    store = field(details, "fulfillmentCompanionBar.pickupSummary.d.store_name")
    if store:
        print("自提门店：" + order_text(store))
    address = field(review, "shippingReview.address.d", {})
    delivery = " ".join(order_text(address.get(key)) for key in ("state", "city", "district", "street", "street2")
                        if address.get(key)) if address.get("street") else field(details, "shippingCompanionBar.address.d.street")
    if delivery:
        print("送货地址：" + order_text(delivery))
    print(f"\n总支付：{total}")
    if interaction.handler is not None:
        details = {"items": [{"name": order_text(field(item, "d.name") or field(item, "d.productName") or field(item, "d.sku")),
                              "sku": field(item, "d.sku"), "quantity": int(field(item, "itemQuantity.d.quantity")),
                              "subtotal": order_price(field(item, "d.totalPrice"))}
                             for item in model_item_nodes(review.get("fulfillmentReview", {}))],
                   "total": total, "store": order_text(store), "address": order_text(delivery)}
        if interaction.handler.confirm(details) is not True:
            raise ValueError("已取消下单，购物袋已保留。")
        return
    try:
        answer = input("👉 按回车确认创建订单并获取付款二维码")
    except EOFError:
        raise ValueError("确认输入已结束，未提交订单；购物袋已保留。") from None
    if answer != "":
        raise ValueError("已取消下单，购物袋已保留。")


def payment_result(flow):
    # 官网 payment-instructions.js 的“现在支付”绑定此模型。
    module = "externalPayAuth" if "externalPayAuth" in flow.model else "thankYouInterstitial"
    header = field(flow.model, module + ".d", {})
    url = header.get("externalUrl", "")
    parsed = urlsplit(url)
    if (header.get("isPost") or parsed.scheme != "https" or parsed.hostname != "openapi.alipay.com"
            or parsed.path != "/gateway.do" or parsed.username or parsed.password):
        raise ValueError("下单结果页未提供预期的支付宝 GET 支付链接，请在官网查看订单；不要重复下单。")
    return {"success": True, "status": "pending_payment", "order_number": header.get("orderNumber"),
            "amount_due": header.get("amountDue"), "payment_url": url,
            "message": "订单已创建，尚未付款；请打开支付链接完成支付宝支付。"}


def payment_qr(result):
    result['qr_loading'] = True
    try:
        result.update(get_payment_qr(result["payment_url"]))
    except (OSError, ValueError, requests.RequestException) as error:
        # 订单已经创建，二维码读取失败不能触发重新下单。
        message = "支付宝网络请求失败。" if isinstance(error, requests.RequestException) else str(error)
        log.warning("支付链接已取得；付款二维码未取得：%s", message)
        result["qr_image"] = None
        result["qr_error"] = message + " 可用 -purl 重试获取二维码。"
    finally:
        result['qr_loading'] = False


def finish_payment(flow):
    for attempt in range(12):
        if urlsplit(flow.url).path == "/shop/checkout/interstitial":
            return payment_result(flow)
        if not field(flow.model, "spinner.a.checkStatus.url"):
            raise ValueError("未取得订单处理状态，请运行 -o 查询订单。")
        if attempt:
            # Check sooner during the first seconds after submission, then
            # keep the existing cadence. Only polls the already submitted order.
            sleep(.5 if attempt == 1 else 1 if attempt == 2 else 2)
        flow.action("spinner", "checkStatus")
    raise ValueError("订单仍在处理中，请稍后运行 -o 查询订单。")


def checkout_review(flow):
    # Billing 的成功响应可能仍带旧步骤模型；重新读取页面同步状态，不能重放 POST。
    for attempt in range(3):
        review = field(flow.model, "checkout.review")
        if isinstance(review, dict):
            return review
        if field(flow.model, "checkout.d.page") != "Billing" or attempt == 2:
            break
        flow.page(flow.apple_url("/shop/checkout"), "同步订单确认页")
    raise ValueError("付款方式步骤未进入订单确认页；尚未提交订单。请检查官网当前结算页的提示。")


def pay_bag(*, flow=None, prepare_only=False):
    standalone = flow is None
    with (requests.Session() if standalone else nullcontext(flow.session)) as session:
        if standalone:
            authenticate(session, strong=True)
            flow = Checkout(session)
        try:
            if standalone or not field(flow.model, "shoppingCart.actions.a.checkout.url") or not flow.headers.get("x-aos-stk"):
                flow.page(BASE_URL + "/shop/bag", "结账购物袋")
            else:
                log.info("[结账购物袋] 复用加购后的实时购物袋模型和会话，跳过重复 GET。")
            cart = flow.require("shoppingCart")
            expected = model_items(cart.get("items", {}))
            if not expected:
                raise ValueError("购物袋为空，无法下单。")
            data = {"shoppingCart.locationConsent.locationConsent": "false",
                    "shoppingCart.summary.promoCode.promoCode": field(cart, "summary.promoCode.d.promoCode", ""),
                    "shoppingCart.actions.fcscounter": "", "shoppingCart.actions.fcsdata": "",
                    "shoppingCart.recommendations.recommendedItem.part": ""}
            for key in ("part", "itemId", "listId", "childPart"):
                data["shoppingCart.bagSavedItems." + key] = ""
            for key, item in cart.get("items", {}).items():
                if not key.startswith("item-"):
                    continue
                prefix = "shoppingCart.items." + key
                data[prefix + ".itemQuantity.quantity"] = str(item["itemQuantity"]["d"]["quantity"])
                address = ".delivery.lineDeliveryOptions.address.provinceCityDistrictTabs"
                for name in ("state", "provinceCityDistrict", "countryCode"):
                    data[prefix + address + "." + name] = field(item, address[1:] + ".d." + name, "CN" if name == "countryCode" else "")
            flow.action("shoppingCart.actions", "checkout", data)
            reauthenticate_checkout(flow)
            return continue_checkout(flow, expected, prepare_only=prepare_only)
        finally:
            if standalone:
                save_auth(session)



def continue_checkout(flow, expected, *, prepare_only=False):
    from checkout_recovery import recover_step
    if isinstance(field(flow.model, 'checkout.review'), dict):
        return pay_review(flow, expected, prepare_only=prepare_only)
    page = field(flow.model, 'checkout.d.page')
    if page not in ('Shipping', 'Billing', 'Review'):
        recover_step(flow, '送货', lambda: checkout_delivery(flow))
    if field(flow.model, 'checkout.d.page') not in ('Billing', 'Review'):
        recover_step(flow, '地址', lambda: checkout_shipping(flow))
    if not isinstance(field(flow.model, 'checkout.review'), dict):
        recover_step(flow, '支付方式', lambda: checkout_billing(flow))
    return pay_review(flow, expected, prepare_only=prepare_only)


def checkout_billing(flow):
    billing = "checkout.billing.billingOptions"
    options = flow.require(billing)["d"].get("options", [])
    if not any(o.get("value") == "ALIPAY" and not o.get('disabled') for o in options):
        raise ValueError("当前订单不支持支付宝。")
    # Continue accepts the selection. Final review still confirms ALIPAY.
    data = {billing + ".selectBillingOption": "ALIPAY", billing + ".bankLookUp.selectBank": "",
            "checkout.locationConsent.locationConsent": "false"}
    log.info("[付款方式] 随继续操作提交支付宝选项，省去独立选择请求；最终复核服务端支付方式。")
    flow.action("checkout.billing", "continueFromBillingToReview", data)


def selected_applecare(model):
    """Inspect purchased rows and selected-plan state, not serialized field names."""
    if isinstance(model, list):
        return any(selected_applecare(item) for item in model)
    if not isinstance(model, dict):
        return False
    data = model.get('d', model)
    if isinstance(data, dict):
        flags = ('hasAppleCare', 'hasAppleCareInstallment', 'isAppleCareSelected',
                 'appleCareSelected', 'appleCareIncluded')
        if any(data.get(flag) is True for flag in flags):
            return True
        names = [data.get(key) for key in ('name', 'productName', 'productTitle', 'title')]
        named_plan = any(isinstance(name, str) and re.search(r'apple\s*care', name, re.I) for name in names)
        if named_plan and (field(model, 'itemQuantity.d.quantity', 0) > 0
                           or data.get('selected') is True or data.get('isSelected') is True):
            return True
    for key, child in model.items():
        normalized = re.sub(r'[^a-z]', '', key.lower())
        if normalized in ('applecare', 'applecareplus', 'serviceplan', 'coverageplan') and isinstance(child, dict):
            plan = child.get('d', child)
            if isinstance(plan, dict):
                if any(plan.get(flag) is False for flag in ('selected', 'isSelected', 'included')):
                    continue
                if any(isinstance(plan.get(name), str) and re.search(r'apple\s*care', plan[name], re.I)
                       for name in ('name', 'productName', 'productTitle', 'title')):
                    return True
        if selected_applecare(child):
            return True
    return False


def pay_review(flow, expected, *, prepare_only=False):
    review = checkout_review(flow)
    if model_items(review.get("fulfillmentReview", {})) != expected:
        raise ValueError("订单复核未通过：官网商品或数量与预期不一致；未提交订单。")
    # A fresh Review HTML can omit the payment-specific child entirely. Use
    # Apple's declared edit action to rebuild Billing, rather than assuming
    # the previous selection survived. Never replay the final order action.
    if field(review, "billingReview.alipayReview.d.alipayLabel") is None:
        if not field(review, "billingReview.a.edit.url"):
            raise ValueError("订单复核未通过：官网未返回支付方式且无法重新选择；未提交订单。")
        log.info("[订单复核] 确认页缺少支付方式详情，返回付款步骤重新选择支付宝。")
        flow.model['checkout'].pop('billing', None)
        flow.action("checkout.review.billingReview", "edit")
        # Do not retain an old review fragment if the server's partial response
        # includes Billing without explicitly removing Review.
        if not isinstance(field(flow.model, "checkout.billing.billingOptions"), dict):
            raise ValueError("订单复核未通过：未能返回付款方式步骤；未提交订单。")
        flow.model['checkout'].pop('review', None)
        checkout_billing(flow)
        review = checkout_review(flow)
    if model_items(review.get("fulfillmentReview", {})) != expected:
        raise ValueError("订单复核未通过：重新选择支付方式后商品或数量发生变化；未提交订单。")
    if field(review, "billingReview.alipayReview.d.alipayLabel") != "ALIPAY":
        raise ValueError("订单复核未通过：官网未确认支付方式为支付宝；未提交订单。")
    if field(review, "placeOrder.d.enableContinue") is not True:
        raise ValueError("订单复核未通过：官网当前不允许提交订单；未提交订单。")
    if getattr(interaction.handler, 'automatic_checkout', False) is True:
        if selected_applecare(review.get('fulfillmentReview', {})):
            raise ValueError('订单复核显示已选择 AppleCare 服务，不符合仅购买裸机的预设；未提交订单。')
    if len(expected) == 1 and getattr(interaction.handler, 'automatic_checkout', False):
        config = ROOT / "checkout.json"
        write_private_json(ROOT / "prepared_checkout.json", {'sku': expected[0][0], 'quantity': expected[0][1],
            'config_hash': hashlib.sha256(config.read_bytes() if config.exists() else b'').hexdigest(), 'created_at': time()})
    confirm_order(flow.model)
    if prepare_only:
        return {"success": True, "status": "prepared", "message": "准备完成，未创建订单。点击立即下单时重新读取官网确认页，核对后提交；会话或库存变化可能需要重新准备。"}
    order_details = getattr(interaction.handler, 'order_details', None)
    save_auth(flow.session)
    interaction.report_stage("正在提交订单", order_submitted=True)
    flow.action("checkout.review.placeOrder", "continueFromReviewToProcess")
    result = finish_payment(flow)
    save_auth(flow.session)  # QR retrieval must not delay persisting issued session cookies.
    (ROOT / "prepared_checkout.json").unlink(missing_ok=True)
    if order_details:
        result.update(items=order_details['items'], delivery_address=order_details['address'], order_total=order_details['total'])
    if PAYMENT_ROOT is not None:
        # Keep the issued payment link even if fetching its QR image is interrupted.
        write_private_json(PAYMENT_ROOT / "payment.json", result)
    ready = getattr(interaction.handler, 'payment_ready', None)
    if callable(ready):
        ready(result)
    payment_qr(result)
    write_private_json((PAYMENT_ROOT or ROOT) / "payment.json", result)
    return result


def warm_cart_fingerprint(session):
    header = session.prepare_request(requests.Request('GET', BASE_URL + '/shop/bag')).headers.get('Cookie', '')
    relevant = sorted(part.strip() for part in header.split(';')
                      if part.strip().partition('=')[0] in {'as_ltn_cn', 'shld_bt_ck', 'dssid2', 'as_cn'})
    if not {'as_ltn_cn', 'shld_bt_ck'} <= {part.partition('=')[0] for part in relevant}:
        return None
    return hashlib.sha256('\n'.join(relevant).encode()).hexdigest()


def warm_empty_bag():
    """Read only: cache public configuration, never a checkout token or SKU."""
    with requests.Session() as session:
        load_auth(session, json.loads(AUTH_PATH.read_text(encoding='utf-8')))
        flow = Checkout(session)
        flow.page(BASE_URL + '/shop/bag', '预热购物袋（不加购）')
        cart = flow.require('shoppingCart')
        # Empty bags still carry items.d/b/p/view metadata. Require Apple's
        # explicit flag as well as absence of physical item nodes.
        empty = (field(cart, 'd.hasItems') is False and isinstance(cart.get('items'), dict)
                 and not any(key.startswith('item-') for key in cart['items'])
                 and not model_items(cart['items']))
        key = re.search(r'<meta\b[^>]*\bname=["\']globalnav-store-key["\'][^>]*\bcontent=["\']([^"\']+)',
                        flow.page_response.text, re.I)
        cache = ROOT / 'warm_empty_bag.json'
        cache.unlink(missing_ok=True)
        fingerprint = warm_cart_fingerprint(session)
        if empty and fingerprint and key and re.fullmatch(r'[A-Za-z0-9._:-]{1,512}', key[1]):
            write_private_json(cache, {'created_at': time(), 'fingerprint': fingerprint,
                                      'public_key': key[1], 'empty': True})
        save_auth(session)
        return empty and cache.exists()


def try_warm_first_add(flow, part, quantity):
    cache = ROOT / 'warm_empty_bag.json'
    if quantity != 1 or getattr(interaction.handler, 'automatic_checkout', False) is not True:
        return False
    try:
        data = json.loads(cache.read_text(encoding='utf-8'))
        valid = (type(data) is dict and data.get('empty') is True
                 and type(data.get('created_at')) in (int, float)
                 and 0 <= time() - data['created_at'] < 120
                 and isinstance(data.get('fingerprint'), str) and len(data['fingerprint']) == 64
                 and data.get('fingerprint') == warm_cart_fingerprint(flow.session)
                 and isinstance(data.get('public_key'), str)
                 and re.fullmatch(r'[A-Za-z0-9._:-]{1,512}', data['public_key']))
    except (OSError, ValueError, TypeError):
        valid = False
    if not valid:
        return False
    # Consume before any mutation. A failed/ambiguous add cannot reuse this
    # shortcut; subsequent attempts start with a fresh live cart read.
    cache.unlink()
    page = requests.Response()
    page.status_code = 200
    page.url = BASE_URL + '/shop/bag'
    page.duo_public_config_prewarmed = True
    page._content = ('<meta name="globalnav-store-key" content="' + data['public_key'] + '">').encode()
    log.info('[预热首次加购] 使用两分钟内的空购物袋公开配置；仍取新令牌并实时核对全部商品。')
    add_error = _add_one_to_bag(flow.session, part, page=page)
    flow.page(BASE_URL + '/shop/bag', '首次加购后购物袋')
    actual = model_items(flow.require('shoppingCart').get('items', {}))
    if actual != [(part, quantity)]:
        raise ValueError(add_error + '加购后购物袋与目标商品、数量不一致；未结算，不自动重复加购。')
    return True


def purchase_sku(part, sms=False, quantity=1, *, prepare_only=False):
    part = normalize_sku(part)
    if type(quantity) is not int or quantity < 1:
        raise ValueError("加购数量必须是正整数。")
    ensure_auth_file()
    with requests.Session() as session:
        load_auth(session, json.loads(AUTH_PATH.read_text(encoding="utf-8")))
        refreshed = ensure_strong_login(session, sms=sms)
        try:
            flow = Checkout(session)
            checkpoint = ROOT / "prepared_checkout.json"
            config = ROOT / "checkout.json"
            config_hash = hashlib.sha256(config.read_bytes() if config.exists() else b'').hexdigest()
            previous = {}
            if checkpoint.exists():
                try:
                    previous = json.loads(checkpoint.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    pass
            if not isinstance(previous, dict):
                previous = {}
            if (previous.get('sku') == part and previous.get('quantity') == quantity
                    and previous.get('config_hash') == config_hash
                    and 0 <= time() - previous.get('created_at', 0) < 300):
                flow.page(BASE_URL + "/shop/checkout", "读取已准备的结算页面")
                if field(flow.model, 'checkout.d.page') in ('Fulfillment', 'Shipping', 'Billing', 'Review') or isinstance(field(flow.model, 'checkout.review'), dict):
                    log.info("[继续结算] 复用官网当前结算步骤，最终重新核对商品、金额和地址。")
                    return continue_checkout(flow, [(part, quantity)], prepare_only=prepare_only)
                log.info("[继续结算] 官网结算步骤不可复用，重新走购物袋流程。")
            warmed = not refreshed and try_warm_first_add(flow, part, quantity)
            if warmed:
                if prepare_only:
                    return pay_bag(flow=flow, prepare_only=True)
                return pay_bag(flow=flow)
            flow.page(BASE_URL + "/shop/bag", "加购前购物袋")
            status = check_login(session, page=flow.page_response)
            if not status["logged_in"]:
                if refreshed:
                    raise ValueError("完整登录后商城仍未识别为已登录，已停止加购；未重复尝试登录。")
                print("当前未登录，开始登录。")
                save_auth(session)
                try:
                    login(sms=sms)
                finally:
                    # 包括登录失败清理后的配置，避免旧 Session 在退出时覆盖新凭证。
                    load_auth(session, json.loads(AUTH_PATH.read_text(encoding="utf-8")))
                flow = Checkout(session)
                flow.page(BASE_URL + "/shop/bag", "重新登录后购物袋")
            else:
                print("商城已登录。")
            print(f"目标购买数量：{part} × {quantity}")
            add_to_bag(part, quantity=quantity, set_total=True, flow=flow)
            if getattr(interaction.handler, 'automatic_checkout', False):
                write_private_json(checkpoint, {'sku': part, 'quantity': quantity,
                    'config_hash': hashlib.sha256(config.read_bytes() if config.exists() else b'').hexdigest(), 'created_at': time()})
            if prepare_only:
                return pay_bag(flow=flow, prepare_only=True)
            return pay_bag(flow=flow)
        finally:
            save_auth(session)


def wait_for_order_query():
    while True:
        try:
            answer = input("👉 支付完成后按回车查询订单；可再次回车刷新，输入其他内容退出：")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer != "":
            return
        try:
            with requests.Session() as session:
                authenticate(session, strong=True)
                try:
                    result = order_list(session)
                finally:
                    save_auth(session)
            print(format_order_table(result))
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as error:
            message = "网络请求失败，请检查连接。" if isinstance(error, requests.RequestException) else str(error)
            print("订单查询失败：" + message + " 可按回车重试查询。")


def preset_sku():
    from checkout_options import read_config
    config = read_config(ROOT / "checkout.json")
    return normalize_sku(config.get("purchase", {}).get("sku") or DEFAULT_PURCHASE_SKU)


def address_preset_ready(config):
    required = ("contact.lastName", "contact.firstName", "contact.emailAddress", "contact.fullDaytimePhone",
                "delivery.state", "delivery.city", "delivery.district", "address.street")
    if not all(isinstance(field(config, key), str) and field(config, key).strip() for key in required):
        return False
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", field(config, "contact.emailAddress"))
                and re.fullmatch(r"\d{11,12}", field(config, "contact.fullDaytimePhone")))


def initialize():
    from checkout_options import ask, configure_address, read_config, save_config

    print(f"初始化 1/3：检查账号配置 {AUTH_PATH}。")
    ensure_auth_file()
    try:
        auth = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError(f"{AUTH_PATH} 格式无效，请修复 JSON 后重试。") from None
    if not isinstance(auth, dict):
        raise ValueError(f"{AUTH_PATH} 必须是 JSON 对象。")
    if not (isinstance(auth.get("account"), str) and auth["account"].strip()
            and isinstance(auth.get("password"), str) and auth["password"]):
        raise ValueError(f"{AUTH_PATH} 缺少账号或密码，已停止初始化。")
    print("本地账号配置已完成。")
    config_path = ROOT / "checkout.json"
    config = read_config(config_path)
    if not address_preset_ready(config):
        print("初始化 2/3：补全本地快捷地址和联系人。")
        configure_address(config_path, AUTH_PATH)
        config = read_config(config_path)
        if not address_preset_ready(config):
            raise ValueError("地址预设仍不完整，已停止，未开始购买。")
    else:
        print("地址预设已完成。")
    part = field(config, "purchase.sku")
    try:
        part = normalize_sku(part) if isinstance(part, str) else None
    except ValueError:
        part = None
    if part is None:
        print("初始化 3/3：设置默认购买 SKU。")
        while True:
            part = ask("默认购买 SKU", DEFAULT_PURCHASE_SKU)
            try:
                part = normalize_sku(part)
                break
            except ValueError as error:
                print(str(error))
    if field(config, "purchase.sku") != part:
        config.setdefault("purchase", {})["sku"] = part
        save_config(config_path, config)
    print("查询当前登录人……")
    with requests.Session() as session:
        load_auth(session, auth)
        try:
            # 直接沿账户页入口认证，不先绕行首页登录，也不重新读取一次账户入口。
            current = account_info(session)
        finally:
            save_auth(session)
    if not current.get("logged_in") or not current.get("apple_id"):
        raise ValueError("无法确认当前登录人，已停止初始化，未开始购买。")
    name = current.get("name") or "未提供姓名"
    print(f"当前登录人：{name}（{current['apple_id']}）")
    print(f"全部本地配置已完成：账号、地址已配置，预设 SKU：{part}。")
    answer = input(f"👉 按回车执行 -k {part[:-2]} -num 2，输入其他内容退出：")
    if answer == "":
        return purchase_sku(part, quantity=2)
    return {"success": True, "initialized": True, "default_sku": part, "config_file": str(config_path),
            "message": "配置已保存，未开始购买。"}


def main(argv=None):
    global AUTH_PATH
    parser = argparse.ArgumentParser(
        description="Apple 商城工具：账号操作默认使用同目录 auth.json，可用 --auth 指定；商店查询 -s 无需登录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""参数规则：
  每次只选一个主操作（如 -s、-k、-l、-a、-p、-bag、-o）。
  --phone 与 --duo 互斥，均必须搭配 -s；--sms 仅搭配 -l 或 -k。
  SKU 不区分大小写，可省略 /A；例如 MK2W4CH 与 MK2W4CH/A 等价。
  -num 必须为正整数，仅搭配 -k 或 -a，默认 1。
  -auth/--auth 可指定认证 JSON；相对路径按当前终端目录解析。
  -k 按目标总数量调整该 SKU，不累加；-a 在已有数量上新增。
  -k 与 -p 会列出整个购物袋的订单复核结果，回车确认才提交订单。
  -s / -s --phone 输出 JSON；-s --duo 输出表格，查询时间为北京时间。
  Duo 的“未开放”表示明确未开放加购，“未知”表示无法确认；均不查询库存。

常用示例：
  python3 duo_killer.py                   # 检查本地账号、地址和 SKU 配置，回车进入购买
  python3 duo_killer.py -k                # 使用预设 SKU 加购并进入结账
  python3 duo_killer.py -s --phone          # 推荐的 iPhone 及 SKU
  python3 duo_killer.py -s --duo            # Duo 配置、价格与加购状态表格
  python3 duo_killer.py -k MK2W4CH -num 2   # 调整为共两件，再复核整个购物袋下单
  python3 duo_killer.py -a MK2W4CH -num 2   # 仅向购物袋新增两件
  python3 duo_killer.py -l --sms -log       # 短信登录并记录日志
  python3 duo_killer.py -me --auth work.json # 使用指定认证文件；不存在时创建并登录
  python3 duo_killer.py -bag               # 查看购物袋
  python3 duo_killer.py -o                 # 查看当前页订单
  python3 duo_killer.py -address           # 配置快捷送货地址/取货联系人
  python3 duo_killer.py -p                 # 选择送货/自提并复核购物袋
  python3 duo_killer.py -purl              # 从保存的支付链接生成付款码
  ./run.sh -h                         # 启动脚本支持同样的参数
""",
        add_help=False)
    parser.add_argument("-h", "--help", action="help", help="显示全部指令和描述后退出")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("-address", action="store_true", help="交互配置本地快捷收货地址和取货联系人，不登录、不下单")
    actions.add_argument("-s", action="store_true", help="匿名查询官网推荐商品、价格及 SKU；系列列出可选配置，不查询库存")
    actions.add_argument("-k", nargs="?", const="", metavar="SKU", help="检查并按需登录，加购后复核下单；省略 SKU 使用初始化预设（首次默认 MK2U4CH），-num 指定目标总数量")
    actions.add_argument("-l", action="store_true", help="完成账号认证和商城 authx 回调，验证后保存 Cookie")
    actions.add_argument("-a", nargs="?", const="", metavar="SKU",
                        help="新增 -num 件商品（默认 1）并返回 item ID；不传 SKU 使用初始化预设（首次默认 MK2U4CH）")
    actions.add_argument("-p", action="store_true", help="列出整个购物袋及总金额，回车确认下单并输出支付宝付款码")
    actions.add_argument("-bag", action="store_true", help="查询当前购物袋，输出商品 item ID、SKU 和数量")
    actions.add_argument("-c", action="store_true", help="检查商城菜单登录状态，不代表账户/结账认证已通过")
    actions.add_argument("-me", action="store_true", help="读取账户姓名和 Apple ID 邮箱；需要有效的账户页认证")
    actions.add_argument("-o", action="store_true", help="查看当前页订单编号、商品、数量、配送/付款状态及详情链接，不自动翻页；必要时完成订单页认证")
    actions.add_argument("-purl", nargs="?", const="", metavar="URL",
                         help="获取支付宝实际付款码；不传 URL 时使用 payment_url.txt 中的链接")
    parser.add_argument("-log", action="store_true", help="输出调试日志，并写入 duo_killer.log")
    parser.add_argument("-auth", "--auth", dest="auth_file", metavar="JSON",
                        help="指定账号认证 JSON；默认使用脚本同目录 auth.json，不存在时按需创建并登录")
    parser.add_argument("-num", type=int, metavar="N", help="仅搭配 -k 或 -a：-k 的目标总数量，或 -a 的新增数量；正整数，默认 1")
    shop_filters = parser.add_mutually_exclusive_group()
    shop_filters.add_argument("--phone", action="store_true", help="配合 -s，仅查询推荐的 iPhone 系列及 SKU，不含配件")
    shop_filters.add_argument("--duo", action="store_true", help="配合 -s，以表格显示 Duo 短 SKU、配置、价格及加购状态；查询时间为北京时间，无法确认时显示未知，不实际加购")
    parser.add_argument("--sms", action="store_true", help="配合 -l 或 -k 登录时强制使用短信；默认优先设备验证码")
    args = parser.parse_args(argv)
    if args.num is not None and (args.num < 1 or (args.k is None and args.a is None)):
        parser.error("-num 必须是正整数，并与 -k 或 -a 一起使用")
    quantity = args.num if args.num is not None else 1
    if args.phone and not args.s:
        parser.error("--phone 必须与 -s 一起使用")
    if args.duo and not args.s:
        parser.error("--duo 必须与 -s 一起使用")
    if args.sms and not (args.l or args.k is not None):
        parser.error("--sms 必须与 -l 或 -k 一起使用")
    initializing = args.k is None and args.a is None and not args.p and args.purl is None and not (args.s or args.l or args.bag or args.c or args.me or args.o or args.address)
    previous_auth_path = AUTH_PATH
    try:
        if args.auth_file is not None:
            AUTH_PATH = resolve_auth_path(args.auth_file)
        if args.log:
            setup_logging()
        if initializing:
            result = initialize()
        elif args.address:
            from checkout_options import configure_address
            result = configure_address(ROOT / "checkout.json", AUTH_PATH)
        elif args.s:
            from shop_catalog import query_duo, query_store
            result = query_duo(request_get) if args.duo else query_store(request_get, phone_only=args.phone)
        elif args.k is not None:
            result = purchase_sku(args.k or preset_sku(), sms=args.sms, quantity=quantity)
        elif args.l:
            result = login(sms=args.sms)
        elif args.purl is not None:
            result = {"success": True, **get_payment_qr(args.purl or None)}
        elif args.bag or args.c or args.me or args.o:
            with requests.Session() as session:
                authenticate(session, strong=args.me or args.o)
                try:
                    if args.c:
                        result = check_login(session)
                    elif args.me:
                        result = account_info(session)
                    elif args.o:
                        result = order_list(session)
                    else:
                        result = {"success": True, "items": bag_items(session)}
                finally:
                    save_auth(session)
        else:
            result = pay_bag() if args.p else add_to_bag(args.a or preset_sku(), quantity=quantity)
        if args.s and args.duo:
            from shop_catalog import format_duo_table
            print(format_duo_table(result))
        elif args.o:
            print(format_order_table(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if (initializing or args.k is not None or args.p or args.purl is not None) and result.get("qr_image"):
            if sys.platform == "darwin" and sys.stdout.isatty():
                subprocess.run(["open", result["qr_image"]], check=False)
            if sys.stdin.isatty() and sys.stdout.isatty():
                wait_for_order_query()
    except (KeyboardInterrupt, EOFError):
        log.info("用户取消，脚本退出。")
        raise SystemExit(130)
    except (OSError, ValueError, KeyError, TypeError, requests.RequestException) as error:
        message = "网络请求失败，请检查连接后重试。" if isinstance(error, requests.RequestException) else str(error)
        log.error("执行失败：%s", message)
        print(json.dumps({"success": False, "error": message}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    finally:
        AUTH_PATH = previous_auth_path


if __name__ == "__main__":
    main()
