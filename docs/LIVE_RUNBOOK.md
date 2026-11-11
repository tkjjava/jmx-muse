# 跑活网验证的运行手册（只给 muse 走代理）

> 为什么需要这一页：muse.ai 的网页和 `/api/session` 走普通 HTTPS，**直连可达**；
> 但它真正的聊天/生成通道是到 `*.metaaivm.com` 的 Noise WebSocket，这个域在
> 本机直连不通（DNS 解析到 Meta 边缘 IP，TCP 443 超时）。没有代理时会出现
> "页面一直停在 `Muse 正在连接…`"、bridge 发送静默失败、最后报
> `180.0 秒内没有产生新回答` 并把账号标 `rate_limited`。这不是代码 bug，
> 是出口网络问题；但代码侧已经补了快速失败（见 `driver.py` 的连接就绪门）。

本手册的所有命令都在 Windows PowerShell 下验证过（2026-11-11 凌晨）。

## 1. 只有一个方向：muse 走代理，其余直连

需要两条规则就够：

```yaml
rules:
  - DOMAIN-SUFFIX,muse.ai,PROXY
  - DOMAIN-SUFFIX,metaaivm.com,PROXY
  - MATCH,DIRECT
```

`MATCH,DIRECT` 是"其余全部直连"的关键；只加前两条而保留 `MATCH,PROXY`
就变成"全机走代理"，与需求相反。

## 2. 起一个本地 mihomo（端口 18000）

```powershell
# 配置放独立目录，别污染用户自己的 Clash 配置
C:\Users\tkjja\mihomo\mihomo-windows-amd64.exe -d 'D:\deep seek\work\mihomo-muse'
```

启动成功标志（日志）：

```
Mixed(http+socks) proxy listening at: 127.0.0.1:18000
RESTful API listening at: 127.0.0.1:19090
```

验证规则真的生效（`hatch.metaaivm.com` 应该拿到 HTTP 401，证明 TLS 已经连到
Meta 边缘；`www.google.com` 应该失败，因为我们把它直连了）：

```powershell
curl.exe -s -o NUL -w '%{http_code}' --proxy http://127.0.0.1:18000 https://hatch.metaaivm.com/v1/noise
curl.exe -s -o NUL -w '%{http_code}' --proxy http://127.0.0.1:18000 https://www.google.com
```

## 3. 把系统代理指到它

```powershell
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' ProxyEnable 1 -Type DWord
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' ProxyServer '127.0.0.1:18000'
# 然后通知 WinINET 刷新（Chrome 才会立刻读到新代理）
```

没通知刷新时 Chrome 会继续用旧配置，表现为"代理起了但页面还是连不上"。

> ⚠ **这一步会把浏览器一起换到 18000 上——2026-11-11 20:1x 的真实事故**
>
> 系统代理是**全机生效**的，不是只给本项目用。18000 的上游隧道一旦全挂，
> 表现就是"**浏览器打不开 muse.ai**"，而命令行里的协议探针可能还在用
> `MUSE_PROTOCOL_PROXY=7890` 正常跑——两边看起来矛盾，其实是两条通道。
>
> 当天实测（mihomo 配置 `D:\deep seek\work\mihomo-muse\config.yaml`，
> 规则 `DOMAIN-SUFFIX,muse.ai,PROXY` → 10 个 vless 节点）：
>
> | 探测 | 结果 |
> | --- | --- |
> | controller `GET /proxies/US-01..10/delay` | **10/10 全 503**（节点全死） |
> | `https://muse.ai/` 经 18000 | `ConnectError: SSL UNEXPECTED_EOF_WHILE_READING` |
> | `https://www.google.com/`、`www.bing.com/` 经 18000 | 200 —— 但那是 `MATCH,DIRECT`，**没走节点**，不证明隧道可用 |
> | `https://muse.ai/` 经 7890（FlClash） | 200 |
> | `https://muse.ai/` 直连 | 200 |
>
> 教训：`MATCH,DIRECT` 的站点通过代理测通了，**不代表 PROXY 组可用**；
> 判断隧道死活只看 `muse.ai`（或任何命中 PROXY 规则的域名）这一个指标。
>
> 遇到"浏览器打不开 muse.ai"时，按这个顺序处理：
> 1. 读注册表确认真实值：`Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'` 的 `ProxyServer`；
> 2. 用上面那张表定位是"隧道死"还是"代理没起"；
> 3. 隧道死了就换成可用通道（本机实测 7890 与直连都行），改完必须发
>    `InternetSetOption` 刷新，Chrome 下一次网络变更才生效；
> 4. 别把"改回 18000"当成恢复现场——它现在指向一条死隧道。

**代理选型兜底（2026-11-11 11:0x 实测）**：本机 mihomo(18000) 的上游节点一度
全挂（controller `GET /proxies/US-xx/delay` 全 503，`GET /proxies/PROXY` 的
`now=US-01` 但实际 TLS 握手 `UNEXPECTED_EOF`），此时 FlClash(7890) 仍可用：
`curl -x http://127.0.0.1:7890 https://muse.ai/` → 200。协议探针/桥若报
`/api/session 网络请求失败（ConnectError: SSL UNEXPECTED_EOF）`，先判代理：
1. `python -Xutf8` 用 `httpx.Client(proxy=..., trust_env=False)` 分别测 18000/7890；
2. 谁有 200 用谁（`MUSE_PROTOCOL_PROXY` 跟着改）；
3. 该错误与"服务器出口坏"无关——客户端已固定 `trust_env=False`，见
   `protocol_client._ensure_http()`，不会再偷用系统代理。

