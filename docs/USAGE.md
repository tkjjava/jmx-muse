# 使用说明

> 作者：**君莫笑** · **永恒之兰**

muse-bridge 的前提是**一个运行中且已登录 muse.ai 的 Chrome**。按顺序做。

---

## 聊天中的图片与视频附件

协议聊天返回媒体时，bridge 会取回并保存成品，继续返回普通文本；附件放在扩展字段
`choices[0].message.media`（非流式）或 `choices[0].delta.media`（SSE）。
流式客户端应把各帧中的 `media` 数组合并。纯文本回复不增加这个字段。

```json
{
  "role": "assistant",
  "content": "生成结果",
  "media": [
    {
      "url": "/v1/media/<token>",
      "mime": "image/png",
      "kind": "image",
      "size": 12345
    }
  ]
}
```

下载附件时继续发送 `Authorization: Bearer <bridge-key>`；`token` 本身不代替鉴权。
`MUSE_PUBLIC_BASE` 已设置时 URL 带该前缀，否则客户端以 bridge 地址解析相对路径。
普通 Markdown 图片标签通常无法附加 Bearer 头，客户端应先鉴权下载，再按
`media[].mime` 创建 blob URL 展示；下载端点统一返回 `application/octet-stream`
和 `X-Content-Type-Options: nosniff`，不要依赖下载响应的 Content-Type 推断图片类型。
外部 relay 需要保留 `media` 扩展字段；本轮只验证 bridge 的离线接口与实际文件保存，
未验证特定 relay 或新一次上游媒体生成。浏览器驱动仍维持已有文本提取行为。

## 一、准备

### 1.1 安装

```bash
git clone <本仓库地址> muse-bridge
cd muse-bridge
python -m pip install -r requirements.txt
python -m playwright install chromium
```

> `playwright install chromium` 会下载浏览器内核。
> 注意：本项目**不复用**这个内核，运行时连的是你自己的 Chrome。
> 这一步只是装 playwright 的驱动能力。

验证：

```bash
python -c "import playwright, httpx, fastapi; print('ok')"
```

### 1.2 生成加密密钥

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

设置（**保存好，丢了库里的 profile 信息就解不开**）：

```powershell
$env:MUSE_BRIDGE_KEY = "你生成的密钥"
```

```bash
export MUSE_BRIDGE_KEY="你生成的密钥"
```

---

## 二、启动带调试端口的 Chrome

这一步是整个流程的关键。**必须用独立 profile**，否则 Chrome 会因为已运行实例而忽略调试端口参数。

### Windows

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\muse-profile"
```

### Linux

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/muse-profile"
```

### macOS

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/muse-profile"
```

### 验证端口通了

```bash
curl http://127.0.0.1:9222/json/version
```

应该返回类似：

```json
{
  "Browser": "Chrome/131.0.0.0",
  "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/..."
}
```

返回不了说明 Chrome 没带对参数启动，或者 9222 被占。

---

## 三、在这个 Chrome 里登录

1. 用上面启动的 Chrome 打开 <https://muse.ai>
2. 正常登录（Meta 账号 OAuth）
3. **保持标签页打开**

登录态存在那个 profile 里，之后重启 Chrome 不用再登。

---

## 四、启动 muse-bridge

先跑自检，确认五件事都齐备：

```bash
python -m muse_bridge.doctor
```

输出示例：

```
muse-bridge 0.1.0  ·  君莫笑  ·  永恒之兰
CDP：http://127.0.0.1:9222
站点：https://muse.ai

  ✓ 加密密钥 — 加解密往返正常
  ✓ 数据库读写 — 写入、加密、读回均正常
  ✓ playwright 依赖 — 已安装 v1.44.0
  ✗ Chrome 调试端口 — 连不上 http://127.0.0.1:9222（ConnectError）
      提示：用 --remote-debugging-port=9222 和独立 --user-data-dir 启动 Chrome
```

五项检查对应五个前置条件，缺哪项修哪项：

| 检查项 | 缺了会怎样 |
|---|---|
| 加密密钥 | 服务起不来 |
| 数据库读写 | 账号存不进去 |
| playwright 依赖 | 连不上浏览器 |
| Chrome 调试端口 | 连不上浏览器 |
| muse.ai 登录态 | 派单全部失败 |

**前四项都通过才会检查第五项**——否则报错会层层叠加，看不出真正的原因。

自检通过后启动服务：

```bash
python -m muse_bridge
```

```
INFO:     Uvicorn running on http://127.0.0.1:8788 (Press CTRL+C to quit)
```

健康检查：

```bash
curl http://127.0.0.1:8788/healthz
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "author": "君莫笑",
  "mark": "永恒之兰",
  "accounts_total": 0,
  "accounts_dispatchable": 0,
  "cdp_url": "http://127.0.0.1:9222"
}
```

---

## 五、连接账号（attach）

打开 <http://127.0.0.1:8788/console>，填账号标签，点「连接并入库」。

或者用命令行：

```bash
curl -X POST http://127.0.0.1:8788/admin/attach \
  -H "Content-Type: application/json" \
  -d '{"account": "muse-main", "label": "主号"}'
