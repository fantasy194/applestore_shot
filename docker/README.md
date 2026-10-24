# Docker 本地部署

在项目根目录执行（Docker Desktop 使用 Linux containers）：

```sh
docker compose up -d --build --wait
docker compose logs app
```

打开 **http://127.0.0.1:17654**，使用首次启动日志中的“首次 Web 访问密码”登录；也可直接使用日志中的 `#access=` 本机链接。已有源码服务默认使用 17653，Docker 默认使用 17654，可同时运行。

Compose 启动 `app` 和 `browser` 两个服务。`app` 提供 Rust Web、React 页面及 Python worker；`browser` 基于 [jlesage/chromium](https://github.com/jlesage/docker-chromium) 镜像提供真实 Chromium，固定多架构 digest 为 `sha256:e42de7469bce122b8a69d4f62101850ece45c41ed7ed952f2b506f99947093ae`（Chromium `152.0.7977.82`），支持 `linux/amd64` 和 `linux/arm64`。桌面与源码服务继续使用本机 Chrome/Edge；`app` 保留原内置浏览器兼容旧单镜像启动，Compose 登录实际使用 `browser` 中的 Chromium。

新增账号、手动登录、自动会话维护和官网登录恢复使用 `browser` 中的普通 Chrome 窗口，继续支持应用现有预览和鼠标 / 键盘输入（含空格键）。Docker 中每个账号由 broker 持有自己的常驻浏览器及 `/data` 下的专用资料，任务结束或取消仅释放操作权，下次任务继续连接同一进程，不因自动 / 手动模式切换而重启或改用无头浏览器。自动任务是否提供人工预览仍由任务类型决定。多个账号的进程、页面和 Cookie 相互隔离；不使用共享的 `/config/chromium` 默认资料，不执行上游默认删除 `Singleton*` 的脚本。侧车保持固定专用 hostname，支持原资料随容器重建继续使用。

worker 通过 `/data/.browser-control/control.sock`（目录 `0700`、socket `0600`）请求单账号独占操作租约及 CDP 通道，CDP 仅绑定容器回环地址；app 为本次任务建立回环转发。worker 断开仅释放租约，常驻浏览器由 broker 继续持有；活跃租约期间，同账号第二个任务不能同时操控页面。旧任务 token 随释放失效，迟到输入、连接或关闭不能影响后来任务。购买优先由应用取消并释放已有维护任务后再获取租约，不并发抢控账号。远端 PID 不交给 app 的进程清理器。

移除账号、显式关闭账号浏览器或服务退出会明确关闭相应常驻进程，并保留原浏览器资料。每个 Chrome 由独立监督子进程持有，保活管道属于 broker；broker 即使被强制结束，管道 EOF 也会清理它启动的账号进程组，不影响其他浏览器、不删除 `Singleton*` 锁文件。broker 正常退出会并行关闭全部受管进程；重启只读取后续任务请求，不主动恢复浏览器、登录或购买。两服务无需共享网络命名空间，也不挂载 Docker socket。CDP、VNC、jlesage Web GUI 均不发布到宿主，文件管理器及终端关闭，仍由应用密码入口提供浏览器操作。

空闲会话维护在账号常驻浏览器中读取官网页面，核验并同步当前会话；自提预热再继续原 `prepare_only` 流程。普通官网登录可用鼠标 / 键盘填写账号和可用密码；验证码、其他安全挑战或缺少密码时提示人工处理。541 及冷却标记保留，不反复认证。手动官网恢复从当前首页菜单进入登录，详情和证据边界见 [官网恢复与空闲会话刷新](../docs/STOREFRONT-SESSION-RECOVERY.md)及[官网自动填写](../docs/BROWSER-LOGIN-AUTOFILL.md)。普通更新继续保留 `/data` 和容器图形显示环境。

Compose 由 Docker 自动选择主机架构；两个镜像均原生构建和发布 amd64/arm64。worker、浏览器 broker 和 Chromium 使用 UID/GID `1000:1000`，jlesage 初始化与显示管理沿用上游机制。Compose 加载 [`chrome-seccomp.json`](chrome-seccomp.json) 允许沙箱所需的用户命名空间，未使用 privileged 或 `--no-sandbox`。该 profile 来自 [Playwright 官方 Docker 配置](https://github.com/microsoft/playwright/blob/main/utils/docker/seccomp_profile.json)，相关说明见 [Playwright Docker 文档](https://playwright.dev/docs/docker)。

## 使用 GHCR 镜像

在项目根目录的 `.env` 加入镜像名：

```dotenv
APP_IMAGE=ghcr.io/fantasy194/applestore_shot:latest
```

拉取并启动已发布镜像（保留现有 Compose 项目名及数据卷）：

```sh
docker compose pull app browser
docker compose up -d --no-build --wait
```

镜像为私有包时，先使用有 `read:packages` 权限的 GitHub token 执行 `docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin`，从标准输入传入 token。`latest` 用于默认分支发布，固定版本可使用发布记录中的 SHA 标签或多架构 manifest digest。镜像标签包含 `linux/amd64` 和 `linux/arm64`，无需设置 `platform` 或启用跨架构模拟；这里的 x86 指 x86-64，不支持 32 位 x86。

浏览器镜像默认使用 `${APP_IMAGE}-browser`，例如 `latest-browser`、`sha-完整提交号-browser` 或 `v版本-browser`；可设置 `BROWSER_IMAGE` 指定配套镜像。若使用 digest 固定 app，必须同时设置配套 `BROWSER_IMAGE` digest，不能在 digest 后附加 `-browser`。两个镜像分别通过原生架构离线服务 / 浏览器检查，再合并各自 amd64/arm64 manifest。

不设置 `APP_IMAGE` 时默认本地构建 `iphoneduo:local` 与 `iphoneduo:local-browser`。使用 GHCR 时更新用上面的 `pull` 和 `--no-build` 命令。

旧部署需要先更新一次 Compose，才能获得 `browser` 服务。重新运行 [安装器](../scripts/install-docker.sh) 会识别并备份旧官方配置或上次受管配置，校验新 Compose 成功后升级；保留原项目名、`.env` 和 `/data`。无法识别的自定义配置会保留并提示手动合并。仅拉取新 app 镜像不会自动新增侧车。启用 [自动更新覆盖文件](../compose.autoupdate.yaml) 后，Watchtower 同时跟踪 app 与 browser；继续沿用原 Compose 项目和数据卷。

### 自动更新配置与诊断

Watchtower 默认每 300 秒检查一次。Actions 发布完成后，等待下一次检查，并核对运行容器的版本或镜像 revision；`healthy` 只说明更新器进程存活。日志中的 `updated=0` 不代表失败，`skipped>0` 也不能直接认定为无更新或认证失败，需要查看具体错误。现场曾出现一轮跳过后，下一轮正常自动升级的情况。

更新器通过只读目录挂载读取 Docker 注册表配置。默认目录为执行 Compose 用户的 `${DOCKER_CONFIG:-$HOME/.docker}`，可使用 `DUO_REGISTRY_CONFIG_DIR` 明确指定；安装器按 Compose 解析后的 `.env` 和环境变量优先级创建缺少的目录，相对路径以部署目录为准，不复制凭据或修改已有 `.env`。公开镜像支持空目录和匿名拉取，私有镜像需要在对应目录执行正常的 `docker login`。目录挂载让 `docker login` 原子替换 `config.json` 后的新配置继续可见。凭据助手还需该助手在更新器内可用，不能仅挂载依赖宿主专用助手的配置。

手动使用覆盖文件前，先创建所选配置目录，例如 `mkdir -p "${DOCKER_CONFIG:-$HOME/.docker}"`。安装、登录和后续 Compose 操作应使用同一用户；`sudo` 可能改变 `HOME`。需要固定路径时，可在原 `.env` 中自行设置 `DUO_REGISTRY_CONFIG_DIR=/home/部署用户/.docker`，并确保该目录存在；不要在 `.env` 中存注册表 token。显式导出的 `DUO_REGISTRY_CONFIG_DIR` 优先于 `.env`。

先查看现有日志；需要定位跳过原因时，临时以 debug 日志重建更新器，应用和浏览器不重建：

```sh
docker compose -f compose.yaml -f compose.autoupdate.yaml logs --since 20m watchtower
WATCHTOWER_LOG_LEVEL=debug docker compose -f compose.yaml -f compose.autoupdate.yaml up -d --no-deps --no-build --pull never watchtower
docker compose -f compose.yaml -f compose.autoupdate.yaml logs --since 10m watchtower
# 诊断完成后恢复默认 info 日志。
docker compose -f compose.yaml -f compose.autoupdate.yaml up -d --no-deps --no-build --pull never watchtower
```

如在 `.env` 固定了日志级别，恢复时也需改回 `info`。debug 可用于定位 `Added container as skipped` 的错误原因；不要启用 `trace`，上游明确说明它可能输出凭据。不要在长期运行的更新器内用 `docker exec ... --run-once` 诊断，该模式可能修改更新器自身的重启策略。排查时区分注册表访问、标签是否更新、容器是否已更换和页面缓存，并保留在途任务与 `/data`。

## 数据与密码

自 0.1.6 起，在应用密码提示或预填字段中输入的密码会按账号保留在应用服务内存中，供后续登录、下单、队列和后台恢复共用。明确密码拒绝、忘记密码或移除账号会清除这份缓存；网络失败与 541 不当作密码错误。重启 app 会清空内存缓存；启用下述持久密码库并勾选保存后，后续任务可从加密库恢复，否则需再次输入；浏览器中直接输入的密码不会被应用采集。原 `/data` 中的浏览器资料、Cookie 和设备信任继续保存，缓存不替代官网对会话的核验。

空闲预热的“官网登录恢复（保留账号）”复用人工恢复使用的官方动态登录入口，但后台统一以无头模式运行；“预热登录”和“提前准备自提”也受同一启动限制。三种模式保持原账号资料，后台遇到验证码或安全挑战停止并提示人工，不切换为可见窗口。人工发起的官网登录和恢复仍提供远程窗口。

反向代理需保留 WebSocket Upgrade 转发。0.1.6 在连接连续失败时切换为经过认证的 HTTP 增量事件读取，继续使用同一 CDP 实时画面和页面归属校验，不改为定时截图。仅画面读取可自动恢复；输入结果不明时先核对官网当前状态，不自动重发点击、登录或键盘输入。

账号、每账号的 Chrome 配置、Cookie、购买队列、日志和加密设置保存在 `iphoneduo_app-data` 数据卷的 `/data` 下。重新构建、重启以及普通 `docker compose down` 均保留数据。`docker compose down -v` 会删除数据，正常更新不要使用 `-v`。

Docker 使用独立数据卷，不导入当前 Windows 服务的数据，也不复制当前浏览器资料。Windows 的 DPAPI 和系统凭据库无法直接移植到 Linux；请在容器页面重新添加账号和登录。Windows/macOS 继续使用各自系统保险库；自 0.1.7 起容器支持独立主密钥的加密密码库，Chrome 资料及已验证会话仍按原实现持久化。

### 容器持久密码库（0.1.7）

[安装器](../scripts/install-docker.sh)默认初始化并启用 [compose.vault.yaml](../compose.vault.yaml)。只有用户勾选保存时才写入密码；未勾选仍仅在当前服务运行期间复用。密文按账号保存在原 `/data/.credentials` 中，32 字节主密钥单独保存在部署目录 `secrets/duo-vault.key`，仅只读挂载给 app 的 `/run/secrets/duo_vault_key`，不交给 browser。目录为 `0700`、密钥文件为 `0400`，属主 UID/GID 为 `1000:1000`；Compose 本地文件 secret 不会自动改变宿主文件权限，也不替代应用加密。

新安装自动完成上述步骤。已有官方部署重新运行安装器后可启用；自定义 Compose 会保留，需要手动合并。手工启用时，先确认没有正在下单或等待确认的任务，保留原部署目录、Compose 项目及全部覆盖文件。以下示例在原基础及自动更新覆盖文件之上增加密码库；有其他覆盖文件时也须继续携带：

```sh
mkdir -p secrets docker
# 先将本仓库 docker/init-vault.py 与 compose.vault.yaml 更新到部署目录。
docker compose -f compose.yaml -f compose.autoupdate.yaml run --rm --no-deps \
  --user 0 --entrypoint python \
  --volume "$PWD/secrets:/vault-host" \
  --volume "$PWD/docker/init-vault.py:/vault-init.py:ro" \
  app /vault-init.py --key-file /vault-host/duo-vault.key --vault-dir /data/.credentials

docker compose -f compose.yaml -f compose.autoupdate.yaml -f compose.vault.yaml \
  up -d --no-deps --no-build --wait app
```

初始化使用 app 镜像里的 Python，不要求宿主安装 Python，不读取账号密码。重复执行保留原密钥；检测到密文存在但主密钥遗失时会停止，不能静默重新生成。已有密钥权限不符时也会停止，确认文件归属后再显式修正。安装器可用 `DUO_ENABLE_VAULT=0` 选择不启用；它不会删除已有密钥或密码库。

后续所有 Compose 更新必须继续使用 `-f compose.vault.yaml`。Watchtower 会保留 app 的只读 secret 挂载，普通重启和镜像更新不会更换主密钥。请将主密钥另行安全备份，并与数据卷分开保管；恢复已保存密码同时需要原密钥和密文。密钥不可放入 `.env`、镜像、Git 或日志。宿主机管理员仍能读取密钥；本功能防止只拿到数据卷副本时直接读取密码。忘记密码或移除账号同时删除保存项，不清除浏览器资料。Docker secrets 是只读文件挂载，详见 [Docker 官方说明](https://docs.docker.com/compose/how-tos/use-secrets/)。

可在根目录创建 `.env`（不会进入镜像）调整端口或访问密码：

```dotenv
DUO_WEB_PORT=17654
DUO_WEB_PASSWORD=replace-with-your-own-password
```

密码需至少 8 个字符。设置 `DUO_WEB_PASSWORD` 时，每次启动都会重设 Web 访问密码；删除该配置后沿用数据卷中的密码。端口在容器内外保持一致，以保留严格的 Host/Origin 校验。默认仅向本机发布 Web 端口，使用 `127.0.0.1` 链接；CDP 和虚拟显示器均未向主机发布。

## 树莓派局域网访问

在部署目录的 `.env` 配置宿主监听地址及允许的真实访问来源，例如树莓派地址为 `192.168.0.200`：

```dotenv
DUO_WEB_BIND=0.0.0.0
DUO_WEB_ALLOWED_ORIGINS=http://192.168.0.200:17654
DUO_WEB_DISABLE_TUNNEL=1
```

更新镜像后执行 `docker compose up -d --no-build --wait app`；使用自动更新覆盖文件时，沿用原来的 `-f compose.yaml -f compose.autoupdate.yaml` 参数。打开 `http://192.168.0.200:17654`，用原 Web 访问密码登录。`0.0.0.0` 只用于监听，不能用作允许来源。需要其他固定入口时，以逗号分隔完整的 `http://` 或 `https://` 来源；不接受通配符、用户信息及 URL 路径。Host 与 Origin 必须相互匹配，局域网入口不能用本机启动 token 代替密码。

Docker Compose 默认设置 `DUO_WEB_DISABLE_TUNNEL=1`，不启动 Cloudflare Quick Tunnel，并拒绝注册 Quick Tunnel 地址。独立桌面 / Web 启动保持原有默认；需要在 Docker 中明确启用时设置为 `0` 并自行管理隧道进程。

由局域网、VPN 或反向代理控制入口时，可在 `.env` 设置 `DUO_WEB_TRUST_NETWORK=1`。此模式关闭 Host/Origin 白名单及写请求来源校验，任意域名或反向代理地址都可访问，无需逐个登记 `DUO_WEB_ALLOWED_ORIGINS`，Web 密码继续生效。HTTPS 反向代理可发送 `X-Forwarded-Proto: https`，服务据此设置登录 Cookie；没有该头时使用浏览器 Origin 的协议。默认值为 `0`。本 Pi 部署启用此模式，继续监听 `0.0.0.0:17654` 并停用 Quick Tunnel。

本机的 `/data` 可以继续使用原命名卷，也可以在首次部署前使用主机目录挂载。例如本 Pi 的持久目录为 `/home/fantasy/applestore_shot/data`，挂到容器 `/data`，目录归 UID/GID `1000:1000` 所有且权限为 `700`。不要把已有命名卷直接换成空目录；先保留并迁移原数据。账号配置、Apple auth/Cookie、浏览器资料、队列及 Web 密码都在 `/data`，容器重建会保留。网页访问会话保存在内存，重建后需用原 Web 密码再次登录。

## 更新、停止与排查

```sh
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail 100 app
docker compose stop
docker compose start --wait
```

停止时 Compose 先停止 Web 引擎并取消在途任务，再停止 browser；broker 退出会清理其拥有的浏览器。重启沿用原任务恢复规则，不自动补跑登录或购买。更新前请在界面确认在途购买已结束。app 健康检查验证 Web 入口与私有 broker 协议；真实 Chromium、沙箱及预览需运行以下离线验收。Docker 健康不代表真实 Apple 登录、541 恢复、库存或下单通过。

首次构建会下载 Node/Rust/Python、固定 jlesage/chromium 基础镜像、项目依赖及兼容回退浏览器，需要 Docker 能访问相应官方源。Compose 字段依据 [Docker Compose 服务文档](https://docs.docker.com/reference/compose-file/services/)。

可使用 `docker compose build --build-arg HTTP_PROXY=http://host.docker.internal:端口 --build-arg HTTPS_PROXY=http://host.docker.internal:端口 --build-arg http_proxy=http://host.docker.internal:端口 --build-arg https_proxy=http://host.docker.internal:端口` 临时给构建下载配置本机代理，再运行 `docker compose up -d --no-build --wait`。这些构建参数不会设置到应用运行环境。`NODE_IMAGE`、`RUST_IMAGE` 构建参数可指定已有的官方基础镜像或其 digest；默认分别使用 Node 24、Rust 1.88 的 Debian Bookworm 镜像，依赖版本仍由锁文件固定。

离线浏览器验收可在 PowerShell 执行 `Get-Content -Raw docker/verify_browser.py | docker compose exec -T app python -`（Linux shell 可用 `docker compose exec -T app python - < docker/verify_browser.py`）；它仅打开临时 fixture 页面，检查真实 jlesage GUI Chromium、非 root worker、沙箱、私有 Unix CDP、预览和空格输入、同时运行的资料隔离、普通浏览器及 Cookie 进程重启保存，不访问 Apple。`verify_cdp_startup.py` 另覆盖慢 WebSocket 连接和恢复标签页；`verify_persistent_browser.py` 覆盖同账号跨任务复用同一进程 / 页面 JS / Cookie、独占租约、取消后保留、迟到 token 拒绝及显式关闭保留资料。Actions 会独立重建 browser 和 app 后再次运行验收，检查两种更新顺序。HTTP 入口可用 `python docker/verify_web.py` 检查。

0.1.15 起，Actions 还执行 `verify_reused_signin_focus.py`：在同一常驻 profile 的两次独立租约中，分别以前台旧标签页、认证 iframe 和额外窗口验证本次输入页激活，并保留输入中字段被替换的拒绝路径。脚本仅在独立合成 profile 中拦截所有官网形状的请求，不访问 Apple；设置 `DUO_ASSERT_SIGNIN_FOCUS=1` 启用验收断言。

此前 amd64 本机部署的检查结果见 [本地 Docker 验收记录](VERIFICATION.md)。该记录不代表 ARM64 或后续镜像已经通过同样检查；新镜像的各架构构建与离线检查以对应 GitHub Actions 运行日志为准，均不证明真实 Apple 登录、库存或下单可用。