## 4. 恢复现场

1. 页面 `Ctrl+R` 刷新，等标题区出现 `Muse 已连接`（不是 `Muse 正在连接…`）。
2. 刷新 bridge 的会话：`POST http://127.0.0.1:8788/admin/accounts/muse1/refresh`
   （账号之前被标成 `rate_limited` 时，这一步会把它拉回 `ready`）。
3. 跑一句最短的聊天验证：

```powershell
curl.exe -s -X POST http://127.0.0.1:8788/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"muse-1\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句中文回答：6+7 等于几？\"}]}"
```

期望 `200`，`content` 是 `6+7等于13。` 这类真回答；如果 `content` 为空串，
就是通道没连上（旧版曾把空串当成功返回，现在会报 504 而不是 200）。

## 5. 纯协议链路（不依赖浏览器）

同一份代理也能让 `muse_bridge.protocol_client` 直连 Noise WS。注意它的
WebSocket 目标必须用 **`hatch.metaaivm.com`**（LB），不是 `/api/session` 里的
`endpoint_url`（那个 per-VM 子域在真机上 TLS 握手直接失败）。

```powershell
python -Xutf8 'D:\deep seek\work\probe_protocol_lb.py'   # 低层：握手/register/subscribe
python -Xutf8 'D:\deep seek\work\probe_pure_chat3.py'    # 完整：客户端+引擎拿正文
```

期望：

- `probe_protocol_lb.py`：`CONNECTED via Noise`、`register-capabilities` 200、
  `subscribe` 200，订阅流里出现 `delta.text_append` 的助手正文。
- `probe_pure_chat3.py`：`CONNECTED in ~3s`、`delta: '13+13等于26。'`、
  `terminal event: done`、`connected=True`。这是 2026-11-11 首次跑通的形态；
  如果这里报 `未收到完整结果`，先看是不是 `protocol_engine` 的收尾逻辑回退了。

## 6. 关掉代理

```powershell
# 恢复直连（把 ProxyEnable 设 0，Chrome 下一次网络变更后生效）
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' ProxyEnable 0 -Type DWord
# 停 mihomo：按端口找 PID
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 18000 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

关代理后页面会回到 `Muse 正在连接…`，这是预期；本地已生成的媒体文件仍然可以
从 `/v1/media/<token>` 下载，不受影响。

## 7. 通道断了的正确表现（2026-11-11 实测）

代理关掉、页面回到 `Muse 正在连接…` 时打一次 chat：

```
POST /v1/chat/completions → 503（约 12.7 秒）
{"detail":{"error":"browser_unavailable",
 "note":"页面未就绪（可能仍在 Muse 正在连接…）：出口到 metaaivm 的通道不通，先恢复代理再重试"}}
```

账号状态变成 `browser_unavailable`（可重试状态），**不是** `rate_limited`。
把代理打开 + `POST /admin/accounts/muse1/refresh` 之后立刻恢复：

```
state: ready
POST /v1/chat/completions → 200，content: "12+12等于24。"
```

旧版的行为是等满 180 秒、报 `timeout`、账号被标 `rate_limited`，让人误以为是
配额问题。现在"出口断"和"模型慢/限流"是两条不同的出口，排障时先看 HTTP 状态：
503 = 通道不可用（可重试），504 = 上游超时/限流**或"消息根本没发出去"**
（后者的 `note` 会明确写"页面上连新气泡都没出现"）。

## 8. 504「页面没有任何反应」的两种成因（2026-11-11 21:0x 补）

现象：`POST /v1/chat/completions` 返回 504，`note` 是
`发送后 45 秒页面没有任何反应——消息很可能没有发出去`，账号随后变
`rate_limited`，再往后一律 503「账号池里没有可用的 muse 账号」。

先分清是哪一种，**不要直接去 refresh 账号**（refresh 只治标）：

1. **消息真的没发出去**（页面/通道问题）。判定办法：用 Playwright 连同一个
   Chrome（`chromium.connect_over_cdp("http://127.0.0.1:9222")`），找到 muse
   标签，手工 `fill` 一句话再 `press("Enter")`。
   - 手工能 5 秒内出现新气泡 → 通道正常，问题在驱动判据或输入步骤（看第 2 条）；
   - 手工也发不出去 → 页面/出口问题，回到 §4 恢复现场。
2. **消息发出去了，但判据认为没反应**（0.4.2 之前的一个真缺陷）。旧实现只比较
   "最后一条助手正文是否变化"：连续两次问同一句话（比如都回 `OK`）时，新回答
   与上一条逐字相同被当成"还停在旧消息上"，于是走满 45 秒反应期限后报 504。
   - 怎么确认：手工发同一条消息能出回答、走桥必 504，且两次回答文本相同；
   - 修法：0.4.2 起改用 `data-message-id` 判断"是不是新消息"
     （`driver._last_assistant_id()`），id 读不到才回退比正文；
   - 升级后复验：同一提示连发三次，应得到 **200/200/200**。

> 为什么把这条写进运行手册：它的症状与"配额用完""通道断了"完全一样，
> 但根因在判据，refresh 账号或换代理都治不好——2026-11-11 当天因此多花了
> 半小时按环境问题排查。
