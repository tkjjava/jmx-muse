# Muse 协议说明

> 作者：**君莫笑** · **永恒之兰**

本文记录 muse.ai（Meta "Hatch"）的协议形态，以及为什么本项目必须驱动浏览器。

**本文只是形态记录，不包含任何注册流程、风控规避手段。**

---

## 一、为什么必须用浏览器

这是本项目最重要的一节。理解了它，就理解了整个架构。

### 1.1 传输层是加密 WebSocket

muse 的聊天不走 REST，也不走 SSE：

```
wss://hatch.metaaivm.com/v1/noise
```

这是 **Noise 协议**帧：

| 环节 | 算法 |
|---|---|
| 密钥交换 | X25519 |
| 密钥派生 | HKDF |
| 对称加密 | AES-GCM |
| 签名 | Ed25519 |

WS URL 里内嵌签名的 `auth_token` 与 `notary_token`。

### 1.2 notary token 是类 attestation 信号

`notary_token` 是服务端下发的远程验证凭据，性质接近 attestation。
它由前端生成/持有，每次连接时携带。

**这意味着什么**：

- 要纯协议实现，必须复刻 Noise 握手 + 复现 notary 签名
- 后者没有公开实现，且属于服务端信任根的范畴
- 所以**没有公开的纯协议客户端**

### 1.3 社区项目的实际选择

| 项目 | 做法 | 说明 |
|---|---|---|
| `czg86389-hub/muse2api` | 浏览器驱动 | 最活跃 |
| `www222fff/muse2api` | driver 抽象，`http` driver 返回 **501** | 印证纯协议未破解 |
| `fierzone/MUSE_MCP` | Playwright | 文档最全，含 HAR 抓包方法 |
| `Nbill27/muse-opencode` | CDP | — |

**全部退回浏览器。** 本项目的选择与社区一致，不是能力不足，是这个传输层本身就不对第三方开放。

### 1.4 本项目的做法

```
muse-bridge ──CDP──▶ 你已登录的 Chrome ──Noise WS──▶ hatch.metaaivm.com
                        ↑
                   前端在这里做加密
```

- 加密由**真实前端**完成，我们不碰
- 我们只做两件事：往输入框填字、从 DOM 读文本
- 不伪造任何签名、不复刻任何加密

---

## 二、认证与会话

### 2.1 Cookie 构成

| Cookie | 有效期 | 作用 |
|---|---|---|
| `hatch_sess` | ~30 天 | 主会话 |
| `hatch_gw` | — | 网关会话 |
| `hatch_vml` | **~48 小时** | 最大痛点 |
| `hatch_native_auth_device` | — | 设备绑定凭证 |

### 2.2 相关端点

| 端点 | 作用 |
|---|---|
| `POST /api/auth/check` | 探测登录态，返回 `{ok, access_token, viewer_id}` |
| `GET /api/session` | 分配云端 VM，返回 `wss://<vm_id>.metaaivm.com/` |
| `POST /api/hatch/vm/wake` | 唤醒休眠的 VM |

### 2.3 续期机制

`hatch_vml` 只有 48 小时。续期方式：

1. 定期调 `GET /api/session` → 服务端下发新的 `Set-Cookie`
2. 调 `POST /api/hatch/vm/wake` 保持 VM 活跃

**关键**：续期依赖的是浏览器当前 cookie 与前端签名。
本项目通过**页面内 `fetch`** 触发（而非外部构造请求），这样浏览器会自动带上一切。

代码位置：`driver.py` 的 `touch_session()` 与 `wake_vm()`。

### 2.4 错误语义

| 码 | 含义 | 本项目的处理 |
|---|---|---|
| 401 | cookie 失效 | 标 `session_expired`，需重新登录 |
| 403 | 地区封锁 / 出口 IP 不一致 / 权限 | 标 `blocked`，人工介入 |
| 429 | 限流 | 标 `rate_limited`，退避 |

> **403 的常见原因**：部署 muse-bridge 的机器出口 IP 与登录 muse.ai 的浏览器不一致。
> 所以 Chrome 与 muse-bridge **必须在同一台机器**上。

---

## 三、模型

muse 是**单一 agent**，代号 `muse-spark-1.3`。

- 没有 `/models` API
- 客户端**不能选模型**
- 所有常见模型名（`gpt-4o`、`claude-opus-4` 等）在 `pool.py` 里做别名映射，
  全部路由到同一个上游

这样现有客户端只要填任何熟悉的名字就能用，不需要改配置。

### 原生能力

除对话外，muse 还支持：

- 2K 生图
- 5/10 秒文生视频 / 图生视频
- 媒体回复为 `muse.ai/files/...` 直链（约 2 天过期）

本项目当前只实现了对话。生成能力需要异步任务模型（POST 创建 + task_id 轮询），
属于后续扩展方向。

---

## 四、DOM 读取策略

