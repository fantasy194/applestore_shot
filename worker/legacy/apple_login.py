#!/usr/bin/env python3
"""Apple 登录协议实现，由 duo_killer.py -l 调用。"""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
import uuid

import requests
from srp import _pysrp as srp
import interaction
from interaction import text_input as input
from platform_support import find_browser

HOME = "https://www.apple.com.cn/"
AUTH = "https://idmsa.apple.com.cn/appleauth/auth"
log = logging.getLogger("duo_killer")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
SESSION_HEADERS = ("scnt", "X-Apple-ID-Session-Id", "X-Apple-Auth-Attributes")
TRUST_COOKIE_NAME = re.compile(r"DES[0-9a-fA-F]+")
srp.rfc5054_enable()
srp.no_username_in_x()


class LoginError(Exception):
    def __init__(self, message, *, status=None, codes=()):
        super().__init__(message)
        self.status = status
        self.codes = tuple(codes)


def browser_profile_path(auth_path=None):
    auth_path = Path(auth_path).expanduser() if auth_path is not None else None
    root = auth_path.parent if auth_path else Path(__file__).resolve().parent
    if auth_path is None or auth_path.name == "auth.json":
        profile_name = "native"
    else:
        identity = str(auth_path.resolve()).encode("utf-8")
        profile_name = "native-" + hashlib.sha256(identity).hexdigest()[:12]
    return root / ".chrome-profile" / profile_name


def load_credentials(path):
    if interaction.handler is not None:
        return interaction.handler.credentials(path)
    path = Path(path).expanduser()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LoginError(f"无法读取账号配置，请检查 {path} 是否存在且为有效 JSON。") from None
    if not isinstance(config, dict):
        raise LoginError(f"账号配置必须是 JSON 对象：{path}")
    account, password = config.get("account"), config.get("password")
    if not isinstance(account, str) or not account.strip():
        raise LoginError(f"请在 {path} 中填写非空字符串字段 account。")
    if not isinstance(password, str) or not password:
        raise LoginError(f"请在 {path} 中填写非空字符串字段 password。")
    return account.strip(), password


def stored_device_trust(config, account):
    """Read only this account's unexpired server-issued device credentials."""
    owner = config.get("trust_account", config.get("account", ""))
    if (not isinstance(owner, str) or owner.strip().casefold() != account.strip().casefold()
            or str(config.get("account", "")).strip().casefold() != account.strip().casefold()):
        return []
    rows = config.get("trust_cookies", [])
    # Older session-only exports lack a dedicated trust list. An explicit empty
    # list means cleared/revoked and must never resurrect an older cookie copy.
    if "trust_cookies" not in config:
        rows = [c for group in config.get("cookies", {}).values()
                if isinstance(group, list) for c in group]
    if not isinstance(rows, list):
        return []
    result = []
    for data in rows:
        if (not isinstance(data, dict)
                or not isinstance(data.get("name"), str)
                or not TRUST_COOKIE_NAME.fullmatch(data["name"])
                or data.get("domain") not in ("idmsa.apple.com.cn", ".idmsa.apple.com.cn")
                or data.get("path") != "/" or not isinstance(data.get("value"), str)):
            continue
        value = data["value"].strip('"')
        if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", value):
            continue
        try:
            expires = int(data["expires"]) if data.get("expires") is not None else None
        except (ValueError, TypeError, OverflowError):
            continue
        if expires is not None and expires <= time.time():
            continue
        result.append({"name": data["name"], "value": value, "domain": data["domain"],
                       "path": "/", "expires": expires})
    return result


def apple_url(url):
    u = urlsplit(url)
    if (u.scheme != "https" or u.username or u.password or u.port not in (None, 443)
            or not re.fullmatch(r"(?:www|secure\d*\.www|idmsa)\.apple\.com\.cn", u.hostname or "")):
        raise LoginError("服务返回了非预期的地址，已停止请求。")
    return url


def json_scripts(html):
    for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
        if "application/json" in attrs:
            try:
                yield attrs, json.loads(body)
            except ValueError:
                continue


def find_value(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            result = find_value(child, key)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = find_value(child, key)
            if result is not None:
                return result
    return None


def password_key(password, salt, iterations, protocol):
    if protocol not in ("s2k", "s2k_fo"):
        raise LoginError("服务返回了不支持的 SRP 协议。")
    if not isinstance(iterations, int) or not 1 <= iterations <= 1_000_000:
        raise LoginError("SRP 迭代次数不合理。")
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    if protocol == "s2k_fo":
        digest = digest.hex().encode("ascii")
    return hashlib.pbkdf2_hmac("sha256", digest, salt, iterations, dklen=32)


def hashcash(bits, challenge, max_seconds=30):
    bits = int(bits)
    if not 0 <= bits <= 26 or not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,256}", challenge):
        raise LoginError("Hashcash 挑战参数不合理。")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    prefix = f"1:{bits}:{stamp}:{challenge}::"
    started = time.monotonic()
    for counter in range(1 << 30):
        token = prefix + str(counter)
        if int.from_bytes(hashlib.sha1(token.encode()).digest(), "big") >> (160 - bits) == 0:
            return token
        if counter % 4096 == 0 and time.monotonic() - started > max_seconds:
            break
    raise LoginError("Hashcash 计算超时。")


