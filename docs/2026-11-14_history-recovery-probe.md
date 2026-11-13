# `chat.history` 恢复接口：静态取证与待验响应

## 结论

前端断线后的恢复路径不是依赖 live 补发，而是重连后调用 `chat.history` 拉历史
对账（按 `chat_events` 的 seq 合并、用 `transcript_revision` 防止覆盖更新的实时
事件）。本轮据此新增最小客户端方法 `chat_history()` 与 8 条帧级回归，**尚未**
取得真实响应形状，因此没有把任何恢复逻辑接进引擎。

## 静态证据

| 事实 | 位置 |
|---|---|
| `chat.history` 路由 = `GET /chat/history` | 3qzcufyl9d94_.js 字节 1488 |
| 调用参数 `{limit, transcript_mode[, session_id]}` | 24thn7in_976_.js 字节 177732 |
| 历史加载用 `limit=V.m`，GET 序列化不带 body | 同一产物 40025 / 2knhq5aux36zv.js 28794 |
| 重连后按 `chat_events` seq 合并、`transcript_revision` 防覆盖 | 24thn7in_976_.js 40654 起 |

同一产物还显示窗口接口 `chat.history_window`（GET `/chat/history-window`，
参数 `anchor_seq`/`before_limit`/`after_limit`），但当前桥只需要整段历史，
暂不实现窗口分页。

## 已落地

```python
history = await client.chat_history(limit=30)
```

- `limit` 必须是 1..200 的整数（bool/浮点/字符串在发送前拒绝）；
- 可选 `session_id` 仅用于线程会话，空值拒绝；
- 请求是 `GET /chat/history?limit=30`，无 JSON body、`end_body=True`；
- 返回 `data` 信封时解包，裸对象原样返回；
- 不重试、不缓存、不改任何订阅状态。

帧级回归覆盖：GET+query+空 body、裸对象与信封两种响应、非法 limit 发送前拒绝、
HTTP 错误保留状态码且只发一次。

## 活网尝试与边界

2026-11-14 深夜共尝试三次：两次「先完成短回答再取历史」（4.4–5.5 秒失败），
以及一次**只连接、不生成**直接取历史（3.6 秒失败）。前两次停在聊天阶段，
第三次已连上但请求期握手再次失败，错误原文为
`Noise WebSocket 连接失败（InvalidStatus HTTP 502）`——与更早的间歇 Upgrade
502 同源，属于链路瞬时故障，**不是** `chat.history` 路由被拒的证据。
随后再用「不走代理」的方式复跑，仍得到同一个 Upgrade 502，说明故障在服务端
握手侧而不是本机代理配置，可以排除"代理把 GET 转坏"这一假设。

连接期就出现 502 时，无法区分"接口不存在/参数错"与"网关瞬时故障"，因此：

- 不把 `chat_history()` 用于自动恢复，不宣称"重连可无损取回答案"；
- 现有"重连后无权威完成即报不完整"的保守策略保持不变；
- 下一次在线路可用时（能稳定完成握手）再取响应结构：只打印键名/计数，
  不打印正文，然后再决定是否实现恢复合并。

## 复现

```powershell
python -m pytest tests/test_protocol_history.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests tools
```