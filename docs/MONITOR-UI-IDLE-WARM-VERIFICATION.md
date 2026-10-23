# 监控界面、保留队列与空闲预热验收

日期：2026-10-23。

## 当前行为

- 无货、未知、样本过期、授权关闭或目标移除，均保留未执行任务及原 FIFO 位置。重新授权且有新鲜有货确认后继续；显式取消有效，历史取消不复活。
- 执行队列按入队时间、序号倒序展示，实际执行仍按原顺序。候选池只在原冻结授权范围内恢复，避免重复购买或扩大授权。
- 监控页按概览、最近有货、门店卡片组织。每条有货历史展示采样时间、SKU、门店名称及编号；默认显示三条，更多记录可展开。历史有货不计入当前库存。
- 连续有货及重启不重复记录；未知不重置周期，只有明确新鲜无货后再次有货才新建事件。SQLite 持久保存状态及时间高水位，拒绝跨客户端旧样本。
- 通知、访问密码及安装说明位于正式“应用设置”导航。门店抢购参数、添加商品、代理配置、日志筛选和说明按需展开，沿用原暖白及黄铜色。
- 每个账号实际下单空闲 10 分钟后执行所选流程，默认“预热登录（不加购）”，也可选择“提前准备自提（不下单）”。手动、定时、监控下单均重置计时并优先于预热；未触发的定时或未派发任务不阻止预热。已知待付款允许预热，未知订单结果暂缓。
- 网页初始化复用订阅首次取得的完整快照，避免连续下载两次相同历史；后续刷新保留。

## 上一轮自动验证

| 验证 | 结果与证据 |
| --- | --- |
| 全量 UI | 105/105；`artifacts/ui-redesign-all-tests.log`。包含正式设置入口、登录/密码/通知手势、倒计时与暂缓、队列倒序、付款/未知常显、历史事件、折叠及 390px 布局。 |
| 全量 Rust | 93/93；`artifacts/queue-fixture-diagnostic-rust-tests.log`。包含保留原任务、授权恢复、FIFO、候选范围、10 分钟调度、定时抢占与迟到边界。 |
| 全量 worker | 601 项中 600 通过、1 项平台跳过；`artifacts/inventory-broker-cleanup-worker-tests.log`。 |
| 分发包 | `pnpm build:web` 通过，真实冻结 worker 和 HTTP smoke 通过；`artifacts/monitor-ui-package-build.log`。前端初始化复用改动随后同步更新运行与分发包静态文件。 |
| Windows 收尾 | 修复查询响应发出后日志仍在写入的线程收尾；原两个失败用例重复 20 轮、40 次通过。代理测试以受控事件消除后台补池时序依赖。 |

历史 Rust 子进程 fixture 曾偶发启动等待失败，当前全量已通过。本轮未据此修改生产购买代码或增加超时；诊断边界见 `artifacts/queue-fixture-startup-diagnostic.md`。

## 上一轮运行与公网

上一轮验收入口（可能随重启变化）：[打开应用](https://upc-airports-pete-combinations.trycloudflare.com)。密码保持不变。Quick Tunnel 的新入口需在设备上重新启用通知；旧订阅不等于新入口已订阅。

更新前确认无账号购买任务执行，暂停队列、停止匿名监控，等待原进程退出后替换。账号列表、库存/代理配置、联系人及订单提交记录的文件哈希一致；队列原逐个模式和已保存上限 2 保留。恢复原 1 个监控任务、3 个目标。

运行程序和分发包可执行文件一致，静态入口也一致。记录：`artifacts/monitor-ui-restart-verification.json`。真实自动空闲预热已返回 healthy；未为验收发起加购或真实下单。

公网密码登录、Secure/HttpOnly/Strict Cookie、未登录隔离、Service Worker 不缓存 API、手机宽度、远程禁止关机、应用设置导航、离线壳与无脚本错误共 8 项通过：`artifacts/pwa-live-verification.json`。

队列、日志、10 分钟预热状态、四个接口的未登录及跨来源隔离、窗口任务隔离等共 12 项公网只读验收通过：`artifacts/queue-session-live.json`。真实自动预热返回 `succeeded`、`warm_login:true`、`healthy`，记录于 `artifacts/idle-warm-live-status.json`。

公网冷启动诊断：新版首次只发 1 次快照，资源版本正确，无脚本错误；一次复测公网约 10.1 秒、本机约 211ms。约 1MB 历史快照及图标传输仍受隧道和网络速度影响；不将本机耗时视为公网保证。证据为 `artifacts/pwa-init-public-readonly.json` 与 `artifacts/pwa-init-local-readonly.json`。

本轮不以离线 fixture 或页面验证代替真实 Apple 订单与设备推送投递证明。

## 本次追加：预热流程选择（离线验证，发布另记）

工作台“空闲预热”增加“预热流程”，设置值为 `login` 或 `pickup_prepare`，默认 `login`。选择后重新计时 10 分钟；运行中任务保留派发时的流程及账号商品、数量，切换不会取消真实下单或重放购买。自提预热读取本账号保存的门店和时段偏好，与手动“提前准备自提”共用 `prepare_only` 边界，不提交订单。

官网明确无货或没有合规时段时，部分准备返回 `waiting_stock`、`preparation_stage=fulfillment`、`review_verified=false`、`order_ready=false`，不会冒充会话健康或 Review 完成。完整准备须返回 `prepared`、`preparation_stage=review`、两项验证标志为真。五分钟缓存只用于后续尝试复用；真实下单仍重新读取官网页面、重新选取可用门店时段并复核。历史准备成功不是持续有效的下单承诺，详见[使用说明](QUEUE-LOGS-SESSION.md#空闲预热与流程选择)。

| 验证 | 已验证内容与证据 |
| --- | --- |
| Worker 预热定向 | 17/17 通过。覆盖两种流程、严格区分部分与完整准备、无效流程与数量、复用手动准备标志、本地偏好不被改写、541 隔离及跨流程冷却。测试：`tests/worker/test_session_maintenance.py`。 |
| Rust 预热定向 | 14/14 通过，包含相关引擎和队列测试。真实离线子进程验证派发账号 SKU / 数量、运行中改流程后保留原流程及部分准备结果；另验证 10 分钟、重启迁移、账号忙、未知提交和下单抢占。测试：`src-tauri/src/session_maintenance.rs`。 |
| 整合 Worker | 共 618 项，617 通过、1 项平台跳过；`artifacts/lean-pickup-worker-tests.log`。 |
| 整合 Rust | 102/102 通过；`artifacts/lean-pickup-rust-tests.log`。 |

上述追加是离线业务和协议 fixture 结果，不证明本次代码已部署，也不证明真实 Apple 库存、真实预热、自提下单或支付成功。上一轮公网入口及数字仅代表上一轮验收；本次 UI 全量、打包、重启和公网结果应在完成后另行补充。
