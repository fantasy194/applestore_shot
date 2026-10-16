# DUO Killer · iPhone DUO 抢购辅助与 Apple 商城快速下单

面向 **iPhone DUO 抢购准备与快速下单** 的 Apple 中国在线商店辅助工具，支持 **macOS、Windows 和本地 Web**。提前配置账号、商品 SKU、购买数量和收货地址，在同一个界面完成登录检查、加入购物袋、结账确认、生成付款二维码和订单查询。

iPhone DUO launch assistant and Apple Store quick checkout tool for macOS, Windows and local Web. Built with Tauri, Rust, React and Python.

本仓库基于 [Jasper-zzq/iPhoneDuo](https://github.com/Jasper-zzq/iPhoneDuo) 的本地修改版本，包含结算提速、失败状态核对恢复及订单取消。默认购买数量为 1；自动结算支持 Duo 白色 256GB（上限 15,999 元）、512GB（上限 17,999 元）及 iPhone 18 Pro 银色 256GB（上限 9,999 元），均不附加 AppleCare+。自动结算配置和地址按账号独立保存，价格与库存以官网实际返回为准。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| iPhone DUO 发售状态 | 无需登录，手动刷新官网返回的发售提示与商品可购买状态，方便开售前查看。未知状态不代表有货。 |
| 商品与数量预设 | 内置 Duo 白色 256GB、512GB、iPhone 18 Pro 银色 256GB 与抛光布 SKU，默认数量为 1。 |
| 快速下单 | 自动送货并使用本地地址和联系方式；核对商品、数量、地址、金额与支付宝后创建订单。 |
| 临时失败恢复 | 送货、地址、支付方式失败后先读取当前结算状态，已完成则继续，仍在原步骤且允许继续时有限重试。 |
| 重复下单 | 加购与最终下单请求不自动重发；用户再次点击下单时正常请求官网，由 Apple 判断是否接受。本地待付款或结果未知记录不会阻止新任务。 |
| 多账号独立运行 | 每个账号独立保存会话、设备信任和浏览器配置；不同账号可以并行操作，验证码与确认提示标明所属账号。 |
| 登录与设备信任 | 支持设备验证码、短信验证码及官网浏览器登录，保存并复用当前账号有效的设备信任；需要重新认证时按提示完成登录。 |
| 配送与门店自提 | 可使用官网已有地址或本地快捷地址，支持选择官网返回的可取货门店。 |
| 支付与订单查询 | 确认下单后立即展示订单信息，随后加载支付宝二维码；支持查询及取消官网允许取消的未付款订单。 |

**DUO 使用流程：**提前配置账号与地址、预热登录 → 刷新发售状态 → 选择商品 → 立即下单 → 自动核对并创建订单 → 扫码支付 → 查询订单。

发售状态目前需要手动刷新，库存、价格与是否可购买以 Apple 实际返回为准；付款由本人扫码完成。

## 视频演示

[▶ 在 Bilibili 观看实际操作演示](https://www.bilibili.com/video/BV1v8HQ6FEYz/)

GitHub README 不支持内嵌 B 站播放器，点击上方链接观看完整视频。

## 支持平台

| 版本    | 使用方式                                          |
| ------- | ------------------------------------------------- |
| macOS   | 打开 `.app` 应用                                  |
| Windows | 打开便携目录中的 `.exe`                           |
| Web     | 在 macOS / Windows 本机启动服务，用默认浏览器操作 |

Web 版不是在线网站。首次登录获取浏览器上下文时，需要安装 Chrome 或 Edge。已构建版本内置 Python 和依赖；Windows 桌面版还需要 WebView2。

## 快速启动（Web）

**使用源码：**准备 Node.js 22.22+、pnpm 11.9、Rust stable、Python 3.10+（建议 3.12）。macOS 需 Xcode Command Line Tools，Windows 需 MSVC C++ Build Tools。具体安装见 [Tauri 前置要求](https://v2.tauri.app/zh-cn/start/prerequisites/)。

克隆仓库后启动：

```sh
git clone https://github.com/zengyh-dev/iPhoneDuo.git
cd iPhoneDuo
pnpm install --frozen-lockfile
pnpm dev:web
```

程序自动准备 Python 环境、编译本地服务并打开浏览器。在启动终端按 **Ctrl+C** 退出服务；仅关闭网页不会退出服务。

**使用构建好的 Web 版：**macOS 双击 `local-web-release/启动.command`，Windows 双击 `local-web-release/start.cmd`。保留整个目录，无需安装开发环境。

## 最简单的购买流程：抛光布

以下为 Web 界面操作截图，使用模拟账号和订单；示例二维码不可付款，金额与状态以实际结账结果为准。

### 1. 正常登录

添加 Apple 账号，输入密码，按提示完成设备或短信验证码。已有账号先勾选，再点击“检查并使用所选账号”。

首次验证后自动保存 Apple 下发的设备信任，下次登录优先复用；只有信任失效或 Apple 再次要求时才输入验证码。设备信任与“保存密码”独立，移除账号会同时清除本机信任；旧 Python 配置导入时也会保留有效信任。

![添加并登录账号](docs/images/purchase-01-login.png)

### 2. 选择抛光布

在工作台的“预制商品”选择 **抛光布 · MLH74FE**，数量先设为 **1**，点击“立即下单”。

![选择抛光布与数量](docs/images/purchase-02-product.png)

### 3. 按提示继续结账

选择配送 / 自提、地址等并继续。核对商品、数量、收货信息和总金额，再点击“确认创建订单”，直到显示付款二维码。结账可能包含购物袋里已有的其他商品，请核对完整清单。

![核对商品与金额并确认订单](docs/images/purchase-03-confirm.png)

### 4. 扫码支付

在实际运行中，用支付宝扫描程序显示的二维码并完成付款。**下图仅为演示，请勿扫码支付。**

![付款二维码演示，不可付款](docs/images/purchase-04-payment.png)

### 5. 查询订单

打开“订单”，点击“查询订单”；也可点击付款码下方的“我已支付，查询订单”。确认商品、数量及官网返回的状态，只有待付款订单不代表付款成功。

![查询订单并确认状态](docs/images/purchase-05-orders.png)

**移除本地账号：**在账号选择页点击账号旁的“移除”，或进入工作台 → 账号与登录 → “移除账号”，确认后清除该账号在本机的数据和保存密码。未完成登录的账号也可移除；执行中的任务需先完成或取消。官网账号和订单、其他账号、通用预设不受影响。

## 预设 SKU 与快捷地址

**保存购买预设：**左侧打开“本地配置”，选择配置范围，在“预制商品”选择已有商品，或选“自定义 SKU”后填写 SKU 和数量，点击“保存购买预设”。

**扩充内置商品列表：**在 [`src/products.ts`](src/products.ts) 的 `presetProducts` 中，照抛光布条目的格式添加 `sku`、`name`、`price`，然后重新构建。`price` 仅作参考，支付金额以官网结账结果为准；本地保存自定义 SKU 不会自动新增下拉列表条目。

**配置快捷物流地址：**在“本地配置”填写姓名、通知邮箱、手机号、省市区和详细地址，点击“保存地址预设”。结账时选择“为我送货”，再选择使用本地快捷地址；仍可选择官网已有地址。

配置范围选“通用预设”时，只供之后添加的新账号继承；选择具体账号则只修改该账号。地址预设保存在本机，不会直接修改官网地址簿。

## 构建

在目标系统及对应架构上构建，不能直接把 Mac 产物拿到 Windows 使用。先准备上述开发环境。

### macOS

```sh
pnpm install --frozen-lockfile
pnpm worker:setup
pnpm worker:build
pnpm bundle
```

输出：`src-tauri/target/release/bundle/macos/DUO Killer.app`。

当前构建未配置发行签名和公证。若首次打开提示无法验证开发者，确认来源可信后，先尝试打开一次，再到 **系统设置 → 隐私与安全性 → 仍要打开**，确认“打开”。此方法针对未认证开发者提示；不要忽略恶意软件或损坏警告。参见 [Apple 官方说明](https://support.apple.com/zh-cn/102445)。

### Windows

安装 Visual Studio C++ Build Tools，勾选“使用 C++ 的桌面开发”，并安装 WebView2；详见 [Tauri 前置要求](https://v2.tauri.app/zh-cn/start/prerequisites/)。在 PowerShell 中进入项目目录，执行：

```powershell
pnpm install --frozen-lockfile
pnpm worker:setup
pnpm worker:build
pnpm bundle
```

输出：`src-tauri/target/release/bundle/portable/DUO Killer/`。打开其中的 `DUO Killer.exe`，分享时保留整个目录及内置 worker。

### 构建 Web 版

在 macOS 或 Windows 的项目目录执行：

```sh
pnpm install --frozen-lockfile
pnpm build:web
```

输出：`local-web-release/`，复制整个目录给相同系统和架构的电脑使用。项目也提供 [桌面构建 CI](.github/workflows/build.yml) 和 [Web 构建 CI](.github/workflows/local-web.yml)。

## 隐私与免责声明

账号、Cookie、地址和支付信息保存在本机。**不要上传或分享 `auth.json`、账号 / 地址 / 支付 JSON、浏览器配置、抓包文件和本地服务启动链接。**具体检查结果见 [隐私检查记录](docs/PRIVACY.md)。

**禁止商业用途，仅供学习和交流。**本项目不是 Apple 官方产品，与 Apple 无隶属或授权关系。请仅使用自己的账号，遵守适用法律和平台规则。下单、付款会产生真实交易，请自行核对并确认；本项目不保证商品库存、价格或接口持续可用。
