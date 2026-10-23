# 验证记录

首次验证日期：2026-10-22，Asia/Hong_Kong；10 月 23 日的修复与复验另列于下方。此记录区分模拟业务、实际打包程序、真实库存接口和实体设备投递。

| 检查 | 结果 | 证据范围 |
| --- | --- | --- |
| Worker 全量回归 | 464 项，跳过 1 项，其余通过 | 登录/结账原有回归、最早时段、监控联动、精确门店、并发取消及提交未知；合成会话和订单 |
| Rust 本地服务 | 34/34 通过 | 账号隔离、定时、库存去重、重启中断、并发参数、认证哈希和会话 |
| React 浏览器回归 | 58/58 通过 | 原工作台及新增监控、多店选择、并发付款码、390px 布局 |
| Web Push / HTTP | 13/13 通过 | 11 项推送测试和 2 项真实本机 HTTP 认证/PWA 测试；实际 AES128GCM 加密和 VAPID 签名使用模拟接收端 |
| 开发与打包 Web 浏览器 | 4/4 通过 | 开发热更新/退出重启、实际打包程序登录隔离、密码登录、真实 Service Worker 激活及无 API 缓存 |
| 完整 Web 打包 | 通过 | TypeScript、Vite、Rust release、PyInstaller；打包后的服务和 worker 通过 HTTP 单实例/持久化/取消 smoke |
| 实际 Quick Tunnel | 7 项检查通过 | 未登录 401、HTTPS 密码登录、Secure/HttpOnly/SameSite Cookie、SW 激活、390px 无溢出、公网禁止关机、离线壳和页面无错误 |
| 原有账号文件 | 保留 | 已有 accounts.json、auth.json、checkout.json 共 3 文件哈希未变化；浏览器资料留在原目录 |

测试日志和截图保存在本机 `artifacts/`，公网报告为 `pwa-live-verification.json`，手机截图为 `pwa-live-mobile.png`，连续实查为 `inventory-live-monitor.json`。运行中的私有数据目录仍使用原 Web 工作空间；启动方式见 [使用说明](MONITOR-PWA.md)。

## 真实只读库存

使用匿名库存会话、未启用付费代理，查询抛光布 `MLH74FE/A`，邮编 `210000`，Apple 首次返回 8 家附近门店及实际编号。虹悦城 `R643` 返回“今天可取货”；苏州 `R688` 返回“暂无供应”。这是查询时刻的结果。

连续三轮实测采用每家店独立编号锚定，两店每轮都获得明确样本，合计 6 个有效店/SKU观察。样本完成间隔约 1.39 秒和 1.28 秒；配置轮询间隔为 1 秒，实际响应速度受网络影响，没有重叠和追赶补发。

实测发现单门店锚定响应可能仅含该店，因此实现已经改为每店并发查询、同店多 SKU 合并；只有响应实际提供的其他门店才共享结果。缺失门店、403/429/541、非 JSON 和挑战均保持未知。上述实查没有登录、加购物袋、提交订单或付款。

## 证据限制

- 并发测试启动真实的独立子进程，覆盖三路同时运行、逐路付款文件导入、交互身份绑定、取消和未知提交不重启。订单来自 fixture，不能证明 Apple 服务端购物袋相互独立，或三路都会成功。
- 初验时付费代理供应商生命周期、白名单、补充节奏、租期及加密持久化通过 fixture；10 月 23 日已按用户提供的新接口做有限的真实提取及只读库存验证，详见下方。没有复制参考项目凭据。
- Web Push 的协议加密、签名、分钟提醒、30 分钟截止、重启去重及终态停止均已验证；没有替用户订阅设备或发送真实外部推送，实体 iPhone 的安装、授权及系统通知投递仍需在设备上完成。
- `pnpm check:format` 仍报告 15 个文件的格式问题。核对 Git HEAD 可复现原有 App/样式/测试文件格式问题，部分文件另有 Windows 换行差异；未为此重排不相关代码。构建、业务回归和上述浏览器检查均通过。

