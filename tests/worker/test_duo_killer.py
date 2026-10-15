import os
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

import duo_killer
from apple_login import AppleStoreLogin, AUTH, LoginError, apple_url, hashcash, load_credentials, password_key, srp


def response(data, status=200, url="https://secure9.www.apple.com.cn/shop/account/home"):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = json.dumps(data).encode()
    return result


class LoginTests(unittest.TestCase):
    def test_login_requests_share_payment_transport_and_logging(self):
        client = AppleStoreLogin()
        with patch.object(client, "store_request", return_value=response({})) as store, \
                patch.object(client.session, "request", return_value=response({})) as auth:
            client.request("POST", "https://secure8.www.apple.com.cn/shop/signIn/idms/authx",
                           data={"grantCode": ""})
            store.assert_called_once()
            self.assertIs(store.call_args.args[0], client.session)
            self.assertFalse(store.call_args.kwargs["allow_redirects"])
            auth.assert_not_called()
            client.request("POST", "https://idmsa.apple.com.cn/appleauth/auth/federate", json={})
            auth.assert_not_called()
            self.assertEqual(store.call_count, 2)
            self.assertEqual(store.call_args.args[1], "Apple 账号认证")

    def test_bootstrap_follows_home_menu_link_and_keeps_referrer(self):
        home_url = "https://www.apple.com.cn/"
        entry = "https://secure8.www.apple.com.cn/shop/signIn?ssi=fresh"
        model = {"meta": {"h": {"x-aos-stk": "fresh-stk", "modelVersion": "v2"}},
                 "signIn": {"customerLoginIDMS": {"d": {
            "serviceURL": "https://idmsa.apple.com.cn/appleauth", "serviceKey": "new-key",
            "callbackSignInUrl": "/shop/signIn/idms/authx?ssi=fresh"}}}}

        def html(data, url, attrs='id="init_data"'):
            result = response({}, url=url)
            result._content = f'<script type="application/json" {attrs}>{json.dumps(data)}</script>'.encode()
            return result

        home = response({}, url=home_url)
        home._content = b'<meta name="globalnav-store-key" content="fresh-public-key">'
        replies = [home, response({"links": [{"type": "signIn", "url": entry}]}), html(model, entry),
                   html({"direct": {"domainId": 43, "authAttributes": "new-attrs"}},
                        "https://idmsa.apple.com.cn/appleauth/auth/authorize/signin", 'class="boot_args"')]
        client = AppleStoreLogin()
        with patch.object(client, "request", side_effect=replies) as request:
            client.bootstrap()
        calls = request.call_args_list
        self.assertEqual(calls[0].args[1], home_url)
        self.assertNotIn("Referer", calls[0].kwargs["headers"])
        self.assertEqual(calls[1].args[1], home_url + "cn/shop/bag/flyout")
        self.assertEqual(calls[1].kwargs["params"], {"apikey": "fresh-public-key", "l": home_url})
        self.assertEqual(calls[2].args[1], entry)
        self.assertEqual(calls[2].kwargs["headers"]["Referer"], home_url)
        self.assertEqual(client.auth_headers["X-Apple-Domain-Id"], "43")
        self.assertEqual(client.auth_headers["X-Apple-OAuth-Redirect-URI"], "https://secure8.www.apple.com.cn")
        self.assertEqual(client.callback, "https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=fresh")
        self.assertEqual(client.store_headers["x-aos-stk"], "fresh-stk")

    def test_home_menu_missing_or_invalid_login_link_does_not_fallback(self):
        home = response({}, url="https://www.apple.com.cn/")
        home._content = b'<meta name="globalnav-store-key" content="public-key">'
        for links in ([], [{"type": "signOut", "url": "/shop/signOut"}],
                      [{"type": "signIn", "url": "https://other.invalid/shop/signIn"}],
                      [{"type": "signIn", "url": "/shop/signIn/account"}]):
            with self.subTest(links=links):
                client = AppleStoreLogin()
                with patch.object(client, "request", side_effect=[home, response({"links": links})]) as request:
                    with self.assertRaises(LoginError):
                        client.bootstrap()
                    self.assertEqual(request.call_count, 2)

    def test_login_reads_config_without_terminal_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "auth.json"
            config.write_text(json.dumps({"account": " test@example.invalid ", "password": " test-password "}))
            with patch.object(duo_killer, "AUTH_PATH", config), \
                    patch("builtins.input", side_effect=AssertionError("不能提示输入账号")), \
                    patch("getpass.getpass", side_effect=AssertionError("不能提示输入密码")), \
                    patch.object(AppleStoreLogin, "initialize_browser_context", return_value=False), \
                    patch.object(AppleStoreLogin, "login") as login, \
                    contextlib.redirect_stdout(io.StringIO()):
                duo_killer.main(["-l"])
            login.assert_called_once_with("test@example.invalid", " test-password ", False)
            self.assertEqual(json.loads(config.read_text())["password"], " test-password ")

    def test_custom_auth_file_is_created_for_login_and_restored_after_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = root / "accounts" / "work.json"
            original = duo_killer.AUTH_PATH
            seen = []

            def fake_login(sms=False):
                seen.append(duo_killer.AUTH_PATH)
                self.assertTrue(duo_killer.ensure_auth_file())
                return {"success": True, "auth_file": str(duo_killer.AUTH_PATH)}

            with patch.object(duo_killer.Path, "cwd", return_value=root), \
                    patch.object(duo_killer, "login", side_effect=fake_login), \
                    patch("builtins.input", side_effect=["work@example.invalid", "secret"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                duo_killer.main(["-l", "--auth", "accounts/work.json"])
            selected = selected.resolve()
            self.assertEqual(seen, [selected])
            self.assertEqual(duo_killer.AUTH_PATH, original)
            self.assertEqual(json.loads(selected.read_text())["account"], "work@example.invalid")
            if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
                self.assertEqual(selected.stat().st_mode & 0o777, 0o600)

    def test_custom_auth_files_use_distinct_browser_profiles(self):
        from apple_login import browser_profile_path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            default = browser_profile_path(root / "auth.json")
            work = browser_profile_path(root / "work.json")
            personal = browser_profile_path(root / "personal.json")
            self.assertEqual(default, root / ".chrome-profile" / "native")
            self.assertNotEqual(work, default)
            self.assertNotEqual(work, personal)

    def test_invalid_credentials_config_fails_without_leaking_values(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "auth.json"
            with self.assertRaises(LoginError):
                load_credentials(config)
            for data in ({"account": "test", "password": ""}, {"account": "", "password": "SECRET"},
                         {"account": "test", "password": 123}, ["SECRET"]):
                config.write_text(json.dumps(data))
                with self.assertRaises(LoginError) as caught:
                    load_credentials(config)
                self.assertNotIn("SECRET", str(caught.exception))

    def test_kdf_protocols(self):
        digest = hashlib.sha256(b"test-password").digest()
        for protocol, source in (("s2k", digest), ("s2k_fo", digest.hex().encode())):
            expected = hashlib.pbkdf2_hmac("sha256", source, b"salt", 100, dklen=32)
            self.assertEqual(password_key("test-password", b"salt", 100, protocol), expected)
        with self.assertRaises(LoginError):
            password_key("test", b"salt", 100, "unknown")

    def test_login_against_local_srp_verifier(self):
        # 模拟服务端验证实际 m1/m2，避免只检查字段是否存在。
        client = AppleStoreLogin()
        account, password = "test@example.invalid", "synthetic-password"
        salt = b"0123456789abcdef"
        key = password_key(password, salt, 100, "s2k")
        n, g = srp.get_ng(srp.NG_2048, None, None)
        x = srp.gen_x(hashlib.sha256, salt, account, key)
        verifier_bytes = srp.long_to_bytes(pow(g, x, n))
        server = None
        paths = []

        def auth(method, path, **kwargs):
            nonlocal server
            paths.append(path)
            data = kwargs["json"]
            if path == "/federate":
                return response({}), {"federated": False}
            if path == "/signin/init":
                server = srp.Verifier(account, salt, verifier_bytes,
                                      base64.b64decode(data["a"]), hash_alg=srp.SHA256)
                _, b = server.get_challenge()
                return response({}), {"salt": base64.b64encode(salt).decode(),
                                       "b": base64.b64encode(b).decode(), "iteration": 100,
                                       "protocol": "s2k", "c": "fresh-challenge"}
            self.assertEqual(path, "/signin/complete")
            self.assertEqual(data["c"], "fresh-challenge")
            proof = server.verify_session(base64.b64decode(data["m1"]))
            self.assertIsNotNone(proof)
            self.assertEqual(proof, base64.b64decode(data["m2"]))
            return response({}), {"authType": "hsa2"}

        with patch.object(client, "bootstrap"), \
                patch.object(client, "initialize_browser_context"), \
                patch.object(client, "auth", side_effect=auth), \
                patch.object(client, "request") as request, \
                patch.object(client, "page") as page, \
                patch.object(client, "finish_store_login") as finish, \
                patch.object(client, "verify_two_factor") as two_factor:
            client.login(account, password)
            self.assertEqual(paths, ["/federate", "/signin/init", "/signin/complete"])
            request.assert_not_called()
            page.assert_not_called()
            finish.assert_called_once_with()
            for entry, expected in (("/shop/signIn/account?ssi=account", {"account": True}),
                                    ("/shop/signIn/orders?ssi=orders", {"orders": True}),
                                    ("/shop/signIn?ssi=checkout", {"checkout": True})):
                finish.reset_mock()
                client.store_page = "https://secure8.www.apple.com.cn" + entry
                client.login(account, password, signin_url=client.store_page)
                finish.assert_called_once_with(**expected)
            two_factor.assert_not_called()  # 200 + authType=hsa2 本身不代表需要验证码。

    def test_reauthentication_refreshes_unsendable_context_before_bootstrap(self):
        for entry in ("/shop/signIn/account", "/shop/signIn/orders", "/shop/signIn"):
            for state in ("missing", "expired", "wrong-domain", "wrong-path", "valid"):
                with self.subTest(entry=entry, state=state):
                    client = AppleStoreLogin()
                    url = "https://secure9.www.apple.com.cn" + entry + "?ssi=current"
                    if state != "missing":
                        client.session.cookies.set("shld_bt_ck", "test-context",
                            domain=".example.invalid" if state == "wrong-domain" else ".apple.com.cn",
                            path="/unrelated" if state == "wrong-path" else "/",
                            expires=int(time.time()) + (-60 if state == "expired" else 3600))
                    events = []
                    with patch.object(client, "initialize_browser_context", side_effect=lambda: events.append("refresh")), \
                            patch.object(client, "bootstrap", side_effect=lambda target: events.append(target)), \
                            patch.object(client, "auth", side_effect=LoginError("stop before credentials")):
                        with self.assertRaisesRegex(LoginError, "stop before credentials"):
                            client.login("test@example.invalid", "secret", signin_url=url)
                    self.assertEqual(events, ([] if state == "valid" else ["refresh"]) + [url])

    def test_failed_context_refresh_stops_before_credentials(self):
        client = AppleStoreLogin()
        with patch.object(client, "initialize_browser_context", side_effect=LoginError("context unavailable")), \
                patch.object(client, "bootstrap") as bootstrap, patch.object(client, "auth") as auth:
            with self.assertRaisesRegex(LoginError, "context unavailable"):
                client.login("test@example.invalid", "secret",
                             signin_url="https://secure9.www.apple.com.cn/shop/signIn/account?ssi=current")
            bootstrap.assert_not_called()
            auth.assert_not_called()

    def test_authx_requires_fresh_store_cookie_and_menu_confirmation(self):
        for status, issued, menu in ((541, False, True), (200, False, True),
                                     (200, True, False), (200, True, True)):
            with self.subTest(status=status, issued=issued, menu=menu):
                client = AppleStoreLogin()
                client.store_page = "https://secure8.www.apple.com.cn/shop/signIn?ssi=fresh"
                client.callback = "https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=fresh"
                client.store_headers = {"x-aos-stk": "fresh-stk"}
                client.session.cookies.set("myacinfo", "fresh-account-cookie", domain=".apple.com.cn", path="/")
                # 即使会话意外残留旧值，也不能当成本次回调成功。
                client.session.cookies.set("as_ltn_cn", "old", domain=".apple.com.cn", path="/")
                reply = response({"head": {"status": 302, "data": {"url": "https://www.apple.com.cn/"}}}, status)
                if issued:
                    reply.cookies.set("as_ltn_cn", "fresh-store-cookie", domain=".apple.com.cn", path="/")
                with patch.object(client, "request", return_value=reply) as request, \
                        patch.object(client, "page") as page, \
                        patch.object(client, "flyout", return_value=[{"type": "signOut" if menu else "signIn"}]):
                    if status == 200 and issued and menu:
                        client.finish_store_login()
                    else:
                        with self.assertRaises(LoginError):
                            client.finish_store_login()
                    request.assert_called_once()
                    self.assertEqual(request.call_args.kwargs["data"], {"grantCode": ""})
                    self.assertEqual(request.call_args.kwargs["headers"]["x-aos-stk"], "fresh-stk")
                    self.assertEqual(request.call_args.kwargs["headers"]["Referer"], client.store_page)
                    if status != 200 or not issued:
                        page.assert_not_called()

    def test_invalid_srp_challenge(self):
        user = srp.User("test", b"key", hash_alg=srp.SHA256)
        user.start_authentication()
        self.assertIsNone(user.process_challenge(b"salt", b"\0"))

    def test_two_factor_initialization_201_continues_with_fresh_headers(self):
        for status in (200, 201):
            with self.subTest(status=status):
                client = AppleStoreLogin()
                initial = response({}, status)
                initial.headers.update({"scnt": "fresh-scnt", "X-Apple-ID-Session-Id": "fresh-id"})
                verified = response({}, 204)
                verified.headers["scnt"] = "verified-scnt"
                with patch.object(client, "request", side_effect=[initial, verified, response({}, 204)]) as request, \
                        patch("builtins.input", return_value="123456") as prompt:
                    client.verify_two_factor()
                prompt.assert_called_once()
                calls = request.call_args_list
                self.assertEqual(len(calls), 3)
                self.assertTrue(calls[1].args[1].endswith("/verify/trusteddevice/securitycode"))
                self.assertEqual(calls[1].kwargs["json"], {"securityCode": {"code": "123456"}})
                self.assertEqual(calls[1].kwargs["headers"]["scnt"], "fresh-scnt")
                self.assertEqual(calls[1].kwargs["headers"]["X-Apple-ID-Session-Id"], "fresh-id")
                self.assertTrue(calls[2].args[1].endswith("/2sv/trust"))
                self.assertEqual(calls[2].kwargs["headers"]["scnt"], "verified-scnt")

    def test_two_factor_html_boot_args_for_device_and_sms(self):
        boot = {"direct": {"hasTrustedDevices": True, "twoSV": {
            "phoneNumberVerification": {"trustedPhoneNumbers": [
                {"id": 7, "obfuscatedNumber": "***00"}]}}}}
        for sms in (False, True):
            with self.subTest(sms=sms):
                client = AppleStoreLogin()
                initial = response({}, 200)
                initial._content = ('<script type="application/json" class="boot_args">' +
                                    json.dumps(boot) + '</script>').encode()
                replies = [initial] + [response({}, 204) for _ in range(3 if sms else 2)]
                with patch.object(client, "request", side_effect=replies) as request, \
                        patch("builtins.input", side_effect=["123456"]), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    client.verify_two_factor(sms=sms)
                calls = request.call_args_list
                self.assertEqual(calls[0].args, ("GET", AUTH))
                self.assertEqual(calls[-1].args, ("GET", AUTH + "/2sv/trust"))
                if sms:
                    self.assertEqual(calls[1].args, ("PUT", AUTH + "/verify/phone"))
                    self.assertEqual(calls[1].kwargs["json"], {"phoneNumber": {"id": 7}, "mode": "sms"})
                    self.assertIn("短信验证码已请求发送", output.getvalue())
                    path = "/verify/phone/securitycode"
                else:
                    self.assertEqual(len(calls), 3)
                    self.assertIn("受信任设备", output.getvalue())
                    self.assertIn("允许", output.getvalue())
                    path = "/verify/trusteddevice/securitycode"
                self.assertEqual(calls[-2].args, ("POST", AUTH + path))
                self.assertEqual(calls[-2].kwargs["json"]["securityCode"], {"code": "123456"})

    def test_two_factor_auto_selection_and_in_session_sms_fallback(self):
        cases = [
            (True, [{"id": 7}], ["123456"], None),
            (False, [{"id": 7}], ["123456"], 7),
            (None, [{"id": 7}], ["123456"], None),
            (None, [{"id": 7}], ["s", "123456"], 7),
            (False, [{"id": 7}, {"id": 9}], ["2", "123456"], 9),
            (True, [{"id": 7}], ["s", "123456"], 7),
        ]
        for has_devices, phones, inputs, phone_id in cases:
            for html in (False, True):
                with self.subTest(has_devices=has_devices, inputs=inputs, html=html):
                    data = {"hasTrustedDevices": has_devices, "trustedPhoneNumbers": phones}
                    if has_devices is None:
                        del data["hasTrustedDevices"]
                    initial = response(data, 200)
                    if html:
                        initial._content = ('<script class="boot_args" type="application/json">' +
                                            json.dumps({"direct": data}) + '</script>').encode()
                    client = AppleStoreLogin()
                    replies = [initial] + [response({}, 204) for _ in range(3 if phone_id else 2)]
                    with patch.object(client, "request", side_effect=replies) as request, \
                            patch("builtins.input", side_effect=inputs), \
                            contextlib.redirect_stdout(io.StringIO()) as output:
                        client.verify_two_factor()
                    calls = request.call_args_list
                    self.assertEqual(calls[0].args, ("GET", AUTH))
                    if phone_id:
                        self.assertEqual(calls[1].args, ("PUT", AUTH + "/verify/phone"))
                        self.assertEqual(calls[1].kwargs["json"], {"phoneNumber": {"id": phone_id}, "mode": "sms"})
                    else:
                        self.assertEqual(len(calls), 3)
                        self.assertFalse(any(call.args[0] == "PUT" for call in calls))
                    state = "有" if has_devices is True else "无" if has_devices is False else "未知"
                    self.assertIn(f"受信任设备={state}", output.getvalue())
                    path = "/verify/phone/securitycode" if phone_id else "/verify/trusteddevice/securitycode"
                    self.assertEqual(calls[-2].args, ("POST", AUTH + path))
                    self.assertEqual(calls[-2].kwargs["json"]["securityCode"], {"code": "123456"})
                    self.assertEqual(calls[-1].args, ("GET", AUTH + "/2sv/trust"))

    def test_two_factor_no_available_method_stops_without_prompt(self):
        client = AppleStoreLogin()
        with patch.object(client, "request", return_value=response({"hasTrustedDevices": False}, 200)) as request, \
                patch("builtins.input") as prompt:
            with self.assertRaisesRegex(LoginError, "无法继续二次认证"):
                client.verify_two_factor()
        request.assert_called_once()
        prompt.assert_not_called()

    def test_two_factor_invalid_phone_selection_does_not_send_sms(self):
        client = AppleStoreLogin()
        data = {"hasTrustedDevices": False, "trustedPhoneNumbers": [{"id": 7}, {"id": 9}]}
        for choice in ("0", "3", "abc"):
            with self.subTest(choice=choice), \
                    patch.object(client, "request", return_value=response(data, 200)) as request, \
                    patch("builtins.input", return_value=choice), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(LoginError, "号码序号无效"):
                    client.verify_two_factor()
                request.assert_called_once()

    def test_two_factor_initialization_errors_do_not_prompt(self):
        for result in (response({}, 401), response({"serviceErrors": [{"code": "-1"}]}, 201)):
            with self.subTest(status=result.status_code):
                client = AppleStoreLogin()
                with patch.object(client, "request", return_value=result) as request, \
                        patch("builtins.input") as prompt:
                    with self.assertRaises(LoginError):
                        client.verify_two_factor()
                request.assert_called_once()
                prompt.assert_not_called()

    def test_two_factor_bad_code_does_not_trust(self):
        client = AppleStoreLogin()
        failed = response({"serviceErrors": [{"code": "-21669"}]}, 400)
        with patch.object(client, "request", side_effect=[response({}, 201), failed, failed, failed]) as request, \
                patch("builtins.input", return_value="123456"):
            with self.assertRaises(LoginError):
                client.verify_two_factor()
        self.assertEqual(request.call_count, 4)

    def test_sms_can_switch_to_device_before_or_after_rejection(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                client = AppleStoreLogin()
                initial = response({"hasTrustedDevices": False, "trustedPhoneNumbers": [{"id": 7}]}, 200)
                failed = response({"serviceErrors": [{"code": "-21669"}]}, 400)
                failed.headers["scnt"] = "rotated-after-rejection"
                replies = [initial, response({}, 200)] + ([failed] if rejected else []) + [response({}, 204), response({}, 204)]
                inputs = (["111111"] if rejected else []) + ["d", "222222"]
                with patch.object(client, "request", side_effect=replies) as request, \
                        patch("builtins.input", side_effect=inputs), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    client.verify_two_factor()
                calls = request.call_args_list
                self.assertEqual(calls[1].args, ("PUT", AUTH + "/verify/phone"))
                if rejected:
                    self.assertEqual(calls[2].args, ("POST", AUTH + "/verify/phone/securitycode"))
                    self.assertEqual(calls[-2].kwargs["headers"]["scnt"], "rotated-after-rejection")
                self.assertEqual(calls[-2].args, ("POST", AUTH + "/verify/trusteddevice/securitycode"))
                self.assertEqual(calls[-2].kwargs["json"], {"securityCode": {"code": "222222"}})
                self.assertEqual(calls[-1].args, ("GET", AUTH + "/2sv/trust"))
                self.assertNotIn("222222", output.getvalue())

    def test_other_verification_errors_do_not_retry(self):
        client = AppleStoreLogin()
        with patch.object(client, "request", side_effect=[response({}, 200), response({}, 429)]) as request, \
                patch("builtins.input", return_value="123456") as prompt:
            with self.assertRaises(LoginError):
                client.verify_two_factor()
        self.assertEqual(request.call_count, 2)
        prompt.assert_called_once()

    def test_hashcash(self):
        token = hashcash(8, "synthetic-challenge")
        self.assertTrue(token.startswith("1:8:"))
        self.assertIn(":synthetic-challenge::", token)
        self.assertEqual(hashlib.sha1(token.encode()).digest()[0], 0)
        with self.assertRaises(LoginError):
            hashcash(99, "test")

    def test_error_redaction(self):
        result = response({"serviceErrors": [{"code": "-20101", "message": "SECRET"}]}, 401)
        with self.assertRaises(LoginError) as caught:
            AppleStoreLogin.expect(result, (200,))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIn("-20101", str(caught.exception))

    def test_auth_credentials_error_explains_config_without_disclosing_response(self):
        for status, code in ((401, "-20101"), (401, "other"), (400, "-21669")):
            with self.subTest(status=status, code=code), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "auth.json"
                client = AppleStoreLogin(auth_path=path)
                reply = response({"serviceErrors": [{"code": code, "message": "SECRET"}]}, status)
                with patch.object(client, "request", return_value=reply) as request:
                    with self.assertRaises(LoginError) as caught:
                        client.auth("POST", "/signin/complete")
                error = caught.exception
                self.assertEqual((error.status, error.codes), (status, (code,)))
                self.assertNotIn("SECRET", str(error))
                request.assert_called_once()
                if code == "-20101":
                    self.assertIn("账号或密码验证失败", str(error))
                    self.assertIn(str(path), str(error))
                    self.assertIn("account 和 password", str(error))
                else:
                    self.assertNotIn("账号或密码验证失败", str(error))

    def test_host_validation(self):
        for url in ("http://www.apple.com.cn", "https://www.apple.com.cn.evil.invalid/",
                    "https://user:pass@www.apple.com.cn/", "https://www.apple.com.cn:8443/"):
            with self.assertRaises(LoginError):
                apple_url(url)
        apple_url("https://secure9.www.apple.com.cn/shop/signIn/account")


class BrowserInitializationTests(unittest.TestCase):
    def test_authx_541_marks_context_without_retry_or_overwriting_auth(self):
        for status in (541, 401, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "auth.json"
                path.write_text('{"account":"test@example.invalid"}')
                client = AppleStoreLogin(auth_path=path)
                client.store_page = "https://secure9.www.apple.com.cn/shop/signIn?ssi=current"
                client.callback = "https://secure9.www.apple.com.cn/shop/signIn/idms/authx?ssi=current"
                client.store_headers = {"x-aos-stk": "current"}
                client.session.cookies.set("myacinfo", "current", domain=".apple.com.cn", path="/")
                with patch.object(client, "request", return_value=response({}, status)) as request, \
                        patch.object(client, "initialize_browser_context") as initialize:
                    with self.assertRaises(LoginError) as caught:
                        client.finish_store_login()
                    self.assertEqual(caught.exception.status, status)
                    request.assert_called_once()
                    initialize.assert_not_called()
                self.assertEqual((client.browser_profile / ".authx-541").exists(), status == 541)
                self.assertEqual(path.read_text(), '{"account":"test@example.invalid"}')

    def test_rejected_profile_is_preserved_and_replaced_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            client = AppleStoreLogin(auth_path=Path(temp) / "auth.json")
            client.mark_rejected_browser_context()
            state = client.browser_profile / "existing-state"
            state.write_text("old browser state")
            client.prepare_browser_profile()
            self.assertFalse(state.exists())
            self.assertFalse((client.browser_profile / ".authx-541").exists())
            backups = list(client.browser_profile.parent.glob("native-rejected-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "existing-state").read_text(), "old browser state")
            state.write_text("new browser state")
            client.prepare_browser_profile()
            self.assertEqual(state.read_text(), "new browser state")
            self.assertEqual(len(list(client.browser_profile.parent.glob("native-rejected-*"))), 1)

    @unittest.skipIf(os.name == "nt", "POSIX Chrome lock uses a symbolic link")
    def test_rejected_profile_with_chrome_lock_is_not_moved(self):
        with tempfile.TemporaryDirectory() as temp:
            client = AppleStoreLogin(auth_path=Path(temp) / "auth.json")
            client.mark_rejected_browser_context()
            (client.browser_profile / "SingletonLock").symlink_to("test-host-12345")
            with self.assertRaisesRegex(LoginError, "进程锁"):
                client.prepare_browser_profile()
            self.assertTrue((client.browser_profile / ".authx-541").exists())
            self.assertFalse(list(client.browser_profile.parent.glob("native-rejected-*")))

    def test_normal_chrome_reuses_profile_and_replaces_non_auth_context(self):
        def cookie(name, domain=".apple.com.cn"):
            return {"name": name, "value": "test-context", "domain": domain,
                    "path": "/", "secure": True, "expires": -1, "httpOnly": True}
        with tempfile.TemporaryDirectory() as temp, \
                patch("playwright.sync_api.sync_playwright") as factory, \
                patch("apple_login.subprocess.Popen") as process_factory, \
                patch("apple_login.Path.is_file", return_value=True), \
                patch("requests.utils.get_environ_proxies", return_value={"https": "http://127.0.0.1:7890"}):
            process_factory.return_value.poll.return_value = None
            client = AppleStoreLogin(auth_path=Path(temp) / "auth.json")
            client.session.cookies.set("as_sfa", "old-context", domain=".apple.com.cn", path="/")
            client.session.cookies.set("as_ltn_cn", "existing-login", domain=".apple.com.cn", path="/")
            launcher = factory.return_value.__enter__.return_value.chromium
            browser = launcher.connect_over_cdp.return_value
            context = browser.contexts[0]
            browser.contexts = [context]
            context.pages = []
            context.cookies.return_value = [cookie("shld_bt_ck"), cookie("as_pcts"),
                                           cookie("myacinfo"), cookie("other", ".example.invalid")]
            page = context.new_page.return_value
            def open_page():
                context.pages = [page]
                return page
            context.new_page.side_effect = open_page
            page.goto.return_value.status = 200
            page.evaluate.return_value = "real-chrome-UA"
            self.assertTrue(client.initialize_browser_context())
            profile = Path(temp) / ".chrome-profile" / "native"
            if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
                self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            args = process_factory.call_args.args[0]
            self.assertIn("--user-data-dir=" + str(profile), args)
            self.assertIn("--proxy-server=http://127.0.0.1:7890", args)
            self.assertIn("--remote-debugging-address=127.0.0.1", args)
            self.assertNotIn("--no-sandbox", args)
            self.assertNotIn("--enable-automation", args)
            self.assertNotIn("about:blank", args)
            self.assertIn("--restore-last-session", args)
            self.assertEqual(launcher.connect_over_cdp.call_args.kwargs, {"timeout": 1000, "no_defaults": True})
            self.assertEqual([call.args[0] for call in page.goto.call_args_list],
                             ["https://www.apple.com.cn/", "https://www.apple.com.cn/shop/bag"])
            page.wait_for_timeout.assert_not_called()  # 已就绪时不额外等待两个五秒。
            expired = cookie("shld_bt_ck")
            expired['expires'] = time.time() - 60
            context.cookies.side_effect = [[expired], [cookie("shld_bt_ck"), cookie("as_pcts"),
                                                       cookie("myacinfo"), cookie("other", ".example.invalid")]]
            self.assertEqual({c.name for c in client.session.cookies}, {"shld_bt_ck", "as_pcts", "as_ltn_cn"})
            self.assertEqual(client.session.cookies.get("as_ltn_cn"), "existing-login")
            self.assertEqual(client.session.headers["User-Agent"], "real-chrome-UA")
            marker = profile / "existing-state"
            marker.write_text("keep")
            restored_page = unittest.mock.MagicMock()
            context.pages = [page, restored_page]
            self.assertTrue(client.initialize_browser_context())
            page.wait_for_timeout.assert_called_once_with(250)
            self.assertEqual(process_factory.call_count, 2)
            self.assertTrue(all("--user-data-dir=" + str(profile) in call.args[0]
                                for call in process_factory.call_args_list))
            self.assertEqual(marker.read_text(), "keep")
            self.assertEqual(browser.new_browser_cdp_session.return_value.send.call_count, 2)
            context.new_page.assert_called_once()
            self.assertEqual(page.close.call_count, 2)
            restored_page.close.assert_called_once_with()
            launcher.launch.assert_not_called()
            launcher.launch_persistent_context.assert_not_called()

    def test_missing_context_closes_owned_browser_and_keeps_session(self):
        with tempfile.TemporaryDirectory() as temp, patch("playwright.sync_api.sync_playwright") as factory, \
                patch("apple_login.subprocess.Popen") as process_factory, \
                patch("apple_login.Path.is_file", return_value=True), \
                patch("requests.utils.get_environ_proxies", return_value={}):
            process_factory.return_value.poll.return_value = None
            client = AppleStoreLogin(auth_path=Path(temp) / "auth.json")
            client.session.cookies.set("as_ltn_cn", "existing", domain=".apple.com.cn", path="/")
            browser = factory.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value
            context = browser.contexts[0]
            browser.contexts = [context]
            page = unittest.mock.MagicMock()
            restored_page = unittest.mock.MagicMock()
            from playwright.sync_api import Error as BrowserError
            restored_page.close.side_effect = BrowserError("already closed")
            context.pages = [restored_page, page]
            context.cookies.return_value = []
            restored_page.goto.return_value.status = 200
            with self.assertRaisesRegex(LoginError, "尚未取得"):
                client.initialize_browser_context()
            browser.new_browser_cdp_session.return_value.send.assert_called_once_with("Browser.close")
            restored_page.close.assert_called_once_with()
            page.close.assert_called_once_with()
            process_factory.return_value.wait.assert_called_once_with(timeout=10)
            self.assertTrue((Path(temp) / ".chrome-profile").exists())
            self.assertEqual(client.session.cookies.get("as_ltn_cn"), "existing")


class FirstRunTests(unittest.TestCase):
    def test_first_login_prompts_and_completes_two_factor_in_same_run(self):
        for sms in (False, True):
            with self.subTest(sms=sms), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "auth.json"
                with patch.object(duo_killer, "AUTH_PATH", path), \
                        patch("builtins.input", side_effect=["", " test@example.invalid ", "", " password ", "123456"]) as prompt, \
                        patch("apple_login.AppleStoreLogin") as factory, \
                        patch.object(requests.Session, "request", side_effect=AssertionError("测试不能联网")), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    client = factory.return_value
                    client.session = requests.Session()
                    client.auth.return_value = (None, {"hasTrustedDevices": True, "trustedPhoneNumbers": [{"id": 7}]})

                    def complete_login(account, secret, sms):
                        self.assertEqual((account, secret), ("test@example.invalid", " password "))
                        AppleStoreLogin.verify_two_factor(client, sms=sms)
                        client.session.cookies.set("as_ltn_cn", "fresh-session", domain=".apple.com.cn", path="/")

                    client.login.side_effect = complete_login
                    duo_killer.main(["-l"] + (["--sms"] if sms else []))
                data = json.loads(path.read_text())
                self.assertEqual(data["account"], "test@example.invalid")
                self.assertEqual(data["password"], " password ")
                if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(sum("密码（明文显示" in call.args[0] for call in prompt.call_args_list), 2)
                self.assertIn("短信六位验证码" if sms else "设备六位验证码", prompt.call_args.args[0])
                self.assertIn('"success": true', output.getvalue())
                self.assertNotIn(" password ", output.getvalue())
                self.assertNotIn("123456", output.getvalue())
                self.assertEqual(data["cookies"]["account_session"][0]["value"], "fresh-session")
                client.auth.assert_any_call("GET", "/2sv/trust", allowed=(200, 204))

    def test_first_account_read_logs_in_before_reading(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                patch("builtins.input", side_effect=["test@example.invalid", "secret"]), \
                patch.object(duo_killer, "login") as login, \
                patch.object(duo_killer, "account_info", return_value={"logged_in": True}) as account, \
                contextlib.redirect_stdout(io.StringIO()):
            def save_login():
                with requests.Session() as session:
                    session.cookies.set("as_ltn_cn", "fresh-session", domain=".apple.com.cn", path="/")
                    duo_killer.save_auth(session)
            login.side_effect = save_login
            duo_killer.main(["-me"])
            login.assert_called_once_with()
            self.assertEqual(account.call_args.args[0].cookies.get("as_ltn_cn"), "fresh-session")

    def test_cancelled_credentials_leave_no_file_or_network(self):
        for inputs in (EOFError(), ["test@example.invalid", EOFError()],
                       ["test@example.invalid", KeyboardInterrupt()]):
            with tempfile.TemporaryDirectory() as temp, \
                    patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                    patch("builtins.input", side_effect=inputs), \
                    patch.object(requests.Session, "request") as network, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    duo_killer.main(["-l"])
                self.assertFalse(duo_killer.AUTH_PATH.exists())
                network.assert_not_called()

    def test_existing_config_is_not_overwritten_even_if_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            for original in ('{"account":"local","password":"secret","custom":"keep"}', 'invalid-json'):
                path.write_text(original)
                with patch.object(duo_killer, "AUTH_PATH", path):
                    duo_killer.ensure_auth_file()
                self.assertEqual(path.read_text(), original)

    def test_help_and_payment_url_do_not_create_auth(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                patch.object(duo_killer, "get_payment_qr", return_value={"success": True}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                duo_killer.main(["-h"])
            self.assertEqual(stopped.exception.code, 0)
            duo_killer.main(["-purl"])
            self.assertFalse(duo_killer.AUTH_PATH.exists())


class FailedLoginLifecycleTests(unittest.TestCase):
    def test_failed_auth_discards_failed_session_but_keeps_identity_password_and_trust(self):
        failures = (LoginError("rejected", status=401, codes=("-20101",)),
                    LoginError("bad code", status=400, codes=("-21669",)),
                    LoginError("callback failed", status=541), requests.Timeout("timeout"), EOFError())
        for entry in (None, "https://secure9.www.apple.com.cn/shop/signIn/account?ssi=current"):
            for error in failures:
                with self.subTest(entry=entry, error=type(error).__name__, status=getattr(error, "status", None)), \
                        tempfile.TemporaryDirectory() as temp, \
                        patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"):
                    path = duo_killer.AUTH_PATH
                    config = {"account": "test@example.invalid", "password": "old-secret", "custom": "keep",
                              "trust_cookies": [{"name": "DESabcdef", "value": "trust", "domain": ".idmsa.apple.com.cn", "path": "/"}],
                              "cookies": {"account_session": [], "server_managed": [], "with_expiry": [], "expiry_unknown": []}}
                    for name, domain in (("as_ltn_cn", ".apple.com.cn"), ("myacinfo", ".apple.com.cn"),
                                         ("idmsa-session", ".idmsa.apple.com.cn"), ("shld_bt_ck", ".apple.com.cn")):
                        config["cookies"]["account_session"].append({"name": name, "value": "old",
                                                                    "domain": domain, "path": "/"})
                    path.write_text(json.dumps(config))
                    client = AppleStoreLogin(auth_path=path)
                    duo_killer.load_auth(client.session, config)
                    with patch.object(client, "_login", side_effect=error) as attempt:
                        with self.assertRaises(type(error)):
                            client.login(config["account"], config["password"], signin_url=entry)
                        attempt.assert_called_once()
                    # 模拟调用方 finally 保存，认证 Cookie 不能复活。
                    duo_killer.save_auth(client.session)
                    saved = json.loads(path.read_text())
                    self.assertEqual({c["name"] for group in saved["cookies"].values() for c in group}, {"shld_bt_ck"})
                    self.assertEqual(saved["trust_cookies"], config["trust_cookies"])
                    self.assertEqual(saved["custom"], "keep")
                    self.assertEqual((saved["account"], saved["password"]),
                                     (config["account"], "old-secret"))
                    with patch("builtins.input", side_effect=AssertionError("不能要求重置账号")):
                        self.assertFalse(duo_killer.ensure_auth_file())
                    if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reentered_other_account_does_not_reuse_device_trust(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"):
            duo_killer.AUTH_PATH.write_text(json.dumps({"account": "", "password": "", "trust_account": "old@example.invalid",
                                                  "trust_cookies": [{"name": "DESabcdef"}], "custom": "keep"}))
            with patch("builtins.input", side_effect=["new@example.invalid", "secret"]):
                self.assertTrue(duo_killer.ensure_auth_file())
            saved = json.loads(duo_killer.AUTH_PATH.read_text())
            self.assertEqual(saved["trust_cookies"], [])
            self.assertNotIn("trust_account", saved)
            self.assertEqual(saved["custom"], "keep")

    def test_cancelled_reentry_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"):
            original = '{"account":"","password":"","custom":"keep"}'
            duo_killer.AUTH_PATH.write_text(original)
            with patch("builtins.input", side_effect=["test@example.invalid", EOFError()]):
                with self.assertRaises(ValueError):
                    duo_killer.ensure_auth_file()
            self.assertEqual(duo_killer.AUTH_PATH.read_text(), original)


class BugAppleLoginTests(unittest.TestCase):
    def test_browser_context_survives_but_login_cookies_are_removed_after_auth_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            config = {"account": "test@example.invalid", "password": "password", "trust_cookies": [],
                      "cookies": {"account_session": [
                          {"name": "as_ltn_cn", "value": "old-login", "domain": ".apple.com.cn", "path": "/"}],
                          "server_managed": [], "with_expiry": [], "expiry_unknown": [
                              {"name": "as_sfa", "value": "old-context", "domain": ".apple.com.cn", "path": "/"}]}}
            path.write_text(json.dumps(config))
            client = AppleStoreLogin(auth_path=path)
            def initialize():
                client.session.cookies.clear()
                client.session.cookies.set("shld_bt_ck", "fresh-context", domain=".apple.com.cn", path="/")
                return True
            with patch.object(duo_killer, "AUTH_PATH", path), patch("apple_login.AppleStoreLogin", return_value=client), \
                    patch.object(client, "initialize_browser_context", side_effect=initialize), \
                    patch.object(client, "_login", side_effect=LoginError("account auth failed")):
                with self.assertRaisesRegex(ValueError, "account auth failed"):
                    duo_killer.login()
            saved = json.loads(path.read_text())
            cookies = {c["name"]: c["value"] for items in saved["cookies"].values() for c in items}
            self.assertEqual(cookies, {"shld_bt_ck": "fresh-context"})
            for key in ("account", "password", "trust_cookies"):
                self.assertEqual(saved[key], config[key])

    def test_login_dispatch_and_sms(self):
        with patch.object(duo_killer, "login", return_value={"success": True}) as login, \
                patch.object(duo_killer, "pay_bag") as pay, \
                patch.object(duo_killer, "add_to_bag") as add, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            duo_killer.main(["-l", "--sms"])
        login.assert_called_once_with(sms=True)
        pay.assert_not_called()
        add.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["success"])

    def test_login_cannot_be_combined_with_purchase(self):
        for args in (["-l", "-a"], ["-l", "-p"], ["--sms"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    duo_killer.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_login_synchronizes_cookies_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            config = {"account": "test@example.invalid", "password": "test-password",
                      "trust_cookies": [], "custom": "keep"}
            path.write_text(json.dumps(config))
            with patch.object(duo_killer, "AUTH_PATH", path), \
                    patch("apple_login.AppleStoreLogin") as factory:
                client = factory.return_value
                client.session = requests.Session()
                client.session.cookies.set("myacinfo", "fresh-cookie", domain=".apple.com.cn", path="/", secure=True)
                client.session.cookies.set("acn01", "fresh-acn", domain=".apple.com.cn", path="/", secure=True)
                result = duo_killer.login()
                client.login.assert_called_once_with(config["account"], config["password"], False)
            saved = json.loads(path.read_text())
            self.assertTrue(result["success"])
            for key, value in config.items():
                self.assertEqual(saved[key], value)
            restored = requests.Session()
            duo_killer.load_auth(restored, saved)
            self.assertEqual(result["scope"], "store_menu")
            self.assertEqual(restored.cookies.get("myacinfo"), "fresh-cookie")
            self.assertEqual(restored.cookies.get("acn01"), "fresh-acn")
            if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_login_preserves_store_cookies_and_returns_failure(self):
        with patch.object(duo_killer, "ensure_auth_file", return_value=False), \
                patch("apple_login.load_credentials", return_value=("test", "password")), \
                patch.object(Path, "read_text", return_value='{"account":"test","password":"password"}'), \
                patch("apple_login.AppleStoreLogin") as factory, \
                patch.object(duo_killer, "save_auth") as save, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            factory.return_value.login.side_effect = LoginError("HTTP 401")
            with self.assertRaises(SystemExit) as error:
                duo_killer.main(["-l"])
        self.assertEqual(error.exception.code, 1)
        self.assertFalse(json.loads(output.getvalue())["success"])
        save.assert_not_called()

    def test_login_preserves_browser_context_without_old_auth_or_account_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.json"
            config = {"account": "test@example.invalid", "password": "password",
                      "headers": {"User-Agent": "captured-agent"},
                      "cookies": {"account_session": [
                          {"name": "as_ltn_cn", "value": "old-login", "domain": ".apple.com.cn", "path": "/"},
                          {"name": "as_dc", "value": "routing", "domain": ".apple.com.cn", "path": "/"},
                          {"name": "shld_bt_ck", "value": "browser-context", "domain": ".apple.com.cn", "path": "/"},
                          {"name": "dssid2", "value": "browser-session", "domain": ".apple.com.cn", "path": "/"},
                          {"name": "myacinfo", "value": "old-account", "domain": ".apple.com.cn", "path": "/"}],
                          "server_managed": [], "with_expiry": [], "expiry_unknown": []}}
            path.write_text(json.dumps(config))
            with patch.object(duo_killer, "AUTH_PATH", path), \
                    patch("apple_login.AppleStoreLogin") as factory, \
                    patch.object(duo_killer, "account_info") as account_info, \
                    patch.object(duo_killer, "save_auth") as save:
                client = factory.return_value
                client.session = requests.Session()
                result = duo_killer.login()
                self.assertTrue(result["success"])
                self.assertEqual(result["scope"], "store_menu")
                save.assert_called_once_with(client.session)
                client.login.assert_called_once_with(config["account"], config["password"], False)
                account_info.assert_not_called()
                self.assertEqual(client.session.headers["User-Agent"], "captured-agent")
                self.assertEqual(client.session.cookies.get("as_dc"), "routing")
                self.assertEqual(client.session.cookies.get("shld_bt_ck"), "browser-context")
                self.assertEqual(client.session.cookies.get("dssid2"), "browser-session")
                self.assertIsNone(client.session.cookies.get("as_ltn_cn"))
                self.assertIsNone(client.session.cookies.get("myacinfo"))
                self.assertIs(client.store_request, duo_killer.request_get)


class CheckoutAuthenticationTests(unittest.TestCase):
    @staticmethod
    def page(model, url):
        result = response({}, url=url)
        result._content = ('<script id="init_data" type="application/json">'
                           + json.dumps(model) + '</script>').encode()
        return result

    def test_checkout_bootstrap_keeps_original_entry_and_session(self):
        session = requests.Session()
        session.headers["User-Agent"] = "existing-browser"
        session.cookies.set("shld_bt_ck", "context", domain=".apple.com.cn", path="/")
        client = AppleStoreLogin(session=session)
        entry = "https://secure8.www.apple.com.cn/shop/signIn?ssi=checkout&up=true"
        callback = "/shop/signIn/idms/authx?ssi=checkout&up=true"
        model = {"meta": {"h": {"x-aos-stk": "checkout-stk"}}, "signIn": {"customerLoginIDMS": {"d": {
            "serviceURL": "https://idmsa.apple.com.cn/appleauth", "serviceKey": "key",
            "callbackSignInUrl": callback}}}}
        auth_page = response({})
        auth_page._content = b'<script class="boot_args" type="application/json">{"domainId":43,"authAttributes":"attrs"}</script>'
        with patch.object(client, "request", side_effect=[self.page(model, entry), auth_page]) as request, \
                patch.object(client, "flyout", side_effect=AssertionError("不能重新获取首页入口")):
            client.bootstrap(entry)
        self.assertIs(client.session, session)
        self.assertEqual(session.headers["User-Agent"], "existing-browser")
        self.assertEqual(session.cookies.get("shld_bt_ck"), "context")
        self.assertEqual(request.call_args_list[0].args[1], entry)
        self.assertEqual(client.callback, "https://secure8.www.apple.com.cn" + callback)
        account_entry = entry.replace("/shop/signIn?", "/shop/signIn/account?")
        with patch.object(client, "request", side_effect=[self.page(model, account_entry), auth_page]):
            client.bootstrap(account_entry)
        self.assertEqual(client.store_page, account_entry)
        for cached_entry in (entry, account_entry, account_entry.replace('/account?', '/orders?')):
            with self.subTest(cached_entry=cached_entry), \
                    patch.object(client, "request", return_value=auth_page) as request, \
                    patch.object(client, "page", side_effect=AssertionError("不能重复 GET 已取得的登录页")):
                client.bootstrap(cached_entry, signin_response=self.page(model, cached_entry))
            request.assert_called_once()
            self.assertIn('/appleauth/auth/authorize/signin', request.call_args.args[1])
            self.assertEqual(client.store_headers['x-aos-stk'], 'checkout-stk')
            self.assertEqual(client.store_page, cached_entry)
        with patch.object(client, 'request') as request, self.assertRaisesRegex(LoginError, '不匹配'):
            client.bootstrap(entry, signin_response=self.page(model, entry + '-wrong'))
        request.assert_not_called()

    def test_cookie_refresh_invalidates_cached_signin_page(self):
        for refresh in (False, True):
            with self.subTest(refresh=refresh), tempfile.TemporaryDirectory() as temp:
                client = AppleStoreLogin(auth_path=Path(temp) / 'auth.json')
                if not refresh:
                    client.session.cookies.set('shld_bt_ck', 'valid', domain='.apple.com.cn', path='/')
                entry = 'https://secure8.www.apple.com.cn/shop/signIn/account?ssi=original'
                cached = self.page({'signIn': {}}, entry)
                with patch.object(client, 'restore_trust'), patch.object(client, 'discard_failed_login'), \
                        patch.object(client, 'initialize_browser_context') as browser, \
                        patch.object(client, 'bootstrap') as bootstrap, \
                        patch.object(client, 'auth', side_effect=LoginError('stop before credentials')), \
                        self.assertRaisesRegex(LoginError, 'stop before credentials'):
                    client.login('test@example.invalid', 'secret', signin_url=entry, signin_response=cached)
                if refresh:
                    browser.assert_called_once()
                    bootstrap.assert_called_once_with(entry)
                else:
                    browser.assert_not_called()
                    bootstrap.assert_called_once_with(entry, signin_response=cached)

    def test_account_read_reauthenticates_exact_entry_in_same_session(self):
        session = requests.Session()
        session.cookies.set("shld_bt_ck", "context", domain=".apple.com.cn", path="/")
        entry = "https://secure8.www.apple.com.cn/shop/signIn/account?ssi=account-context"
        signin = self.page({"signIn": {}, "meta": {"page": {"htmlUrl": "/shop/signIn/account"}}}, entry)
        with patch.object(duo_killer, "request_get", return_value=signin), \
                patch.object(AppleStoreLogin, "login") as login:
            result = duo_killer.account_info(session, reauthenticate=False)
            self.assertFalse(result["logged_in"])
            self.assertTrue(result["reauthentication_required"])
            login.assert_not_called()
        landing = self.page({"home": {"d": {"signOutUrl": "/shop/signOut", "firstName": "test"},
                                     "devices": {"d": {"appleID": "test@example.invalid"}}}},
                            "https://secure8.www.apple.com.cn/shop/account/home")
        with patch.object(duo_killer, "request_get", return_value=signin), \
                patch("apple_login.load_credentials", return_value=("test@example.invalid", "password")), \
                patch.object(AppleStoreLogin, "login", autospec=True, return_value=landing) as login:
            result = duo_killer.account_info(session)
            self.assertTrue(result["logged_in"])
            self.assertEqual(result["apple_id"], "test@example.invalid")
            self.assertIs(login.call_args.args[0].session, session)
            self.assertEqual(login.call_args.kwargs["signin_url"], entry)
            self.assertEqual(login.call_args.kwargs["signin_response"].url, entry)
            login.assert_called_once()
        with patch.object(duo_killer, "request_get", return_value=landing), \
                patch.object(AppleStoreLogin, "login") as login:
            self.assertTrue(duo_killer.account_info(session, reauthenticate=False)["logged_in"])
            login.assert_not_called()
        account_page = self.page({"home": {"d": {"signOutUrl": "/shop/signOut"},
                                           "customerAccount": {"d": {"appleId": "test@example.invalid"}},
                                           "devices": {"d": {"appleID": ""}}}}, landing.url)
        with patch.object(duo_killer, "request_get", return_value=account_page), \
                patch.object(duo_killer.Checkout, "action") as action:
            self.assertEqual(duo_killer.account_info(session)["apple_id"], "test@example.invalid")
            action.assert_not_called()

    def test_account_authx_only_follows_account_get_redirect(self):
        client = AppleStoreLogin()
        client.store_page = "https://secure8.www.apple.com.cn/shop/signIn/account?ssi=fresh"
        client.callback = "https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=fresh"
        client.store_headers = {"x-aos-stk": "account-stk"}
        client.session.cookies.set("myacinfo", "fresh", domain=".apple.com.cn", path="/")
        target = "https://secure8.www.apple.com.cn/shop/account/home"
        for redirect, valid in (({"url": target}, True), ({"url": target, "method": "post"}, False),
                                ({"url": "https://www.apple.com.cn/"}, False)):
            with self.subTest(redirect=redirect), \
                    patch.object(client, "request", return_value=response({"head": {"status": 302, "data": redirect}})), \
                    patch.object(client, "page") as page, patch.object(client, "flyout") as flyout:
                if valid:
                    self.assertIs(client.finish_store_login(account=True), page.return_value)
                    page.assert_called_once_with(target)
                else:
                    with self.assertRaises(LoginError):
                        client.finish_store_login(account=True)
                    page.assert_not_called()
                flyout.assert_not_called()

    def test_checkout_authx_posts_pltn_then_follows_redirect_as_get(self):
        client = AppleStoreLogin()
        client.store_page = "https://secure8.www.apple.com.cn/shop/signIn?ssi=checkout&up=true"
        client.store_referrer = client.store_page
        client.callback = "https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=checkout&up=true"
        client.store_headers = {"x-aos-stk": "checkout-stk"}
        client.session.cookies.set("myacinfo", "fresh", domain=".apple.com.cn", path="/")
        start = "https://secure8.www.apple.com.cn/shop/checkout/start"
        authx = response({"head": {"status": 302, "data": {
            "url": start, "method": "post", "args": {"pltn": "fresh-pltn"}}}})
        redirect = response({}, 302, start)
        redirect.headers["Location"] = "/shop/checkout?_s=Fulfillment-init"
        landing = self.page({"checkout": {"fulfillment": {"d": {"ready": True}}}},
                            "https://secure8.www.apple.com.cn/shop/checkout?_s=Fulfillment-init")
        with patch.object(client, "request", side_effect=[authx, redirect, landing]) as request, \
                patch.object(client, "flyout", side_effect=AssertionError("结账不能跳回首页检查")):
            self.assertIs(client.finish_store_login(checkout=True), landing)
        calls = request.call_args_list
        self.assertEqual([(c.args[0], c.args[1]) for c in calls],
                         [("POST", client.callback), ("POST", start), ("GET", landing.url)])
        self.assertEqual(calls[1].kwargs["data"], {"pltn": "fresh-pltn"})
        self.assertNotIn("data", calls[2].kwargs)
        self.assertNotIn("x-aos-stk", calls[1].kwargs["headers"])

    def test_checkout_authx_rejects_other_post_targets(self):
        client = AppleStoreLogin()
        client.store_page = "https://secure8.www.apple.com.cn/shop/signIn?ssi=test"
        client.callback = "https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=test"
        client.store_headers = {}
        client.session.cookies.set("myacinfo", "fresh", domain=".apple.com.cn", path="/")
        result = response({"head": {"status": 302, "data": {
            "url": "https://secure8.www.apple.com.cn/shop/checkoutx/placeOrder",
            "method": "post", "args": {"pltn": "test"}}}})
        with patch.object(client, "request", return_value=result), patch.object(client, "page") as page:
            with self.assertRaises(LoginError):
                client.finish_store_login(checkout=True)
            page.assert_not_called()

    def test_resume_checkout_reuses_session_and_loads_returned_model(self):
        session = requests.Session()
        flow = duo_killer.Checkout(session)
        entry = "https://secure8.www.apple.com.cn/shop/signIn?ssi=original&up=true"
        flow.read_page(self.page({"meta": {"page": {"htmlUrl": "/shop/signIn"}}, "signIn": {}}, entry), "test")
        self.assertEqual(flow.url, entry)
        landing = self.page({"meta": {"h": {"x-aos-stk": "new-checkout-stk"}},
                             "checkout": {"fulfillment": {"d": {"ready": True}}}},
                            "https://secure8.www.apple.com.cn/shop/checkout/start")
        with patch("apple_login.load_credentials", return_value=("test@example.invalid", "password")), \
                patch.object(AppleStoreLogin, "login", autospec=True, return_value=landing) as login, \
                patch.object(duo_killer, "save_auth") as save:
            self.assertTrue(duo_killer.reauthenticate_checkout(flow))
            save.assert_called_once_with(session)
            self.assertIs(login.call_args.args[0].session, session)
            self.assertEqual(login.call_args.kwargs["signin_url"], entry)
            self.assertEqual(login.call_args.kwargs["signin_response"].url, entry)
        self.assertIn("Fulfillment-init", flow.url)
        self.assertEqual(flow.headers["x-aos-stk"], "new-checkout-stk")
        with patch.object(AppleStoreLogin, "login") as login:
            self.assertFalse(duo_killer.reauthenticate_checkout(flow))
            login.assert_not_called()

    def test_failed_reauthentication_stops_before_delivery_or_order(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "ROOT", Path(temp)), \
                patch.object(duo_killer, "authenticate"), patch.object(duo_killer, "save_auth"), \
                patch.object(duo_killer, "Checkout") as factory, \
                patch.object(duo_killer, "model_items", return_value={"SKU": 1}), \
                patch.object(duo_killer, "reauthenticate_checkout", side_effect=ValueError("authentication failed")), \
                patch.object(duo_killer, "checkout_delivery") as delivery:
            factory.return_value.require.return_value = {"items": {}}
            with self.assertRaises(ValueError):
                duo_killer.pay_bag()
            delivery.assert_not_called()
            self.assertEqual(factory.return_value.action.call_count, 1)
            self.assertEqual(factory.return_value.action.call_args.args[:2], ("shoppingCart.actions", "checkout"))
            self.assertFalse((Path(temp) / "bugApple.pending.json").exists())


class StrongLoginTests(unittest.TestCase):
    def config(self, *, expired=None, missing=None, expires=2000000000):
        return {"account": "test@example.invalid", "password": "secret", "cookies": {"account_session": [
            {"name": name, "value": "saved-" + name, "domain": ".apple.com.cn", "path": "/",
             "secure": True, "expires": 1 if name == expired else expires}
            for name in ("as_ltn_cn", "shld_bt_ck") if name != missing],
            "server_managed": [], "with_expiry": [], "expiry_unknown": []}}

    def test_expired_or_missing_credentials_login_once_and_reload_session(self):
        for name in ("as_ltn_cn", "shld_bt_ck"):
            for mode in ("expired", "missing"):
                with self.subTest(name=name, mode=mode), tempfile.TemporaryDirectory() as temp, \
                        patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                        requests.Session() as session, contextlib.redirect_stdout(io.StringIO()):
                    duo_killer.AUTH_PATH.write_text(json.dumps(self.config(**{mode: name})))
                    fresh = self.config()
                    fresh["headers"] = {"User-Agent": "fresh-browser-UA"}
                    with patch.object(duo_killer, "login", side_effect=lambda **kwargs: duo_killer.AUTH_PATH.write_text(json.dumps(fresh))) as login, \
                            patch.object(requests.Session, "request", side_effect=AssertionError("不能联网")):
                        duo_killer.authenticate(session, strong=True)
                        login.assert_called_once_with(sms=False)
                        self.assertEqual(session.headers["User-Agent"], "fresh-browser-UA")
                        self.assertFalse(duo_killer.ensure_strong_login(session))
                        self.assertEqual(login.call_count, 1)

    def test_valid_or_session_cookies_skip_login_and_ignore_expired_analytics(self):
        for expires in (2000000000, None):
            with self.subTest(expires=expires), requests.Session() as session, patch.object(duo_killer, "login") as login:
                duo_killer.load_auth(session, self.config(expires=expires))
                session.cookies.set("analytics", "expired", domain=".apple.com.cn", path="/", expires=1)
                self.assertFalse(duo_killer.ensure_strong_login(session))
                login.assert_not_called()

    def test_541_marker_forces_login_even_when_cookies_have_not_expired(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), requests.Session() as session:
            duo_killer.AUTH_PATH.write_text(json.dumps(self.config()))
            duo_killer.load_auth(session, self.config())
            client = AppleStoreLogin(auth_path=duo_killer.AUTH_PATH)
            client.mark_rejected_browser_context()
            with patch.object(duo_killer, "login", side_effect=lambda **kwargs: client.prepare_browser_profile()) as login:
                self.assertTrue(duo_killer.ensure_strong_login(session))
                login.assert_called_once_with(sms=False)
                self.assertFalse(duo_killer.ensure_strong_login(session))

    def test_wrong_cookie_scope_triggers_full_login_and_forwards_sms(self):
        for domain, path in ((".example.invalid", "/"), (".apple.com.cn", "/unrelated")):
            with self.subTest(domain=domain, path=path), requests.Session() as session, \
                    patch.object(duo_killer, "login", side_effect=ValueError("stop login")) as login:
                duo_killer.load_auth(session, self.config())
                session.cookies.clear(".apple.com.cn", "/", "as_ltn_cn")
                session.cookies.set("as_ltn_cn", "wrong-scope", domain=domain, path=path)
                with self.assertRaisesRegex(ValueError, "stop login"):
                    duo_killer.ensure_strong_login(session, sms=True)
                login.assert_called_once_with(sms=True)

    def test_incomplete_new_session_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                requests.Session() as session, patch.object(duo_killer, "login") as login:
            duo_killer.AUTH_PATH.write_text(json.dumps(self.config(missing="as_ltn_cn")))
            with self.assertRaisesRegex(ValueError, "完整登录后仍缺少"):
                duo_killer.authenticate(session, strong=True)
            login.assert_called_once()

    def test_lightweight_checks_do_not_force_login_for_expired_cookies(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                requests.Session() as session, patch.object(duo_killer, "login") as login:
            duo_killer.AUTH_PATH.write_text(json.dumps(self.config(expired="as_ltn_cn")))
            duo_killer.authenticate(session)
            login.assert_not_called()

    def test_expired_account_and_order_sessions_login_before_query(self):
        for command, handler in (("-me", "account_info"), ("-o", "order_list")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp, \
                    patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                    contextlib.redirect_stdout(io.StringIO()):
                duo_killer.AUTH_PATH.write_text(json.dumps(self.config(expired="as_ltn_cn")))
                events = []
                def login(**kwargs):
                    events.append("login")
                    duo_killer.AUTH_PATH.write_text(json.dumps(self.config()))
                def query(session):
                    events.append("query")
                    self.assertEqual(session.cookies.get("as_ltn_cn"), "saved-as_ltn_cn")
                    return {"success": True, "orders": []}
                with patch.object(duo_killer, "login", side_effect=login), patch.object(duo_killer, handler, side_effect=query):
                    duo_killer.main([command])
                self.assertEqual(events, ["login", "query"])

    def test_login_failure_prevents_cart_checkout_and_queries(self):
        for command in (["-me"], ["-o"], ["-a", "MKWA4FE/A"], ["-p"], ["-k", "MKWA4FE/A"]):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp, \
                    patch.object(duo_killer, "ROOT", Path(temp)), \
                    patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                    patch.object(duo_killer, "login", side_effect=ValueError("login failed")) as login, \
                    patch.object(duo_killer, "request_get") as network, \
                    patch.object(duo_killer, "account_info") as account, patch.object(duo_killer, "order_list") as orders, \
                    contextlib.redirect_stdout(io.StringIO()):
                duo_killer.AUTH_PATH.write_text(json.dumps(self.config(expired="shld_bt_ck")))
                with self.assertRaises(SystemExit):
                    duo_killer.main(command)
                login.assert_called_once()
                network.assert_not_called()
                account.assert_not_called()
                orders.assert_not_called()


class PurchaseFlowTests(unittest.TestCase):
    def setUp(self):
        refresh = patch.object(duo_killer, "ensure_strong_login", return_value=False)
        refresh.start()
        self.addCleanup(refresh.stop)
        network = patch.object(requests.Session, "request", side_effect=AssertionError("测试不能联网或下单"))
        network.start()
        self.addCleanup(network.stop)
        page = patch.object(duo_killer.Checkout, "page")
        self.cart_page = page.start()
        self.addCleanup(page.stop)

    @staticmethod
    def review_model():
        items = {
            "item-band": {"d": {"sku": "MKWA4FE/A", "name": "<b>表带</b>", "totalPrice": "RMB 1,598"},
                          "itemQuantity": {"d": {"quantity": 2}}},
            "item-phone": {"d": {"sku": "MJYH4CH/A", "name": "手机 256G 黑色", "totalPrice": "RMB 1,499"},
                           "itemQuantity": {"d": {"quantity": 1}}},
        }
        return {"shoppingCart": {"items": items}, "checkout": {
            "billing": {"billingOptions": {"d": {"options": [{"value": "ALIPAY"}]}}},
            "review": {"fulfillmentReview": {"items": items},
                       "summary": {"d": {"total": "RMB 3,107"}},
                       "placeOrder": {"d": {"enableContinue": True}},
                       "billingReview": {"alipayReview": {"d": {"alipayLabel": "ALIPAY"}}}}}}

    def test_check_login_add_and_pay_in_order(self):
        for logged_in in (True, False):
            with self.subTest(logged_in=logged_in), tempfile.TemporaryDirectory() as temp, \
                    patch.object(duo_killer, "ROOT", Path(temp)), \
                    patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                    patch.object(duo_killer, "ensure_auth_file"), patch.object(duo_killer, "load_auth"), \
                    patch.object(duo_killer, "save_auth"), contextlib.ExitStack() as stack, \
                    contextlib.redirect_stdout(io.StringIO()):
                duo_killer.AUTH_PATH.write_text('{}')
                events = []
                for name, result in (("check_login", {"logged_in": logged_in}), ("login", {"success": True}),
                                     ("add_to_bag", {"success": True}), ("pay_bag", {"qr_image": "qr.png"})):
                    def record(*args, _name=name, _result=result, **kwargs):
                        events.append((_name, args, kwargs))
                        return _result
                    stack.enter_context(patch.object(duo_killer, name, side_effect=record))
                self.assertEqual(duo_killer.purchase_sku(" mkwa4fe ", sms=True, quantity=2), {"qr_image": "qr.png"})
                self.assertEqual([e[0] for e in events],
                                 ["check_login"] + ([] if logged_in else ["login"]) + ["add_to_bag", "pay_bag"])
                self.assertEqual(events[-2][1], ("MKWA4FE/A",))
                self.assertEqual(events[-2][2]["quantity"], 2)
                self.assertTrue(events[-2][2]["set_total"])
                self.assertIs(events[-2][2]["flow"], events[-1][2]["flow"])
                if not logged_in:
                    self.assertEqual(events[1][2], {"sms": True})

    def test_failed_login_stops_before_add_or_pay(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "ROOT", Path(temp)), \
                patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                patch.object(duo_killer, "ensure_auth_file"), patch.object(duo_killer, "load_auth"), \
                patch.object(duo_killer, "save_auth"), patch.object(duo_killer, "check_login", return_value={"logged_in": False}), \
                patch.object(duo_killer, "login", side_effect=ValueError("login failed")), \
                patch.object(duo_killer, "add_to_bag") as add, patch.object(duo_killer, "pay_bag") as pay:
            duo_killer.AUTH_PATH.write_text('{}')
            with self.assertRaisesRegex(ValueError, "login failed"):
                duo_killer.purchase_sku("MKWA4FE/A")
            add.assert_not_called()
            pay.assert_not_called()

    def test_legacy_pending_file_does_not_block_new_purchase(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "ROOT", Path(temp)), \
                patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                patch.object(duo_killer, "ensure_auth_file"), patch.object(duo_killer, "load_auth"), \
                patch.object(duo_killer, "save_auth"), patch.object(duo_killer, "ensure_strong_login", return_value=False), \
                patch.object(duo_killer, "check_login", return_value={"logged_in": True}) as check, \
                patch.object(duo_killer, "add_to_bag") as add, patch.object(duo_killer, "pay_bag", return_value={"success": True}) as pay:
            (Path(temp) / "bugApple.pending.json").write_text('not even valid JSON')
            duo_killer.AUTH_PATH.write_text('{}')
            self.assertTrue(duo_killer.purchase_sku("MKWA4FE/A")["success"])
            check.assert_called_once()
            add.assert_called_once()
            self.assertEqual(add.call_args.kwargs["quantity"], 1)
            self.assertTrue(add.call_args.kwargs["set_total"])
            pay.assert_called_once_with(flow=add.call_args.kwargs["flow"])

    def test_invalid_sku_does_not_start_login(self):
        with patch.object(duo_killer, "ensure_auth_file") as ensure:
            with self.assertRaisesRegex(ValueError, "SKU"):
                duo_killer.purchase_sku("../bad")
            ensure.assert_not_called()

    def test_confirmation_displays_all_rows_and_server_total(self):
        with patch("builtins.input", return_value="") as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            duo_killer.confirm_order(self.review_model())
        self.assertIn("1. 表带 × 2  小计 RMB 1,598", output.getvalue())
        self.assertIn("2. 手机 256G 黑色 × 1  小计 RMB 1,499", output.getvalue())
        self.assertIn("总支付：RMB 3,107", output.getvalue())  # 含运费，不自行加总为 3,097。
        prompt.assert_called_once()

    def test_missing_prices_never_prompt_or_approve(self):
        for path in ("checkout.review.summary.d", "checkout.review.fulfillmentReview.items.item-band.d"):
            model = self.review_model()
            node = duo_killer.field(model, path)
            node.pop("total" if "summary" in path else "totalPrice")
            with self.subTest(path=path), patch("builtins.input") as prompt:
                with self.assertRaisesRegex(ValueError, "金额"):
                    duo_killer.confirm_order(model)
                prompt.assert_not_called()

    def test_order_is_submitted_only_after_enter_confirmation(self):
        for answer in ("", "n", " ", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__ if isinstance(answer, BaseException) else repr(answer)), \
                    tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "ROOT", Path(temp)), \
                    patch.object(duo_killer, "authenticate"), patch.object(duo_killer, "save_auth"), \
                    patch.object(duo_killer, "Checkout") as factory, \
                    patch.object(duo_killer, "reauthenticate_checkout"), patch.object(duo_killer, "checkout_delivery"), \
                    patch.object(duo_killer, "checkout_shipping"), \
                    patch.object(duo_killer, "finish_payment", return_value={"success": True}) as finish, \
                    patch.object(duo_killer, "payment_qr") as qr, contextlib.redirect_stdout(io.StringIO()):
                flow = factory.return_value
                flow.model = self.review_model()
                flow.require.side_effect = lambda path: duo_killer.field(flow.model, path)
                flow.apple_url.return_value = "https://secure8.www.apple.com.cn/shop/checkout/status"
                events = []
                def confirm(prompt):
                    self.assertFalse((Path(temp) / "bugApple.pending.json").exists())
                    events.append("confirm")
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer
                def action(module, name, *args):
                    if name == "continueFromReviewToProcess":
                        self.assertEqual(events, ["confirm"])
                        self.assertFalse((Path(temp) / "bugApple.pending.json").exists())
                        events.append("submit")
                flow.action.side_effect = action
                with patch("builtins.input", side_effect=confirm):
                    if answer == "":
                        self.assertTrue(duo_killer.pay_bag()["success"])
                        self.assertEqual(events, ["confirm", "submit"])
                        qr.assert_called_once()
                    else:
                        with self.assertRaises(KeyboardInterrupt if isinstance(answer, KeyboardInterrupt) else ValueError):
                            duo_killer.pay_bag()
                        self.assertEqual(events, ["confirm"])
                        finish.assert_not_called()
                        qr.assert_not_called()
                self.assertFalse((Path(temp) / "bugApple.pending.json").exists())

    def test_k_dispatch_outputs_and_opens_qr(self):
        with patch.object(duo_killer, "purchase_sku", return_value={"success": True, "qr_image": "/tmp/test-qr.png"}) as purchase, \
                patch.object(duo_killer.sys, "platform", "darwin"), patch.object(duo_killer.subprocess, "run") as opened, \
                contextlib.redirect_stdout(io.StringIO()), patch.object(duo_killer.sys.stdout, "isatty", return_value=True), \
                patch.object(duo_killer.sys.stdin, "isatty", return_value=True), \
                patch.object(duo_killer, "wait_for_order_query") as wait:
            duo_killer.main(["-k", "MKWA4FE/A", "--sms"])
            purchase.assert_called_once_with("MKWA4FE/A", sms=True, quantity=1)
            opened.assert_called_once_with(["open", "/tmp/test-qr.png"], check=False)
            wait.assert_called_once_with()

    def test_cashier_qr_generates_real_png_offline(self):
        import alipay_qr
        reply = requests.Response()
        reply.status_code = 200
        reply._content = b'<div id="J_qrPayArea"><input id="J_qrCode" value="https://qr.alipay.com/test-only"></div>'
        with tempfile.TemporaryDirectory() as temp, patch.object(alipay_qr, "ROOT", Path(temp)), \
                patch.object(requests.Session, "get", return_value=reply):
            result = alipay_qr.get_payment_qr("https://openapi.alipay.com/gateway.do?test=1")
            path = Path(result["qr_image"])
            self.assertTrue(path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class OrderListTests(unittest.TestCase):
    @staticmethod
    def page(model, path="/shop/order/list"):
        reply = response({}, url="https://secure8.www.apple.com.cn" + path)
        reply._content = ('<script id="init_data">' + json.dumps(model) + '</script>').encode()
        return reply

    @staticmethod
    def model():
        return {"orderList": {"d": {"hasItems": True, "moreOrdersAvailable": False},
                              "order-WTEST": {"d": {"webOrderNumber": "WTEST"}, "c": ["11", "12"],
                                              "11": {"d": {"productShortName": "抛光布", "quantity": 2,
                                                           "deliveryDate": "我们正在等待你的付款",
                                                           "orderDetailUrl": "/shop/order/detail/WTEST?_si=11"}},
                                              "12": {"d": {"productShortName": "表带", "quantity": 1,
                                                           "deliveryDate": "预计送达日期 2026/09/18 - 2026/09/20"}}}}}

    def test_logged_in_reads_order_list_without_mutation(self):
        with requests.Session() as session, \
                patch.object(duo_killer, "request_get", return_value=self.page(self.model())) as request, \
                patch.object(AppleStoreLogin, "login") as login, patch.object(duo_killer.Checkout, "action") as action:
            result = duo_killer.order_list(session)
            self.assertEqual(result["count"], 1)
            items = result["orders"][0]["items"]
            self.assertEqual([i["quantity"] for i in items], [2, 1])
            self.assertEqual(items[0]["delivery_info"], "我们正在等待你的付款")
            self.assertEqual(items[0]["detail_url"], "https://secure8.www.apple.com.cn/shop/order/detail/WTEST?_si=11")
            self.assertNotIn("amount", result["orders"][0])
            self.assertFalse(result["has_more"])
            self.assertEqual(request.call_args.args[2], duo_killer.BASE_URL + "/cn/shop/goto/order/list")
            login.assert_not_called()
            action.assert_not_called()

    def test_orders_reauthentication_keeps_exact_entry_and_session(self):
        entry = "/shop/signIn/orders?ssi=orders-session"
        with requests.Session() as session, \
                patch.object(duo_killer, "request_get", return_value=self.page({"signIn": {}}, entry)), \
                patch("apple_login.load_credentials", return_value=("test@example.invalid", "secret")), \
                patch.object(AppleStoreLogin, "login", autospec=True, return_value=self.page(self.model())) as login:
            self.assertEqual(duo_killer.order_list(session)["count"], 1)
            self.assertIs(login.call_args.args[0].session, session)
            self.assertEqual(login.call_args.kwargs["signin_url"], "https://secure8.www.apple.com.cn" + entry)
            self.assertEqual(login.call_args.kwargs["signin_response"].url, "https://secure8.www.apple.com.cn" + entry)

    def test_empty_list_and_more_orders_are_explicit(self):
        for model, count, more in (({"orderList": {"d": {"hasItems": False, "moreOrdersAvailable": False}}}, 0, False),
                                   (self.model(), 1, True)):
            model["orderList"]["d"]["moreOrdersAvailable"] = more
            with self.subTest(count=count), requests.Session() as session, \
                    patch.object(duo_killer, "request_get", return_value=self.page(model)):
                result = duo_killer.order_list(session)
                self.assertEqual((result["count"], result["has_more"]), (count, more))

    def test_missing_or_inconsistent_models_are_not_empty_orders(self):
        for model in ({"home": {}}, {"orderList": {"d": {"hasItems": True, "moreOrdersAvailable": False}}}):
            with self.subTest(model=model), requests.Session() as session, \
                    patch.object(duo_killer, "request_get", return_value=self.page(model)):
                with self.assertRaises(ValueError):
                    duo_killer.order_list(session)

    def test_order_authx_rejects_checkout_and_cross_host_redirects(self):
        client = AppleStoreLogin()
        client.store_page = "https://secure8.www.apple.com.cn/shop/signIn/orders?ssi=fresh"
        client.callback = "https://secure8.www.apple.com.cn/shop/signIn/idms/authx?ssi=fresh"
        client.store_headers = {"x-aos-stk": "fresh"}
        client.session.cookies.set("myacinfo", "test", domain=".apple.com.cn", path="/")
        target = "https://secure8.www.apple.com.cn/shop/order/list"
        for redirect, valid in (({"url": target}, True), ({"url": target, "method": "post"}, False),
                                ({"url": target, "args": {"unexpected": "value"}}, False),
                                ({"url": "https://secure9.www.apple.com.cn/shop/order/list"}, False),
                                ({"url": "https://secure8.www.apple.com.cn/shop/checkout/start"}, False)):
            with self.subTest(redirect=redirect), \
                    patch.object(client, "request", return_value=response({"head": {"status": 302, "data": redirect}})), \
                    patch.object(client, "page") as page:
                if valid:
                    client.finish_store_login(orders=True)
                    page.assert_called_once_with(target)
                else:
                    with self.assertRaises(LoginError):
                        client.finish_store_login(orders=True)
                    page.assert_not_called()

    def test_order_command_saves_session_and_never_starts_payment(self):
        with patch.object(duo_killer, "authenticate"), patch.object(duo_killer, "save_auth") as save, \
                patch.object(duo_killer, "order_list", return_value={"success": True, "orders": []}) as orders, \
                patch.object(duo_killer, "pay_bag") as pay, patch.object(duo_killer, "add_to_bag") as add, \
                contextlib.redirect_stdout(io.StringIO()):
            duo_killer.main(["-o"])
            orders.assert_called_once()
            save.assert_called_once_with(orders.call_args.args[0])
            pay.assert_not_called()
            add.assert_not_called()


class QuantityTests(unittest.TestCase):
    def test_quantity_cli_dispatch(self):
        for action, function in (("-k", "purchase_sku"), ("-a", "add_to_bag")):
            with self.subTest(action=action), patch.object(duo_killer, function, return_value={"success": True}) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                duo_killer.main([action, "MKWA4FE/A", "-num", "2"])
                expected = {"quantity": 2, **({"sms": False} if action == "-k" else {})}
                run.assert_called_once_with("MKWA4FE/A", **expected)

    def test_invalid_quantity_stops_before_login_or_cart(self):
        cases = [["-k", "MKWA4FE/A", "-num", n] for n in ("0", "-1", "1.5", "abc")]
        cases += [["-num", "2"], ["-p", "-num", "2"], ["-l", "-num", "2"]]
        with patch.object(duo_killer, "ensure_auth_file") as ensure, \
                patch.object(requests.Session, "request") as network:
            for args in cases:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        duo_killer.main(args)
                    self.assertEqual(error.exception.code, 2)
            ensure.assert_not_called()
            network.assert_not_called()

    @contextlib.contextmanager
    def cart_requests(self, quantities, maximum=10, update_error=None, duplicate=False, missing_action=False):
        part = "MKWA4FE/A"
        module = "shoppingCart.items.item-test.itemQuantity"
        action_url = "/shop/bagx?_a=updateQuantity&_m=" + module + "&version=from-page"
        product = response({})
        product._content = b'<meta name="globalnav-store-key" content="test-key">'
        def model(quantity):
            items = {"item-other": {"d": {"sku": "MJYH4CH/A", "totalPrice": "RMB 100"},
                                    "itemQuantity": {"d": {"quantity": 1}}}}
            if quantity:
                item = {"d": {"sku": part, "totalPrice": f"RMB {quantity * 799}"},
                        "itemQuantity": {"d": {"quantity": quantity, "maxQuantity": maximum},
                                         "a": {} if missing_action else {"updateQuantity": {"url": action_url}}}}
                items["item-test"] = item
                if duplicate:
                    items["item-engraved"] = item
            return {"meta": {"h": {"x-aos-stk": f"page-stk-{quantity}", "modelVersion": "v2",
                                    "x-aos-model-page": "cart", "syntax": "graviton"},
                             "l": ["/shoppingCart"]},
                    "shoppingCart": {"items": items, "summary": {"d": {"total": f"RMB {quantity * 799 + 100}"}}}}
        snapshots = iter(model(n) for n in quantities)
        def authenticate(session, **kwargs):
            session.cookies.set("as_atb", "1|test|token", domain=".apple.com.cn", path="/")
        def request(session, stage, url, **kwargs):
            if stage == "商品页":
                return product
            if stage in ("加购令牌", "执行加购"):
                return response({"addedToBag": True})
            if stage == "updateQuantity":
                if update_error:
                    raise update_error
                return response({"head": {"status": 200}, "body": next(snapshots)}, url=url)
            result = response({}, url=url)
            result._content = ('<script id="init_data">' + json.dumps(next(snapshots)) + '</script>').encode()
            return result
        with patch.object(duo_killer, "authenticate", side_effect=authenticate), \
                patch.object(duo_killer, "request_get", side_effect=request) as request_mock, \
                patch.object(duo_killer, "save_auth") as save, \
                patch.object(requests.Session, "request", side_effect=AssertionError("测试不能联网")), \
                contextlib.redirect_stdout(io.StringIO()):
            yield request_mock, save

    def test_existing_item_updates_once_with_current_action_headers_and_session(self):
        with self.cart_requests([1, 4]) as (request, save):
            result = duo_killer.add_to_bag("mkwa4fe", quantity=3)
            self.assertEqual(result["added_quantity"], 3)
            self.assertEqual(result["items"][0]["quantity"], 4)
            self.assertEqual(result["line_total"], "RMB 3196")
            self.assertEqual(result["bag_total"], "RMB 3296")
            self.assertEqual([c.args[1] for c in request.call_args_list], ["加购前购物袋", "updateQuantity"])
            update = request.call_args
            module = "shoppingCart.items.item-test.itemQuantity"
            self.assertEqual(update.args[2], duo_killer.BASE_URL + "/shop/bagx?_a=updateQuantity&_m=" + module + "&version=from-page")
            self.assertEqual(update.kwargs["data"], {module + ".quantity": "4"})
            self.assertEqual(update.kwargs["method"], "POST")
            self.assertEqual(update.kwargs["headers"]["x-aos-stk"], "page-stk-1")
            self.assertEqual(update.kwargs["headers"]["Referer"], duo_killer.BASE_URL + "/shop/bag")
            self.assertIs(update.args[0], request.call_args_list[0].args[0])
            save.assert_called_once()

    def test_new_item_adds_once_then_sets_three(self):
        with self.cart_requests([0, 1, 3]) as (request, save):
            result = duo_killer.add_to_bag("mkwa4fe", quantity=3)
            self.assertEqual(result["items"][0]["quantity"], 3)
            stages = [c.args[1] for c in request.call_args_list]
            self.assertEqual(stages.count("执行加购"), 1)
            self.assertEqual(stages.count("updateQuantity"), 1)
            self.assertEqual(request.call_args.kwargs["headers"]["x-aos-stk"], "page-stk-1")
            self.assertEqual(len({id(c.args[0]) for c in request.call_args_list}), 1)
            save.assert_called_once()

    def test_purchase_target_quantity_does_not_accumulate(self):
        for before, snapshots, mutations in ((0, [0, 1, 2], 1), (1, [1, 2], 1),
                                             (2, [2], 0), (4, [4, 2], 1)):
            with self.subTest(before=before), self.cart_requests(snapshots) as (request, save):
                result = duo_killer.add_to_bag("MKWA4FE/A", quantity=2, set_total=True)
                self.assertEqual(result["items"][0]["quantity"], 2)
                self.assertEqual(result["added_quantity"], max(0, 2 - before))
                updates = [c for c in request.call_args_list if c.args[1] == "updateQuantity"]
                self.assertEqual(len(updates), mutations)
                if updates:
                    self.assertEqual(list(updates[0].kwargs["data"].values()), ["2"])
                self.assertEqual(sum(c.args[1] == "执行加购" for c in request.call_args_list), int(before == 0))

    def test_default_quantity_still_adds_one_without_update(self):
        with self.cart_requests([0, 1]) as (request, save):
            self.assertEqual(duo_killer.add_to_bag("MKWA4FE/A")["added_quantity"], 1)
            self.assertEqual(sum(c.args[1] == "执行加购" for c in request.call_args_list), 1)
            self.assertFalse(any(c.args[1] == "updateQuantity" for c in request.call_args_list))

    def test_limits_stop_before_update_and_preserve_first_add_if_needed(self):
        for snapshots in ([1], [0, 1]):
            with self.subTest(snapshots=snapshots), self.cart_requests(snapshots, maximum=2) as (request, save):
                with self.assertRaisesRegex(ValueError, "超过页面允许的最大数量 2"):
                    duo_killer.add_to_bag("MKWA4FE/A", quantity=3)
                self.assertFalse(any(c.args[1] == "updateQuantity" for c in request.call_args_list))
                self.assertEqual(sum(c.args[1] == "执行加购" for c in request.call_args_list), int(snapshots[0] == 0))
                save.assert_called_once()

    def test_quantity_mismatch_stops_without_retry(self):
        with self.cart_requests([1, 2]) as (request, save):
            with self.assertRaisesRegex(ValueError, "服务器返回数量与目标不一致"):
                duo_killer.add_to_bag("MKWA4FE/A", quantity=3)
            self.assertEqual(sum(c.args[1] == "updateQuantity" for c in request.call_args_list), 1)
            save.assert_called_once()

    def test_update_timeout_stops_and_instructs_cart_check(self):
        with self.cart_requests([1], update_error=requests.Timeout()) as (request, save):
            with self.assertRaisesRegex(ValueError, "先运行 -bag"):
                duo_killer.add_to_bag("MKWA4FE/A", quantity=3)
            self.assertEqual(sum(c.args[1] == "updateQuantity" for c in request.call_args_list), 1)
            save.assert_called_once()

    def test_missing_action_and_duplicate_rows_do_not_modify_cart(self):
        for options in ({"missing_action": True}, {"duplicate": True}):
            with self.subTest(options=options), self.cart_requests([1], **options) as (request, save):
                with self.assertRaises(ValueError):
                    duo_killer.add_to_bag("MKWA4FE/A", quantity=3)
                self.assertEqual([c.args[1] for c in request.call_args_list], ["加购前购物袋"])

    def test_partial_add_failure_does_not_start_checkout(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(duo_killer, "ROOT", Path(temp)), \
                patch.object(duo_killer, "AUTH_PATH", Path(temp) / "auth.json"), \
                patch.object(duo_killer, "ensure_auth_file"), patch.object(duo_killer, "load_auth", side_effect=lambda session, data: session.cookies.set("as_atb", "1|test|token", domain=".apple.com.cn", path="/")), \
                patch.object(duo_killer, "check_login", return_value={"logged_in": True}), \
                patch.object(duo_killer, "ensure_strong_login", return_value=False), \
                patch.object(duo_killer, "pay_bag") as pay, \
                self.cart_requests([0, 1], maximum=2) as (request, save):
            duo_killer.AUTH_PATH.write_text('{}')
            with self.assertRaisesRegex(ValueError, "超过页面允许"):
                duo_killer.purchase_sku("MKWA4FE/A", quantity=3)
            self.assertEqual(sum(c.args[1] == "执行加购" for c in request.call_args_list), 1)
            pay.assert_not_called()


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth.json"
        self.original = {"account": "test@example.invalid", "password": "local-password",
                         "headers": {"existing": "keep"}, "cookies": {"existing": "keep"},
                         "trust_cookies": [
                             {"name": "DESabcdef", "value": "OLD+VALUE==SRVX",
                              "domain": ".idmsa.apple.com.cn", "path": "/", "expires": None}]}
        self.path.write_text(json.dumps(self.original))
        self.client = AppleStoreLogin(auth_path=self.path)
        self.client.account = self.original["account"]

    @staticmethod
    def response(status=204):
        result = requests.Response()
        result.status_code = status
        result._content = b""
        return result

    def test_restore_domain_and_quoted_header(self):
        self.client.restore_trust()
        with patch.object(self.client.session, "request", return_value=self.response()) as send:
            self.client.request("GET", AUTH)
        self.assertIn('DESabcdef="OLD+VALUE==SRVX"', send.call_args.kwargs["headers"]["Cookie"])
        prepared = self.client.session.prepare_request(requests.Request("GET", "https://secure9.www.apple.com.cn/"))
        self.assertNotIn("DESabcdef", prepared.headers.get("Cookie", ""))

    def test_wrong_account_and_expired_cookie_not_restored(self):
        self.client.account = "other@example.invalid"
        self.client.restore_trust()
        self.assertEqual(len(self.client.session.cookies), 0)
        self.original["trust_cookies"][0]["expires"] = int(time.time()) - 10
        self.path.write_text(json.dumps(self.original))
        self.client.account = self.original["account"]
        self.client.restore_trust()
        self.assertEqual(len(self.client.session.cookies), 0)

    def test_trust_response_rotates_and_immediately_persists(self):
        self.client.restore_trust()
        expires = int(time.time()) + 3600

        def respond(method, url, **kwargs):
            result = self.response(201 if url == AUTH else 204)
            if url.endswith("/2sv/trust"):
                self.client.session.cookies.set("DESabcdef", "NEW+VALUE==SRVX",
                                                domain=".idmsa.apple.com.cn", path="/",
                                                secure=True, expires=expires)
                result.headers["Set-Cookie"] = "DESabcdef=NEW+VALUE==SRVX; Path=/; Secure; Max-Age=3600"
            return result

        with patch.object(self.client.session, "request", side_effect=respond), \
                patch("builtins.input", return_value="123456"):
            # 真实执行验证码方法和 request/auth；不执行商店回调。
            self.client.verify_two_factor()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["trust_cookies"][0]["value"], "NEW+VALUE==SRVX")
        self.assertEqual(saved["trust_account"], self.original["account"])
        self.assertEqual(saved["trust_cookies"][0]["expires"], expires)
        for key in ("account", "password", "headers", "cookies"):
            self.assertEqual(saved[key], self.original[key])
        if os.name != 'nt':  # Windows uses inherited per-user ACLs, not POSIX mode bits.
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        fresh = AppleStoreLogin(auth_path=self.path)
        fresh.account = self.original["account"]
        fresh.restore_trust()
        self.assertEqual(next(iter(fresh.session.cookies)).value, "NEW+VALUE==SRVX")

    def test_server_deletion_clears_saved_cookie(self):
        self.client.restore_trust()

        def respond(*args, **kwargs):
            self.client.session.cookies.clear(".idmsa.apple.com.cn", "/", "DESabcdef")
            result = self.response()
            result.headers["Set-Cookie"] = "DESabcdef=; Max-Age=0; Path=/"
            return result

        with patch.object(self.client.session, "request", side_effect=respond):
            self.client.request("GET", AUTH + "/2sv/trust")
        self.assertEqual(json.loads(self.path.read_text())["trust_cookies"], [])

    def test_response_without_cookie_preserves_config(self):
        before = self.path.read_bytes()
        self.client.restore_trust()
        with patch.object(self.client.session, "request", return_value=self.response()):
            self.client.request("GET", AUTH + "/2sv/trust")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
