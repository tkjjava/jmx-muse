# Muse 协议说明

> 作者：**君莫笑** · **永恒之兰**

本文记录 muse.ai（Meta "Hatch"）的协议形态，以及为什么本项目必须驱动浏览器。

**本文只是形态记录，不包含任何注册流程、风控规避手段。**
**行号提示**：本仓库正被多人并行改动，文中 `muse_bridge/*.py` 的行号以 2026-11-11 00:5x 快照为准，漂移时以函数名为准查找。

---

## 一、为什么必须用浏览器

> **0.4.0 纠正（见 §八）**：本节结论「必须用浏览器」在 0.1–0.3 期间成立，
> 现在已被推翻——Noise 握手与内层 HTTP-over-Noise 帧已由三家独立实现证明可以纯协议复现，
> 本仓库也已落地 `noise.py` / `proto.py` / `protocol_client.py` / `ws_fallback.py`。
> **2026-11-11 再推进两步：纯协议文本链路真机打通（§1.6），纯协议图像生成真机打通
> （§1.6.1）——生成不再必须浏览器。** 但默认执行路径仍是浏览器（
> `MUSE_PROTOCOL_DRIVER` 默认关）；打开开关且账号导入 cookie 后，聊天与生成都走协议，
> 协议生成失败（可重试类）回退一次浏览器。
> 浏览器通道保留给：额度面板、协议失败兜底、协议未覆盖的 UI 行为。
> Confidential VM 账号只能退回「明确不做」而不是退回浏览器（§8.7）。
> 阅读顺序建议：§二（会话与 cookie，两条路径共用）→ §八（纯协议）→ §四/§五/§六（浏览器路径）。

这是 0.3 之前最重要的一节。理解了它，就理解了浏览器路径为什么长这样。

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

WS URL 的形态（host/path 已由 2026-11-11 真机钉死为**常量**，见下节「端点来源」与 §1.6）：

```
wss://hatch.metaaivm.com/v1/noise
  ?vm_id=<session.vm_id>
  &auth_token=<EdDSA JWT>
  &app_id=hatch-web
  &request_id=<uuid4，每次连接新值>
  &notary_token=<可选，缺省则不带该参数>
```

| 组成 | 来源 | 证据 |
|---|---|---|
| host `hatch.metaaivm.com` / path `/v1/noise` / `app_id=hatch-web` | 前端常量 `HATCH_SHARED_LB_HOST` / `NOISE_WS_PATH` / `HATCH_APP_ID` —— **2026-11-11 真机已证实这就是能握手的形态**（§1.6） | 真实 chunk `24vdgjhlw22uj.js`；`work/muse-re/HANDOFF_re-wire.md` §1（wire-protocol.md:8-19 + PHASE_0_REPORT.md:71-76）；真机 TLS+WS 101 + Noise XX 成功 |
| 本仓库实现 | 拼 URL 与建连 | `muse_bridge/protocol_client.py` 的 `ws_url_from_endpoint()`：默认恒取常量 host/path；由 `endpoint_url` 派生的路径是**默认关闭**的备选（`use_endpoint_host=True`）。`client.endpoint_url` 仍记录服务端下发值（它是 `/api/hatch/token` 的 `vmAddress`）。query 含 `vm_id/auth_token/app_id/request_id`，`notary_token` 有值才带 |

`request_id` 不是可选装饰：排障时它是把服务端日志与本地一次连接对应上的唯一线索，缺了就只能靠时间窗猜。

#### 端点来源（**已被 2026-11-11 真机推翻：LB 才是 WS host**）

活网实测 `GET https://muse.ai/api/session?vm_state=live` 的返回：

```json
{"status": "assigned", "vm_state": "RUNNING",
 "endpoint_url": "wss://c2edbb14-91c9-486e-ad2e-a70d0ee2ade0.metaaivm.com/"}
```

`<vm_id>` 是那次会话的 VM UUID（`c2edbb14-…` 是当次实测值，不是每次都一样）。
前一轮据此得出了"**每台 VM 一个子域、endpoint_url 才是 WS 目标**"的结论，
并把 `hatch.metaaivm.com` 降级为回退。**那个结论是错的**——同一轮真机里把
Noise 链路真正打通的是常量入口，而 `wss://<vm_id>.metaaivm.com/` 作 WS 目标时
TLS 直接失败：

```
ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure
```

（同一本机 mihomo 代理、同一份 token，`hatch.metaaivm.com` 正常返回 101。）
所以现在反过来：

- **常量 `hatch.metaaivm.com` + `/v1/noise` 是首选目标**：
  `ws_url_from_endpoint()` 默认完全不看 `endpoint_url`，host/path 恒取常量。
- **`endpoint_url` 只作记录与换 token 的参数**：它仍是 `/api/hatch/token` 的
  `vmAddress`，`client.endpoint_url` 照样保存；缺它仍然判"不可连"（换不到 token
  就没有 `auth_token`）。但它**不再参与 WS host 派生**。
- **endpoint 派生保留成"默认关闭的备选"**：`ws_url_from_endpoint(..., use_endpoint_host=True)`
  默认 `False`，注释写明"真机 TLS 不通，只作备选/未来 LB 切换用"。保留它是为了
  将来 LB 形态变了能开一次开关验证，而不是把"只验证过一种来源"写死进代码结构。

**前提**：本链路只有走**本机代理**时才通（直连被墙：本机 DNS / TCP 443 到不了
`.metaaivm.com`，WS 直连会 `ConnectionResetError`）。mihomo 固定用
`127.0.0.1:18000`；2026-11-11 11:0x 该隧道上游节点全挂（controller delay 全 503）
时改用 FlClash `127.0.0.1:7890` 跑通（视频真机验收即走 7890，§1.6.1c）。完整链路
证据见 §1.6。

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

### 1.5 2026-11-11 真机复核：WS 的**创建侧**观察（未定论）

> 素材：真账号 + 已登录的 muse.ai 页面。只读 CDP 事件与页面内 Hook，不改前端、
> 不注入绕过脚本、不导出 cookie。`/api/session` 的响应形状与 DNS/TCP 观察已在
> §1.1「端点来源」记录，本节不重复；这里只写**WS 是谁建的**这一类问题。
> **未定论不是结论。**

**实测：CDP 看不到 WS 创建事件，但密文帧一直在流**

| 观察 | 结果 |
|---|---|
| `Network.webSocketCreated` | 页面刷新**前**、刷新**后**都没有事件 |
| 主线程 Hook `window.WebSocket` 构造器后刷新 | `__wsLog` 计数为 **0**（主线程上没有 `new WebSocket`） |
| `Network.webSocketFrameSent` / `webSocketFrameReceived` | **持续出现**，内容是 base64 密文帧 |

密文帧长度抽样：44 / 108 / 212 / 248 / 260 / 264 / 300 / 648 / 4532 …
（**疑似 Noise 帧**，未解密、未归类）。长度分布只说明"有分片、长短帧交替"，
不能据此断言协议细节。

补一句背景：同一时点浏览器里的聊天是正常的（能发能收）。于是「本机裸 TCP 不通」
（见 §1.1）与「页面通道在工作」同时成立——这正是下面两条未定论的由来，
不要拿其中任何一条单独当结论。

**同期还看到的其它请求**（只记方法与路径，用途未知不推断）：

- `GET https://muse.ai/api/session?vm_state=live`
- `POST /api/auth/check`
- 多条 `POST /api/falco`（**用途未知**）

**未定论（1）：WS 不是主线程 page 直接创建的**

`webSocketCreated` 无事件 + 主线程 `window.WebSocket` Hook 计数为 0，而帧照常收发 ⇒
至少两种可能，本轮未区分：

1. WS 在 **worker** 里创建（page 级 Hook 与 `webSocketCreated` 都看不到）；
2. WS 在 **attach CDP 之前**就已建立（CDP 事件流只报增量，不回溯历史连接）。

**未定论（2）：页面真正连接的 WS URL 仍未取到** —— 但**目标已由纯协议侧定案**

`/api/session` 下发的 `endpoint_url`（`wss://<vm_id>.metaaivm.com/`）与前端常量记录的
`hatch.metaaivm.com/v1/noise` 形状不同。页面真实 WS URL 这轮仍然没抓到
（`webSocketCreated` 没有事件），所以"浏览器连的是哪一个"严格说仍无直接证据。

但**纯协议侧已经用一次成功握手把可用目标钉死**（§1.6）：常量
`hatch.metaaivm.com/v1/noise` 能 TLS+WS 101 并完成 Noise XX；
`endpoint_url` 的 per-VM 子域作 WS 目标时 TLS 直接
`SSLV3_ALERT_HANDSHAKE_FAILURE`。因此代码里 host 一律取常量，`endpoint_url`
只作记录与换 token 参数（§1.1「端点来源」）。

**下一步取证手段**（本轮**没做**；写清是为了下次不重复猜）：

1. **worker target 级 Hook**：用 `Target.setAutoAttach`
   （`autoAttachRelated:true` + `waitForDebuggerOnStart:true`）把 worker 的 Runtime
   纳入，再在**每个 execution context** 里各跑一次 `window.WebSocket` 构造器 Hook，
   而不是只在 page 主线程跑。
2. **原始 CDP 会话带 `sessionId`**：分别对 page 与 worker target 发 `Network.enable`，
   确认 `webSocketCreated` 是否只出现在子 target 上。
3. 最省事的判别实验：刷新**之前**就 attach 并 `Network.enable`，看首帧前有没有
   `webSocketCreated`——这一条就能把「worker 创建」与「attach 太晚」分开。

### 1.5b 2026-11-11 11:0x 浏览器侧抓到真实 WS URL（未定论 2 关闭）

照着上面第 3 条做：在**新 tab 的 about:blank** 上先 `Page.enable` /
`Network.enable`，再 `Page.navigate` 到 muse.ai，于是 `webSocketCreated` 正常上报。

抓到浏览器真正连的 URL（敏感段已截断，仅保留结构）：

```
wss://hatch.metaaivm.com/v1/noise
  ?vm_id=<uuid>
  &auth_token=muse-secret-edge.<EdDSA/Ed25519 JWT>      # kid=HATCH_EDGE_TO_VM_ADMISSION
  &notary_token=endorsement.v1.<JWT>.<...>.uno-…        # 有 restrictions/identity/public_key
  &app_id=hatch-web
  &app_version=1
  &request_id=<uuid v4>
```

**这直接关闭了 §1.5 的「未定论 2」**：

1. 浏览器连的就是**常量 LB host + `/v1/noise`**，不是 `endpoint_url` 的 per-VM 子域；
   本仓库 `ws_url_from_endpoint` 默认 host 取常量的决定因此有浏览器侧，而不只是
   "协议侧能连"的单边证据。
2. query 参数与顺序（`vm_id, auth_token, notary_token, app_id, app_version,
   request_id`）、`app_version=1`、`request_id` 是 uuid 全部对上
   `protocol_client._build_ws_url`（`HATCH_APP_VERSION = "1"`，`@33403` 静态锚点）。
3. `notary_token` 是 **`endorsement.v1`**（标准背书，不是 `delegation.` 机密 VM
   前缀），与本仓库"非机密 VM 才继续"的硬边界一致；同一页面 reload 期间
   `webSocketCreated` 出现 **2 次**（app 会重建连接/或主+旁路），说明浏览器侧也存在
   "重开连接"行为，但**没有抓到带 `after_stream_seq` 的 resume 订阅**（重连时机不在
   抓包窗口内）——所以"浏览器如何 resume"仍是空的，只有我们自己的离线实现。
4. 同一轮 `Network.webSocketFrameSent/Received` 事件开始出现（第一轮没 attach 够早
   时是 0），证明"帧事件不出现"是 attach 时机问题，不是 worker 创建问题；
   但帧是 Noise 密文，浏览器侧**未见任何明文 RPC**，内层形状仍只能靠纯协议侧实证。

### 1.6 2026-11-11 纯协议首次打通（真机）

> 环境：Windows 真机、Python 3.14、**本机 mihomo 代理 `127.0.0.1:18000` → Meta 的 LB**、
> 真账号 cookie。**不走浏览器、不碰 CDP**，整条链路只用 `noise.py` + `proto.py` +
> `protocol_client.py`。**直连被墙：不开代理时 DNS/TCP 443 都到不了 `.metaaivm.com`；
> 也就是说本链路的前提是那只本地代理，不是网络本身。**
>
> 结果：TLS + WS 升级返回 **101**，Noise XX 握手成功，`connect()` 全程约 **3.3s**，
> 之后 `register-capabilities` / `subscribe` / `/chat/stream` / 事件流全部有真实往返。
> 这是本项目第一次用纯协议（不是浏览器）拿到助手正文。

下面按实测顺序记 1~6，每条都写"旧实现错在哪"与"证据原文"，避免下一个人重犯。

#### 1) WS 目标地址：常量 LB host 才是对的

真正能连的 URL（每次连接都带新的 `request_id`）：

```
wss://hatch.metaaivm.com/v1/noise
  ?vm_id=<uuid>
  &auth_token=<EdDSA JWT>
  &app_id=hatch-web
  &request_id=<uuid4>
  [&notary_token=...]
```

