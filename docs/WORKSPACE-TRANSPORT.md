# Web 实时状态与按需任务详情

任务与日志仍完整持久化。Web 工作台使用轻量视图，历史查询结果、付款二维码与历史日志不会随每秒状态轮询整包传输。

## 读取接口

所有接口均经过现有访问密码、Host 与 Origin 检查。

| POST 接口 | 参数 | 返回 |
| --- | --- | --- |
| `/api/workspace_snapshot` | `{}` | 轻量 `Snapshot`，用于主动刷新 |
| `/api/workspace_changes` | `{cursor?: string}` | 初始/过期时轻量 `snapshot`；其余仅变更任务、删除 ID、账号与预设变更 |
| `/api/task_detail` | `{accountId, taskId, includeLogs?: boolean}` | 严格验证账号归属的单项完整任务；`includeLogs:false` 不返回旧日志 |

旧 `/api/snapshot` 保留给内部诊断与本地脚本。Web Bridge 的 `snapshot()` 与持续订阅不会调用它。桌面原快照保持兼容。

轻量任务保留状态、阶段、交互 prompt、定时信息、执行队列关联与 `order_submitted`。结果保留 `success/status/message/logged_in/scope/name/account_removed/order_number/device_trust_saved` 等小标量。历史订单查询额外保留官网已核实的最小订单号、验证来源与全部商品状态，以保持旧待付款卡片的取消/过期判断；不带商品详情、链接、联系人或二维码。`is_summary:true` 表明需要显式读取详情；`detail_revision` 来自完整任务内容指纹，`log_count` 表明保存的日志数量。单项详情带 `is_summary:false` 与 `logs_included`。

`workspace_changes` 返回不透明 `cursor`。进程更换、未知、未来或超出保留范围的游标都会返回轻量重置，不会重放任务。最多保留 128 轮变更；每个订阅按串行请求合并，网络中断后继续从上一个成功游标恢复。不能用秒级 `updated_at` 判断任务是否变化，同一秒内的状态/结果/日志变化均由内容指纹识别。

工作台仅对正在查看的任务读取详情；监控页仅对当前监控/检查任务读取 `includeLogs:false` 的结果。查看历史日志使用独立活动日志查询。

## 离线验证边界

`workspace_feed` Rust 专项覆盖同秒更新、同长度日志替换、账号增删、预设更新、跨账号详情拒绝、游标过期与有界缓存。340 条历史任务的合成数据中，完整快照为 19,584,294 字节，首次轻量视图 135,284 字节，单轮监控变更 503 字节，无日志单项结果 520 字节；341 条持久任务均保留。这是合成数据测量，不代表公网耗时保证。

`tests/local_web/test_pwa.py` 验证真实 HTTP 服务的认证、跨源限制、参数类型、任务归属与轻量/完整详情差异，可用 `DUO_TEST_SERVER_EXE` 指定隔离测试二进制。`tests/ui/workspace-transport.spec.ts` 验证浏览器端增量合并、重置、重试、请求不重叠与取消，不触发 Apple 请求。
