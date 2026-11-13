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

2026-11-14 深夜两次尝试（先正常完成一条短回答，再调用历史接口）都在连接阶段
失败（约 4.4–5.5 秒，`ProtocolError`），未到达 `chat.history`；与更早的间歇
Upgrade 502 现象一致，不能据此判定接口不存在或不可用。响应字段名、`transcript_mode`
的取值、`chat_events` 与 transcript 的具体结构仍未知。

因此：

- 不把 `chat_history()` 用于自动恢复，不宣称"重连可无损取回答案"；
- 现有"重连后无权威完成即报不完整"的保守策略保持不变；
- 下一步在链路可用时用探针打印**响应结构**（只打键名/计数，不打正文）再决定
  是否实现恢复合并。

## 复现

```powershell
python -m pytest tests/test_protocol_history.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests tools
```