`/api/session` 返回的 `endpoint_url = wss://<vm_id>.metaaivm.com/`（per-VM 子域）
**在做 WS 目标时 TLS 握手直接失败**：

```
ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure
```

（同一代理、同一份 token 下，`hatch.metaaivm.com` 正常 101。）所以：

- 常量 `NOISE_WS_HOST="hatch.metaaivm.com"` / `NOISE_WS_PATH="/v1/noise"` **恢复为首选**；
  `_build_ws_url` 不再从 endpoint 取 host。
- `ws_url_from_endpoint()` 那套 endpoint 派生**降级为未验证备选**，默认关闭
  （`use_endpoint_host=False`），注释写清"真机 TLS 不通，只作备选 / 未来 LB 切换"。
- `endpoint_url` 属性/记录**保留**：它仍然是 `_fetch_token` 的 `vmAddress` 参数，
  只是不再当 WS host。

#### 2) Noise 握手：`vm_type` 为空串也能成功

`session.raw.get("vm_type")` 为空串时，msg1（32 字节 CSPRNG nonce）→ msg2 →
msg3 正常走完。**CVM / delegation 守卫保持不动**：`notary_token` 带
`delegation.` 前缀、`vm_type` 含 `confidential`、`cvm_eligibility.state` 非
`NOT_ELIGIBLE` 系，仍然硬停且不回退浏览器（模块头第 2 条）。本次实测的账号
三者都不是。

#### 3) `/client/register-capabilities`：只发 `client_id`

旧 body 把 `capabilities` 当**字符串数组**发（`["chat_cancel","delta_stream",...]`），
服务端 **400**：

```json
{"ok":false,"error":{"code":"invalid_params",
 "message":"parse JSON: invalid type: string \"chat_cancel\", expected a map at line 1 column 145"}}
```

—— `capabilities` 的元素必须是 **map/object**，不是字符串。

旧 body 去掉 `capabilities`、只留 `{"client_id": "<node_id uuid>"}` → **200**：

```json
{"accepted_data_sources":0,"accepted_device_commands":0,
 "accepted_hatch_app_commands":0,
 "capabilities":["video_file_ref_ingest","voice_user_speech_status"],
 "rejected_ids":[]}
```

**结论与选择**：注册时只发 `client_id`（`platform` / `display_name` / `version`
可留可不留——实测只发 client_id 就 200；本仓库选"只发 client_id"这个**被实测过的
最小 body**，并在代码注释里写明依据）。响应里的 `capabilities` 是服务端侧的字符串
清单，**不能**拿它反推请求侧该发 map —— 两个方向不是一回事。

另记一条（不处理，只记录）：`GET /client/register-capabilities` → **403**

```json
{"error":{"code":"forbidden","message":"matched route missing from endpoint ACL"}}
```

说明 ACL 只放行 POST。

#### 4) `/chat/subscribe`：请求必须 `end_body=True`，body `{}`

旧实现发 `end_body=False` 再等响应头 → 服务端**永远不回**，现象是：

```
ProtocolError: 等待 stream N 响应头超时
```

把请求体按 `end_body=True` 发送（空 body `{}` 即可）→ **200**，
`content-type: application/x-ndjson`，首行：

```json
{"id":"http-subscribe-<uuid>","payload":{"agent_id":"<uuid>","channel":"main",
 "conn_id":"<uuid>","is_primary":true,"is_thread":false,"session_id":"<uuid>",
 "stream_scope":"all","subscribed":true,"supported_reply_element_kinds":[...],
 "supported_reply_to_target_types":[...]},"status":"ok","type":"res"}
```

这条流**永不结束**：body 一行一条 NDJSON，`type` 是 `"res"` 或 `"event"`。
所以 `read_stream()` 不能再是"等 response 结束"的旧模型，必须重写成
**从订阅流的累积 body 里按行增量消费 NDJSON**（按换行切、逐行 `json.loads`、
坏行跳过不杀流、消费够 4MiB 就丢已消费前缀防内存增长）。

#### 5) 人类发消息：`POST /chat/stream`

请求体（实测 200 的那一次）：

```json
{"message": "<prompt>", "node_id": "<client_id uuid>",
 "capabilities": [], "timezone": "Asia/Shanghai"}
```

响应：

```json
{"channel":"main","is_thread":false,"message_id":"<uuid>",
 "reply_to_message_id":"<uuid>","session_id":"<uuid>"}
```

（实测 `message_id` 与 `reply_to_message_id` 相同。）`node_id` 必须等于
register 用的 `client_id`，否则事件被路由到别的 client。

#### 6) 回复事件序列与 `delta.text_append`

订阅流实测顺序与形状：

```
res
→ model_lock.updated
→ agent.status           (activity_code:"online", activity_text:"online")
→ task.status            (status:"completed" / "running")   ← 注意：可能在正文之前
→ approvals.snapshot
→ message.user           (payload.chat_context.chat_id = session_id, display_text ...)
→ agent.status           (working)
→ agent.status           (responding)
→ delta.message_start
→ delta.text_append      ← 关键：助手正文
→ agent.status           (回到 online / idle)
```

`delta.text_append` 原文：

```json
{"event":"delta.text_append","payload":{"is_thread":false,
 "message_id":"assistant-msg-<uuid>","message_seq":37,"parent_agent_id":"<uuid>",
 "parent_message_id":"assistant-msg-<uuid>","text":"9+9等于18。"},
 "seq":…,"ts_ms":…,"type":"event"}
```

- 助手正文在 **`payload.text`**。实例里整段回答 `9+9等于18。` 就是**一条**
  `delta.text_append`；也可能多次 append，**必须按到达顺序累加**。
- **结束信号是多条件，不能只靠单一信号**：
  1. 前提是**已经收到过正文**（`agent.status(online)` 与 `task.status(completed)`
     实测排在正文之前，只看状态会在开头把流关掉、把回答丢掉）；
  2. `agent.status` 的 `activity_code` 回到非 `working`/`responding`（如 `online`/`idle`）；
  3. 或 `task.status` 回到 `completed`；
  4. 或旧的 `delta.message_done`（本次序列没出现，保留兼容老路径/引擎契约）。
- 本仓库落点：`read_stream()` 把 `delta.text_append.payload.text` 转成文本增量事件，
  对外保留原始键（`event`/`payload`/`seq`/`ts_ms`/`type`）+ `name`，**另加**
  `kind`（`delta`/`status`/`event`）与 `text`（仅 delta 事件带）——是加法，
  不替换原有键，所以 `protocol_engine.py` / `protocol_generation.py` 的鸭子契约不受影响；
  `message.user` / `delta.message_start` 只映射成状态事件，**不重复正文**。
- **引擎侧收尾修正（同日活网抓到）**：`protocol_engine._run` 原来只认
  `delta.message_done` 当 done，而真机序列根本没有它；客户端按上面第 2 条收尾、
  流自己结束后，引擎会抛 `纯协议生成超时，未收到完整结果`，把一次成功回答变成报错。
  改为"**已收到过正文 + 未到 deadline ⇒ 算完成**"（"是否真的收完"的多条件判定
  由客户端 `_turn_ended` 负责）；"流结束且一段正文都没有"仍然报错，不放宽成空回答。
  活网复测：`13+13` → `13+13等于26。`（建连 3.1s、事件流 6.1s、1 条
  `delta.text_append` + 1 条 `done`）。复测脚本 `D:\deep seek\work\probe_pure_chat3.py`。
- **多轮（同日活网）**：同一 `HatchProtocolClient`、同一条订阅流连发两条
  （`14+14` / `15+15`）→ `14+14等于28。`（3.4s）/ `15+15等于30。`（2.4s），
  第二轮**没有回放上一轮正文**（`read_stream` 复用订阅时先 `_seek_to_end` 跳尾）。
  仍未验证的是**断线重连/resume 的真机行为**（`after_stream_seq` 那套）：离线已实现
  （幂等 `connect()` + 带游标重订阅 + 两段式降级 + 本地 seq 去重，证据等级常量
  `RESUME_EVIDENCE`），但本项目还没在真机制造过断线，字段是否被服务端接受、
  重放是否严格大于游标都属未证实（见 §8.8 第 6 项）。
  脚本 `D:\deep seek\work\probe_pure_chat_multi.py`。

### 1.6.1 2026-11-11 纯协议**图像生成**真机验收

> 结论先说：`/chat/stream` 就是生成入口（没有独立的 gen RPC），生成物在
> `delta.presentation` 的 `image_path` 里，`sandbox://workspace/imagine_media/…webp`，
> 用 `/fs/read` 分页取回字节。**全程不碰 CDP**。同一个账号（`muse1`）连跑三次，
> 三次都成功，两次是同一条 bridge 代码路径。

**链路**（逐步证据，全部可复跑）：

1. `POST /chat/stream` `{"message":"生成一张图片：…","node_id":<uuid>,
   "capabilities":[],"timezone":"Asia/Shanghai"}` → 200 同步 ack；
2. 订阅流里出现 `delta.presentation`，payload 带
   `image_path: "sandbox://workspace/imagine_media/media-generation-0-<uuid>.webp"`
   与 `fallback_text: "![…](sandbox://…)"`（这是生成物引用，**不是**用户上传件：
   `workspace/user/` 下的路径被 `_require_workspace_path(allow_user=False)` 排除）；
3. `POST /fs/read {path, offset, len}` 分页到 `eof`，每页 `data_base64` 校验长度；
   实测一次 **181,726 B**、一次 **685,900 B**，文件头 `RIFF…WEBP`，嗅探
   `image/webp` 与声明一致；
4. 引擎 `ProtocolEngine.generate_image` 返回 `GenerationResult(data, mime, kind)`
   （进度里程碑 25/40/50/100 实测出现）；`ProtocolDriver.generate` 落盘后翻译成
   `{path,mime,kind,size,ext,via:"protocol"}`，`pool.generate_media` 与浏览器路径
   共用同一条落盘/任务收尾代码。

**三层验收**（越往上越接近真实调用方）：

| 层 | 做法 | 结果 |
|---|---|---|
| 引擎直连 | `probe_engine_generation.py`：`ProtocolDriver` → `_engine.generate_image` | `image/webp` 181,726 B，mime/kind 正确，字节已落盘 |
| bridge API（**CDP 指向无人监听的 `127.0.0.1:9`**） | `probe_restart_bridge_dead_cdp.py` + `probe_bridge_protocol_gen.py`：`POST /v1/images/tasks` → 轮询 → `/v1/media/{token}` | `queued → processing → completed`（26s），进度 100，媒体 685,900 B WebP；**CDP 不可用仍出图 ⇒ 生成本次只可能走协议** |
| 画布 UI | `qa_canvas_flow.py` 在 8788 画布上点「生成」 | 节点 323→325（prompt+图片卡），图片 `src=http://127.0.0.1:8788/canvas/api/media/f54f…`，保存 `node_count 323→324`，页面错误 `[]` |

**代码落点**：`protocol_driver.ProtocolDriver.generate`（落盘 + 翻译，`via="protocol"`）
→ `protocol_engine.ProtocolEngine.generate_image`（事件裁决 + `fs_read` 取件）
→ `protocol_client.HatchProtocolClient.read_stream` / `fs_read`。
派单选择在 `pool._build_driver`：`MUSE_PROTOCOL_DRIVER=1` + 账号有 cookie ⇒
`capability in ("chat","generation")` 都返回协议驱动；`generation-browser` 是
**回退专用**取值（`pool.generate_media` 在可重试的协议生成错误后释放锁、重派一次
浏览器，`_generation_fallback_allowed` 明确只放行"可换号重试"类，拒绝/额度不足不换链路）。

**仍未验收（不许写成已验证）**：

- **视频生成**：**已真机验收**（2026-11-11 11:0x，1280×720/5.0s/2,963,296 B mp4，
  见 1.6.1c），`--via-driver` 落盘形状也已真机跑通（11:4x，10.0s/6,858,917 B，
  `duration_ms` 已回填），不再列入未验收。
- **断线重连 / resume**：**已真机验收**（2026-11-11 11:2x 主动掐断 WS → 重连 1 次、
  带 `after_stream_seq=322` 重订阅 200、尾部事件与 `delta.presentation` 照常到达，
  见 1.6.1d）。仍未钉死：游标边界（seq=322 是否重发）属推断，去重靠本地。
- 参考素材上传 → 生成：**已真机验收，见 1.6.1b**（不再列入未验收）。
- 仍未真机验的只剩：Spaces 成功路径（账号 0 Space）、`/muse/export` 2xx（同上）、
  浏览器侧自己的 resume 订阅参数（§1.5b 只抓到重连本身）。
  **11:5x 补记（尝试造一个 Space 失败）**：muse.ai 页面确实有 `创建空间` 入口
  （icon-only button，`aria-label=创建空间`；用户页当前显示"连接错误"）。临时把系统
  代理切到 FlClash 7890（探针 finally 恢复 18000）后新开标签页，分别用 JS `click()`
  与 CDP 真实鼠标事件点击该按钮：`modal_count=0`、`forms=0`、页面无新增浮层
  （`work/canvas_re/space-create-flow-probe.json`）。入口可能依赖 hover 菜单/尚未
  加载的子应用或创建走异步 agent，本轮不继续；Spaces 成功路径与 export 成功响应
  仍只有离线夹具证据，如实保留。