```

成功：

```json
{
  "ok": true,
  "account": "muse-main",
  "state": "ready",
  "cdp_url": "http://127.0.0.1:9222",
  "url": "https://muse.ai/chat"
}
```

失败：

| 返回 | 原因 | 解决 |
|---|---|---|
| `browser_unavailable` | 连不上 9222 | 检查 Chrome 是否带 `--remote-debugging-port` 启动 |
| `not_logged_in` | 页面未登录 | 在那个 Chrome 里登录 muse.ai |
| `没有 muse.ai 的标签页` | 没打开站点 | 打开 muse.ai 后重试 |

---

## 六、调用

### 6.1 非流式

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-1",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### 6.2 流式

```bash
curl -N http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "muse-1",
    "messages": [{"role": "user", "content": "写一首四行诗"}],
    "stream": true
  }'
```

首次调用会慢一些——要填输入框、点发送、等前端渲染。

### 6.3 Python 客户端

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8788/v1",
    api_key="not-needed",     # 没配 MUSE_BRIDGE_API_KEYS 时随意
)

response = client.chat.completions.create(
    model="muse-1",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

### 6.4 模型别名

`muse-1`、`gpt-4o`、`claude-opus-4`、`gemini-pro` 等**全部等价**——
muse 只有一个 agent，都路由到 `muse-spark-1.3`。

---

## 七、日常维护

### 7.1 看状态

```bash
curl http://127.0.0.1:8788/admin/accounts
```

### 7.2 续期会话（每 30 小时左右做一次）

```bash
curl -X POST http://127.0.0.1:8788/admin/accounts/muse-main/refresh
```

`hatch_vml` 只有 48 小时，`refresh` 会打一次 `/api/session` 续期。

### 7.3 唤醒 VM

```bash
curl -X POST http://127.0.0.1:8788/admin/accounts/muse-main/wake
```

### 7.4 删除账号

```bash
curl -X DELETE http://127.0.0.1:8788/admin/accounts/muse-main
```

---

## 八、故障排查

### 8.1 连不上浏览器

```
浏览器连不上 CDP http://127.0.0.1:9222
```

按顺序查：

1. Chrome 是否带 `--remote-debugging-port=9222` 启动？
2. 是否用了 `--user-data-dir` 指定独立 profile？（不指定的话，已运行的 Chrome 会吞掉参数）
3. 端口是否被占：`netstat -ano | findstr 9222`
4. 手动测：`curl http://127.0.0.1:9222/json/version`

### 8.2 找不到输入框

```
找不到输入框，页面可能改版或未加载完成
```

原因：`selectors.py` 里的选择器全部失效。

解决：

1. 在 Chrome 里打开 muse.ai，按 F12
2. 用元素检查器点一下输入框，看到它的实际选择器
3. 加到 `selectors.py` 的 `COMPOSER_INPUTS` **最前面**
4. 重启服务

### 8.3 回复取不到 / 一直超时

```
180 秒内没有产生新回答
```

排查：

1. 在浏览器里手动问一句，看是否正常
2. 如果手动能问但工具不能，多半是 `ASSISTANT_MESSAGES` 选择器失效——同 8.2 的方法修
3. 如果是 VM 休眠，先调 `/wake`

### 8.4 403 错误

```
上游 403
```

最常见原因：**出口 IP 不一致**。

muse 部署在 Meta 的 `fwdproxy` 后，对出口 IP 敏感。
如果 muse-bridge 跑在服务器上，而你在本地浏览器登录，IP 不一致就会 403。

**解决：Chrome 与 muse-bridge 必须在同一台机器。**

### 8.5 流式输出不连续

这是正常的。DOM 轮询的粒度是 250ms，前端渲染也可能分批。
看到的效果是"一小段一小段地出现"，而不是逐字符。

想调节判定灵敏度：

```bash
export MUSE_QUIET_MS=300    # 更灵敏，但可能提前截断
export MUSE_QUIET_MS=1200   # 更保守，但收尾更慢
```

