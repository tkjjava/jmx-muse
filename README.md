# muse-bridge

<p align="center">
  <a href="https://github.com/tkjjava/jmx-muse/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/tkjjava/jmx-muse/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <img alt="tests" src="https://img.shields.io/badge/tests-51%20passed-brightgreen">
  <img alt="version" src="https://img.shields.io/badge/version-0.1.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="transport" src="https://img.shields.io/badge/transport-CDP%20%2B%20Noise%20WS-orange">
  <img alt="no registration" src="https://img.shields.io/badge/registration-none-critical">
</p>

<p align="center">
  <b>把 muse.ai 暴露成标准 OpenAI 兼容接口</b><br>
  <sub>作者：君莫笑 · 永恒之兰</sub>
</p>

<p align="center">
  <b>中文文档</b> · <a href="README.en.md">English</a>
</p>

---

把 muse.ai（Meta "Hatch"）暴露成标准 OpenAI 兼容接口。**不含注册环节**——会话由你自己在浏览器里登录后导入。

> ⚠️ 本项目**必须**配合一个运行中且已登录的 Chrome。
> muse 的传输层是加密 Noise WebSocket，无公开纯协议实现。

## 特性

| | |
|---|---|
| **OpenAI 兼容** | `/v1/chat/completions`（含 SSE 流式）、`/v1/models` |
| **CDP 驱动** | 复用你已登录的 Chrome，只断开不关闭 |
| **选择器集中** | 前端改版只改 `selectors.py` 一个文件 |
| **并发安全** | 账号级互斥锁，覆盖整个请求含流式输出 |
| **会话续期** | 页面内 fetch 续 `hatch_vml`（48h）+ VM 唤醒 |
| **加密存储** | SQLite + Fernet，密钥缺失直接抛错 |
| **自检工具** | `python -m muse_bridge.doctor` 五项逐一验证 |
| **零注册代码** | 不介入 Meta OAuth，不做任何登录自动化 |
| **无限画布** | `/canvas` 自研画布：零构建 ES modules、暗色玻璃 UI、提示词直接出图、本地/SQLite 双存、只读分享与链接管理（复制/轮换/撤销，二次确认） |

```
OpenAI 客户端 ──▶ muse-bridge :8788/v1 ──▶ 纯协议 Noise WS  ──▶ hatch.metaaivm.com
                                     └──▶ CDP 驱动真实 Chrome ──▶ muse.ai（兜底/额度）
```

## 双通道：纯协议优先，浏览器兜底

muse 的传输层不是 REST，也不是 SSE，而是**加密的 Noise WebSocket**：

```
wss://hatch.metaaivm.com/v1/noise
Noise_XX_25519_AESGCM_SHA256 + 内层 HTTP-over-Noise（418 条方法白名单）
```

WS URL 里内嵌签名的 `auth_token` / `notary_token`。社区项目（muse2api 等）大多退回
浏览器驱动；本仓把协议层**从零逆了出来并真机验收**（`docs/PROTOCOL.md` §1.6）：

1. 用导入的 cookie 续签 `/api/session` → 换 `/api/hatch/token` → Noise 握手；
2. 内层用 `verb/path/headers/body/end_body` 帧实现 HTTP 语义，覆盖
   `/chat/*`、`/fs/read|write|stat|rename|delete`、`/spaces/v2/*` 等；
3. 生成链路：`/chat/stream` → 订阅流 `delta.presentation` →
   `sandbox://workspace/imagine_media/…` → `/fs/read` 分页取件 → 成品字节。

浏览器驱动仍然保留，但只剩三类用途：**额度面板**（上游没有额度 REST）、
**协议生成失败（可重试类）的一次回退**、以及**首次登录 / attach / 导出 cookie**
这类一次性引导。会话续期（`refresh` / `keepalive` / `wake`）在导入 cookie 后
全部走纯 HTTP，不再需要浏览器窗口在跑。两条通道由 `MUSE_PROTOCOL_DRIVER`
开关与账号是否导入 cookie 决定。

按功能看（2026-11-11 11:5x 实测口径）：

