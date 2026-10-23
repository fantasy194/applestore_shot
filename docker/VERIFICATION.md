# 本地 Docker 验收记录

日期：2026-10-23（Asia/Hong_Kong）。环境：Windows Docker Desktop、Linux containers、amd64。

## 已部署

- 镜像：`iphoneduo:local`。
- 镜像 ID：`sha256:ddc851f50cf9d65f645d428373864bb0104d57ac56f0410a145599cecc68934d`。
- Compose 服务：`iphoneduo-app-1`，健康检查通过。
- 入口：`http://127.0.0.1:17654`，仅向本机发布。
- 数据卷：`iphoneduo_app-data`，挂载到 `/data`；没有导入 Windows 账号或浏览器资料。
- 运行用户：`duo`，Chrome `156.0.8078.47`；镜像运行环境不含构建代理变量。

本次使用已缓存的官方 Node 24、Rust 1.95 基础镜像 digest 构建；Dockerfile 默认使用 Rust 1.88。Node digest 为 `sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e`，Rust digest 为 `sha256:adab7941580c74513aa3347f2d2a1f975498280743d29ec62978ba12e3540d3a`。构建依赖下载临时使用本机代理，运行时没有配置该代理。

## 通过的检查

| 检查 | 结果与证据 |
| --- | --- |
| Compose 配置、Python 语法与 shell 换行 | `docker compose config --quiet`、Python AST 解析通过；worker 启动脚本为 LF。 |
| 前后端构建 | TypeScript 检查、Vite local-web 构建、Linux Rust release 构建通过。 |
| Rust 回归 | Windows Rust 1.88 执行 `cargo test --locked --manifest-path local-web/Cargo.toml -- --test-threads=1`，90 项通过。 |
| 容器内 worker 回归 | Linux 路径发现、桌面协议、浏览器登录与远程控制共 36 项通过；日志在 `artifacts/docker-worker-tests.log`。 |
| 发布的 HTTP 入口 | 页面和 PWA manifest 可读；密码登录、Cookie 注销、Bearer API 认证通过；错误 Host/Origin 被拒绝，私有文件返回 404。见 `artifacts/docker-web.json`。 |
| 实际 Chrome GUI | 正式 Chrome 以 GUI 模式启动，沙箱开启；`navigator.webdriver` 为 false，UA 不含 HeadlessChrome。CDP、现有 broker 预览、帧更新、鼠标与文本输入通过。见 `artifacts/docker-browser.json`。 |
| 浏览器资料 | 临时本地页面的 Cookie 在关闭并重新启动 Chrome 后保留，测试资料随后清理。 |
| 正式 React 页面 | 实际容器 Chrome 打开生产页面并显示首次添加账号表单，无 pageerror；截图在 `artifacts/docker-web.png`。 |
| 容器重建与数据保留 | 确认账号、任务均为零后停止并强制重建容器；旧容器退出码 0，新容器健康。数据卷探针、访问密码配置 SHA256 保持一致，探针已删除；重建后再次通过 HTTP 和密码登录检查。见 `artifacts/docker-persistence.json`。 |

浏览器检查仅使用临时本地 HTTP 页面和空账号工作空间。没有执行真实 Apple 登录、二次验证、541 恢复、库存、下单或付款验证。系统凭据保险库的 Linux 限制及测试方法见 [部署说明](README.md)。