### 1.6.1b 参考素材上传 → 生成真机验收（2026-11-11 10:0x，同账号 muse1）

**第一次真机是失败，正好把请求体形状钉死**：文生图成功（28132 B WebP）后，把
`fs_write` 返回的 `sandbox://workspace/user/files/reference_0_….webp` 直接塞进
`/chat/stream` 的 `items[].path`，服务端 500：

```
[runtime] failed to resolve file_ref path sandbox:/workspace/user/files/
reference_0_e6c2a31a.webp: No such file or directory (os error 2)
```

注意报错里是 **`sandbox:/`（单斜杠）**：服务端把 URI 当"scheme + 绝对路径"再拼回
单斜杠形态去解析，于是必然找不到文件。随后在 hatch-web chunk 里找到两处真实调用点：

| 锚点 | 原文 / 语义 |
|---|---|
| `07hpppij0ssd9.js@67023` | `l.push({type:"file_ref", mime_type:r.mimeType, path:n, filename:r.filename})`；`n = r.uploadPath ?? t6(r.filename)`，上传用 `` e(r, `/${n}`, rawFile) `` ⇒ **path 是 uploadPath（workspace 相对路径，无 `sandbox://`）** |
| `24thn7in_976_.js@97321` | 带附件发送：`{items:s, node_id, capabilities, …}`，其中 `s` 含 `{type:"text", text:e}` 文本项；纯文本发送另一处是 `{message:e, items:[browserTab], …}` ⇒ **带附件走 items、文本也在 items、不发 `message`** |

修复落点（本仓库）：

- `protocol_client._stream_body`：references 非空时 `body.pop("message")`，
  改发 `items=[{type:"text",text:prompt}, *refs]`；text-only 保持真机已验证的四字段。
- `protocol_engine._stage_references`：`fs_write` 的 URI 形态**只在发请求前**剥掉
  `sandbox://`，`items[].path` 用裸 `workspace/user/files/…`；`upload_refs`
  仍记 URI 形态（它服务于 fs 清理，不是请求体）。字段对齐浏览器：
  `{type:"file_ref", mime_type, path, filename}`。

**修复后真机重跑**（`work/canvas_re/refs-live2/`，同一脚本、同账号）：

| 回合 | 动作 | 结果 |
|---|---|---|
| round 1 | 文生图（造参考小图） | `image/webp` 23,774 B，sha256 `864d693b…87b3`；进度 25/40/50/100 |
| round 2 | data URL（31,723 chars）→ `fs_write` → 参考图生成 | **成功**：`image/webp` 89,642 B，sha256 `18100185…f22fb`；进度 25/40/50/100；事件序列 34 条（`task.status=running` → `message.user` → `delta.text_append` → `delta.presentation` → 收尾） |
| 上传引用 | `engine.upload_refs` | `sandbox://workspace/user/files/reference_0_735e2289.webp` |

复跑：`python -Xutf8 D:\deep seek\work\canvas_re\probe_protocol_gen_refs.py --live-confirm`
（默认 dry-run；每次真跑消耗 2 次生图额度）。产物：`refs-live2/probe-gen-refs-source.webp`
（23,774 B）与 `refs-live2/probe-gen-refs-product.webp`（89,642 B，参考图重绘成品）。

> 结论：**"上传参考素材 → 生成" 在纯协议路径上已端到端跑通**；此前失败是请求体
> 形状问题（第 1 次失败留档），不是协议能力缺失。

复跑命令（WORK 根目录）：

```powershell
python -Xutf8 D:\deep seek\work\canvas_re\probe_engine_generation.py
# 或（证明不碰浏览器）：
python -Xutf8 D:\deep seek\work\probe_restart_bridge_dead_cdp.py
python -Xutf8 D:\deep seek\work\canvas_re\probe_bridge_protocol_gen.py
python -Xutf8 D:\deep seek\work\probe_restart_bridge.py   # 跑完必须恢复正常 CDP
```

### 1.6.1c 视频生成真机验收（2026-11-11 11:0x，同账号 muse1，**已花额度**）

§8.8 #6 里一直挂着"视频生成未真机验收"。本轮用户明确授权花额度后，用
`probe_protocol_gen_video.py --live-confirm --skip-reference` 真跑了一次
**文生视频**（不走浏览器、不碰 CDP），结果：

| 项 | 实测 |
|---|---|
| 连接 | `host=wss://hatch.metaaivm.com/v1/noise` `vm_state=RUNNING`；`wake_ok=False`（VM 本来就在跑，未声称"唤醒成功"） |
| 请求 | `duration=5`、`size=16:9`、纯文本 prompt（无参考图），走 `ProtocolEngine.generate_video` → `expect="video"` |
| 成品 | `video/mp4`、**1280×720**、`duration_ms=5000 (~5.0s)`、**2,963,296 B**、sha256 `74873812370a8cb72311be48399c218baca5fb7cef7bb7194488b48ef797af82` |
| MP4 头 | 前 16 字节 `00 00 00 20 66 74 79 70 69 73 6f 6d …`（`ftypisom`，合法 ISO BMFF） |
| 事件 | 20 条：`task.status pending_init/running` → `delta.message_start` → `delta.text_append` → `delta.presentation`（视频成品在 presentation 里） |
| 进度 | 里程碑 `[25, 40, 50, 100]` |
| 落盘 | `work/canvas_re/video-live1/probe-gen-video-product.mp4` |
| 额度消耗 | 用量面板从 4% → 7%（本轮图像+视频各一次；见 §8.8 #8 的机制订正） |

结论：**"协议只能出图、视频没真跑过"的说法作废**。视频与图像共用同一套编排
（`/chat/stream` + presentation + `/fs/read`），差异只在 `expect="video"` 的裁决与
`ProtocolMediaTypeError`（期望视频却拿到图片一律硬拒绝，仍由离线测试钉住）。

**11:4x 补记（`--via-driver` 分支也已真机跑通，2026-11-11）**：同一账号再跑一次
`probe_protocol_gen_video.py --live-confirm --skip-reference --via-driver`，验证
`ProtocolDriver.generate(expect="video")` 的落盘形状：`path=data/media/*.mp4`、
`mime=video/mp4`、`kind=video`、`w=1280/h=720`、`duration_ms=10000 (~10.0s)`、
**6,858,917 B**、sha256 `23de908706f93db2612897432b5dafea0f4f79647f77a029c6d52e0ffc5e2d38`，
落盘 `work/canvas_re/video-driver1/probe-gen-video-driver-product.mp4`。

- 驱动 dict **现在包含 `duration_ms`**（`protocol_driver.py` 回填 `GenerationResult`
  的上游元数据）；探针里"驱动丢弃时长"的旧 WARN 已改掉，实测作废。
- 时长不是硬约束：提示词写"时长严格为 5 秒"，产品实际 10,000ms。引擎层记录上游
  实际值，`DURATION_WHITELIST` 只约束提示词可请求值，不能承诺服务端遵守。
