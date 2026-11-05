# 使用说明

> 作者：**君莫笑** · **永恒之兰**

muse-bridge 的前提是**一个运行中且已登录 muse.ai 的 Chrome**。按顺序做。

---

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

## 十、安全提示

1. **Chrome 的调试端口不要暴露到公网**。`--remote-debugging-port` 等于把浏览器完全开放。
   确认它只监听 `127.0.0.1`（默认如此）。
2. **`muse-profile` 目录包含完整登录态**，等同于账号密码。不要分享、不要提交到 git。
3. **`MUSE_BRIDGE_KEY` 拿到就能解密账号库**，注意环境变量可见性。
4. **默认只监听 127.0.0.1**，别改成 `0.0.0.0`。

---

作者：**君莫笑**
**永恒之兰**
