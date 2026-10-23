# 队列、日志与会话验收记录

日期：2026-10-23，Windows 本机。运行服务使用原数据目录 `%APPDATA%/com.niulai.local-web-dev`，保留 1 个已添加账号、原浏览器资料、动态代理配置和 3 个监控目标。先确认没有进行中的登录、下单或定时任务，再停止匿名监控并更新服务；只恢复原监控意图，不重放购买或登录。更新前的程序与静态网页备份保存在本机 `.local-runtime/rollback-queue-session-*`。

当前入口：[手机 PWA](https://superintendent-que-grip-bee.trycloudflare.com)。访问密码不变，保存在本机私有 `.local-runtime/pwa-access.txt`；本文不包含密码。Quick Tunnel 重新创建后网址会变化。

## 软件检查

| 检查 | 结果与证据 |
| --- | --- |
| Rust 后端 | 72/72，通过；`artifacts/queue-session-rust-recheck.log`。 |
| worker | 545 项运行，544 通过、1 跳过；`artifacts/queue-session-worker-tests.log`。跳过项是 Windows 不适用的 POSIX Chrome 符号链接锁检查。 |
| 标准 UI 配置 | 87/87，通过；`artifacts/queue-session-ui-tests.log`。最后补充时间与序号冲突的 FIFO 显示修正，相关 5/5 再次通过（`artifacts/queue-fifo-ui-tests.log`）。均使用真实 `dev:web` 启动器，未使用临时 Vite 配置。 |
| HTTP 与推送逻辑 | 16/16，通过；`artifacts/queue-session-http-tests.log`。使用项目 `.venv`，未验证真实系统推送投递。 |
| 类型与桌面编译 | TypeScript 检查及 `cargo +1.90.0 check --locked --manifest-path src-tauri/Cargo.toml` 通过；本机 Rust 默认版本未更换。 |
| Windows Web 分发包 | `pnpm build:web` 通过。实际 PyInstaller worker 与 HTTP 服务完成账号隔离、串行重试/队列校验、队列与维护配置、SQLite 日志、远程任务隔离、重启与单实例检查；`artifacts/queue-session-package-build.log`。 |

Rust 跨进程队列检查真实运行 `Engine → worker/main.py → Service → QueuedCheckout → 独立子进程`。本地桩禁止网络、浏览器、系统凭据和真实购买；核对两店同时执行、第三店后到先排队、一个目标结束后立即补位、FIFO、准确 SKU/门店、分配代次、3 个合成待付款结果及所有名额释放。首轮全套曾出现一次子进程启动前超时，随后多轮全套和单项未复现；保留 4 秒线程栈诊断，12 秒等待未放宽，不将其表述为已查明并修复的生产故障。

## 公网与官网窗口

- 基础 PWA 7 项检查通过：HTTPS 密码登录、未登录 API 401、安全 Cookie、Service Worker 无 API 缓存、390 像素手机宽度、公网禁止关闭本机、离线页及页面无异常。证据：`artifacts/pwa-live-verification.json`。
- 新功能公网 12 项检查通过：四个新 API 的未登录 401 与跨来源 403，队列状态及手机页面、真实业务日志和日志页面、30 分钟维护配置、无效浏览器任务 400 且不泄露控制能力、页面无异常。证据：`artifacts/queue-session-live.json`。这次检查只读，没有启动维护或下单。
- 独立 Chrome 本地页面验证通过：登录主线程空闲时预览继续刷新、实际鼠标和文本输入、broker 注销不关闭拥有者浏览器、注册文件清理；官方域名信任 iframe 由本地返回，信任请求恰好一次并等待响应结束。全部外网请求被阻止。证据：`artifacts/browser-remote-fixture.json`。
- 真实官网窗口通过本机 Rust 和 Quick Tunnel 在 PWA 中实际显示：采集 3 个有效帧、序号推进、图片完成渲染；结束后浏览器 API 返回 400。未输入密码/验证码、未发送远程输入、未创建订单；拿到证据后取消本次预览任务。证据：`artifacts/official-window-live.json`。

## 当前账号与 541

一次实际只读维护返回 `requires_login`，保留账号且没有订单提交。只读浏览器历史显示账号页跳转到了官网 `/shop/signIn/account`，属于官网要求重新登录，不能把“启动浏览器成功”当作“恢复登录成功”。本次维护窗口很快关闭，未采集到帧；后续单独的官方窗口预览已验证画面链路正常。证据：`artifacts/session-maintenance-live.json`。

最终代码仅把明确的官方登录跳转标记为 `requires_login`。浏览器未安装、启动失败、CDP 超时、网络失败、HTTP 错误或未知落点分别返回安全错误码与 `failed`，不据此宣告账号过期；每次维护结果与冷却跳过都有结构日志。诊断改动的完整 worker 回归已包含在上述 545 项中，未反复触发现场登录检查。

需要验证时，由本人点击“官网登录恢复（保留账号）”，在官网画面输入密码或验证码；维护等待恢复，不自动反复认证。现场检查之后，2026-10-23 07:35 左右运行历史出现新的 `me` 成功结果（`success=true`、`logged_in=true`、`scope=account`），维护状态随之清除需登录标记，恢复等待下次空闲检查；原账号仍保留。最终运行采样见 `artifacts/queue-session-final-state.json`。这是当时账号检查成功的证据，不能据此保证后续结账免认证。

旧错误记录证实曾在商城会话交换阶段返回 HTTP 541，且当时部分 Cookie 仍未过期；证据：`artifacts/session-541-readonly.json`。不能认定闲置 30 分钟是原因，也不能把后续账号检查成功归因为定时维护，或承诺每 30 分钟只读维护消除风控。

尚未验证：实体 iPhone 的操作与系统通知投递、完整的本人官网登录与信任步骤轨迹、真实库存触发后的订单成功及付款。分发包验证和本地页面测试均未向 Apple 发起购买。`artifacts/` 是忽略的本机证据目录，不包含在分发包内。