class AppleStoreLogin:
    def __init__(self, user_agent=UA, auth_path=None, session=None, *, renew_device_trust=False):
        self.session = session if session is not None else requests.Session()
        if session is None:
            self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "zh-CN,zh;q=0.9"})
        self.auth_path = Path(auth_path).expanduser() if auth_path is not None else None
        self.store_request = None
        self.store_referrer = None
        self.auth_headers = {}
        self.hc = {}
        self.account = None
        self.renew_device_trust = renew_device_trust
        self.trust_persistence_ready = not renew_device_trust

    @property
    def browser_profile(self):
        # 同目录的多个账号配置不能共用浏览器 Cookie；散列完整路径也避免同名文件冲突。
        return browser_profile_path(self.auth_path)

    def mark_rejected_browser_context(self):
        if self.auth_path is None:
            return
        self.browser_profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = self.browser_profile / ".authx-541"
        marker.touch(mode=0o600)
        print("商城回调返回 541：已标记本次浏览器上下文，下次登录将保留旧配置并创建新配置；本次不自动重试。")

    def prepare_browser_profile(self):
        profile = self.browser_profile
        if (profile / ".authx-541").exists():
            # 不移动仍被 Chrome 使用的配置，也不删除旧数据。
            if os.path.lexists(profile / "SingletonLock"):
                raise LoginError("待重建的专用 Chrome 配置仍有进程锁，请先关闭该浏览器后重试。")
            backup = profile.with_name("native-rejected-" + uuid.uuid4().hex)
            profile.rename(backup)
            print(f"上次商城回调返回 541，旧浏览器配置已保留至 {backup}，本次重新取得上下文。")
        profile.mkdir(mode=0o700, exist_ok=True)
        profile.chmod(0o700)
        return profile

    def initialize_browser_context(self):
        started = time.monotonic()
        log.info("[浏览器上下文] 开始初始化")
        profile_root = self.browser_profile.parent
        # 正常启动与旧自动化启动使用不同配置，保留旧目录但不继承其状态。
        try:
            from playwright.sync_api import sync_playwright, Error as BrowserError
        except ImportError:
            raise LoginError("首次获取浏览器 Cookie 需要 Playwright，请使用当前 Python 安装 requirements.txt。") from None
        try:
            chrome = find_browser()
        except ValueError as error:
            raise LoginError(str(error)) from None
        proxies = requests.utils.get_environ_proxies(HOME)
        proxy_url = proxies.get("https") or proxies.get("all")
        profile_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile_root.chmod(0o700)
        profile = self.prepare_browser_profile()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        args = [str(chrome), "--user-data-dir=" + str(profile),
                "--remote-debugging-port=" + str(port), "--remote-debugging-address=127.0.0.1",
                "--restore-last-session", "--no-first-run", "--no-default-browser-check",
                "--window-size=500,640"]
        if proxy_url:
            parsed = urlsplit(proxy_url)
            if parsed.username or parsed.password:
                raise LoginError("自动获取上下文暂不支持需要账号密码的代理，请使用本机无认证代理。")
            args.append("--proxy-server=" + proxy_url)
        print(f"正常启动 Chrome，使用专用配置：{profile}。自动获取上下文后关闭并保留浏览器数据。")
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        interaction.report_stage("正在获取浏览器上下文", browser_pid=process.pid)
        try:
            with sync_playwright() as playwright:
                browser = None
                deadline = time.monotonic() + 30
                while browser is None:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise LoginError("未能连接脚本启动的 Chrome，请关闭占用专用配置的浏览器后重试。")
                    try:
                        browser = playwright.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{port}", timeout=1000, no_defaults=True)
                    except BrowserError:
                        time.sleep(0.2)
                try:
                    context = browser.contexts[0]
                    page = context.pages[0] if context.pages else context.new_page()
                    # 两个页面仍完成正常加载；Cookie 就绪后立即继续，不固定等待十秒。
                    for url in (HOME, HOME + "shop/bag"):
                        reply = page.goto(url, wait_until="load", timeout=45000)
                        if reply is None or reply.status >= 400:
                            raise LoginError("Chrome 未能正常打开官网页面，未更新上下文。")
                    for _ in range(80):
                        cookies = context.cookies()
                        if any(c["name"] == "shld_bt_ck" and c["value"]
                               and c["domain"].lstrip(".") == "apple.com.cn"
                               and (c.get("expires", -1) < 0 or c["expires"] > time.time()) for c in cookies):
                            break
                        page.wait_for_timeout(250)
                    else:
                        raise LoginError("Chrome 尚未取得所需浏览器上下文，已停止；请检查浏览器页面或代理后重试。")
                    user_agent = page.evaluate("navigator.userAgent")
                finally:
                    # 关闭专用配置中本次及历史恢复的标签页，避免下次启动继续累积。
                    # 保留 restore-last-session，让 Chrome 继续持久保存会话 Cookie。
                    for owned_context in browser.contexts:
                        for opened_page in list(owned_context.pages):
                            try:
                                opened_page.close()
                            except BrowserError:
                                pass
                    try:
                        browser.new_browser_cdp_session().send("Browser.close")
                    except BrowserError:
                        pass
        except BrowserError:
            raise LoginError("Chrome 初始化失败，请检查网络/代理及专用浏览器配置。") from None
        finally:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            interaction.report_stage("浏览器已关闭", browser_pid=None)
        # 成功采集后整组替换旧非认证上下文，不拼接两次会话中的 Cookie。
        for cookie in list(self.session.cookies):
            if (cookie.domain.lstrip(".") in ("apple.com.cn", "www.apple.com.cn")
                    and cookie.name not in {"as_ltn_cn", "as_cn", "as_disa", "as_rec", "myacinfo", "acn01"}):
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        count = 0
        for c in cookies:
            if c["domain"].lstrip(".") not in ("apple.com.cn", "www.apple.com.cn"):
                continue
            if c["name"] in {"as_ltn_cn", "as_cn", "as_disa", "as_rec", "myacinfo", "acn01"}:
                continue
            self.session.cookies.set_cookie(requests.cookies.create_cookie(
                c["name"], c["value"], domain=c["domain"], path=c["path"], secure=c["secure"],
                expires=int(c["expires"]) if c["expires"] > 0 else None,
                rest={"HttpOnly": None} if c.get("httpOnly") else {}))
            count += 1
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "zh-CN,zh;q=0.9"})
        print(f"Chrome 初始化完成，已取得 {count} 个商城上下文 Cookie。")
        log.info("[浏览器上下文] 初始化耗时 %.2fs", time.monotonic() - started)
        return True

    def restore_trust(self):
        if self.auth_path is None:
            return
        config = json.loads(self.auth_path.read_text(encoding="utf-8"))
        # The dedicated trust list is authoritative over generic session copies.
        for cookie in list(self.session.cookies):
            if TRUST_COOKIE_NAME.fullmatch(cookie.name) and cookie.domain.lstrip(".") == "idmsa.apple.com.cn":
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        if self.renew_device_trust:
            log.info('[设备信任] 本次使用官方二次验证更新信任；旧保存配置保留，不自动删除。')
            return
        restored = stored_device_trust(config, self.account)
        for data in restored:
            cookie = requests.cookies.create_cookie(
                data["name"], data["value"], domain=data["domain"], path="/", secure=True,
                expires=data.get("expires"), rest={"HttpOnly": None})
            self.session.cookies.set_cookie(cookie)
        if restored:
            log.info("[设备信任] 已恢复本账号的有效信任凭证，优先复用本机验证结果。")
            interaction.report_stage("已恢复本机设备信任，正在登录")

    def update_trust(self, response):
        if not self.trust_persistence_ready:
            return
        if self.auth_path is None or not self.account:
            return
        # CookieJar 已由 requests 更新；同时检测过期/删除的 Set-Cookie，避免旧值复活。
        raw_headers = getattr(response.raw, "headers", None)
        values = (raw_headers.getlist("Set-Cookie") if hasattr(raw_headers, "getlist")
                  else [response.headers.get("Set-Cookie", "")])
        changed = any(re.search(r"(?:^|,\s*)DES[0-9a-fA-F]+=", value) for value in values)
        if not changed and not any(TRUST_COOKIE_NAME.fullmatch(c.name) for c in response.cookies):
            return
        cookies = [{"name": c.name, "value": c.value.strip('"'), "domain": c.domain,
                    "path": c.path, "expires": c.expires}
                   for c in self.session.cookies
                   if TRUST_COOKIE_NAME.fullmatch(c.name) and not c.is_expired()
                   and c.domain.lstrip(".") == "idmsa.apple.com.cn" and c.path == "/"]
        # 重新读取配置，只更新信任字段；运行期间用户修改的其他字段也保留。
        config = json.loads(self.auth_path.read_text(encoding="utf-8"))
        if config.get("account", "").strip().casefold() != self.account.casefold():
            return
        config["trust_cookies"] = cookies
        config["trust_account"] = self.account
        fd, tmp = tempfile.mkstemp(dir=self.auth_path.parent, prefix=".auth-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(config, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(tmp, self.auth_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print("本机设备信任已保存，下次登录将自动尝试复用。" if cookies else "服务端已清除本机设备信任。")

    def request(self, method, url, **kwargs):
        apple_url(url)
        is_auth_host = urlsplit(url).hostname == "idmsa.apple.com.cn"
        if is_auth_host:
            prepared = self.session.prepare_request(requests.Request(method, url, headers=kwargs.get("headers")))
            cookie_header = prepared.headers.get("Cookie", "")
            # Apple DES 值按带引号的格式发送，其他 Cookie 保持原样。
            quoted = re.sub(r'(^|;\s*)(DES[0-9a-fA-F]+)=([^;]*)',
                            lambda m: m[1] + m[2] + '="' + m[3].strip('"') + '"', cookie_header)
            if quoted != cookie_header:
                kwargs["headers"] = {**(kwargs.get("headers") or {}), "Cookie": quoted}
        # 不自动重试登录，也不把认证头随重定向转发。
        if self.store_request is not None:
            path = urlsplit(url).path
            stage = ("Apple 账号认证" if is_auth_host else
                     "交换商城会话凭证" if path.endswith('/signIn/idms/authx') else
                     "初始化结算会话" if path == '/shop/checkout/start' else
                     "读取结算页面" if path == '/shop/checkout' else "登录商城")
            response = self.store_request(self.session, stage, url, method=method,
                                          allow_redirects=False, **kwargs)
        else:
            response = self.session.request(method, url, timeout=(10, 30), allow_redirects=False, **kwargs)
            log.info("%s %s%s → %s", method, urlsplit(url).hostname, urlsplit(url).path, response.status_code)
        if is_auth_host:
            self.update_trust(response)
        return response

    def page(self, url, *, method="GET", data=None):
        for _ in range(10):
            headers = {"Referer": self.store_referrer} if self.store_referrer else {}
            if method == "POST":
                headers.update({"Origin": "https://" + urlsplit(self.store_referrer or url).netloc,
                                "Content-Type": "application/x-www-form-urlencoded"})
            response = self.request(method, url, headers=headers, **({"data": data} if method == "POST" else {}))
            if response.status_code not in (301, 302, 303, 307, 308):
                self.expect(response, (200,))
                self.store_referrer = response.url
                return response
            location = response.headers.get("Location")
            if not location:
                raise LoginError("跳转响应缺少 Location。")
            target = apple_url(urljoin(url, location))
            if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
                method, data = "GET", None
            if method == "POST" and urlsplit(target).netloc != urlsplit(url).netloc:
                raise LoginError("结账表单跳转到了不同主机，已停止发送。")
            url = target
        raise LoginError("页面跳转次数过多。")

    @staticmethod
    def expect(response, allowed):
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        errors = data.get("serviceErrors") if isinstance(data, dict) else None
        if response.status_code not in allowed or errors:
            codes = [str(x.get("code", "")) for x in (errors or []) if isinstance(x, dict)]
            codes = [x for x in codes if re.fullmatch(r"[-A-Za-z0-9_]{1,64}", x)]
            raise LoginError(f"请求失败：HTTP {response.status_code}，错误代码 {codes}。"
                             "未输出响应正文，以免泄露账号或令牌。",
                             status=response.status_code, codes=codes)
        return data

    def remember_headers(self, response):
        for key in SESSION_HEADERS:
            if response.headers.get(key):
                self.auth_headers[key] = response.headers[key]
        for key in ("X-Apple-HC-Bits", "X-Apple-HC-Challenge"):
            if response.headers.get(key):
                self.hc[key] = response.headers[key]

    def auth(self, method, path, allowed=(200,), headers=None, **kwargs):
        response = self.request(method, AUTH + path,
                                headers={**self.auth_headers, **(headers or {})}, **kwargs)
        self.remember_headers(response)
        try:
            data = self.expect(response, allowed)
        except LoginError as error:
            if error.status == 401 and "-20101" in error.codes:
                config_path = self.auth_path or "auth.json"
                raise LoginError(
                    "Apple 账号或密码验证失败（HTTP 401，错误代码 -20101）。"
                    f"请检查 {config_path} 中的 account 和 password，注意大小写、输入法及首尾空格；"
                    "修改后重新运行 -l。若确认账密正确，请先在官网验证登录；此错误不一定由密码输错导致。",
                    status=error.status, codes=error.codes) from None
            raise
        if not isinstance(data, dict):
            raise LoginError("认证接口返回了非预期 JSON。")
        return response, data

    def flyout(self):
        response = self.request("GET", HOME + "cn/shop/bag/flyout",
                                params={"apikey": self.api_key, "l": HOME},
                                headers={"Referer": HOME, "Accept": "*/*"})
        data = self.expect(response, (200,))
        if not isinstance(data, dict) or not isinstance(data.get("links"), list):
            raise LoginError("首页菜单未返回有效的 links，无法判断登录状态。")
        return [link for link in data["links"] if isinstance(link, dict)]

    def bootstrap(self, signin_url=None, *, signin_response=None):
        if signin_url is None:
            home = self.page(HOME)
            key = re.search(r'<meta\b[^>]*\bname=["\']globalnav-store-key["\'][^>]*\bcontent=["\']([^"\']+)',
                            home.text, re.I)
            if not key:
                raise LoginError("官网首页未提供商城 API key，无法取得登录入口。")
            self.api_key = key[1]
            links = [link.get("url") for link in self.flyout() if link.get("type") == "signIn"]
            if len(links) != 1 or not isinstance(links[0], str) or not links[0]:
                raise LoginError("首页菜单未提供唯一的登录链接，已停止，未改走账户页。")
            entry = apple_url(urljoin(home.url, links[0]))
        else:
            entry = apple_url(signin_url)
        allowed_paths = {"/shop/signIn", "/shop/signIn/account", "/shop/signIn/orders"} if signin_url else {"/shop/signIn"}
        if urlsplit(entry).hostname == "idmsa.apple.com.cn" or urlsplit(entry).path not in allowed_paths:
            raise LoginError("首页菜单返回了非预期的商城登录入口。")
        if signin_response is not None:
            if signin_response.url != entry or signin_response.status_code != 200:
                raise LoginError("已有登录页与本次登录入口不匹配，已停止。")
            page = signin_response
            self.store_referrer = page.url
            log.info("复用当前会话已取得的登录页，跳过重复 GET。")
        else:
            page = self.page(entry)
        model = next((v for attrs, v in json_scripts(page.text) if 'id="init_data"' in attrs), {})
        cfg = find_value(model, "customerLoginIDMS")
        if not isinstance(cfg, dict) or "d" not in cfg:
            raise LoginError("未取得登录页配置；可能是页面改版、网络拦截或会话已登录。")
        cfg = cfg["d"]
        if cfg.get("serviceURL", "").rstrip("/") != AUTH.rsplit("/auth", 1)[0]:
            raise LoginError("登录服务地址与预期不符。")
        store_origin = "https://" + urlsplit(page.url).netloc
        self.store_page = page.url
        callback = cfg.get("callbackSignInUrl")
        if not isinstance(callback, str) or not callback:
            raise LoginError("登录页未提供商城回调地址。")
        self.callback = apple_url(urljoin(page.url, callback))
        if (urlsplit(self.callback).netloc != urlsplit(page.url).netloc
                or urlsplit(self.callback).path != "/shop/signIn/idms/authx"):
            raise LoginError("商城登录回调与预期不符。")
        self.store_headers = {key: value for key, value in model.get("meta", {}).get("h", {}).items()
                              if key.lower() in {"x-aos-stk", "x-as-actk", "x-aos-model-page", "modelversion", "syntax"}}
        if not any(key.lower() == "x-aos-stk" for key in self.store_headers):
            raise LoginError("登录页缺少本次商城请求令牌。")
        if cfg.get("features", {}).get("enableOIDCGrantCode"):
            raise LoginError("站点启用了当前未支持的 OIDC grant-code 回调。")
        frame = "auth-" + str(uuid.uuid4())
        key = cfg["serviceKey"]
        params = {"frame_id": frame, "iframeId": frame, "client_id": key,
                  "redirect_uri": store_origin, "response_type": "code",
                  "response_mode": "web_message", "state": frame,
                  "authVersion": "latest", "language": cfg.get("locale", "zh_CN")}
        response = self.request("GET", AUTH + "/authorize/signin", params=params)
        self.expect(response, (200,))
        boot = [v for attrs, v in json_scripts(response.text) if "boot_args" in attrs]
        domain = find_value(boot, "domainId")
        attrs = response.headers.get("X-Apple-Auth-Attributes") or find_value(boot, "authAttributes")
        if domain is None or not attrs:
            raise LoginError("认证初始化缺少 domainId 或 authAttributes。")
        self.auth_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json", "Origin": "https://idmsa.apple.com.cn",
            "Referer": "https://idmsa.apple.com.cn/", "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
            "X-Apple-Widget-Key": key, "X-Apple-OAuth-Client-Id": key,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": store_origin,
            "X-Apple-OAuth-Response-Mode": "web_message", "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-State": frame, "X-Apple-Frame-Id": frame,
            "X-Apple-Domain-Id": str(domain), "X-Apple-Locale": cfg.get("locale", "zh_CN"),
            "X-Apple-Auth-Attributes": attrs,
            "X-Apple-I-FD-Client-Info": json.dumps({"U": self.session.headers["User-Agent"],
                                                    "L": "zh-CN", "Z": "GMT+08:00", "V": "1.1"}, separators=(",", ":")),
        }
        self.remember_headers(response)
        log.info(f"初始化完成：{urlsplit(page.url).hostname}，domainId={domain}，"
              f"会话 Cookie 数 {len(self.session.cookies)}。")

    def verify_two_factor(self, sms=False):
        # 双重认证初始化会返回 201；这里只允许该步骤的成功状态。
        response, data = self.auth("GET", "", allowed=(200, 201))
        if not data:
            data = next((value for attrs, value in json_scripts(response.text)
                         if "boot_args" in attrs), {})
        phones = find_value(data, "trustedPhoneNumbers") or []
        has_devices = find_value(data, "hasTrustedDevices")
        device_state = "有" if has_devices is True else "无" if has_devices is False else "未知"
        print(f"Apple 返回的认证方式：受信任设备={device_state}，受信任手机号数量={len(phones)}。")
        sms = sms or has_devices is False
        payload = {}
        path = "/verify/trusteddevice/securitycode"
        if not sms:
            if device_state == "未知":
                print("Apple 未明确返回设备状态，请先查看设备；不会自动发送短信。")
            print("设备验证码验证：请查看已登录此 Apple 账号的受信任设备，"
                  "在登录提示中点“允许”，然后输入设备显示的六位验证码。")
            if phones:
                print("设备收不到验证码时，输入 s 切换到短信，无需重新登录。")
            code = input("👉 设备六位验证码（输入 s 改用短信）：" if phones else "👉 设备六位验证码：").strip()
            sms = bool(phones) and code.lower() == "s"
        if sms:
            if not phones:
                raise LoginError("服务器未提供可用的受信任设备或短信号码，无法继续二次认证。")
            for index, phone in enumerate(phones, 1):
                print(f"{index}: {phone.get('obfuscatedNumber', '受信任号码')}")
            index = 0
            if len(phones) > 1:
                if interaction.handler is not None:
                    index = interaction.handler.choose('选择接收短信的受信任号码',
                        [(i, phone.get('obfuscatedNumber', '受信任号码')) for i, phone in enumerate(phones)], 0)
                    if type(index) is not int or not 0 <= index < len(phones):
                        raise LoginError("号码选项无效。")
                else:
                    choice = input("👉 选择接收短信的号码序号：").strip()
                    if not choice.isdecimal() or not 1 <= int(choice) <= len(phones):
                        raise LoginError("号码序号无效。")
                    index = int(choice) - 1
            payload = {"phoneNumber": {"id": phones[index]["id"]}, "mode": "sms"}
            self.auth("PUT", "/verify/phone", allowed=(200, 204), json=payload)
            path = "/verify/phone/securitycode"
            print("短信验证码已请求发送；请输入该号码收到的短信验证码。"
                  "设备弹窗验证码请先输入 d 切换到设备验证。")
            code = input("👉 短信六位验证码（输入 d 改用设备）：").strip()
        for attempt in range(3):
            if sms and code.lower() == "d":
                sms = False
                payload = {}
                path = "/verify/trusteddevice/securitycode"
                print("已切换到设备验证，请输入受信任设备显示的当前验证码。")
                code = input("👉 设备六位验证码：").strip()
            if not re.fullmatch(r"[0-9]{6}", code):
                raise LoginError("验证码必须是六位数字。")
            try:
                self.auth("POST", path, allowed=(200, 204),
                          json={**payload, "securityCode": {"code": code}})
                break
            except LoginError as error:
                if error.status != 400 or error.codes != ("-21669",) or attempt == 2:
                    raise
                print("本次验证码未通过，请确认验证码来源与当前验证方式一致后重新输入。")
                code = input("👉 短信六位验证码（输入 d 改用设备）：" if sms
                             else "👉 设备六位验证码：").strip()
        trust_response, _ = self.auth("GET", "/2sv/trust", allowed=(200, 204))
        if self.renew_device_trust:
            self.trust_persistence_ready = True
            self.update_trust(trust_response)

    def discard_failed_login(self, error):
        auth_names = {"as_ltn_cn", "as_cn", "as_disa", "as_rec", "myacinfo", "acn01"}

        def is_auth(name, domain):
            return name in auth_names or domain.lstrip(".") == "idmsa.apple.com.cn"

        # 同时清理内存，避免账户/结账流程 finally 中的 save_auth 把失败凭证写回。
        for cookie in list(self.session.cookies):
            if is_auth(cookie.name, cookie.domain):
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        if self.auth_path is None or not self.auth_path.exists():
            return
        config = json.loads(self.auth_path.read_text(encoding="utf-8"))
        for group, cookies in config.get("cookies", {}).items():
            config["cookies"][group] = [c for c in cookies if not is_auth(c["name"], c["domain"])]
        fd, temporary = tempfile.mkstemp(dir=self.auth_path.parent, prefix=".auth-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(config, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary, self.auth_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def login(self, account, password, sms=False, *, signin_url=None, signin_response=None):
        try:
            return self._login(account, password, sms, signin_url=signin_url, signin_response=signin_response)
        except (LoginError, requests.RequestException, EOFError, KeyboardInterrupt) as error:
            self.discard_failed_login(error)
            if isinstance(error, LoginError) and (error.status is None or error.status in (400, 401, 403, 409, 541)):
                interaction.report_stage("重新认证未通过", authentication_failed=True)
            if isinstance(error, LoginError) and error.status == 401 and "-20101" in error.codes:
                raise LoginError(
                    "Apple 账号或密码验证失败（HTTP 401，错误代码 -20101）。"
                    "账号、保存的密码和设备信任已保留；本次失败会话已失效，不自动重试。"
                    "此错误不一定由密码输错导致，请先核对官网登录。",
                    status=error.status, codes=error.codes) from None
            raise

    def begin_authentication_context(self):
        """Do not replay an imported session-only IDMS transaction cookie.

        Keep the account, password, store cookies and DES device trust. The
        authorize response issues a fresh aasp for this transaction; it must
        then survive federate/init/complete without being cleared again.
        """
        removed = 0
        for cookie in list(self.session.cookies):
            if (cookie.name == 'aasp' and cookie.domain.lstrip('.') == 'idmsa.apple.com.cn'
                    and cookie.expires is None):
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
                removed += 1
        self.auth_headers = {}
        self.hc = {}
        log.info('[认证上下文] 开始新认证，旧会话级 aasp 数量=%s；商城会话与设备信任保留。', removed)

    def _login(self, account, password, sms=False, *, signin_url=None, signin_response=None):
        self.account = account
        self.begin_authentication_context()
        self.restore_trust()
        if signin_url is None:
            self.bootstrap()
        else:
            # requests 不会发送过期 Cookie；文件里仍有记录不代表当前请求可用。
            apple_url(signin_url)
            prepared = self.session.prepare_request(requests.Request("GET", signin_url))
            context_cookie = re.search(r"(?:^|;\s*)shld_bt_ck=([^;]+)", prepared.headers.get("Cookie", ""))
            if not context_cookie or (self.browser_profile / ".authx-541").exists():
                print("重新认证所需的浏览器上下文不可用，先刷新后继续原登录入口。")
                self.initialize_browser_context()
                signin_response = None  # 浏览器刷新过 Cookie，旧页面的 SSI/STK 不再复用。
            if signin_response is None:
                self.bootstrap(signin_url)
            else:
                self.bootstrap(signin_url, signin_response=signin_response)
        _, data = self.auth("POST", "/federate", params={"isRememberMeEnabled": "true"},
                            json={"accountName": account, "rememberMe": True})
        if data.get("federated"):
            raise LoginError("此账号使用企业联合登录，需要走其身份提供商流程。")
        user = srp.User(account, b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        _, a = user.start_authentication()
        _, challenge = self.auth("POST", "/signin/init", json={
            "a": base64.b64encode(a).decode(), "accountName": account, "protocols": ["s2k", "s2k_fo"]})
        salt = base64.b64decode(challenge["salt"], validate=True)
        user.p = password_key(password, salt, challenge["iteration"], challenge["protocol"])
        m1 = user.process_challenge(salt, base64.b64decode(challenge["b"], validate=True))
        if m1 is None or user.H_AMK is None:
            raise LoginError("SRP 服务端挑战无效。")
        hc = {}
        if self.hc.get("X-Apple-HC-Challenge"):
            hc["X-APPLE-HC"] = hashcash(self.hc["X-Apple-HC-Bits"], self.hc["X-Apple-HC-Challenge"])
        response, data = self.auth("POST", "/signin/complete", allowed=(200, 409), headers=hc,
                                  params={"isRememberMeEnabled": "true"}, json={
                                      "accountName": account, "rememberMe": True, "c": challenge["c"],
                                      "m1": base64.b64encode(m1).decode(),
                                      "m2": base64.b64encode(user.H_AMK).decode()})
        if response.status_code == 409:
            if data.get("authType") != "hsa2":
                raise LoginError("服务器要求额外认证，但不属于当前支持的 hsa2 验证码流程。")
            self.verify_two_factor(sms)
        elif data.get("authType") not in ("hsa2", "hsa", "sa", "non-sa"):
            raise LoginError("密码认证返回未知状态，未宣告登录成功。")
        print("Apple 账号认证完成。")
        if signin_url is not None:
            if urlsplit(self.store_page).path == "/shop/signIn/account":
                return self.finish_store_login(account=True)
            if urlsplit(self.store_page).path == "/shop/signIn/orders":
                return self.finish_store_login(orders=True)
            return self.finish_store_login(checkout=True)
        return self.finish_store_login()

    def finish_store_login(self, *, checkout=False, account=False, orders=False):
        headers = {**self.store_headers, "Origin": "https://" + urlsplit(self.store_page).netloc,
                   "Referer": self.store_page, "X-Requested-With": "Fetch", "Accept": "*/*",
                   "Content-Type": "application/x-www-form-urlencoded",
                   "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
        prepared = self.session.prepare_request(requests.Request("POST", self.callback, headers=headers))
        names = {part.strip().partition("=")[0] for part in prepared.headers.get("Cookie", "").split(";")}
        log.info("商城回调 Cookie：" + "，".join(
            f"{name}={'有' if name in names else '无'}" for name in ("myacinfo", "acn01", "shld_bt_m", "shld_bt_ck")))
        if "myacinfo" not in names:
            raise LoginError("账号认证后缺少可发送给商城的 myacinfo，未提交回调。")
        response = self.request("POST", self.callback, data={"grantCode": ""}, headers=headers)
        after = self.session.prepare_request(requests.Request('POST', self.callback))
        after_names = {part.strip().partition('=')[0] for part in after.headers.get('Cookie', '').split(';')}
        if 'myacinfo' not in after_names:
            log.info('商城回调响应后 myacinfo 已不可发送，按服务器返回的凭证有效期处理；后续结算可能再次要求账号认证。')
        if response.status_code == 541:
            self.mark_rejected_browser_context()
            log.info('商城回调诊断：HTTP 541；Content-Type=%s；响应字节数=%s；'
                     'Apple 账号认证已完成；未记录响应正文或凭据。',
                     response.headers.get('Content-Type', ''), len(response.content))
            raise LoginError('Apple 账号认证已完成，但商城会话交换被拒绝（HTTP 541）。'
                             '这不是验证码错误；商城登录尚未完成。', status=541)
        data = self.expect(response, (200,))
        head = data.get("head", {}) if isinstance(data, dict) else {}
        target = head.get("data", {}).get("url")
        if head.get("status") != 302 or not isinstance(target, str) or not target:
            raise LoginError("商城 authx 未返回成功跳转，未保存为已登录会话。")
        target = apple_url(urljoin(self.store_page, target))
        if account or orders:
            redirect = head["data"]
            expected_path = "/shop/order/list" if orders else "/shop/account/home"
            if (urlsplit(target).netloc != urlsplit(self.store_page).netloc
                    or urlsplit(target).path != expected_path
                    or str(redirect.get("method", "get")).lower() != "get"
                    or redirect.get("args")):
                raise LoginError("认证未返回预期的订单列表跳转，已停止。" if orders else "账户认证未返回预期的账户页跳转，已停止。")
            return self.page(target)
        if checkout:
            redirect = head["data"]
            method = str(redirect.get("method", "get")).upper()
            args = redirect.get("args", {})
            if (urlsplit(target).netloc != urlsplit(self.store_page).netloc
                    or urlsplit(target).path not in ("/shop/checkout/start", "/shop/checkout")
                    or method not in ("GET", "POST") or not isinstance(args, dict)
                    or (method == "GET" and args)
                    or (method == "POST" and (urlsplit(target).path != "/shop/checkout/start"
                        or set(args) != {"pltn"} or not isinstance(args["pltn"], str) or not args["pltn"]))):
                raise LoginError("结账认证返回了未支持的跳转方式或参数，已停止。")
            print(f"结账认证回调成功，按 {method} 继续 checkout/start。")
            return self.page(target, method=method, data=args if method == "POST" else None)
        if not any(c.name == "as_ltn_cn" and c.value and not c.is_expired()
                   and c.domain.lstrip(".") == "apple.com.cn" and c.path == "/" for c in response.cookies):
            raise LoginError("商城 authx 没有下发有效的 as_ltn_cn，未保存为已登录会话。")
        self.page(target)
        states = {link.get("type") for link in self.flyout()} & {"signIn", "signOut"}
        if states != {"signOut"}:
            raise LoginError("商城首页菜单未确认登录，未保存为已登录会话。")
        print("商城已下发 as_ltn_cn，首页菜单验证通过。")