- 探针的媒体目录跟随进程 cwd：在仓库父目录（`D:\deep seek`）执行时文件落到
  `D:\deep seek\data\media\`（本次已挪进证据目录并清掉误建的 `data/`）。从
  `muse-bridge/` 内执行才与 bridge 的 `data/media` 一致。

### 1.6.1d 断线 resume 真机验收（2026-11-11 11:2x，图像生成回合，已花额度）

§8.9 里"真机断线恢复未验收"是本模块最后一块空白。用户授权后做了**主动掐断实验**
（`work/canvas_re/probe_live_resume_image.py`）：

1. 连上 Noise、发图像生成 prompt，正常收到 `task.status` 等前奏事件；
2. 在 `task.status` 之后、成品 `delta.presentation` 之前，**强制 close + abort
   底层 WS 传输**（模拟真实断线；不是暂停/挂起）；
3. `read_stream` 原地重连：`connect()` 全流程重跑，重注册 client_id，带
   `after_stream_seq=322` / `after_chat_event_seq=0` 重订阅；
4. 结果：`_reconnect_count=1`，**重订阅 200 且没有触发降级重订**（降级会再发一次
   空 `{}` 订阅——实测 subscribe 调用仅 2 次），随后继续收到
   `model_lock.updated` / `agent.status` / `task.status` / `approvals.snapshot`
   的尾部事件与最终 `delta.presentation`（成品在重连**之后**才生成完）。

⇒ 三条之前只敢写"推断"的结论现在有真机证据：
* 服务端接受 `after_stream_seq` / `after_chat_event_seq` 字段；
* 重连后服务端会继续推送（含写往 presentation 的最终事件）；
* 本地 `seq <= 游标` 去重没有造成正文丢失（正文 delta 与成品事件都齐全）。

**仍未钉死**：游标边界语义（传回的 seq=322 本身服务端是否会再发一次）没有单独
验证；`RESUME_EVIDENCE` 里仍标"边界属推断"，去重不押在服务端。实验记录：
`work/canvas_re/resume-live2/resume-live2.json`（另有一次 v1 文本实验因"掐断时
回答已整段到达本地缓冲区"导致 reconnect=0，属实验设计无效，已留档并注明）。

**2026-11-14 追加（聊天回合）**：断线窗口内的事件不会被补发（2 次探针：断线前
seq=880，重连后首收 922；正文 66/28 字，对比无断线基线 456 字），但**已提交历史
可以取回完整正文**：`GET /chat/history` 返回 `{channel, chat_events, has_more,
transcript_revision}`，事件带 `event_name`/`role`/`seq`/`message_id`/`display_text`。
引擎据此默认开启 `history_recovery`：重连回合以普通状态收尾时，若历史中的
assistant 正文是已收正文的严格前缀，就用全文完成；实测 169→456 字、整链路
2 段 delta→456 字。历史 seq 与 live 流 seq 是两套序列（683 vs 734），不做游标
比较。证据：docs/2026-11-14_resume-continuity-report.md、
docs/2026-11-14_history-recovery-probe.md。

### 1.6.2 2026-11-11 09:5x 剩余路由面真机探针（Spaces / idea-cards / 额度候选）

> 环境同上（真账号 `muse1` + 本机 mihomo）。**全部只发 GET**：不创建 Space、
> 不触发生成、不碰 CDP、不花额度。脚本 `work/canvas_re/probe_protocol_routes.py`，
> 原始 body 与 summary 落 `work/canvas_re/protocol-route-dump/`。

| 路由 | 实测结果 | 判读 |
|---|---|---|
| `GET /spaces/v2`、`GET /spaces/v2?include_constructing=true` | `200 {"spaces":[]}`（13 B） | 列表可用；该账号当前确为 0 个 Space |
| `GET /spaces/v2/{slug}` | `404 {"code":"space_unavailable","message":"space source directory was not found"}`（98 B） | **单 Space GET 路由存在**，只是资源不存在（与 `save` 的 `shared artifact not found` 是两套措辞） |
| `GET /spaces/{slug}/export.html` | `404 {"code":"not_found","message":"web artifact was not found"}`（80 B） | 路由存在、资源不存在 |
| `GET /spaces/{slug}/export` | `404 route not found`（69 B） | 导出**只有 `.html` 这一种形态** |
| `GET /api/idea-cards` | `200`，**67,759 B**，7 个 section（Ideas / Productivity / Financial Management / Relationships / Health & Fitness / Shopping…） | Explore 卡片列表可纯协议读；每卡含 `id/ideaCardId/title/summary/buildStatus/presentation.icon.variants[].url` |
| `GET /api/idea-cards/{ideaCardId}` | `200`，1,877 B，`{"ok":true,"result":{"card":{…}}}` | 卡片详情：`presentation.icon.variants[].url`（相对 `/api/idea-cards/media/<hash>`）、`items`、`sharing.kind` |
| `GET /api/idea-cards/media/icon-57e8…aa6` | `200 image/png`，**1,045 B，`\x89PNG` 签名已验证** | 纯协议可取卡片预览图 ⇒ 画布"导入 idea-card 预览卡"不需要浏览器、不花额度 |
| `GET /api/idea-cards/media/{任意}` | `404 {"message":"idea card media was not found"}` | 媒体路由存在，handle 不存在 |
| `GET /api/usage`、`/api/quota`、`/api/billing/usage`、`/api/me/usage`、`/api/session` | 全部 `404 route not found`（69 B，与 `/api/zzz-not-a-route` 同形） | **Noise 内层没有这些命名的额度路由**；额度面板仍需浏览器面（§8.9），静态逆向继续找真实 path（见 `docs/PROTOCOL_QUOTA.md`） |

**复跑**：

```powershell
python -Xutf8 D:\deep seek\work\canvas_re\probe_protocol_routes.py
```

产物：`protocol-route-dump/summary.json`（逐路由 status/bytes/content-type）、
同名 `.txt`（文本 body）与 `.bin`（原始字节，含 1,045 B PNG）。

### 1.6.3 2026-11-11 12:5x 真机：用 idea-card 造出第一个 Space（Spaces 成功路径）

> 目的：账号 0 Space，导致 `GET /spaces/v2/{slug}`、`/spaces/{slug}/export.html`
> 只有 404 形状可看。这一轮用 Explore 灵感卡的 `execute`（协议内写操作）真造一个
> Space，把"路由存在"升级成"成功路径"。花了一次构建额度；脚本
> `work/canvas_re/probe_idea_card_execute.py`（默认 dry-run，`--yes` 才执行）。

步骤与结果：

| # | 动作 | 结果 |
|---|---|---|
| 1 | `GET /spaces/v2` | `200 {"spaces":[]}`（确认 0 Space） |
| 2 | `POST /api/idea-cards/idea_494d0b22b28c404aa2ec653f6f3bf19b/execute` body `{"ideaCardId":"idea_494d…","mode":"full"}` | `200 / 142 B`：`{"ok":true,"result":{"cardId":"idea_494d…","executionId":"1131870c-…","status":"queued"}}` |
| 3 | 轮询 `GET /api/idea-cards/{id}` + `GET /spaces/v2?include_constructing=true` | +12.3 s：`build=complete`，spaces=0；**+20.0 s：spaces=1** |
| 4 | `GET /spaces/v2?include_constructing=true` | 新 Space：`slug=welcome-to-muse`、`name="Welcome to Muse"`、`construction_status=constructing`、`runtime=static`、`has_server_actions=true`、`is_current_build_published=false`、`space_id=605d0388-…`、`preview_image.{icon,thumbnail,preview_card}` 指向 `…metaaivm.com/fs/raw/workspace/…` |
| 5 | `GET /spaces/v2/welcome-to-muse`（detail） | 200 / **237,224 B**，body 不是 JSON（是页面壳/HTML），`json()` 解析为 `null`；**detail 路由 200 不等于结构化详情**，要用 list 里的条目或前端页面渲染 |
| 6 | `GET /spaces/welcome-to-muse/export.html`（构建中，+20 s） | **400 `application/json`**：`{"ok":false,"error":{"code":"invalid_params","message":"This web artifact has server-backed functionality, so it can't be exported to a standalone .html file. S…"}}` |
| 7 | 等构建完成后再查：`GET /spaces/v2?include_constructing=true` | `construction_status=succeeded`、**`has_server_actions=false`**、`accessed_at_ms` 已更新 |
| 8 | 再次 `GET /spaces/welcome-to-muse/export.html` | **200 / 5,311,894 B HTML**（`<!doctype html>`，内联 base64 JPEG 资源）；同时 bridge 路由 `GET /canvas/api/muse/export/welcome-to-muse` 也是 200 / 5,311,894 B `application/octet-stream` |

**结论（含一次自我更正）**：

1. **Spaces 可以纯协议创建**——但入口不是 `POST /spaces/v2`（不存在），是
   `api.idea-cards.execute`：卡片的 `build_status` 与 Space 的 `construction_status`
   共用状态机（`docs/CANVAS_PROTOCOL.md` §8.4 已记录），执行卡片 = 让 agent 构建
   一个带 Space 的产物。20 秒内 Space 就出现在 `spaces/v2` 列表。
2. **构建中的 400 是暂时的，不是永久约束**。第一次 export 失败时
   `construction_status=constructing`、`has_server_actions=true`，上游回
   `invalid_params: …server-backed functionality…`；构建完成（`succeeded`）后
   `has_server_actions` 变成 `false`，同一条 export 路由返回 **200 / 5.3 MB 单文件
   HTML**。所以"server-backed 不能导出"应理解为**该构建尚未产出可导出的静态产物**，
   而不是"这类 Space 永远不能导出"——上一版结论把它当永久约束，这里更正。
3. **导出前先看 `construction_status` 与 `has_server_actions`**：`constructing` 时
   调用只会拿到 400；正确做法是轮询到 `succeeded`（或前端同一状态机）再导出。
   若真遇到 `has_server_actions=true` 且已 `succeeded`，才需要把上游
   `error.message` 原样透出（`MuseSpacesError` 的 snippet 已带上）。
4. 证据：`work/canvas_re/idea-card-execute/execute.json`（轮询与当时 400 body）、
   `work/canvas_re/idea-card-execute/space-state.json`（完成态 + 200/5,311,894 B）、
   `export-welcome-to-muse.html`（第一次的 JSON 错误体，未覆盖；成功体未落盘）。

---

## 二、认证与会话

### 2.1 Cookie 构成

| Cookie | 有效期（实测） | 作用 | 是否决定账号生死 |
|---|---|---|---|
| `hatch_sess` | **≈30 天**（`Max-Age=2592000`），`HttpOnly` | 主会话，**唯一必要 cookie**（上游 `ESSENTIAL_COOKIES=('hatch_sess',)`） | 是 |
| `hatch_vml` | **固定 ≈48 小时（`Max-Age=172800`），且不因使用而延长** | VM 生命周期时钟 | **是——它就是账号寿命** |
| `hatch_gw` | ≈1 年（`Max-Age=31536000`） | 网关路由缓存，**可由 `/api/session` 重新签发** | 否 |
| `hatch_native_auth_device` | 30 天（`Max-Age=2592000`） | 设备绑定凭证 | 否（不决定生死，别拿它当到期时间） |
| `hatch_native_auth_csrf` 等 | 600 秒 | CSRF/临时/状态类 | 否 |

三条容易踩的实践结论：

1. **账号寿命只能以 `hatch_vml` 的真实 `expires` 为准**。`hatch_vml` 的 48 小时是固定窗口，
   不会因为你在用就往后推；调 `/api/session` 的意义是**重新签发**这套 cookie，不是续命到无限。
   拿不到真实到期时间时，按「导入时间 + 2×86400」估算并显式标记 `expires_estimated`——
   绝对不要退回用 `hatch_native_auth_device` 的 30 天（那会把已死账号当健康号派单）。
   依据：`czg86389-hub/muse2api` `engine.py:537`、`store.py:27-31`、`app.py:1052-1057`、
   `lichao199208/muse2api` `docs/muse-auth-protocol-full.md:150-158`；
   本仓库落点：`muse_bridge/session_http.py:140-250`（`parse_set_cookie`/`CookieExpiry`/`account_expiry`）。
2. **2026-10-27 起上游不再下发 `hatch_gw`/`hatch_vml`**（`www222fff/muse2api` `upstream/muse.py:26-31`，PR #5 已闭）。
   必需集收缩为 `hatch_sess` + `hatch_native_auth_device`，`hatch_gw`/`hatch_vml` 转可选。
   所以任何「缺 `hatch_vml` 就判无效」的硬校验都要放宽为「缺了就按估算 + 探针确认」。
3. native 侧属性实测：`HttpOnly + Secure + SameSite=Strict + Path=/`，host-only 与 `Domain=.muse.ai` 各一份。
   裸 curl（无 cookie jar）打多数 native 接口会 403 并带 `csrfFailed`；CSRF 走 **body 的 `csrf_token` → 头 `X-Hatch-CSRF-Token`**，
   与 cookie `hatch_native_auth_csrf` 不是同一个东西。

### 2.2 相关端点

| 端点 | 作用 | 请求体 / 响应 | 证据与本仓库落点 |
|---|---|---|---|
| `POST /api/auth/check` | 探测登录态 | `{ok, access_token, viewer_id}` | ⚠ 字段来自文档与一处页内 fetch，**本轮未逐字段核对**（§8.8）；本文保留但不作为实现依据 |
| `GET /api/session`（可带 `?vm_state=live`） | 分配/确认云端 VM，**并重新签发 cookie** | `{status:"assigned", vm_id, vm_state, endpoint_url, vm_type, cvm_eligibility, vm_name}` | `lichao199208` `protocol_client.py:250-283`、`czg86389` `engine.py:535-709`；本仓库 `session_http.py:439-462`（判定用 `connectable`＝`status+vm_id+vm_state+endpoint_url` 四者齐，缺 `endpoint_url` 直接判不可连，见 `:280-296`） |
| `POST /api/hatch/vm/wake` | 唤醒休眠 VM | 请求 `{vm_id, retry_count: 0, connect_attempt_id: <uuid4>}`；响应 `{status:"wake_requested"}`。**200 只是受理**，必须再 `GET /api/session` 读到 `vm_state==RUNNING` 才算唤醒成功 | `engine.py:588-606`、`api-reference.md:29-33`；本仓库 `session_http.py:482-556`（含 poll-to-RUNNING 确认，未确认则 `SessionUnconfirmed`） |
| `POST /api/hatch/token` | 用 VM 端点换 Noise 准入 token | 请求 `{vmAddress: <endpoint_url>, vmName}`；响应 `{token: <EdDSA JWT>, notary_token}`，`kid=HATCH_EDGE_TO_VM_ADMISSION:1` | `lichao199208` `docs/muse-auth-protocol-full.md:443`、`api-reference.md:22-47`；本仓库 `protocol_client.py:214-239` |
| `POST /api/hatch/noise-notary-token` | **单独续 `notary_token`**（不动 auth_token） | 请求 `{vmName}`；响应 `{notaryToken}`（**camelCase**，与 `/api/hatch/token` 的 `notary_token` 字段名不同，这是实测差异不是笔误） | `manishkumar1601/muse-ai-api` `src/bootstrap/session.ts:125`；`api-reference.md:22-47` |
| `POST /api/hatch/lease-vm` | 没有可用 VM 时租一台 | 请求 `{vmType:"standard"}`；响应 `{status, gatewayUrl, vmName}`；**已有 VM 时返 403**（403≠cookie 失效） | `engine.py:616`；`PHASE_0_REPORT.md:64-76` |
| `POST /api/ping` | 会话内保活 | `{presence:"active"}`，上游每 30 秒一次 | `lichao199208` `protocol_client.py:417-421`；本仓库 `protocol_client.py:448-460,719-722`（`HEARTBEAT_SECONDS=30.0`） |

> **2026-11-11 真机复核**：`GET /api/session?vm_state=live` 活网返回
> `status=assigned`、`vm_state=RUNNING`（字面量大写）、`endpoint_url=wss://<vm_id>.metaaivm.com/`；
> 四字段判据与 `session_http.py` 的 `connectable`（写作时 `:279`）一致。`vm_id` 是
> UUID 形状，`endpoint_url` 每台 VM 一个子域。**但该子域只作 `vmAddress`（换 token）
> 与排障记录，不作 WS host**：真机用它做 WS 目标时 TLS 不通，能握手的是常量
> `hatch.metaaivm.com`（§1.1「端点来源」、§1.6）。同会话还观察到
> `POST /api/auth/check` 与多条 `POST /api/falco`（用途未知，未定论）。细节见 §1.5。

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

| 常量 | 现首选项（实测项一律**前置**，旧猜测项保留在后） | 实测来源与频次 |
|---|---|---|
| `ASSISTANT_MESSAGES` | `[data-hatch-assistant-message-body="true"]` → `[data-message-item][data-message-role="assistant"]` → `[data-message-role="assistant"]` → `[data-role="assistant"]` → `[data-message-item]` → `div[class*="hatch-chat-groupable-bubble"][class*="agent-bubble"]` → `div[class*="hatch-chat-groupable-bubble"]` → 旧候选（**role 判据必须在通用气泡类名之前**，2026-11-11 真机复核见 §4.1.1） | `hatch-chat-groupable-bubble` ×44/7 文件、`data-message-item/role` 见 MUSE_MCP README.md:293、`engine.py:717,1231-1232,1404-1408`；顺序以 `selectors.py:75-86` 为准 |
| `COMPOSER_INPUTS` | `[data-hatch-composer-root] textarea` → `[data-lexical-editor="true"]` → `textarea[placeholder]` → … | MUSE_MCP README.md:287-288 |
| `STREAMING_MARKERS` | `[data-testid="hatch-composer-stop-button"]` → `[data-testid="streaming"]` → `button[aria-label*="Stop"]`/`停止` | `hatch-composer-stop-button` ×22/9；`engine.py:719-721,1234-1236` |
| `SEND_BUTTONS` | `button[aria-label*="发送"]` → `button[aria-label*="Send"]` → `button[type="submit"]`（**已删除无任何实测来源的 `button[data-testid="send-button"]`**） | `engine.py:1075-1077` |
| `RESULT_MEDIA` / `ATTACHMENTS` / `HYDRATION` / `COMPOSER_ROOT` / `ERROR_NOTICES` / `THREAD_ROWS` | 本轮新增：`div[class*="hatch-agent-bubble-bg"] img|video`、`[data-testid^="hatch-chat-attachment-presentation-"]`、`[data-hatch-shell-hydration-state="hydrated"]`、`[data-hatch-composer-root]`、`[data-testid="assistant-response-error-notice"]`、`[data-testid="hatch-thread-row"]` | `hatch-agent-bubble-bg` ×40/15、`hatch-chat-attachment-presentation` ×35/14、`data-hatch-shell-hydration-state` ×30/15 |
| `REASONING_MESSAGES` | **仍是猜测的常见形态，未活网确认**；`MUSE_REASONING=0` 时完全不查询 | 见 `selectors.py:88-96` 自述 |

按顺序尝试，第一个命中的胜出。这样前端换了类名，只要有一个旧选择器还在就能继续工作。
频次口径：全语料扫描（含 `recon/upstream/engine.py` 与 MUSE_MCP 文档），不是单文件出现次数。

前端改版时**只需改这一个文件**。

### 4.1.1 2026-11-11 真机复核：聊天 DOM（实测）

