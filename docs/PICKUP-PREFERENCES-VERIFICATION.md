# 多门店自提偏好验收记录

日期：2026-10-23，Windows 本机。实现说明见[自提门店与时段偏好](PICKUP-PREFERENCES.md)。使用现有 Apple Store 网页结账模型；官网的[取货说明](https://www.apple.com.cn/shop/help/shipping_delivery)支持更换自提门店及部分商品的预约时段，并要求有效的取货人资料。

后续真实 Review 联系人脱敏误判已修复，详见[联系人修复记录](PICKUP-CONTACT-FIX.md)；该记录中的全量检查及重启信息更新。

## 软件检查

| 检查 | 结果与本机证据 |
| --- | --- |
| worker 完整回归 | 576 项运行，575 通过、1 项 Windows 不适用的 POSIX 锁检查跳过；`artifacts/pickup-preferences-worker-tests.log`。 |
| Rust 后端 | 81/81；`artifacts/pickup-priority-rust-tests.log`。包含真实 Python 协调器及隔离子进程的离线跨边界验证。 |
| 标准 UI 配置 | 95/95；`artifacts/pickup-preferences-ui-tests.log`。相关偏好和监控查询 30/30；`artifacts/pickup-running-query-ui-tests.log`。 |
| HTTP 与推送逻辑 | 16/16；`artifacts/pickup-preferences-http-tests.log`。 |
| 类型与桌面编译 | TypeScript 通过；`cargo +1.90.0 check --locked --manifest-path src-tauri/Cargo.toml` 通过，记录于 `artifacts/pickup-preferences-tauri-check.log`。 |
| Windows Web 分发包 | `pnpm build:web` 通过；真实 PyInstaller worker 完成无本地联系人保存多门店/时间窗口、保留已有送货联系人、重启恢复与原有隔离检查；`artifacts/pickup-preferences-package-build.log`。扩展的 `pnpm test:web` 再次通过，额外确认打包 worker 可加载共享库存查询模块且默认不启用代理；`artifacts/pickup-preferences-package-smoke.log`。 |

离线结账覆盖门店顺序、首店无货/无匹配时段后切换、最早/最晚/完整窗口和后备规则、只接受当次响应的门店列表与签名、门店状态字段缺失或类型错误时停止且不换店、其他门店的“无需预约”状态不可混用、官网联系人完整字段/明确掩码绑定复用、复核信息不一致停止、准备等待后重新取得有效时段。结果只包含实际门店和公开时段摘要，不输出时段签名。

队列检查确认两家候选店同时有货只形成一个购买任务；仅包含明确授权的门店，保留顺序和时段规则。等待任务只能收窄候选范围，未知库存不重置补货周期，冲突配置在界面可见。固定单店、人工下单、显式并发与串行失败重试继续回归。

## 运行与只读官网验证

该次验收使用入口 `parker-commissions-nights-boxed.trycloudflare.com`，已在后续联系人修复重启时更换；当前地址见[联系人修复记录](PICKUP-CONTACT-FIX.md)。密码保持原值，私有记录位于本机 `.local-runtime/pwa-access.txt`。Quick Tunnel 重建后网址会改变。

部署前确认没有进行中的账号、登录、定时或购买任务，备份原程序和静态网页到本机 `.local-runtime/rollback-pickup-preferences-*`。沿用原数据目录及浏览器资料，保留 1 个账号、代理配置和 3 个原监控目标；更新后恢复原监控及运行队列的意图，没有恢复或重放登录/购买任务，没有替用户更改账号的门店规则。

- 新功能公网验证 8 项通过：390 像素界面可设置预选多店、时间窗口和官网联系人来源；从自动监控页发起一次真实匿名门店查询，返回 8 家附近门店。查询前后是同一监控任务，检查时间继续推进；3 个监控目标及账号/通用自提文件保持不变。共享查询日志证明请求经监控持有的同一代理池处理，当时可派发连接为 10。证据：`artifacts/pickup-preferences-live-verification.json`。
- 基础 PWA 验证 7 项通过：HTTPS 密码登录、安全 Cookie、未登录 API 隔离、Service Worker 与离线页、390 像素宽度、公网禁用本机关闭接口、页面无异常。证据：`artifacts/pwa-live-verification.json`。
- 队列、日志、维护与浏览器任务隔离公网检查 12 项通过，未启动维护、登录或下单。证据：`artifacts/queue-session-live.json`。
- 运行程序与分发包的可执行文件、完整静态网页逐文件哈希一致；最终运行状态记录于 `artifacts/pickup-preferences-final-state.json`。

这些证据验证协议处理、队列联动和实际门店发现，未执行真实加购、订单提交或付款。结账库存、预约时段和官网会话仍以 Apple 当次响应为准；实体 iPhone 的系统通知投递也不属于本次验证范围。