---

## 九、可选配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `MUSE_BRIDGE_API_KEYS` | 空 | 客户端密钥，逗号分隔 |
| `MUSE_BRIDGE_DB` | `./data/muse.db` | 账号库 |
| `MUSE_BRIDGE_PORT` | `8788` | 端口 |
| `MUSE_BRIDGE_HOST` | `127.0.0.1` | 监听地址 |
| `MUSE_CDP_URL` | `http://127.0.0.1:9222` | Chrome 调试端点 |
| `MUSE_HEADLESS` | `false` | 无头模式（不建议） |
| `MUSE_QUIET_MS` | `600` | 静默窗口（毫秒） |
| `MUSE_REPLY_TIMEOUT` | `180` | 等回复超时（秒） |

---

## 十、生成端点与纯协议路径（0.4.0）

### 10.1 图像 / 视频生成

| 端点 | 行为 |
|---|---|
| `POST /v1/images/generations` | 默认同步；`async_mode=true` 时立即回任务 |
| `POST /v1/images/edits` | OpenAI 编辑端点的 **JSON 变体**（`images` 至少 1 张） |
| `POST /v1/images/tasks` | 入队，立即回 task_id（可带 `Idempotency-Key`） |
| `POST /v1/videos`、`POST /v1/videos/generations` | **总是异步**：上游 600 秒的生成不可能挂在 HTTP 里等 |
| `GET /v1/images/tasks/{id}`、`/v1/videos/{id}` | 轮询任务 |
| `GET /v1/media/{token}` | 成品下载（**必须带 key**，支持 Range） |
| `DELETE /v1/media/{token}` | 删除一份成品（同一把 key） |

```bash
curl -X POST http://127.0.0.1:8788/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"prompt":"雨夜霓虹街道，电影感","size":"16:9"}'

# 参考图（data URL / http(s) / 服务端路径都行）
curl -X POST http://127.0.0.1:8788/v1/images/edits \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"prompt":"把天空换成黄昏","images":["refer.png"]}'
```

**multipart 不支持**：`/v1/images/edits` 只收 JSON。用 multipart 会拿到一条
点名原因的 400（不是 422），因为本服务没装 `python-multipart`，也不为这一个
端点多一个运行时依赖。

### 10.2 fal.ai 队列兼容

给 fal 客户端用的薄壳（提交 → 轮询 → 取结果）：

| 端点 | 行为 |
|---|---|
| `GET /v1/fal/models` | 两个模型：`muse-image` / `muse-video` |
| `POST /v1/fal/muse/image`、`POST /v1/fal/muse/video` | 回 `request_id` + `status=IN_QUEUE` |
| `GET /v1/fal/muse/requests/{id}/status` | `IN_QUEUE` / `IN_PROGRESS` / `COMPLETED` / `ERROR` |
| `GET /v1/fal/muse/requests/{id}` | 没跑完回 **202**；跑完回 `images` 或 `video` |

`duration` / `aspect_ratio` / `seed` 会编进提示词文本——已证实的生成载荷里
没有这些结构化字段（见 `docs/PROTOCOL.md` 第九章）。

### 10.3 运维端点

| 端点 | 行为 |
|---|---|
| `GET /admin/tasks`、`GET /admin/tasks/{id}` | 任务清单/单条（默认不外发账号标签） |
| `POST /admin/tasks/clear?keep=N` | 只清**终态**任务，`keep` 保留最新 N 条已结束任务 |
| `GET /admin/accounts/{a}/quota` | 读额度面板（**必须浏览器**：muse.ai 没有额度 REST 接口） |
| `POST /admin/accounts/{a}/keepalive` | 续期 + 唤醒一次跑完，返回值带 `via` |
| `POST /admin/accounts/{a}/cookies` | 导入 cookie（纯协议路径凭据），只回 cookie 名字 |
| `GET /admin/observability` | 分阶段计数与耗时（见 10.5） |

### 10.5 可观测性与媒体清理

`GET /healthz` 的 `warm_pool` 只放**池内状态**（`enabled` / `accounts` / `refs` /
`leased_accounts`），不放失败率——匿名探针不该看到内部错误分布。

要看"到底卡在哪一段"，用管理面：

```bash
curl -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8788/admin/observability
```

