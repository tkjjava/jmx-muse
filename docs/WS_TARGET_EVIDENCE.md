# WS 目标地址：浏览器侧原始证据（2026-11-11）

这份文件只记一条东西：**浏览器真正连的 WebSocket URL 长什么样**。
它在此之前一直没被直接抓到（CDP 的 `Network.webSocketCreated` 早先一次都没触发，
主线程 `window.WebSocket` Hook 也是 0 次构造），所以 `protocol_client.py` 里的
host / path / query 一直属于"前端常量 + 静态推断"，直到本轮真机联网后抓包成功。

## 证据来源

- 方法：Playwright 连 CDP `http://127.0.0.1:9222`，对已登录的 muse.ai 页面
  `context.new_cdp_session(page)` → `Network.enable` → 监听
  `Network.webSocketCreated` / `Network.requestWillBeSent` /
  `Network.webSocketFrameSent|Received` → `page.reload()`。
- 复核脚本：`D:\deep seek\work\probe_cdp_ws.py`，原始结果
  `D:\deep seek\work\ws_evidence.json`（含 211 个帧事件、1 个建连事件）。
- 前提：本机出口必须断开到 `*.metaaivm.com` 的阻塞（本次用 mihomo 把
  `muse.ai` / `metaaivm.com` 走代理），否则页面停在「Muse 正在连接…」，
  自然也不会有建连事件。

## 抓到的 URL（token 值已脱敏）

```
wss://hatch.metaaivm.com/v1/noise
  ?vm_id=c2edbb14-91c9-486e-ad2e-a70d0ee2ade0
  &auth_token=muse-secret-edge-<EdDSA JWT>          # len=914
  &notary_token=endorsement.v1.<不透明串>            # len=575
  &app_id=hatch-web
  &app_version=1
  &request_id=59e27d59-2146-43f8-9f46-7761<uuid4 其余>
```

参数名与长度是逐字解析出来的（`urllib.parse.parse_qsl`），不是抄来的：

| 参数 | 长度 | 形状 |
|---|---|---|
| `vm_id` | 36 | UUID |
| `auth_token` | 914 | `muse-secret-edge-` 前缀 + EdDSA(JWT) |
| `notary_token` | 575 | `endorsement.v1.` 前缀 + 不透明 base64url 串 |
| `app_id` | 9 | `hatch-web` |
| `app_version` | 1 | `1` |
| `request_id` | 36 | UUID |

## 与现有实现的差异

1. host/path 与前端常量一致：`hatch.metaaivm.com` + `/v1/noise`。**每个 VM 一个
   子域的 `endpoint_url`（`wss://<vm_id>.metaaivm.com/`）不是 WS 目标**——真机
   对那个子域做 TLS 握手会直接 `SSLV3_ALERT_HANDSHAKE_FAILURE`（同一代理下
   hatch 正常）。这条把一个推断结论纠正了。
2. 浏览器多带一个 `app_version=1`，现行客户端没带。服务端在没带时也接受了
   （101 + Noise XX 握手成功），所以它不是硬门槛，但为了"和浏览器一致"应当补上。
3. `notary_token` 一定存在（`endorsement.v1.` 前缀），不是可选装饰。

## 仍然未定/未做的

- `auth_token` 的 `muse-secret-edge-` 前缀是否必须：本客户端拿到的是
  `/api/hatch/token` 返回的 token，握手能过，说明当前服务端不校验这个前缀；
  但如果哪天握手被拒，第一件事就是比对这个前缀。
- 建连后的 211 个帧事件本轮只用来证明"确实有流量"，没有逐帧解出明文
  （明文解出来也没用：那是浏览器自己的会话）。