> 素材：真账号 + 真 muse.ai 聊天页，**刷新前、刷新后各抓一次** DOM（只读
> `querySelectorAll` + 属性/`innerText` 导出；不改页面、不点按钮、不导出 cookie）。
> 下面写的是**当前构建**的结构；早先一次渲染出现过 `DIV.flex.flex-col.gap-2.assistant`，
> 只能当兜底（见末段）。

**消息根容器（用户、助手共用同一套属性）**

```text
DIV.flex.flex-col.gap-2
  data-message-item="true"
  data-message-role="user" | "assistant"
  data-message-side
  data-message-id
  data-message-turn-id
  data-message-groupable
```

**助手正文**

```text
DIV[data-hatch-assistant-message-body="true"]
  └ DIV.prose[data-hatch-plain-text="true"]
      └ P.whitespace-pre-line          ← 正文在这里
气泡类名：hatch-chat-groupable-bubble rounded-22 bg-[var(--hatch-agent-bubble-bg)]
```

**用户气泡**：同一套气泡类名，但底色 token 是 `bg-[var(--hatch-user-bubble-bg)]`；
它的 `innerText` 是 **`你：\n\n<提问>`**（`你：` 是前端加的**渲染前缀**，不是用户输入的内容）。

**判据结论（现场 bug 的直接原因）**

- `[data-role="assistant"]` 在**当前构建刷新后不存在**；早先一次渲染里出现过
  `DIV.flex.flex-col.gap-2.assistant`，只能当兜底候选。
- `hatch-chat-groupable-bubble` **用户气泡与助手气泡都命中**。
- 所以 role 判据（`[data-message-role="assistant"]`）必须排在通用气泡类名**之前**，
  否则会把用户提问当成回答——现场表现是非流式回答里混进 `你：…` 回显。
- `selectors.py:75-86` 的候选顺序满足这条：最精确的正文容器
  `[data-hatch-assistant-message-body="true"]`（`:76`）在首位，其次是
  `[data-message-item][data-message-role="assistant"]`（`:77`）；通用类名
  `div[class*="hatch-chat-groupable-bubble"]` 排在 `:83`，`[data-role="assistant"]`
  降到 `:79` 只作兜底（当前构建不命中）。同文件 `:60-74` 的注释已按这份真机证据改写。

> **推断（未单独构造用例验证）**：`[data-message-item]` 是**每条**消息都带的属性
> （user/assistant 一致），若它排到 role 判据前面，就会把用户消息一起选中。
> 当前顺序下它（`:81`）只在 role 判据全不命中时才生效。

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

### 4.5 2026-11-11 真机复核：成品提取（`_JS_EXTRACT` 少一个右括号）

> 定位约定：`driver.py` 本轮有并行改动，行号会漂；下面一律给**函数/常量名**，
> 行号只在写作时点一次（`muse_bridge/driver.py:1751` 的 `_JS_EXTRACT`）。

**现象（实测）**：浏览器路径生图，模型确实生成了，但交付恒 500，错误文本
「已生成但未能取回文件」（`driver.py` 的 `extract_media()` 收尾处抛
`MediaExtractError`）。失败点在**取字节**，不在生成。

**根因（实测）**：`muse_bridge/driver.py` 的 JS 常量 `_JS_EXTRACT`
（`(async (pair) => { … }` 形状）**少一个右括号**。Playwright 的 `page.evaluate()`
拿到的不是合法函数表达式，直接抛 `SyntaxError`；而 `_js()` 把异常**吞成 `None`**
（同文件 `_js()` 的 `except Exception: return None`），于是 `extract_media()`
只看到"页面没返回结果"，重试 4 次后抛 `MediaExtractError`。

**为什么看起来像"生成的锅"**：`media_links()` 收上来的 `blob:` 源只有在页面上下文里
`fetch` 才解得开（`extract_media()` 的 docstring 已写这条理由），所以取回这一步
**必然**走 `page.evaluate`。JS 片段语法坏了，表现就是"生成成功、文件永远拿不到"，
而且日志里没有任何 JS 报错。

**修复后实测**：同一个 `blob:` 源取回 `image/webp`，371KB / 448KB 两份，base64 正常
解码，字节数与页面 `blob.size` 一致。补上右括号后该常量以 `})` 收尾。

**离线哨兵**：`tests/test_driver_js_syntax.py` —— 括号配平（`:25-39`）+ 必须写成
函数表达式或可拼接的 `const` 片段（`:42-51`）。它只能抓"少/多括号"这类形态错误，
**语义错误仍要真机跑**（该文件头 `:1-8` 已写明这条边界）。

> 留给代码的一条（**本轮未改**）：`_js()` 吞异常省了一次崩溃，也吞掉了唯一线索。
> 同类静默失效至少要在返回值里区分「JS 抛错」与「JS 返回 false」。

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

### 并发模型：为什么必须串行

同一个 Chrome 窗口只有一组输入框和一条消息流。两个请求同时：

1. 都往同一个输入框 `fill()` → 后写的覆盖先写的，prompt 串了
2. 都监听"最后一条助手消息" → 各自看到对方的回答
3. 一个请求结束时机与另一个错位 → 回复归属完全错乱

所以每个账号有一把 `asyncio.Lock`，**覆盖整个请求（含流式输出）**。
锁不写进数据库——它是进程内调度状态，重启即清。

三种情况的行为：

| 情况 | 行为 |
|---|---|
| 有其它空闲账号 | 自动切换，请求照常成功 |
| 全部账号都在忙 | 返回 429，说明「muse 只能串行」 |
| 派单失败 | 保证释放锁，不漏锁 |

代码位置：`pool.py` 的 `_lock_for()` / `DispatchResult.release()`。

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

## 八、纯协议路径（0.4.0 起）

### 8.1 结论与证据链

**muse 的传输层可以完全脱离浏览器。** 判据不是"有人声称可行"，而是三份互相独立、
分别用不同语言与不同密码学栈实现、并且能对上同一个协议名与同一个 protobuf 包名的实现：

| # | 来源 | 栈 | 关键证据 |
|---|---|---|---|
| 1 | `lichao199208/muse2api` | Python `cryptography` | `app/muse_engine/protocol_client.py:36-146` 手写 `Noise_XX_25519_AESGCM_SHA256`；msg1 载荷 = protobuf field1 里 32 字节 CSPRNG nonce；实测帧长 66/166/64；`:212-319` bootstrap、`:460-487` HTTP-over-WS；`README.md:26` 默认 `ENGINE_BACKEND=protocol`，依赖仅 `websockets+cryptography+requests`，**无 playwright** |
| 2 | `manishkumar1601/muse-ai-api` | TypeScript `@noble/{curves,ciphers,hashes}` + `protobufjs` | `src/noise/handshake.ts:6-45`（XxInitiator，ee/es/sk 齐全）；`src/hatch/transport.ts:4` 的 protobuf 类型名 `ingress_rev_proxy.NoiseTransportFrame` 与 #1 从抓包里拿到的包名**完全一致**；`src/proto/schemas/*.binpb` 是从前端抽出的 5 个 FileDescriptorSet |
| 3 | `tianrking/muse-workers-relay` | 浏览器 WebCrypto + Cloudflare Worker | `studio/src/lib/noise.mjs:2,153`（WebCrypto 跑 XX/AES-GCM/X25519）；`relay/worker.template.mjs:135-148` 直接反代 `hatch.metaaivm.com/v1/noise`；`relay/worker.test.mjs:45,167-169` 专门测 `?host=evil`/重复 `vm_id` 的目标注入 |

三份实现 + 同一个协议名字面量（出自真实 chunk `1w9duffuzanuc.js`）+ 同一个 proto 包名，
构成"可复现"的交叉验证：不是某个人碰巧跑通，而是握手本身可被第三方独立重建。

本仓库落点（全部为纯 stdlib / 已有依赖，未引入 protobuf 运行时）：

| 模块 | 职责 |
|---|---|
| `muse_bridge/noise.py` | `Noise_XX_25519_AESGCM_SHA256` 握手（`msg1/read_msg2/msg3/split`）、`Symmetric`、`Cipher`(AES-256-GCM, 握手期 AD=`h`、传输期 AD=`b""`)、`hkdf`/`mix_hash`/`mix_key`/`encrypt_and_hash`、`Reassembler` |
| `muse_bridge/proto.py` | 手写 protobuf 线格式编解码：`NoiseTransportFrame`/`ServiceRequest`/`ServiceResponse`/`ServiceFrame`/`ApplicationRequest`/`ApplicationResponse`/`BodyChunk`/`Reset` |
| `muse_bridge/ws_fallback.py` | 裸 RFC6455 实现（`websockets` 未安装时的兜底），含 `build_accept`/掩码/控制帧/代理隧道 |
| `muse_bridge/protocol_client.py` | 三段式 bootstrap → Noise 建连 → 内层 HTTP 请求/响应 → `/chat/subscribe`、`/chat/stream`、`/fs/*`、`/api/ping` |
| `muse_bridge/protocol_engine.py` | 语义层：事件解析（`delta.text_append` 等）、媒体提取、提示词拼装、file_ref 阈值、失败分类（拒绝/额度不换号） |
| `muse_bridge/session_http.py` | cookie 面：`/api/session` 续签与解析、`/api/hatch/vm/wake` + poll-to-RUNNING、`Set-Cookie` 到期换算 |

### 8.2 token 链（三步 + 两个补充端点）

```
1) GET  {site}/api/session?vm_state=live
      -> {status:"assigned", vm_id, vm_state, endpoint_url, vm_type, cvm_eligibility, vm_name}
      （四个字段缺一不可连：status+vm_id+vm_state+endpoint_url）
2) POST {site}/api/hatch/vm/wake   {vm_id, retry_count:0, connect_attempt_id:<uuid4>}
      -> {status:"wake_requested"}      ← 200 只代表受理
      必须回到步骤 1 读到 vm_state==RUNNING 才算唤醒成功
3) POST {site}/api/hatch/token     {vmAddress:<endpoint_url>, vmName}
      -> {token:<EdDSA JWT>, notary_token}
      -> 拼 wss://hatch.metaaivm.com/v1/noise?vm_id&auth_token&app_id=hatch-web&request_id[&notary_token]
         （host/path 是**常量**：2026-11-11 真机唯一握手成功的就是它。endpoint_url 的
           per-VM 子域作 WS 目标时 TLS 直接失败（SSLV3_ALERT_HANDSHAKE_FAILURE）；
           endpoint_url 只作上面的 vmAddress 与排障记录。见 §1.1「端点来源」、§1.6）

补充：
   POST {site}/api/hatch/noise-notary-token {vmName} -> {notaryToken}   # 单独续 notary
   POST {site}/api/hatch/lease-vm           {vmType:"standard"} -> {status,gatewayUrl,vmName}
                                            # 已有 VM 时 403；403 不是 cookie 失效
```

`auth_token` 与 `notary_token` **都由服务端签发**：不需要在前端算，也不是从 cookie 里抄。
这一步是"纯协议可行"的真正原因——凭据是协议里可申请的产物，不是浏览器的私有物。

### 8.3 内层不是私有 RPC，是 HTTP-over-Noise

```
NoiseTransportFrame            ← 分片层：chunk_id / chunk_index / total_chunks / payload
  └─ ServiceRequest / ServiceResponse
       └─ ServiceFrame
            ├─ ApplicationRequest   verb + path + headers + body
            ├─ ApplicationResponse  status + headers + body
            ├─ BodyChunk            大 body 的分块
            └─ Reset                取消/错误
```

包名 `ingress_rev_proxy.NoiseTransportFrame`（证据 #1/#2 一致）。本仓库由 `proto.py` 手写编码，
不引 protobuf 库——这样依赖面不变，也便于离线逐字节对拍。

### 8.4 会话注册与 verb/path ACL

| 动作 | 端点 | 说明 |
|---|---|---|
| 订阅事件 | `POST /chat/subscribe` 空 body `{}`（**请求必须 `end_body=True`**）→ 200 + `application/x-ndjson` 长流，首行 `{"type":"res","payload":{"subscribed":true,...}}` | 2026-11-11 真机；`end_body=False` 会让服务端永不回响应头（§1.6 第 4 条）。本仓库 `protocol_client.py` 的 `subscribe()` / `read_stream()` |
| 提交/读流 | `POST /chat/stream` `{message, node_id, capabilities:[], timezone:"Asia/Shanghai"}` → 200 `{channel,is_thread,message_id,reply_to_message_id,session_id}` | 真机形态（§1.6 第 5 条）；`node_id` 必须等于 register 的 `client_id`。**GET 同一 path 返 403**：ACL 按 verb+path 判定，不是按 path 判定（`api-reference.md:169-182`） |
| 发消息备选 | `/chat/send` | 只接受 `kind:"action"` |
| 节点与能力注册 | `POST /client/register-capabilities` `{client_id}` → 200；`POST /api/nodes/register` 仍按上游证据记录 | 真机只发 `client_id`（字符串 `capabilities` 会 400 `expected a map`，§1.6 第 3 条）；`GET /client/register-capabilities` → 403 `matched route missing from endpoint ACL`。`/api/nodes/register` 本次真机**未调用**，是否需要仍未验证 |
| 保活 | `POST /api/ping {presence:"active"}` | 每 30 秒 |

