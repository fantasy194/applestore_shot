"""读取支付宝收银台签发的扫码付款内容，不提交付款、不使用 Apple Cookie。"""
import logging
import os
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import interaction
from platform_support import private_fd

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("duo_killer")


class CashierPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.qr_url = ""
        self.has_pay_area = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "J_qrPayArea":
            self.has_pay_area = True
        if tag == "input" and attrs.get("id") == "J_qrCode":
            self.qr_url = attrs.get("value", "")


def get_payment_qr(payment_url=None):
    """返回 payment_url、支付宝 qr_url 和本地 qr_image；默认读取 payment_url.txt。"""
    if payment_url is None:
        payment_url = (ROOT / "payment_url.txt").read_text(encoding="utf-8").strip()
    payment_url = payment_url.strip()
    initial = urlsplit(payment_url)
    if initial.hostname != "openapi.alipay.com" or initial.path != "/gateway.do":
        raise ValueError("请传入完整的 https://openapi.alipay.com/gateway.do 支付链接。")
    # 独立会话：只跟随支付宝域名内的 HTTPS 跳转，不访问商户 return_url。
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"})
        url = payment_url
        for _ in range(6):
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password or
                    parsed.port not in (None, 443) or not (parsed.hostname or "").endswith(".alipay.com")):
                raise ValueError("收银台返回非预期跳转，支付链接可能已失效，请在浏览器核对。")
            if (getattr(interaction.handler, 'automatic_checkout', False) is True
                    or getattr(interaction.handler, 'retry_enabled', False) is True):
                from checkout_retry import request
                response = request(session, 'GET', url, read_only=True, stage='读取支付宝二维码', allow_redirects=False)
            else:
                response = session.get(url, timeout=(5, 15), allow_redirects=False)
            log.info("[支付宝二维码] HTTP %s %s%s", response.status_code, parsed.hostname, parsed.path)
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            if response.status_code != 200:
                raise ValueError(f"支付宝收银台返回 HTTP {response.status_code}，未取得付款二维码。")
            page = CashierPage()
            page.feed(response.text)
            qr = urlsplit(page.qr_url)
            if (not page.has_pay_area or qr.scheme != "https" or qr.hostname != "qr.alipay.com"
                    or qr.username or qr.password or qr.port not in (None, 443) or not qr.path.strip("/")):
                raise ValueError("收银台没有提供扫码付款二维码；可能已付款、链接失效或需要页面交互。")
            break
        else:
            raise ValueError("支付宝跳转次数过多，未取得付款二维码。")

    # 与官网 qrUseImage=false 时一样，将收银台 qrCode 内容绘制成二维码。
    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage
    except ImportError as error:
        raise ValueError("缺少二维码依赖 qrcode/pypng。") from error
    path = ROOT / "payment-qr.png"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
        private_fd(file.fileno())
        qrcode.make(page.qr_url, image_factory=PyPNGImage).save(file)
    return {"payment_url": payment_url, "qr_url": page.qr_url, "qr_image": str(path)}