muse 的正文只能从页面读。这里有两层问题：**选择器稳定性** 和 **如何判定回答结束**。

### 4.1 选择器表

所有选择器集中在 `selectors.py`，且**每条都是候选列表**：

```python
ASSISTANT_MESSAGES = (
    '[data-testid="assistant-message"]',
    '[data-role="assistant"]',
    'div[class*="assistant"]',
    'div[class*="message"][class*="ai"]',
)
```

按顺序尝试，第一个命中的胜出。这样前端换了类名，只要有一个旧选择器还在就能继续工作。

前端改版时**只需改这一个文件**。

### 4.2 判定"生成结束"

没有可靠的前端事件可用，所以用两个信号组合：

**信号 1：文本单调扩展 + 静默窗口**

```
每 250ms 取一次最后一条助手消息的文本
如果新文本以旧文本为前缀 → 取新增部分作为 delta
如果文本在 quiet_ms（默认 600ms）内没有增长 → 认为可能结束
```

**信号 2：流式标记消失**

```
STREAMING_MARKERS 里任一元素存在 → 仍在生成
```

两个信号都满足（文本静默 **且** 流式标记消失）才结束。

### 4.3 为什么不只看 DOM 变化事件

因为：
- `MutationObserver` 会在流式输出时高频触发，需要自己节流
- 无法从事件本身判断"结束了"还是"在思考"
- 轮询 + 静默窗口更简单，且对前端框架差异不敏感

250ms 轮询的开销在可接受范围（一次 `inner_text()` 调用）。

### 4.4 非单调文本的处理

如果新文本**不以**旧文本开头，说明页面重绘或消息被替换了。
此时不能简单取差值（会丢内容），代码的选择是**整体重发**：

```python
if current.startswith(last):
    delta = current[len(last):]
elif current != last:
    delta = current        # 不单调：整体重发
```

代价是客户端可能收到重复内容——但比丢内容好。

---

## 五、状态机

```
        ┌──────────────┐
attach ─│  unverified  │
        └──────┬───────┘
               │ 检测到已登录
               ▼
        ┌──────────────┐   VM 休眠   ┌──────────────┐
        │    ready     │◀───────────▶│ vm_sleeping  │
        └──────┬───────┘             └──────────────┘
               │ hatch_vml >42h
               ▼
        ┌──────────────┐
        │ vml_expiring │  （仍可派单，心跳续期）
        └──────────────┘

  浏览器连不上 ──▶ browser_unavailable
  浏览器里未登录 ──▶ session_expired
  上游 403     ──▶ blocked                    （终态）
  上游 401     ──▶ session_expired
```

### 派单闸门

与 hark-bridge 不同，muse **读不到会话到期时间**（前端不暴露），
因此只靠探针新鲜度判定：

```
状态 ∈ {ready, vm_sleeping, vml_expiring}
且 last_probe_ok_at 在 15 分钟内
```

代码位置：`state.py` 的 `is_dispatchable()`。

### 为什么 VM_SLEEPING 也可派单

因为派单流程里会先调 `wake_vm()`。把它排除在派单池外没意义——
唤醒本身就是使用它的一部分。

---

## 六、浏览器连接

### 为什么复用而不是自己启动

| 做法 | 问题 |
|---|---|
| 每次 `browser.launch()` | 新 profile 没登录态；频繁启动易被识别 |
| 复用已开的 Chrome（本项目） | 登录态、扩展、指纹都是真实的 |

本项目用 `connect_over_cdp()` 连接你手动启动的 Chrome：

```python
browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
```

**它不会关闭你的 Chrome**——`close()` 只断开连接。

### 为什么默认有头（headless=False）

无头 Chrome 的指纹与有头不同，muse 更容易识别。
默认有头，是让浏览器行为更接近真实使用。

> 注意：这不是"反检测"——我们不伪造任何指纹。只是不去主动选择更容易被拒的模式。

---

## 七、协议变更时改哪里

| 上游变了 | 改哪 |
|---|---|
| 页面结构 / 类名 | `selectors.py`（唯一需要改的地方） |
| 续期端点 | `driver.py` 的 `touch_session()` |
| VM 唤醒端点 | `driver.py` 的 `wake_vm()` |
| 新增错误码 | `app.py` 的状态落库逻辑 |
| 模型代号变了 | `pool.py` 的 `UPSTREAM_MODEL` |

---

## 八、明确不做的事

1. **不做注册**。muse 需要 Meta 账号 OAuth，本项目完全不介入。
2. **不复刻 Noise 加密 / 不伪造 notary token**。那是绕过服务端信任根。
3. **不做指纹伪装**。不修改 UA、不注入 stealth 脚本。
4. **不共享账号**。

理由同上一个项目：这些会让工具从"协议适配"变成"绕过服务方控制"。

---

作者：**君莫笑**
**永恒之兰**
