# 自提联系人 Review 脱敏误判修复

2026-10-23。用户日志显示联系人表单、付款选择和 Review 均返回 HTTP 200、业务状态 200，本地随后报“官网复核取货联系人与本次确认的信息不一致”。新增字段级诊断进一步定位到 `emailAddress`。

## 实际差异

在账号空闲时仅 GET 当前官网结账页，得到 HTTP 200、`Review`，没有请求最终提交。取货姓、名与本地值相同；官网 `pickupContactInfo.d.emailAddress` 隐藏邮箱中间字符，`fullDaytimePhone` 以八个圆点加末四位显示。Review 没有提供联系人字段的掩码提交绑定。

以下只用虚构值说明格式：

| 字段 | 本次表单发送 | Review 展示 |
| --- | --- | --- |
| 邮箱 | `tester123@example.com` | `t•••••••3@example.com` |
| 手机 | `13800000000` | `••••••••0000` |

原实现将完整提交值和脱敏展示值直接比较，因此误拦。手机号掩码数量也不代表被隐藏的号码位数：八个圆点可代表七位数字。实际诊断记录仅包含字段名、格式和比较结果，不保存姓名、完整邮箱或手机号：`artifacts/pickup-contact-live-diff.json`。

## 修复规则

联系人表单路径保持 `checkout.pickupContact.selfPickupContact.selfContact.address.<field>`。只有确实发入本次表单、且官网已进入有效 Billing/Shipping 步骤的完整值，才在本次内存证明中标记 `server_accepted`。失败、仍停留在联系人页面或未实际发送的字段不能获得该标记。

复核时，有上述接收证据才允许匹配官网脱敏展示：邮箱必须域名完全相同、用户名两端可见字符对应；手机号必须保留可核对的末四位，整个掩码表达须与本次完整号码匹配。全掩码、可见字符不符、不同域名或没有本次接收证据的 Review 掩码继续阻止提交。隐藏字符无法从掩码反推，以本次成功提交的完整值为依据。原有明确的官网掩码绑定复用方式保持独立，不向官网发送掩码字符串。

同时统一表单与复核两侧的 HTML 展示处理、邮箱大小写及电话号码分隔符；只有明确的 `+86`/`0086` 和合法中国手机号组合才去除国码，不用任意数字后缀代替完整号码核对。准备恢复流程使用同一规范化规则。错误只显示字段名和原因，不显示联系人值。

## 验证

- 49 项相关离线回归通过，涵盖实际脱敏形状、Contact → Billing → Review → prepared、未接收字段不能获得证明、真实联系人变化阻止提交，以及原门店/时段/准备流程。`artifacts/pickup-contact-review-mask-focused-tests.log`。
- 完整 worker 回归 590 项运行，589 通过、1 项 Windows 不适用的 POSIX 锁检查跳过；`artifacts/pickup-contact-worker-tests.log`。前一轮曾在未修改的并发进程清理路径出现一次 Windows 管道关闭异常，独立 12 项复测和最终全量均通过，未将其描述为已查明的联系人问题。
- `pnpm build:web` 通过，内置的实际 PyInstaller worker 与 HTTP 分发包检查通过。`artifacts/pickup-contact-package-build.log`。
- 当前真实 Review 的只读字段核对通过，未发 POST、未提交订单、未写账号文件。`artifacts/pickup-contact-review-live-verification.json`。该检查将当前保存的联系人与已经由用户成功发送到 Billing 的表单对应，不将其描述为新建订单或新的完整下单验证。
- 诊断准备流程遇到正在进行的手动购买时退出，没有抢占账号锁或打断用户任务；没有自动登录、重复加购或最终提交。

本次读取不证明付款或完整真实下单成功。原始会话过期时不自动重放，只有当时可读取的当前 Review 用于字段差异验证。

## 重启

已正常重启原数据目录中的服务，恢复原 3 个监控目标及队列运行状态。账号自提配置、库存配置和订单提交标记在重启前后逐文件哈希相同；没有重放登录或购买。第一次启动遇到旧进程尚未释放的工作空间锁，确认旧进程退出后重试，没有删除锁文件或强行抢占。运行程序与分发包的可执行文件、完整静态网页哈希一致。证据：`artifacts/pickup-contact-restart-verification.json`。

当前入口：[手机 PWA](https://cakes-instant-mystery-phys.trycloudflare.com)。访问密码保持原值，私有记录仍在本机 `.local-runtime/pwa-access.txt`。Quick Tunnel 重启后网址改变，需要在新地址重新登录并启用通知。