事件名（`protocol_engine.py` 消费端，来源 `lichao199208` `protocol_engine.py:411-447`）：
`delta.text_append` / `delta.presentation` / `delta.message_done` / `delta.tool_done` /
`delta.placeholder` / `delta.message_removed`。会话内并发不串台：每任务一个 `node_id`，
事件用 `payload.chat_context.device_id` demux。

### 8.5 分片与重组的硬约束

| 约束 | 值 | 违规处理 |
|---|---|---|
| 单帧明文上限 `MAX_PLAINTEXT_FRAME` | **65489** 字节 | 超限不分帧直发即为协议违规 |
| 分块上限 `MAX_CHUNKS` | **256** | `total_chunks` 不在 `1..256` → 抛错 |
| `chunk_index` | 必须 `0 <= idx < total` | 越界抛错 |
| 同一 `chunk_id` 上的 `total_chunks` | 必须恒定 | 中途变化 → 抛错（id 被复用或上游错乱） |
| 重复 `chunk_index` | 不允许 | 抛错 |
| 组装时机 | **收齐才拼**，拼完立即从缓冲删除 | 防止长连接上无限堆积 |

本仓库落点：`noise.py:63-73`（常量）、`noise.py:327-371`（`Reassembler.push/pending`）。

### 8.6 媒体走协议内 FS，不走 HTTP 下载

| 方向 | 序列 | 细节 |
|---|---|---|
| 取成品 | `POST /fs/read {path, offset, len:1MiB}` **循环到 eof** + 总长度校验 | 块大小 `FS_CHUNK_BYTES=1MiB`，总量上限 `FS_MAX_TOTAL_BYTES=128MiB`；`path` 必须以 `sandbox://` 开头（`protocol_client.py:623-658`） |
| 送参考素材 | `POST /fs/write`（分片 append）→ `POST /fs/stat`（size 校验）→ `POST /fs/rename`（临时名 → 正式名）→ 产出 `file_ref`；**任一步失败即 `POST /fs/delete` 清理临时文件（fail closed）** | 上传前缀 `workspace/user/`（`protocol_client.py:659-717`） |
| 内联 or 引用 | 按 `/chat/subscribe` 返回的 `capabilities` 决定（如 `video_file_ref_ingest`） | 本仓库 `protocol_engine.py:452-466`（`should_use_file_ref`） |

两条刻意的排除规则（照搬上游语义）：`workspace/user/` 前缀的路径**不得当成品**（那是上传件），
媒体路径归一见 `protocol_engine.py:371-404`。

### 8.7 硬边界：Confidential VM 撞上就抛错，不回退浏览器

`notary_token` 以 `delegation.` 开头 ⇒ 该账号是 Confidential VM（委托背书）；
或 `vm_type` 含 `confidential`；或 `cvm_eligibility.state` 不是 NOT_ELIGIBLE 系。

**三种情况一律抛错，绝不退回浏览器路径。** 这不是保守，而是这条边界本身是安全属性：

- CVM 的意义在于服务端用 attestation 校验"对端确实是那块机密 VM"。
- 把它绕过去（换一条浏览器通道冒充同一个身份）等于绕过服务端信任根。
- 上面三份独立实现**都没有做** CVM 路径——`lichao199208` `protocol_client.py:294-312,346-351` 是直接拒绝。

本仓库落点：`protocol_client.py:226-237`（`delegation.` 前缀拒绝 + `vm_type` 含 confidential 拒绝 +
握手挑战后再次判定 `:340-345`），全部 `raise ProtocolError(... no browser fallback)`。

### 8.8 已知未验证项（如实列出，不许粉饰）

| # | 项目 | 现状 |
|---|---|---|
| 1 | 真实 muse.ai 前端 chunk 的端点/常量全量清单 | **grep 未跑完**：9 个 chunk（389KB）未完成扫描；`dpl_AU2F73G98WTk6ngM4hDqSqzwsTaf` 的构建漂移风险只在报告里描述过，未逐 chunk 复核 |
| 2 | `POST /api/auth/check` 的请求体与响应字段 | 只见于文档与一处页内 fetch，**本轮未逐字段核对**；§2.2 保留但不作为实现依据 |
| 3 | Noise msg2 里的 attestation / endorsement 链 | **未校验**。`attestation_bundle.proto`、`revocation_list.proto` 已在 schemas 目录中，但未解析、未验证；即"我们信了握手对端的静态密钥，但没有核它的背书" |
| 4 | `muse.ai/files/` 直链的确切 TTL 与路径规则 | 只有社区文档级证据（约 2 天），**无抓包/HEAD 实测**；本仓库 `MUSE_MEDIA_TTL=172800` 是按该文档值取齐，不是实测 |
| 5 | 浏览器侧的活网抓包 | **已完成（2026-11-11 11:0x，§1.5b）**：attach-before-navigate 后 `Network.webSocketCreated` 抓到了真实 WS URL——常量 `hatch.metaaivm.com/v1/noise` + `vm_id/auth_token(muse-secret-edge JWT)/notary_token(endorsement.v1)/app_id=hatch-web/app_version=1/request_id`，与 `_build_ws_url` 完全一致；帧事件也开始出现（密文，未解）。**仍未抓到浏览器自己的 resume 订阅**（重连不在窗口内）。纯协议侧聊天/图片/视频/参考素材生成均已真机打通（§1.6/1.6.1/1.6.1b/1.6.1c） |
| 6 | 本仓库事件流读取的实际链路 | 已落地：`read_stream()` 按行增量消费订阅流的 NDJSON（`delta.text_append.payload.text` → 文本增量，多条件收尾），并已真机跑通（§1.6 第 4/6 条）。0.4.1 起聊天与**图像生成**都已 **opt-in 接进**派单路径（`MUSE_PROTOCOL_DRIVER=1` + 账号有 cookie → `ProtocolDriver`，失败自动回退浏览器并记 `protocol_fallback`；响应头 `X-Muse-Driver` 可看出实际驱动）。参考素材上传 → 生成已真机验收（§1.6.1b），视频生成已真机验收（§1.6.1c）。**断线重连/resume 已离线实现并通过离线测试**（`protocol_client.connect()` 幂等重连、`subscribe(after_stream_seq=…)`、`_resume_subscription()` 两段式降级 + 每回合重连上限/退避；证据等级常量 `RESUME_EVIDENCE`，测试 `tests/test_protocol_resume.py`），但**真机断线恢复已验收**（§1.6.1d：主动掐断 WS → 重连 1 次 → 带 `after_stream_seq=322` 重订阅 200 → 尾部事件与 presentation 照常到达；游标边界仍属推断，去重靠本地）。 |
| 7 | `nodes/register` / `register-capabilities` | `register-capabilities` 已真机验证：只发 `{client_id}` 得 200，字符串 `capabilities` 会 400（§1.6 第 3 条）。`/api/nodes/register` 本次真机**未调用**，它是否必要仍未验证 |
| 8 | muse 是否有纯 HTTP 的额度/usage 端点 | **协议侧额度面已做静态+真机双证据**：Noise 内层 418 方法白名单里没有任何 `usage/quota/billing/plan/credit` 命名（`subscription.callback`/`subscription.redeem_complimentary` 仅注册表、0 调用点；`wallet.get` 是支付方式目录）；`/api/usage`、`/api/quota`、`/api/billing/usage`、`/api/me/usage` 真机 GET 全部 `404 route not found`；连 `/api/session` 也不在内层路由表（只能走外层 HTTP，§8.9 更正）。**机制订正（2026-11-11 11:1x 浏览器抓包）**：用 bridge 打开设置→使用情况面板时，页面**没有发出任何额度取数请求**（Network 事件里只有 `POST /`(settings telemetry)、`/api/hatch/invite`、`/api/checkout/stripe/publishable-key`、`/api/falco`；fetch 钩子同结论），面板却立刻渲染 `免费版 / 每周限额将在 11月17日重置 / 已使用 7% / 额外的使用额度 0%(剩余 10 亿个词元)` ⇒ 额度数字要么随**页面 boot 的 RSC/HTML** 一起下来、要么早已在 WS 订阅的内存状态里，**不是**面板打开时才请求的 HTTP 端点；抓到 `/api/hatch/invite` 响应体只含邀请营销文案（`max_uses/use_count/...`），不含用量数字。结论不变且更硬：**内层没有额度面；面板取数点不是可重放的 HTTP 端点**。详见 `docs/PROTOCOL_QUOTA.md` 与 `muse_bridge/quota_protocol.py` |

### 8.9 当前落地状态（0.4.0 收尾时的准确口径）

**已经落地的**：

| 部分 | 状态 | 证据 |
|---|---|---|
| `noise.py` / `proto.py` | 离线单测 + 与独立实现字节级对拍 | `tests/test_noise.py`、`tests/test_proto.py` |
| `ws_fallback.py` | 代理 TLS 顺序、失败/取消清理及可信证书回环集成测试 | `tests/test_ws_fallback_proxy.py`、`tests/test_ws_fallback_tls_integration.py`；历史表格的 `test_ws_fallback.py` 在当前树中不存在，不再引用为证据 |
| `protocol_client.py`（bootstrap / HTTP-over-Noise / 事件流 / `/fs/*`） | 离线单测（假 WS 帧、假 responses 表） | `tests/test_protocol_client.py` |
| `protocol_generation.py`（生成编排） | 离线单测：未证实端点必抛、`workspace/user/` 回声必挡、直链取件必抛、done 后 12 秒宽限 | `tests/test_protocol_generation.py` |
| `protocol_engine.py`（语义参考层） | 离线单测：提示词措辞、错误分类 | `tests/test_protocol_engine.py` |
| `session_http.py`（cookie 面） | 离线单测（假 httpx client） | `tests/test_session_http.py` |
| **无浏览器运维面（续期 / 唤醒 / 刷新）** | 导入 cookie 后 `refresh`、`keepalive`、`wake` 三条都走纯 HTTP（`session_http`），不构造 `MuseDriver`；只有没 cookie 的账号才回退 CDP。真机（2026-11-11 11:5x）：探针过期导致 `accounts_dispatchable=0`，`POST /admin/accounts/muse1/refresh` 返回 `via=http, refreshed=true, vm_state=assigned`，随后 dispatchable 回到 1 且未起浏览器 | `tests/test_generation_api.py`（含 3 条 refresh HTTP 契约）、`tests/test_admin_auth.py`（无 cookie 仍 503 浏览器分支） |

**没有落地的（不许宣称）**：

1. **纯协议生成已接进 `pool.generate_media`（2026-11-11，真机验收，§1.6.1）**。
   `MUSE_PROTOCOL_DRIVER=1` + 账号有 cookie 时 `capability in ("chat","generation")`
   都走 `ProtocolDriver`；协议生成失败（`ProtocolGenerationError` 中"可换号重试"类）
   释放账号锁后以 `capability="generation-browser"` 回退一次浏览器。
   **额度口径更正（2026-11-11 10:2x）**：`/api/session` **不在 Noise 内层路由表**
   （内层 GET 同样 `404 route not found`，与 `/api/usage` 等四条候选同形），所以
   `protocol_engine.quota()` 原来的内层读法真机必 404。现在
   `HatchProtocolClient.session_quota()` 改走**外层 HTTP**（`session_http.renew_session`，
   带 `hatch_sess` cookie），`ProtocolDriver.quota()` 返回 vm_state/预计到期时间。
   真机实测（`work/canvas_re/probe_session_quota.py`，只读不花额度）：
   `http_status=200, status=assigned, vm_state=RUNNING, estimated_expiry_at=1794537469`
   （外壳续签后的 hatch_vml 到期时间，epoch 秒）；cookie 续期前后名字集合一致。
   **每周用量百分比协议侧不可读**（418 内层白名单无额度命名，面板取数点不在 220 chunk
   语料内，见 `docs/PROTOCOL_QUOTA.md`），不编造、不做"None 当 0%"；浏览器路径
   `/admin/accounts/{a}/quota` 仍走 UI 文本（活网实测 `免费版 / 每周限额将在
   11月17日重置 / 已使用 7%（本轮 11:1x 抓包；随图像/视频消耗从 4% 涨到 7%）`）。
2. **真机验收覆盖到"文本 + 图像生成 + 参考图重绘"**。文本：§1.6（流式/非流式，
   `X-Muse-Driver: protocol`）；图像：§1.6.1（引擎直连、CDP 不可用的 bridge API、
   画布 UI 三层）；参考素材上传 → 生成：§1.6.1b（先失败留档、静态锚点定形状、
   修复后 89,642 B 重绘成功）；**视频生成**：§1.6.1c（1280×720/5.0s/2,963,296 B
   mp4，已花额度真机验收）；**断线 resume**：§1.6.1d（主动掐断 WS → 重连 1 次、
   带 `after_stream_seq=322` 重订阅 200、尾部事件与 presentation 照常到达）。
   仍未验收：Spaces 成功路径（本账号 0 Space；11:5x 试过浏览器「创建空间」入口，
   程序化/真实鼠标点击都没弹出表单/浮层，`modal_count=0`，证据
   `work/canvas_re/space-create-flow-probe.json`）、`/muse/export` 2xx（同上）、
   浏览器侧自己的 resume 订阅参数（§1.5b 只抓到重连本身）。
