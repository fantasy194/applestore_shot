# 隐私检查记录

检查日期：2026-10-08。范围为当前项目源码、文档、测试、Git 忽略规则、项目内运行文件，以及现有 macOS app / Web 产物；未访问 Apple 或上传文件。

## 已处理

- 删除 README 中的真实邮箱示例；测试中的相同账号前缀改为 `sample17@example.test`。
- 删除图标和实施文档中的本机用户名、绝对路径及临时生成文件位置。
- 清理测试目录内两份已无监听服务的 `.dev-web.json`。它们包含本地服务访问凭证，原本已被 `artifacts/` 忽略，文件权限为 `0600`。
- 补充忽略 `accounts.json`、`purchase.json`、`task.json`、`.dev-web.json` 及临时文件、`.env.*`，以及 `accounts/`、`settings/` 运行目录。

## 检查结论

- 当前项目内未发现真实账号的 `auth.json`、`checkout.json`、`payment.json`、HAR、浏览器 Cookie 数据库或私钥文件。测试代码中的账号、电话、地址及密码字符串为合成测试数据；URL 用户名密码的反例也不是实际登录凭证。
- 可被 Git 收录的 JSON 为 `package.json`、`tsconfig.json`、`src-tauri/tauri.conf.json` 和 `src-tauri/capabilities/default.json`，属于工程配置，不能统一删除或忽略全部 JSON。
- 网页静态产物未命中本次检查的真实邮箱前缀或本机用户名。对 app / Web 内置 Python 包中十个自有业务模块的解包检查，也未命中这些标识。
- `certifi/cacert.pem` 是依赖附带的公开 CA 证书集合，不是用户私钥。
- 首次提交前仅检查并收录源码、工程配置、合成测试及文档素材；依赖、构建产物和本机运行数据由 Git 忽略。本记录不能证明以前对外发送过的文件已被收回。

## 仍存在的构建信息

**现有 Rust 可执行文件包含构建时的本机用户名路径。**它们不是账号密码或会话 Cookie，但能暴露构建机器的用户名，因此现有产物不能称为完全去标识化。

- `local-web-release/duo-killer-web`
- `src-tauri/target/release/bundle/macos/DUO Killer.app/Contents/MacOS/duo-killer`
- 旧名称 app 里的同类可执行文件也存在此信息。

部分第三方原生依赖还带有其构建机器的路径。当前发布构建尚未配置 Rust 路径映射；若要分发完全去标识化的版本，需要配置后重新构建和检查。不要直接压缩整个开发目录，里面还包含构建缓存和测试产物。

## 正常运行会保存哪些信息

账号数据默认在系统应用数据目录中，桌面、正式 Web、开发 Web 分别使用 `com.niulai.desktop`、`com.niulai.local-web`、`com.niulai.local-web-dev`。DUO Killer 改名后继续保留这些标识及原系统钥匙串服务名，以兼容已保存的账号、设备信任和配置；它们不是界面显示名。

| 文件                          | 可能包含的信息             |
| ----------------------------- | -------------------------- |
| `accounts.json`               | 账号邮箱、昵称及购买预设   |
| `auth.json`                   | Cookie、设备信任与认证会话 |
| `checkout.json`               | 联系人、电话、邮箱及地址   |
| `task.json` / `payment*.json` | 任务结果、订单及支付信息   |
| `.chrome-profile/`            | 专用浏览器资料             |
| `.dev-web.json`               | 本地开发服务访问凭证       |

桌面 / Web 业务入口会移除 `auth.json` 中的密码字段；选择保存密码且登录成功时，Windows / macOS 使用系统凭据保险库，Linux / Docker 使用显式配置的 AES-GCM 加密保险库。Docker 主密钥保存在宿主机 `secrets/duo-vault.key`，只读挂载给应用；密文位于持久卷 `/data/.credentials`。密钥须独立安全备份，丢失后无法解密；Compose 文件挂载不会加密宿主机密钥，宿主机管理员仍可读取。未启用时不保存，浏览器直接输入的密码不会被采集。上述运行数据仍是敏感信息，Git 忽略只防止常规误提交，不等同于加密或自动脱敏。检查没有删除系统应用目录中的真实账号、地址或订单数据。

本次为静态检查和文件核对，不等同于网络流量审计或“绝无泄漏”的保证。

手动“移除账号”会清理所选账号目录（会话、浏览器资料、地址和购买预设、本地任务 / 支付记录）、账号索引及系统保存密码。通用预设和其他账号保留；官网账号和订单不会被删除。清理失败会显示错误，可重试，已经清除的本地数据不会恢复。