| 功能 | 走哪条 | 需要浏览器？ |
|---|---|---|
| `/v1/chat/completions`（流式/非流式） | Noise WS | 否（协议失败直接 502，不自动回退） |
| `/v1/images/tasks`、视频、参考图重绘 | Noise WS | 正常路径否；仅"可重试类"失败回退一次浏览器 |
| 引用素材上传（`/v1/files` → 生成） | 协议 `/fs/write` 或回显 URL | 否 |
| `refresh` / `keepalive` / `wake` | 外层 HTTP（`hatch_sess`） | 否（有 cookie 时） |
| muse Spaces / 灵感卡 / `muse/export` | 协议 `/spaces/v2/*` | 代码路径否；**成功路径未真机验收**（账号 0 Space） |
| 额度百分比 | 浏览器 UI 文本 | **是** |
| 账号接入 | CDP 读登录态 + 取 cookie | **是**（一次性） |

## 边界（硬性）

- **不提供注册功能**。muse 需要 Meta 账号 OAuth 登录，本项目不介入任何注册/登录流程。
- **不规避 Cloudflare / 风控**。不做指纹伪装、不做 notary token 伪造、不做 challenge 求解。
- **不共享账号**。设计用途是单个使用者管理自己的账号。
- **不伪造 token**。协议层只做握手与帧编解码，cookie/签名仍由你自己登录后获得。

## 安装

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

需要 Python 3.11+。

> 如果不装 playwright，服务仍可启动，但 muse 上游不可用（会明确报错）。

## 配置

```bash
# 生成加密密钥
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 设置
export MUSE_BRIDGE_KEY="<上面生成的密钥>"
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `MUSE_BRIDGE_KEY` | 必填 | Fernet 密钥 |
| `MUSE_BRIDGE_DB` | `./data/muse.db` | 账号库 |
| `MUSE_BRIDGE_PORT` | `8788` | 端口 |
| `MUSE_BRIDGE_HOST` | `127.0.0.1` | 监听地址 |
| `MUSE_BRIDGE_API_KEYS` | 空 | 客户端密钥，逗号分隔 |
| `MUSE_CDP_URL` | `http://127.0.0.1:9222` | Chrome 调试端口 |
| `MUSE_HEADLESS` | `false` | 是否无头。默认有头——muse 更容易检测无头 |
| `MUSE_SITE` | `https://muse.ai` | 站点地址 |
| `MUSE_QUIET_MS` | `600` | 判定回答结束的静默窗口（毫秒） |
| `MUSE_REPLY_TIMEOUT` | `180` | 等回复超时（秒） |

## 快速开始

### 1. 启动带调试端口的 Chrome

```powershell
# Windows
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\muse-profile"

# Linux / macOS
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/muse-profile"
```

### 2. 在这个 Chrome 里登录 muse.ai

手动登录一次。之后会话保存在该 profile 里。

### 3. 启动 muse-bridge

```bash
# 先自检：五项前置条件逐个验证
python -m muse_bridge.doctor

# 再启动
python -m muse_bridge
```

### 4. 导入会话

打开 <http://127.0.0.1:8788/console>，或直接调：

```bash
curl -X POST http://127.0.0.1:8788/admin/attach \
  -H "Content-Type: application/json" \
  -d '{"account": "me-main", "label": "主号"}'
```

它会连接 CDP、检查 muse.ai 是否已登录、把会话状态记入池。

