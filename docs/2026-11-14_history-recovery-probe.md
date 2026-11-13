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

## 页面结构：静态锚点（非活网样本）

在等待链路期间，从第一阶段前端产物补齐了历史页的候选结构。以下都是
**静态引用**，不是本仓库抓到的真实响应：

| 事实 | 锚点 |
|---|---|
| 分页用 `before_seq` 取更早、`after_seq` 取更新 | 24thn7in_976_.js @85765 / @103814 |
| 顶层 `transcript_revision` 一旦建立就必须存在 | 同一产物 @36022 |
| 顶层 `chat_events`、`has_more`；事件带 `seq` 并按 seq 合并 | 同一产物 @86006 / @36022 |
| 历史事件转消息：`message_id`、`payload`、`metadata`、`display_text`/`content`、`occurred_at_ms`、`resources`、`reply_to_message_id` | 同一产物 @59874 |
| 历史事件里 `delta.text_append` 也带 `event_name` 与 `message_id`/`seq` | 同一产物 @25966 |
| `message.user` / `message.assistant` 事件形状校验（seq、occurred_at_ms、role、content） | 0ikdqggttpa8f.js @19646（共享会话同类形状） |
| GET 路由的参数在 query，body 为空 | 参考实现 `work/muse2api_harvest/muse-cli/src/muse_cli/gateway.py`（第三方产物，仅佐证） |

## 解析模块（已落地，未接入恢复）

新增 `muse_bridge/history.py`：把任意可接受的页面形状归一为消息列表，对
未知嵌套保持宽容，不打印正文、不做网络请求。

| 函数 | 行为 |
|---|---|
| `page_of` / `page_events` | 解 `result`/`data` 信封；事件按 seq 排序，无 seq 的排最后且稳定 |
| `messages` | 提取 `(seq, role, message_id, text)`；无正文事件跳过 |
| `latest_assistant_text` | 取最后一条 assistant 正文（恢复时最可能需要的一段） |
| `newest_seq` / `transcript_revision` / `has_more` | 供调用方与本地游标、revision 对账 |

16 条纯函数回归覆盖：扁平/`payload.content`/`display_text` 优先级、显式 role 与
`event_name` 推断、空字符串不遮蔽真实文本、非 dict 与缺 seq 事件、`data`/`result`
双信封、按 seq 排序、`has_more` 非布尔不撒谎。

**未接入**：引擎仍不会在重连后自动拉历史合并；在真实响应形状确认之前，自动
恢复可能把旧回合正文误当本轮结果。

## 复现

```powershell
python -m pytest tests/test_protocol_history.py tests/test_history_parse.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests tools
```