3. **`protocol_engine.py` 与 `protocol_generation.py` 各有一份 `GenerationResult` /
   `normalize_media_path` / `should_use_file_ref`**。这是本轮多代理并行的已知重复。
   **实机验收走的是 `protocol_engine.py` 的路径**（`generate_image` → `read_stream` →
   `fs_read`），`protocol_driver.generate` 已接它；`protocol_generation.py` 仍是对拍
   参考实现，尚未合并/删除——后续要么让它复用引擎，要么在两份都跑通后收敛成一份，
   见 §8.9。
4. **`workspace/user/` 之外的上传前缀（如未来出现的临时区）未验证**：目前只按
   两份参考实现排除这一个前缀。

### 8.10 跨项目交叉验证（muse2api 系 / muse-cli，2026-11-11 12:2x）

本轮把 GitHub 上能定位到的相关实现 clone 到 `work/`（**只读源码与文档，不运行、
不复制其代码**），逐文件对照。结论先写死：**它们没有推翻本仓的协议结论，只在
少数工程细节上提供了独立证据；边界项一律不采纳。**

| 仓库 | 传输 | 实际覆盖 | 自承的缺口 |
|---|---|---|---|
| `lichao199208/muse2api` | **纯协议**（默认 `ENGINE_BACKEND=protocol`，Noise WS + `chat.stream`；仍保留 `cdp.py` 可选） | 文本/图片/视频/参考素材、暖连接池、协议注册+卡池 | 视频"大规模端到端验收仍有限"、断线恢复有限、confidential/CVM 不静默回退 CDP |
| `czg86389-hub/muse2api`（镜像 `judy-gotv/muse_api`） | **浏览器 CDP**（无头 Chrome 驱动网页端） | 文本/图片/视频、48h 续期、VM 唤醒 | 依赖 Chromium；额度靠页面文本 |
| `www222fff/muse2api` | 可插拔 driver：mock / browser-CDP / **http（占位）** | v0.1 框架，分层与账号池 | 纯协议 driver 只有位置，没有实现 |
| `nikships/muse-cli` | 纯协议（Noise XX + protobuf 信封） | 个人网关的全量方法表（402 条）、订阅/历史/媒体/导出 | 不是 OpenAI 兼容网关；只服务单账号 CLI |

**交叉验证成立、并写进本仓判断的**：

1. **分片重组**：`muse-cli/docs/PROTOCOL.md`（Layers/Framing 段）独立记录
   `NoiseTransportFrame{chunk_id, chunk_index, total_chunks}`、不同 `chunk_id`
   会交错、单块解析必失败（`egress.approvals` ≈123 KB 触发过）。本仓
   `noise.py:342-380` 的 `Reassembler` 是同一规则且有单测——不是我们多虑，是
   线上真会撞到的形态。
2. **WS URL 参数**：`lichao199208/muse2api/docs/noise-protocol.md` 记录
   `vm_id / auth_token / notary_token / app_id=hatch-web / request_id`，与本仓
   `protocol_client.ws_url_from_endpoint()` 完全一致（`notary_token` 有值才带）。
3. **额度不可协议读**：`lichao` 的 `protocol_engine.py` 根本没有 quota 实现，
   管理台额度仍标 CDP（`docs/engine-local.md`）；`muse-cli` 只把 `/api/session`
   当外层 HTTP。与本仓 §8.8 第 8 条同一结论，**"纯协议项目也读不到百分比"**
   这条不再是单点判断。
4. **图片交付格式**：`lichao` 的 `protocol_engine.py:43-62` 明确把上游图片
   `to_png_bytes()` 后再对外交付（"图片结果会本地转为真实 PNG"）。本仓采纳了
   **`b64_json` 这一处**（`media.png_deliverable`，仅 WebP→PNG；Pillow 缺席时
   原样返回），URL 端点仍交付原始 WebP + `X-Muse-Media-Mime`，不改变已验收形状。

**看懂了但不采纳的（附理由）**：

| 项 | 对方做法 | 本仓决定 |
|---|---|---|
| 协议注册 + 卡池年龄验证 | 邮箱物料→OTP→`register-completion`→$1 年龄验证→租工作区→设密 | **不做**。这是 §九第 1 条边界；"零浏览器"是靠越界换来的，不是协议能力更强 |
| 暖 Noise 连接池 | `protocol_pool.py` 按账号复用同一会话，任务用 `node_id` 分流 | **仍不抄现成实现，但要移植设计**。我们的模型是每回合自己订阅（`read_stream` 从 offset 0 读自己的 stream），直接复用会串流；移植方案是"单订阅 + listener 分发 + 每 listener 独立 seq"，见 `docs/DEEP_DIVE_REJECTED.md` §3 |
| `GET /media/raw/workspace/<p>` 裸 daemon 路由 | `muse-cli` 记录媒体可绕过 `fs.read` 直接取 | 不采纳。per-VM 域在边缘会被 TLS reset，需要 Chrome 指纹（`curl_cffi`）才稳；本仓 `fs.read` 已真机验收 |
| `dyi.export` 全量导出 | `muse-cli` 记录 `POST /dyi/export` 返回约 347 MB / 290 文件 zip（约 21 s） | 不做。它是"账号数据导出"，与本仓 canvas 的 `/muse/export`（Space→画布）不是同一件事，不混名 |
| TLS 指纹伪装（`SIGNUP_TLS_IMPERSONATE=chrome`） | 注册 HTTP 走 curl_cffi 的 Chrome 指纹 | **按需**。真机证明当前 hatch 边缘不卡 JA3/JA4（纯协议全通），只在出现"cookie 有效但 403/挑战页"时启用；设计见 `docs/DEEP_DIVE_REJECTED.md` §4 |
| 路由元数据（`requestTimeoutMs` / `maxResponseBytes`） | 按方法表逐路由设超时/上限 | **要抄数据**：`max_bytes` 与 idle timeout 是防炸项，逐路由 timeout 是体验项；映射表与实现要点见 `docs/DEEP_DIVE_REJECTED.md` §5 |

> 四项（协议注册、卡池年龄验证、暖池、TLS 指纹、路由元数据）的**机制层**
> 完整逆向——每步请求体、PTT JWE 封装、卡池 SQL 与失败策略、暖池的 fan-out
> 规则、402 条路由的 timeout/bytes 分布——单独写在
> [`docs/DEEP_DIVE_REJECTED.md`](DEEP_DIVE_REJECTED.md)，本文不重复。

**VM 重启窗口已修（2026-11-11 12:4x）**：`muse-cli` 记录 `/api/session` 可返回
`200 {"status":"unavailable","vm_resolution_issue":{"kind":"retryable"}}`（VM
重启中）。现在 `session_http._parse_session_payload()` 把该形态标成
`SessionUnconfirmed(retryable=True, vm_issue="retryable")`；协议侧
`protocol_client.renew_with_retry()` 只对 `retryable` 做 2 次退避重试
（1s/2s，预算 `SESSION_UNAVAILABLE_RETRIES`），其它非 assigned 仍一次失败。
HTTP 运维面（`/admin/.../refresh`）**不套这层重试**，仍如实返回一次事实。
测试：`tests/test_session_http.py`（4 组 kind 参数）、`tests/test_session_retry.py`
（重试成功/非 retryable 不重试/预算耗尽）。仍留一项：`status=unavailable` 时
没有 `vm_id`，无法用"记住上次 vm_id 再 wake"的更优路径，目前靠等它自己恢复。

### 8.11 逐路由预算 / idle 收尾 / 会话代理（2026-11-11 13:3x–13:5x 落地）

三项都是"从被拒清单里挑出真正值得做的"，机制依据见
[`docs/DEEP_DIVE_REJECTED.md`](DEEP_DIVE_REJECTED.md) §5（路由元数据）。

**1) 逐路由超时 + 响应体上限**（新文件 `muse_bridge/route_limits.py`）

数据来源是 `muse-cli/src/muse_cli/routes.json` 的 402 条路由表（24 条带
`requestTimeoutMs`、18 条带 `maxResponseBytes`），本仓只抄真的会调的：

| 路由 | timeout | max_bytes | 依据 |
|---|---|---|---|
| `POST /chat/subscribe` | 30 s | 不收（订阅流） | 原值 30,000 ms |
| `POST /api/dyi/export` | 3900 s | 32 MiB | 原值 3,900,000 ms（65 min） |
| `GET /fs/read` | 120 s | 32 MiB | 实测最大成品 6.8 MB |
| `GET /fs/thumbnail/*` | 30 s | 614,400 | 原值 614,400 B |
| 其它 | 120 s | 32 MiB | 默认域 |

`request()` 的 `timeout` 默认值从 `120.0` 改成 `None`（未给就查表），
`max_bytes` 未给同样查表；显式传参一律优先，不改变任何既有调用点的语义。

**上限在接收路径上执法，不是读完再判**：`HatchProtocolClient._append_body()`
（`protocol_client.py`，被 `_receiver()` 的 response 帧与 body_chunk 两处调用）
在追加前检查 `len(body)+len(data) > limit`，超了就**停止累积**、置
`ProtocolResponse.limit_exceeded`、把该 stream 标记结束。为什么不在接收循环里抛异常：
循环是整条连接的公共设施，抛一次会把"一个超大响应"升级成"整条连接断线"，
连累同一连接上的其它 stream。判定与 `ProtocolError("…响应超过上限…")` 由
`request()` 统一给出，并在 `finally` 里清掉 `_stream_limits` 登记。
订阅流（`end_body=False`）不登记上限——它的 body 设计上持续增长。

**2) idle 收尾**（`read_stream(idle_timeout=...)` + `MUSE_STREAM_IDLE_TIMEOUT`）

`timeout` 是"整回合多久算超时"，idle 是"多久没动静算死"，两把尺子。判定只认
**body 有没有长新字节**：主循环每轮比较 `len(response.body)`，变了就刷新
`last_progress`；`_wait_update` 的 0.5 s 正常空转不算活动。到点 `return`
（与 `timeout` 一致，"没等到"由上层裁决），置 `_idle_timeout_hit` 并打一条
WARNING（含已收字节数）。默认 `None`/0 = 关闭，行为与旧版完全一致。

驱动侧接线：`ProtocolEngine._stream_extras()` 从 env 读值，并且**按签名探测**
（`accepts_kwarg`）后才传——`self.client` 是鸭子契约，离线测试的假件签名更窄，
多传一个不认识的 kwarg 会把"打开开关"变成"所有生成 TypeError"。
测试 `tests/test_engine_stream_extras.py` 10 条钉住这条契约。

**3) 会话面代理（真 bug，不是优化）**

`/admin/accounts/{a}/refresh` 报 `/api/session 网络请求失败（ConnectError）`，
而同一时刻本机 7890 经 `muse.ai` 是 200。根因：`session_http._OwnClient` 建
`httpx.AsyncClient` 时**从来不传 proxy**，直连被墙时这条链路必死；而协议 WS
那条路早就有 `MUSE_PROTOCOL_PROXY`。修法：

* `env.SESSION_PROXY` = `MUSE_SESSION_PROXY`，未配时回落到 `MUSE_PROTOCOL_PROXY`；
* `renew_session(..., proxy=None)` / `wake_vm(..., proxy=None)` 接受显式覆盖；
* `_OwnClient` 只在**自建** client 时套代理（`proxy` 键整个省掉而不是传 None），
  注入了 client 就不碰——那是调用方的连接池与出口。

真机证据（2026-11-11 13:4x，同一账号连续两次）：

```text
修复前  POST /admin/accounts/muse1/refresh → 502
        {"error":"session_error","note":"/api/session 网络请求失败（ConnectError）…"}
修复后  POST /admin/accounts/muse1/refresh → 200 / 2.07 s
        {"account":"muse1","ok":true,"refreshed":true,"via":"http","vm_state":"assigned"}
        /healthz → accounts_dispatchable: 0 → 1
```

顺带解释了一个容易误判的现象：`accounts_dispatchable=0` 不等于账号坏了。
它由 `last_probe_ok_at` 是否落在 `PROBE_TTL_SECONDS=15*60` 内决定；重启 bridge
后若上次探测已过期，必然先看到 0，需要一次成功的 refresh 才会回到 1。

真机回归（`MUSE_STREAM_IDLE_TIMEOUT=60` 打开状态）：

```text
POST /v1/chat/completions  "用一句话说明1+1等于多少" → 200 / 11.8 s / "1+1等于2。" / usage 14
POST /v1/chat/completions  "回答两个字：收到"        → 200 / 11.7 s / "收到"
```

测试：`tests/test_route_limits.py`（7 条，含超限抛错与登记清理）、
`tests/test_stream_idle.py`（5 条，含"idle 不吞已到事件"与"每回合重置"）、
`tests/test_session_proxy.py`（4 条，含注入 client 不被套代理）。

### 8.12 暖 Noise 池（`MUSE_WARM_POOL`，2026-11-11 14:0x–14:2x）

**能这么做的前提，是先读懂了既有代码**：`HatchProtocolClient.read_stream(prompt=...)`
本来就支持跨回合复用同一条订阅——`protocol_client.py:1737-1742` 的 `reused`
分支先 `_seek_to_end()` 把游标推到本回合起点（跳过上一回合没消费完的尾巴），
`_resume_subscription()` 负责空闲期断线后的恢复。所以这一层**不需要**自己管
游标、seq 去重或订阅重建。

