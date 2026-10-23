# Docker 本地部署

在项目根目录执行（Docker Desktop 使用 Linux containers）：

```sh
docker compose up -d --build --wait
docker compose logs app
```

打开 **http://127.0.0.1:17654**，使用首次启动日志中的“首次 Web 访问密码”登录；也可直接使用日志中的 `#access=` 本机链接。已有源码服务默认使用 17653，Docker 默认使用 17654，可同时运行。

镜像内置 Rust Web 服务、编译后的 React 页面、Python 及依赖、Chrome / Chromium、Xvfb 和 Openbox。`linux/amd64`（x86-64）使用正式 Google Chrome，`linux/arm64`（ARM64）使用 Debian 的 Chromium 和 `chromium-sandbox` 包，两者均为原生架构。自动空闲预热使用 `--headless=new`，不需要 `DISPLAY`，不注册远程窗口；手动登录、手动预热和官网登录恢复仍使用 Xvfb 内的 GUI 窗口，可通过页面现有的浏览器预览与控制区域操作。两种模式沿用该账号的专用浏览器资料、Cookie 和本机 CDP，不更换认证实现。Linux 浏览器发现优先保留 Chrome/Edge，再查找 `/usr/bin/chromium`；桌面版及源码启动仍默认只监听回环地址。

空闲十分钟后，两种自动预热都会先用无头浏览器读取官网首页、购物袋和账户页，核验并同步当前会话；自提预热再继续原 `prepare_only` 流程。后台需要重新认证时停止，即使已保存密码也不提交密码或验证码；541 及冷却标记保留。手动官网恢复从当前首页菜单进入登录，详情和证据边界见 [官网恢复与空闲会话刷新](../docs/STOREFRONT-SESSION-RECOVERY.md)。无需另设无头环境变量，普通更新继续保留 `/data`，也保留 Xvfb 供人工恢复使用。

Compose 不指定固定平台，由 Docker 自动选择主机架构；本地构建同样支持这两个平台。ARM64 的包来源见 [Debian Chromium](https://packages.debian.org/bookworm/chromium) 和 [Chromium 沙箱](https://packages.debian.org/bookworm/chromium-sandbox)。两个架构均使用非 root 用户及沙箱，Compose 加载 [`chrome-seccomp.json`](chrome-seccomp.json) 允许沙箱所需的用户命名空间，未使用 privileged 或 `--no-sandbox`。该 profile 来自 [Playwright 官方 Docker 配置](https://github.com/microsoft/playwright/blob/main/utils/docker/seccomp_profile.json)，相关说明见 [Playwright Docker 文档](https://playwright.dev/docs/docker)。

## 使用 GHCR 镜像

在项目根目录的 `.env` 加入镜像名：

```dotenv
APP_IMAGE=ghcr.io/fantasy194/applestore_shot:latest
```

拉取并启动已发布镜像（保留现有 Compose 项目名及数据卷）：

```sh
docker compose pull app
docker compose up -d --no-build --wait
```

镜像为私有包时，先使用有 `read:packages` 权限的 GitHub token 执行 `docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin`，从标准输入传入 token。`latest` 用于默认分支发布，固定版本可使用发布记录中的 SHA 标签或多架构 manifest digest。镜像标签包含 `linux/amd64` 和 `linux/arm64`，无需设置 `platform` 或启用跨架构模拟；这里的 x86 指 x86-64，不支持 32 位 x86。

不设置 `APP_IMAGE` 时默认使用本地 `iphoneduo:local`，继续运行开头的 `docker compose up -d --build --wait` 即可。使用 GHCR 时更新用上面的 `pull` 和 `--no-build` 命令，避免在本地覆盖下载的镜像。

## 数据与密码

账号、每账号的 Chrome 配置、Cookie、购买队列、日志和加密设置保存在 `iphoneduo_app-data` 数据卷的 `/data` 下。重新构建、重启以及普通 `docker compose down` 均保留数据。`docker compose down -v` 会删除数据，正常更新不要使用 `-v`。

Docker 使用独立数据卷，不导入当前 Windows 服务的数据，也不复制当前浏览器资料。Windows 的 DPAPI 和系统凭据库无法直接移植到 Linux；请在容器页面重新添加账号和登录。当前系统凭据保险库仅支持 Windows/macOS，容器中登录时不要勾选“保存密码”；Chrome 资料及已验证会话仍按原实现持久化。

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

本机的 `/data` 可以继续使用原命名卷，也可以在首次部署前使用主机目录挂载。例如本 Pi 的持久目录为 `/home/fantasy/applestore_shot/data`，挂到容器 `/data`，目录归 UID/GID `1000:1000` 所有且权限为 `700`。不要把已有命名卷直接换成空目录；先保留并迁移原数据。账号配置、Apple auth/Cookie、浏览器资料、队列及 Web 密码都在 `/data`，容器重建会保留。网页访问会话保存在内存，重建后需用原 Web 密码再次登录。

## 更新、停止与排查

```sh
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail 100 app
docker compose stop
docker compose start --wait
```

停止时先通知原 Web 引擎取消在途任务，再关闭 GUI 显示；重启沿用原任务恢复规则，不会自动补跑登录或购买。更新前请在界面确认在途购买已结束。健康检查同时验证 Web 入口和 GUI 显示可用。Docker 的健康状态不代表真实 Apple 登录、541 恢复、库存或下单已经通过；这些需使用自己的账号在官网流程中验证。

首次镜像构建会下载 Node/Rust/Python 基础镜像、项目依赖及对应架构的 Google Chrome 或 Debian Chromium，需要 Docker 能访问相应官方源。Compose 字段依据 [Docker Compose 服务文档](https://docs.docker.com/reference/compose-file/services/)。

可使用 `docker compose build --build-arg HTTP_PROXY=http://host.docker.internal:端口 --build-arg HTTPS_PROXY=http://host.docker.internal:端口 --build-arg http_proxy=http://host.docker.internal:端口 --build-arg https_proxy=http://host.docker.internal:端口` 临时给构建下载配置本机代理，再运行 `docker compose up -d --no-build --wait`。这些构建参数不会设置到应用运行环境。`NODE_IMAGE`、`RUST_IMAGE` 构建参数可指定已有的官方基础镜像或其 digest；默认分别使用 Node 24、Rust 1.88 的 Debian Bookworm 镜像，依赖版本仍由锁文件固定。

离线浏览器验收可在 PowerShell 执行 `Get-Content -Raw docker/verify_browser.py | docker compose exec -T app python -`（Linux shell 可用 `docker compose exec -T app python - < docker/verify_browser.py`）；它仅打开临时本地页面，检查当前架构的真实 GUI Chrome/Chromium、非 root 用户、沙箱、CDP、现有预览和输入，以及浏览器资料重启保存，不访问 Apple。HTTP 入口可用 `python docker/verify_web.py` 检查。

此前 amd64 本机部署的检查结果见 [本地 Docker 验收记录](VERIFICATION.md)。该记录不代表 ARM64 或后续镜像已经通过同样检查；新镜像的各架构构建与离线检查以对应 GitHub Actions 运行日志为准，均不证明真实 Apple 登录、库存或下单可用。