### 5. 调用

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"muse-1","messages":[{"role":"user","content":"你好"}]}'
```

## 端点

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容，支持流式 |
| `GET /v1/models` | 模型别名（全部指向 `muse-spark-1.3`） |
| `POST /v1/images/generations` | 文生图（`async_mode=true` 走任务模型） |
| `POST /v1/images/edits` | 图生图/编辑（**JSON 变体**，multipart 会拿到点名原因的 400） |
| `POST /v1/videos`、`POST /v1/videos/generations` | 文生视频，**总是异步** |
| `GET /v1/images/tasks/{id}`、`GET /v1/videos/{id}` | 轮询任务 |
| `GET /v1/media/{token}`、`DELETE /v1/media/{token}` | 成品下载（Range）/ 删除，都要 key |
| `GET /v1/fal/models` 等 `/v1/fal/*` | fal.ai 队列兼容（提交 → 轮询 → 取结果） |
| `GET /healthz` | 健康检查 |
| `GET /console` | 授权台 |
| `POST /admin/attach` | 连接 CDP 并把账号入库 |
| `GET /admin/accounts` | 账号池状态（脱敏） |
| `POST /admin/accounts/{a}/cookies` | 导入 cookie（纯 HTTP 续期用；只回 cookie 名字） |
| `POST /admin/accounts/{a}/refresh`、`/wake`、`/keepalive` | 续期 / 唤醒 / 两者一次跑完 |
| `GET /admin/accounts/{a}/quota` | 读额度面板（必须浏览器） |
| `GET /admin/tasks`、`/admin/tasks/{id}`、`POST /admin/tasks/clear` | 任务运维 |
| `GET /canvas` | 无限画布前端（静态页，同源挂载；`/canvas/?share=<token>` 为只读分享页） |
| `GET/POST /canvas/api/spaces`、`GET/PUT/DELETE /canvas/api/spaces/{id}` | 画布空间 CRUD（`/canvas/api/health` 免密探活） |
| `GET/POST /canvas/api/spaces/{id}/share`、`DELETE /canvas/api/shares/{token}` | 只读分享管理（Bearer；同空间轮换 token） |
| `GET /canvas/api/shares/{token}` | 只读分享读取（**免鉴权** capability URL，无写入口） |
| `GET /canvas/api/media/{token}` | 成品同源代理（给画布 `<img>` 用；Range→206） |
| `GET /canvas/api/muse/spaces`、`POST /canvas/api/muse/import`、`GET /canvas/api/muse/export/{slug}` | muse 只读 Space 适配：列表（§2.1 归一化）、导入预览卡（不写 muse）、export.html **仅附件下载**（octet-stream+nosniff+CSP sandbox，禁同源 inline） |
| `GET /canvas/api/muse/idea-cards`、`.../idea-cards/{id}`、`.../idea-cards/media/{handle}` | muse Explore 灵感卡只读列表/详情/同源图标代理（栅格 mime inline，SVG 强制附件） |

完整的请求/响应形状、curl 示例见 [docs/USAGE.md](docs/USAGE.md) 第十章。

## 已知限制

- **双通道：纯协议优先，浏览器兜底**（0.4.1+）。`MUSE_PROTOCOL_DRIVER=1` 且账号已导入
  cookie 时，聊天与**生成**都走 Noise 协议（WS 经 `MUSE_PROTOCOL_PROXY` 指向本机代理到
  `hatch.metaaivm.com`），`/healthz` 的 `chat_driver` 报 `protocol`。协议侧生成失败
  （仅"可换号重试"那一类）会自动用 `generation-browser` 回退一次浏览器。
  不开这个开关、或账号没有 cookie 时仍是纯浏览器驱动——此时**必须有一个运行中的
  Chrome**，这不是缺陷，是 transport 决定的。
  真机验收（2026-11-11）：把 `MUSE_CDP_URL` 指到无人监听的端口（9），
  `POST /v1/images/tasks` 仍 26 秒 completed，取回 `image/webp` 685,900 B——
  证明这次生成全程没碰浏览器。证据与复现步骤见
  [`docs/PROTOCOL.md`](docs/PROTOCOL.md) §1.6.1。
- **图片交付格式**：URL 端点交付原始 WebP（`X-Muse-Media-Mime` 标明），
  `response_format=b64_json` 会先转真 PNG 再 base64——OpenAI 该字段没有 mime，
  严格客户端按 PNG 解码，原样塞 WebP 会报 "not a PNG file"（2026-11-11 12:3x，
  `media.png_deliverable`；Pillow 缺席时原样返回，不新增硬依赖）。
- **续期/唤醒也已经是纯 HTTP**（导入 cookie 后）。2026-11-11 11:5x 真机：
  账号因探针过期变成 `accounts_dispatchable=0`，调 `POST /admin/accounts/muse1/refresh`
  返回 `{"via":"http","refreshed":true,"vm_state":"assigned"}`，紧接着
  `/healthz` 回到 `accounts_dispatchable=1`，全程没起浏览器；随后
  `/v1/chat/completions` 17.1s 返回、响应头 `X-Muse-Driver: protocol`。
  此前 `refresh` 是唯一无条件走 CDP 的运维端点，浏览器没开就 503，已修。
- **额度端点仍必须浏览器**。muse 没有额度 REST，读面板只能点 UI。
- **模型不可选**。muse 是单一 agent，代号 `muse-spark-1.3`，服务端决定。所有模型别名都路由到它。
- **DOM 变更是浏览器通道的主要失效点**。前端改版会导致选择器失效，需要按 [`docs/PROTOCOL.md`](docs/PROTOCOL.md) 的选择器表更新。
- **`hatch_vml` 约 48 小时过期**。心跳协程会尝试续期，但上游策略可能变化。
- **403 常见于出口 IP 与登录浏览器不一致**。所以 Chrome 与 muse-bridge 应在同一台机器。
- **并发必须克制**。同一时刻只应有一个对话在生成。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | 从零开始，含 CDP 排查 |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Noise WS、DOM 选择器、续期机制 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献规范 |

## 许可

MIT © 2026 **tkjjava (君莫笑)**

**永恒之兰**

## 免责声明

本项目与 muse.ai / Meta 无关，未获其授权或认可。它通过浏览器自动化访问第三方服务，
使用者需自行确认并遵守该服务的服务条款及适用法律。仅供互操作与学习用途。

## 思考过程透传（0.3.0+，默认关闭）

启动前设置环境变量 `MUSE_REASONING=1` 后，若页面确实把思考渲染成独立容器，桥会以
`delta.reasoning_content` 单独把思考增量发给客户端，正文仍走 `delta.content`；思考
计入用量估算。**关闭时（默认）思考容器根本不被查询，输出与没有此功能时逐字节一致。**
relay 等网关按字节透传该字段，无需改动。

思考容器的 DOM 选择器默认是候选列表（命中即用，命中不到就当没有思考——它们不是对
muse 真实 DOM 的确认）。页面结构变化时用 `MUSE_REASONING_SELECTORS` 覆盖，逗号分隔：

```
MUSE_REASONING_SELECTORS=[data-testid="reasoning-content"],.thinking-body
```

非流式响应同样带思考：`message.reasoning_content` 仅有值时出现（OpenAI/DeepSeek 语义）。

---

## 管理端点鉴权（0.4.0：调用面与管理面分钥匙）

两块密钥，职责不同：

| 环境变量 | 覆盖范围 | 说明 |
|---|---|---|
| `MUSE_BRIDGE_API_KEYS` | `/v1/*` | 客户端调用密钥（生成、轮询、下载媒体） |
| `MUSE_BRIDGE_ADMIN_KEYS` | `/admin/*` | 管理密钥：attach / 删号 / 续期 / 唤醒 / 清任务 / 读额度 / 导入 cookie |

**为什么必须分开**：上游多个中转把 `/v1` 与 `/admin` 共用一把 key，任何拿到普通调用
key 的客户端都能删号、换配置——权限坍缩（本轮漏洞表 V-01，见
[docs/VULN_HARVEST.md](docs/VULN_HARVEST.md)）。

- 设了 `MUSE_BRIDGE_ADMIN_KEYS`：`/admin/*` **只**认这把，普通调用 key 进不来；
  调用面也**不**接受管理 key，两个作用域互不越界。
- 没设：`/admin/*` 退回认 `MUSE_BRIDGE_API_KEYS`（不破坏既有部署），但 `/healthz`
  的 `admin_key_separated=false` 会把这条降级如实标出来。
- 两者都为空：与 0.3.x 一样直接放行，只适合本机自用（`MUSE_BRIDGE_HOST` 非回环
  时启动会打警告）。

控制台 `/console` 的"API 密钥"输入框（记在 localStorage）会同时用于调用与管理操作；
生产部署建议给管理面单独一把，并只在需要的机器上配置它。

`MUSE_PUBLIC_BASE`（如 `https://api.example.com`）让任务与同步响应里的媒体 `url`
变成可直接下载的绝对地址；留空则保持相对路径 `/v1/media/{token}`。

冒烟脚本 `python -m muse_bridge.smoke` 同样认这些密钥：配了
`MUSE_BRIDGE_API_KEYS` 就自动带上第一把，拿到 401 会明确告诉你该配哪把密钥，而不是
丢一个 401 的 traceback。

数值型环境变量写错也会直接告诉你哪个变量不对，而不再抛 traceback——空值按"没配置"
处理，超时/冷却必须大于 0，端口必须在 1..65535。

```
curl -X POST http://127.0.0.1:8788/admin/attach \
  -H "Authorization: Bearer <MUSE_BRIDGE_ADMIN_KEYS 里的密钥>" \
  -H "Content-Type: application/json" -d '{"account":"主号","cdp_url":"http://127.0.0.1:9222"}'
```