也不需要参考实现的 fan-out：`work/muse2api_pure/app/muse_engine/protocol_pool.py`
要按 `chat_context.device_id` 把一条共享订阅的事件分发给多个 listener，是因为它
允许同号并发；本仓的 `pool.dispatch()` 用账号级锁（`pool.py:217-224`，非阻塞抢锁，
抢不到就当"这个号忙"换下一个），同号天然串行，单订阅单消费者，游标独占。

于是这一层只做三件既有代码不管的事（`muse_bridge/protocol_hub.py`）：

1. **别每次派单都新建 client**。`_build_driver()` 原本每回合
   `ProtocolDriver(...)` → `HatchProtocolClient(...)` → `connect()`，一次 WS 升级
   + Noise XX 握手。复用后只有首回合付这个成本。
2. **验活**（`usable()`）：ws 在、没在关闭、握手完成过、接收任务在跑、
   订阅若存在且未被服务端 end、`_connection_lost()` 为假——少一条就重建。
3. **引用计数 + 空闲回收**：回合结束只减引用（断了就白暖），空闲超
   `MUSE_WARM_POOL_TTL`（默认 300 s）才真关；`lifespan` 退出时 `close_all()` 兜底。

键 = `(cookie 指纹, proxy, model)`，任一变化就真换 client（换身份了还复用等于
拿旧会话打新账号）。同身份但验活失败时**不换 client**，而是 `client.connect()`
重连——它开头就 `_drop_connection()`（`protocol_client.py:857-865`），能续用
token/cookie 续期路径。

**真机踩到的坑（第一版就是这么错的）**：把"订阅必须存在"写进了验活。但
`connect()` 只做 WS 升级 + 握手 + 前奏，此时 `_sub_stream_id is None`、
`responses` 为空，订阅要到 `read_stream(prompt=...)` 才建。结果刚连好的连接全被
判死，暖池 100% 不命中，每回合还多烧一次重连。探针数据（`work/probe_warm_pool.py`）：

```text
修复前  cold acquire: 4.40s reused=False usable=False   ← 连好即判死
        warm acquire: 2.781s reused=False               ← 每回合都在重连
修复后  cold acquire: 4.77s reused=True  usable=True
        warm acquire: 0.000s reused=True same=True      ← 命中即零成本
```

同进程三回合端到端（`work/probe_warm_e2e.py`，走 `pool.dispatch` →
`driver.connect` → `collect_reply`，排除 server 层噪声）：

```text
#1 dispatch=6.83s connect=0.00s reply=4.15s   warm={'accounts':['muse1'],'refs':{'muse1':1}}
#2 dispatch=0.00s connect=0.00s reply=2.93s   warm={'accounts':['muse1'],'refs':{'muse1':1}}
#3 dispatch=0.00s connect=0.00s reply=3.04s   warm={'accounts':['muse1'],'refs':{'muse1':1}}
```

HTTP 面（重启后连发三次）`200 / 8.34s`、`200 / 2.78s`、`200 / 5.83s`——**这一组
不作收益证据**：server 层排队、跨进程首连、上游推理耗时都混在里面，波动大
（实测见过 34 s / 11 s / 3.6 s / 6.1 s）。握手省了多少只看上面那组进程内数据。

另一个真 bug（同一轮抓的）：验活失败走重连时，第一版在锁内 `refs += 1` 却在
失败分支只做"减回去"，重连成功也不归一，两回合就能把 `refs` 抬到 2 而永不下降
（探针 `stats` 直接暴露 `refs: {'muse1': 2}`）。现在重连成功后引用**归一为 1**：
死连接上不可能还有回合持有，旧计数是脏账。

开关默认关（`MUSE_WARM_POOL` 不配 = 旧行为一字不差），关掉时 hub 的函数一个都
不会调。测试 `tests/test_protocol_hub.py`（23 条）+ `tests/test_protocol_hub_wiring.py`
（4 条）：验活五条逐条打、换身份真换 client、重复 release 不减别人引用、重连失败
摘槽并关连接、`test_connected_without_subscription_is_usable` 专门钉住上面那个坑。

**空闲回收必须与请求无关**（2026-11-11 14:3x 补的缺口）：`release()` 里确实会调
`evict_idle()`，但那等于"只有下一个请求到来时才回收"。进程彻底空闲时（夜间、低峰）
连接会一直挂着——TTL 形同虚设，还占着服务端会话。补 `sweep_forever()` 后台时钟，
`interval` 默认 60 s，由 `app.py` 的 `lifespan` 在开关打开时创建、退出时取消。
它自己吞异常继续下一轮（清扫失败不能让回收永久失效），只对 `CancelledError` 放行。

**可观测性**：`/healthz` 新增 `warm_pool` 字段（`{enabled, accounts, refs}`）。
暖池的失败形态是"悄悄没命中"，从响应时间上看不出来，只有摊开
`warm_accounts` 才能现场判断。真机验证：

```text
GET /healthz → "warm_pool": {"enabled": true, "accounts": [], "refs": {}}
POST /v1/chat/completions → 200 / 12.16 s
GET /healthz → "warm_pool": {"enabled": true, "accounts": ["muse1"], "refs": {"muse1": 0}}
```

`refs` 回到 0 而 `accounts` 仍列出该账号 —— 正是设计意图：回合结束只减引用、
连接留着暖。

**生命周期加固（2026-11-11 15:0x，路线图 P0-A）**：上面那版还有四个会在真机
上安静出错的地方，逐个补掉：

1. **初次连接失败/取消没有清理**。`acquire()` 先把槽位写进 `_SLOTS` 再
   `await client.connect()`；失败（或任务被取消）后槽位和引用都留着——下一个
   回合先付一次验活失败再重连。现在用 `except BaseException` 覆盖取消路径，
   失败时摘槽 + 关连接后原样抛出，且摘槽前做**身份比较**
   （`slot.client is client`），不会误删失败期间别人建好的新槽位。
2. **身份指纹只取前 12 字符**。两套 `hatch_sess` 前缀相同、尾部不同时会被判成
   同一身份，语义上等于"拿旧会话打新账号"。改为对完整 name+value 集合求 sha256
   （明文不落盘、不进日志）。
3. **release 无法校验归属**。晚归还的旧租用会把新一代的引用减掉。新增
   `WarmSlot.lease_id` + 全局单调发号器 `_next_lease()`，
   `release(account, lease=None) -> bool`；号不匹配记一条 WARNING 并返回 False。
   **发号器必须是全局单调、不随槽位销毁复位**：槽位被 `drop` 后重建，若从 1
   重新编号，旧租用就会撞上新租用（这条是写测试时才暴露出来的，值得单独记住）。
   `release(account)` 无参形态保持兼容，`protocol_driver.py` 不必改。
4. **并发 acquire 只告警**。同账号第二次 `acquire()` 过去是告警后**共享同一条
   订阅**——两个回合抢同一个游标是数据正确性问题。现在抛 `WarmPoolBusy`。
   账号锁本来就保证同号串行，走到这里说明有人绕过了锁，所以选择"明确失败"
   而不是"等待"（等待会把一次锁泄漏变成一次静默超时）。

测试随之更新：`tests/test_protocol_hub.py` 30 条（含
`test_concurrent_acquire_is_rejected_not_shared`、
`test_cookie_tail_change_is_a_different_identity`、
`test_initial_connect_failure_leaves_no_slot`、
`test_initial_connect_cancellation_leaves_no_slot`、
`test_stale_cleanup_does_not_remove_a_newer_slot`、
`test_stale_lease_release_does_not_touch_new_slot`）+
`tests/test_protocol_hub_wiring.py` 4 条。注意旧测试里"连续两次 acquire 验复用"
的写法已不成立，现在必须先 `release()` 再 acquire——这不是测试将就实现，而是
契约变了：同一时刻一个账号只允许一个回合持有。

**协议回退必须留痕（2026-11-11 15:5x 排障产物）**：协议 WS 失败后 `pool` 会回退
浏览器驱动，而回退原因只写在内存里的 `fallback_note`；调用方最终拿到的是
"页面 45 秒没反应"的 504。这一轮就是靠直连探针才把根因（
`Noise WebSocket 连接失败（ConnectionResetError）`）挖出来的。现在回退处加一条
WARNING 日志（只含异常类型与消息，不含 cookie），并计入
`/admin/observability` 的 `protocol_fallback` 计数与 `browser_driver_built`
（协议优先静默失效时能一眼看见）。


---

### 8.13 可观测性与媒体清理（2026-11-11 15:1x–15:4x，路线图 P2-C）

**为什么需要**：暖池和聊天媒体的失败形态都是"看起来成功了"。复用没命中不报错、
只是变慢；附件取件失败被上层包成一条 502，从总耗时里看不出卡在握手、生成、取件
还是下载。**没有一个能分开计数的东西，排障就只剩猜**——这一轮就吃过一次。

新模块 `muse_bridge/observability.py`：线程安全的计数（`bump()`）与分阶段耗时
（`timed()` 上下文管理器，异常路径同样计时）+ `snapshot()`。边界写死在模块头：

- 纯进程内，进程重启归零；**不是持久化指标库，也没有导出协议**。要接
  Prometheus 应该另外做，而不是把这个 dict 硬塞成 exporter。
- 只记**计数与耗时**，不记 cookie、Bearer key、提示词正文或上游原始报文。
- 读它要 admin key。

**面怎么分**：

| 面 | 端点 | 放什么 | 为什么 |
| --- | --- | --- | --- |
| 公开 | `GET /healthz` | `warm_pool: {enabled, accounts, refs, leased_accounts}` | 只放**池内状态**；`leased_accounts` 是按 refs 算出的真实派生量。匿名探针不该看到内部失败率 |
| 管理 | `GET /admin/observability` | `{counters, timing}` | 分阶段计数与耗时（平均 + 最大） |

已埋点的阶段：`warm_acquire`（驱动构造）、`chat_media_fetch` /
`chat_media_store`、`media_download`。已埋点的计数：
`protocol_driver_built` / `browser_driver_built`（**协议优先是否真的生效**——聊天
却拿到浏览器驱动时就会涨，这是"静默失效"最直接的信号）、`protocol_fallback`、
`chat_media_fetch_failed` / `chat_media_stored`、`media_download_failed`、
`media_sweep_runs` / `media_sweep_removed`。

**媒体清理：一个真缺口**。`MediaStore.sweep()` 早就写好（按 mtime 删超龄成品 +
回收崩溃残留的 `.part`，且只删 token 形态的文件），但**全仓只有测试调它**——
生产路径零调用点。也就是说 `MEDIA_TTL_SECONDS`（默认 2 天）和 `MAX_MEDIA_BYTES`
都只在"写入的那一刻"生效：单个文件不会超限，但磁盘会随时间无限增长。补
`media.sweep_forever()`（新常量 `MUSE_MEDIA_SWEEP_INTERVAL`，默认 3600 s），由
lifespan 无条件建任务（媒体是必用功能，不像暖池有开关），退出时一并取消。
两个细节：清扫走 `asyncio.to_thread`（目录遍历是阻塞 IO）；`MediaStore()` 的构造
放在 `try` **里面**——它会 `mkdir`，磁盘满或权限不对时会抛，放在循环外会让任务
当场死掉且没有日志，表现成"清理从来没跑过"，与"跑了但没东西可删"完全无法区分。

`media_sweep_runs` 和 `media_sweep_removed` 故意分开记：只有前者能证明清扫**活着**。

测试 `tests/test_observability.py` 10 条：快照初值、`timed` 在异常路径也计数、
admin 面读得到计数、配了 admin key 后普通 key 读不到（401）、`/healthz` 不带
内部计数、`leased_accounts` 随租用变 1/0、清扫真跑真删（mtime 要超过
`MEDIA_TTL_SECONDS` 才会被删）、清扫异常不致命、lifespan 建并收掉清扫任务。

> 注意 `require_admin` 在**没配** `MUSE_BRIDGE_ADMIN_KEYS` 时会**放行**（既有降级
> 设计，`/healthz` 的 `admin_key_separated` 会如实报 false）。所以测试只钉"配了
> admin key 之后普通 key 读不到"，不假定默认部署是安全的。要真正分隔管理面，
> 必须部署时配上 `MUSE_BRIDGE_ADMIN_KEYS`。

---

## 九、明确不做的事

1. **不做注册**。muse 需要 Meta 账号 OAuth，本项目完全不介入。
2. **不伪造 notary token、不做 Confidential VM 的 attestation 绕过**。CVM 的委托背书（`delegation.` 前缀）是服务端信任根的一部分，绕它就是伪造身份；上面三份独立实现都没做，本项目也**不做，并且不回退浏览器**（§8.7）。
3. **不复刻 Noise 加密以绕过计费/配额**。§八 复刻的是传输层，为了让工具能带你自己的账号跑通；用它去伪造会话身份、多开额度、规避服务端限频，属于另一件事，不做。
4. **不做指纹伪装**。不修改 UA、不注入 stealth 脚本。（`protocol_client.py:62-64` 的 UA 常量是前端原样值的照抄，用于协议握手的一致性，不是伪装成别人。）
5. **不共享账号**。

理由同上一个项目：这些会让工具从"协议适配"变成"绕过服务方控制"。

---

作者：**君莫笑**
**永恒之兰**