```json
{
  "counters": {
    "protocol_driver_built": 12,
    "browser_driver_built": 0,
    "protocol_fallback": 1,
    "chat_media_fetch_failed": 0,
    "chat_history_recovered": 0,
    "chat_history_recovery_missed": 0,
    "chat_history_recovery_failed": 0,
    "chat_done_tail_emitted": 0,
    "media_sweep_runs": 3,
    "media_sweep_removed": 41
  },
  "timing": {
    "count": { "warm_acquire": 12, "chat_media_fetch": 4 },
    "average_seconds": { "warm_acquire": 0.0003, "chat_media_fetch": 1.82 },
    "max_seconds": { "warm_acquire": 0.004, "chat_media_fetch": 3.1 }
  }
}
```

几个字段的实际用途：

- `browser_driver_built` 在**聊天**路径上大于 0：说明"协议优先"静默失效了——要么
  `MUSE_PROTOCOL_DRIVER` 没开，要么该账号没导入 cookie。这是最容易悄悄退化的地方。
- `protocol_fallback` 大于 0：协议 WS 失败并回退过浏览器。回退原因同时会打一条
  WARNING 日志（只含异常类型与消息，不含 cookie）。
- `media_sweep_runs` 与 `media_sweep_removed` 分开看：前者证明后台清理**活着**，
  后者是实际回收量。只有后者的话，"没删东西"和"没在跑"分不清。
- `chat_history_recovered`：断线回合从已提交历史取回了完整正文（恢复成功）。
  `chat_history_recovery_missed` 是"查了历史但对不上严格前缀"，`chat_history_recovery_failed`
  是历史请求本身失败——两者都会让该回合按"连续性无法确认"报错，不是静默失败。
- `chat_done_tail_emitted`：驱动把 `done` 里的完整正文补发到 app 层。
  transcript-only 回合与断线恢复都会 +1；如果它很大而用户仍看到截断回答，
  说明问题在更上层的事件消费。

**媒体清理**：`data/media/` 下的成品按 `MUSE_MEDIA_TTL`（默认 172800 秒 = 2 天）
过期，后台每小时清一次（`MUSE_MEDIA_SWEEP_INTERVAL`，默认 3600 秒）。清理只删
token 形态的文件和崩溃残留的 `.part`，不会碰目录里其他东西；单文件上限是
`MUSE_MAX_MEDIA_BYTES`（默认 256 MiB）。

### 10.4 纯协议路径：现在到哪一步了

这是本版的主体，也是**最容易被过度宣称**的部分。准确的现状：

1. **协议层已完成并有离线测试**：`noise.py`（Noise_XX 握手）、`proto.py`
   （手写 protobuf wire）、`protocol_client.py`（三阶段 bootstrap +
   HTTP-over-Noise + NDJSON 流 + `/fs/*` 分块读写）、`protocol_engine.py` /
   `protocol_generation.py`（生成编排）。本仓库**从未连接真实 muse.ai**：
   结论全部标了验证等级，没证实的能力显式抛 `UNVERIFIED`，不假装能跑。
2. **实际生成仍然走浏览器**。账号库里的默认凭据是「Chrome profile + CDP
   端点」，纯协议生成尚未接进 `pool.generate_media`——接进去需要真机对拍
   （尤其成品回传路径与配额扣减时点），没有账号时接进去就是拿用户当试验品。
3. **已经能无浏览器做的一件事**：导入 cookie 后，`keepalive` 走纯 HTTP
   （`session_http.renew_session` + `wake_vm`），完全不启动 Chrome：

```bash
# 从浏览器开发者工具复制 hatch_sess / hatch_vml 等 cookie（值不要外传）
curl -X POST http://127.0.0.1:8788/admin/accounts/muse-main/cookies \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"cookies":{"hatch_sess":"...","hatch_vml":"..."}}'

curl -X POST http://127.0.0.1:8788/admin/accounts/muse-main/keepalive \
  -H "Authorization: Bearer $KEY"
# → {"via":"http","refreshed":true,"woken":true,...}
```

cookie 落库走 `MUSE_BRIDGE_KEY` 加密列；响应只回 cookie 名字，不回值。

---

## 十一、安全提示

1. **Chrome 的调试端口不要暴露到公网**。`--remote-debugging-port` 等于把浏览器完全开放。
   确认它只监听 `127.0.0.1`（默认如此）。
2. **`muse-profile` 目录包含完整登录态**，等同于账号密码。不要分享、不要提交到 git。
3. **`MUSE_BRIDGE_KEY` 拿到就能解密账号库**，注意环境变量可见性。
4. **默认只监听 127.0.0.1**，别改成 `0.0.0.0`。

---

作者：**君莫笑**
**永恒之兰**
