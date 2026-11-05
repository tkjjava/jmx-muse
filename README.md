# muse-bridge

> 作者：**君莫笑**
>
> **永恒之兰**
>
> MIT License

把 muse.ai（Meta "Hatch"）暴露成标准 OpenAI 兼容接口。**不含注册环节**——会话由你自己在浏览器里登录后导入。

```
OpenAI 客户端 ──▶ muse-bridge  :8788/v1  ──▶ CDP 驱动真实 Chrome ──▶ muse.ai
```

## 为什么必须驱动浏览器

muse 的传输层不是 REST，也不是 SSE，而是**加密的 Noise WebSocket**：

```
wss://hatch.metaaivm.com/v1/noise
X25519 + HKDF + AES-GCM + Ed25519
```

WS URL 里内嵌签名的 `auth_token` / `notary_token`。**至今没有任何公开的纯协议实现**——
社区项目（muse2api 等）全部退回浏览器驱动，其 `http` driver 至今返回 501。

因此本项目采取的路径是：

1. 用 CDP 连接一个真实的 Chrome（由你手动登录 muse.ai）
2. Cookie 与前端加密逻辑由浏览器自己完成
3. 监听页面 DOM 中助手消息文本的稳定增长
4. 转成 OpenAI SSE 输出

**这是当前唯一可行的方式**，不是偷懒。

## 边界（硬性）

- **不提供注册功能**。muse 需要 Meta 账号 OAuth 登录，本项目不介入任何注册/登录流程。
- **不规避 Cloudflare / 风控**。不做指纹伪装、不做 notary token 伪造、不做 challenge 求解。
- **不共享账号**。设计用途是单个使用者管理自己的账号。
- **不复制浏览器加密逻辑**。Noise 层由真实前端完成，本项目不重实现。

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
| `GET /healthz` | 健康检查 |
| `GET /console` | 授权台 |
| `POST /admin/attach` | 连接 CDP 并把账号入库 |
| `GET /admin/accounts` | 账号池状态（脱敏） |

## 已知限制

- **必须有一个运行中的 Chrome**。浏览器关掉就不可用。这不是缺陷，是 transport 决定的。
- **模型不可选**。muse 是单一 agent，代号 `muse-spark-1.3`，服务端决定。所有模型别名都路由到它。
- **DOM 变更是主要失效点**。前端改版会导致选择器失效，需要按 [`docs/PROTOCOL.md`](docs/PROTOCOL.md) 的选择器表更新。
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

MIT © 2026 **君莫笑**

**永恒之兰**

## 免责声明

本项目与 muse.ai / Meta 无关，未获其授权或认可。它通过浏览器自动化访问第三方服务，
使用者需自行确认并遵守该服务的服务条款及适用法律。仅供互操作与学习用途。