## 2026-10-23 修复与复验

大漠接口在午夜返回 `2026/10/23 0:06:38` 一类未补零时间，原 ISO 解析拒绝该有效期，导致成功提取后仍报 `provider_expiry` / `proxy_pool_empty`。现兼容中国时区的该格式，继续拒绝非法日期、已过期和异常远期租约。单次多店检查也会按实际并发请求数等待代理补充，空目标不提取付费代理。

用户新提供的接口已替换到本机加密配置，未写进源码或本记录。00:11 的有限真实采样有 7 个可派发代理，供应商错误为空；玄武湖和新街口返回 Apple 原文“请于 10 月 23 日查看具体供应状况”，另一店请求未完成，均保留库存未知。代理可连接不能证明有货。报告为 `artifacts/proxy-midnight-verification.json`。界面现在区分补充代理、供应商错误、无有效样本和真正过时样本，并保留官网提示。

人工、定时和监控自提的默认路数统一为 1，保留显式 1–10 路。新增 `retry_count`，默认 0、范围 0–10，为额外重试次数；每轮完成清理后才串行重试，待付款、提交标志、未知/损坏提交证据、认证要求及取消均停止。异常耗尽与并发全部失败保持失败状态，任务页显示已执行及额外重试次数。

实际失败日志表明商城 Cookie 可发送且购物袋已识别登录，随后结账页单独要求再认证。并发结果现在返回 `CHECKOUT_REAUTH_REQUIRED` 等准确分类，提示使用单路完成结账认证，不再引导反复预热；子任务仍不启动浏览器登录或验证码，也不嵌套自动重试。未通过新一轮真实下单证明 Apple 会接受并发结账。

| 复验 | 结果 | 证据范围 |
| --- | --- | --- |
| Worker 全量 | 495 项，跳过 1 项，其余通过 | 时间解析、代理补充、串行重试、整批清理、部分成功、落盘与丢失 IPC 提交证据、认证停止、取消及原业务回归 |
| Rust 本地服务 | 40/40 通过 | 默认参数、旧定时兼容、冻结重试次数、监控载荷、失败状态及进度不覆盖待付款 |
| React 浏览器 | 72/72 通过 | 默认单路、0 次重试、手动/定时/监控配置、已执行次数、失败与成功展示及窄屏 |
| TypeScript / 完整 Web 包 | 通过 | Rust release、Vite、PyInstaller、真实打包 HTTP smoke；实际冻结 worker 在认证前拒绝无效重试配置 |
| 实际 HTTPS PWA | 7 项通过，另验证页面缺省值 | 密码登录、API 隔离、SW、手机宽度、离线与公网禁止关机；运行页面手动及现有 3 个监控目标均为 1 路、0 次额外重试 |

更新后继续使用原数据目录、账号、密码及加密配置，按更新前的运行意图恢复库存监控；没有重放中断的购买或登录任务。最新运行入口保存在本机 `.local-runtime/pwa-access.txt`；Quick Tunnel 重建会改变网址。日志为 `artifacts/retry-final-worker-tests.log`、`retry-ui-tests.log`、`retry-web-build.log`，公网证据为 `pwa-live-verification.json` 和 `retry-live-verification.json`。上述回归使用合成订单；没有代替用户发起真实下单、付款或实体手机推送投递。

## 复验命令

```powershell
pnpm test:worker
cargo test --locked --manifest-path local-web/Cargo.toml
pnpm test:ui
pnpm build:web
pnpm test:web:ui
```

推送与 HTTP fixture 需 `.venv` 和已构建的 debug 本地服务，设置 `PYTHONPATH=worker;worker/legacy`、`PYTHONUTF8=1` 后运行 `python -B -m unittest discover -s tests/local_web -p 'test_p*.py'`。实际只读采样可用 `.venv/Scripts/python.exe scripts/verify-inventory-live.py`，最多三轮；公网只读 UI 验收可用 `node scripts/verify-pwa-live.mjs`，密码文件只从本机 `.local-runtime/pwa-access.json` 读取。
