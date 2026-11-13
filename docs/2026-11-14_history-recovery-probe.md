# `chat.history` 恢复接口：静态取证、活网确认与恢复接线

## 结论

前端断线后的恢复路径不是依赖 live 补发，而是重连后调用 `chat.history` 拉历史
对账。本轮先按第一阶段前端产物实现契约与宽容解析，随后链路恢复后取得真实响应，
端到端验证了**“断线丢失的正文可以从已提交历史取回”**：

- 断线后截断在 169 字，从历史取回完整 456 字（严格前缀命中）；
- 引擎整链路（`stream_chat` 内部重连）断线后只收到 2 段 delta，`done` 直接产出
  完整 456 字，无错误；
- 因此 `ProtocolEngine.history_recovery` 已**默认开启**（`False` 可关闭）；
  历史取不到或前缀不符时仍回退“连续性无法确认”，不伪造结果。

## 静态证据

| 事实 | 位置 |
|---|---|
| `chat.history` 路由 = `GET /chat/history` | 3qzcufyl9d94_.js 字节 1488 |
| 调用参数 `{limit, transcript_mode[, session_id]}` | 24thn7in_976_.js 字节 177732 |
| 历史加载用 `limit=V.m`，GET 序列化不带 body | 同一产物 40025 / 2knhq5aux36zv.js 28794 |
| 重连后按 `chat_events` seq 合并、`transcript_revision` 防覆盖 | 24thn7in_976_.js 40654 起 |
| 分页用 `before_seq` 取更早、`after_seq` 取更新 | 同一产物 @85765 / @103814 |
| 历史事件转消息：`message_id`、`payload`、`metadata`、`display_text`/`content`、`occurred_at_ms`、`resources`、`reply_to_message_id` | 同一产物 @59874 |

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
- `data` / `result` 信封都会解包，裸对象原样返回；
- 不重试、不缓存、不改任何订阅状态。

10 条帧级回归覆盖：GET+query+空 body、裸对象/`data`/`result` 响应、非法 limit
发送前拒绝、HTTP 错误保留状态码且只发一次。

## 活网确认

链路先经历了一段间歇 Upgrade `502`（有/无代理都复现：两次「先聊天再取历史」、
一次「只连接取历史」、一次解析层提交后复验，全部停在握手）。故障恢复后：

### 页面结构（真实响应，只记录键名与长度）

```text
page:  {channel: "all", chat_events: [ ... ], has_more: bool,
        transcript_revision: int}
event: {seq:int, event_name:"message.assistant"|"message.user",
        role:"assistant"|"user", message_id:str, display_text:str,
        display_text_ready:bool, display_text_updated_at_ms:int,
        occurred_at_ms:int, resources:[...], reply_to_message_id:str,
        agent_id,parent_agent_id,parent_message_id,reaction_emoji,
        transcript_surface,visibility, chat_context{...},
        payload{content,display_text,display_text_ready,role,seq,
                message_id,chat_event_seq,is_thread,session_id,source,
                status,metadata,chat_context,transcript_surface,visibility}}
```

实测同一事件顶层 `display_text` 与 `payload.display_text` / `payload.content`
长度一致；`role` 与 `event_name` 一致（`assistant`/`message.assistant`、
`user`/`message.user`）。**历史 seq 与 live 订阅流 seq 不是同一序列**：一次
断线回合里已提交最新 seq=683，而 live 游标是 734——因此不能用 seq 比较判断
历史是否陈旧，恢复守卫改用严格前缀。

### 解析器实测

真实页（limit=5）经 `muse_bridge/history.py` 解析：

```json
{"messages": 5, "roles": ["assistant", "user", "assistant", "user", "assistant"],
 "text_lens": [456, 96, 456, 96, 456], "latest_assistant_len": 456,
 "newest_seq": 680, "has_more": true, "transcript_revision_type": "int"}
```

### 恢复实测

| 场景 | 断线前已收 | 恢复结果 |
|---|---|---|
| 恢复助手直接调用 | 169 字 | 取回 456 字（严格前缀） |
| 引擎 `stream_chat` 全链路 | 2 段 delta | `done` 输出 456 字，无错误 |

第一次实测时守卫还要求“历史最新 seq ≥ live 游标”，结果被 683 < 734 误拦；
移除该无效比较后两次都通过。这正是“先用活网数据证伪假设”的价值。

### 传到 app 层（驱动层修正）

引擎把恢复文本放在 `done` 里，但驱动原来的 `stream_reply_events` 收到 `done`
就直接返回，**丢弃了 done 的正文**——上层只会拿到断线前的截断 delta。现在驱动
在 `done` 时补发“缺失尾部”（仅当 done 文本是已发文本的严格扩展），不重写已发
内容、不改变上层 content 增量契约。副作用修复：`message_done` 只带 transcript
（没有任何 delta）的回合，以前到 app 层是空回答，现在也能拿到正文。

回归：4 条驱动层用例（恢复尾部、transcript-only、矛盾的 done 文本被忽略、
字符串投影同样带尾部）。

可观测：引擎在成功/未命中/请求失败三态分别计数 `chat_history_recovered` /
`chat_history_recovery_missed` / `chat_history_recovery_failed`，驱动补发尾部时
计数 `chat_done_tail_emitted`，都可在 `/admin/observability` 读取（USAGE §10.5）。

### Bridge 级验证（隔离实例，不碰生产）

引擎与驱动层的测试都绕过了 bridge 的 HTTP 面。为证明**用户实际看到的那条链**
（`/v1/chat/completions` SSE → pool → driver → engine）在断线后仍完整，用
`tools/bridge_disconnect_probe.py` 做了一次隔离实测：

1. 复制账号库到临时目录，独立端口起 bridge（`MUSE_PROTOCOL_DRIVER=1`，代理指向
   工具自建的 CONNECT 中继）；复制库的探针时间戳已过期，先经 admin keepalive 刷新；
2. SSE 发起约 400 字回答，收到第一个正文块后掐断中继内所有隧道（监听保持，故可重连）；
3. 结果：

```json
{"ok": true, "length_verified": true, "driver": "protocol",
 "content_chunks": 14, "content_len": 456, "committed_len": 456,
 "finish_reason": "stop", "relay_killed_at_s": 23.3,
 "counters": {"protocol_driver_built": 1, "chat_history_recovered": 1,
              "chat_done_tail_emitted": 1},
 "elapsed_s": 32.56}
```

即：上游连接在回答中途被强制掐断，bridge 对外的 SSE 仍以 `finish_reason=stop`
结束，客户端收到完整 456 字，与已提交历史的长度一致；恢复计数与尾部补发计数
各 +1。这条证据把引擎、驱动、HTTP 三层串起来了。

### 线上部署冒烟（本机 8788，当前代码）

验证时本机栈处于停止状态（8788/8765 均无监听），因此不是"重启"而是重新拉起：

1. `python tools/restart_bridge.py --log data/bridge.log` 起服务（relay 8765 仍未起）；
2. `/healthz` 报 `version=0.4.2 driver=protocol`，但 `accounts_dispatchable=0/1`——
   复制自旧库的探针时间戳过期；`tools/refresh_account.py` 刷新后回到 1/1；
3. 冒烟一条聊天（经桥 HTTP 面，非直连协议）：

```json
{"status": 200, "driver": "protocol", "content_len": 2,
 "finish_reason": "stop", "elapsed_s": 17.19,
 "counters": {"protocol_driver_built": 1, "browser_driver_built": null,
              "protocol_fallback": null}}
```

即：运行中的 bridge 走协议驱动、无浏览器回退、请求正常收尾。断线恢复行为本身
仍由上面的隔离实例实测负责——当前线上没有可控代理，不人为制造线上断线。

## 恢复接线

`ProtocolEngine(history_recovery=True)`（默认）：

| 检查 | 不满足时 |
|---|---|
| 重连标记 `_turn_resumed` 为真、终态不是权威 `delta.message_done` | 直接判不完整 |
| 历史页最后一条 assistant 正文非空 | 判不完整 |
| 已收正文是它的**严格前缀**（更短且 `full.startswith(partial)`） | 判不完整 |
| 历史请求任何异常 | 判不完整，不掩盖原错误 |

通过后把 `done` 文本替换为全文；`stream_chat` 仍先按原样吐已收 delta，
`collect_chat` 在 `done` 存在时采用其文本。

回归：16 条纯函数（`tests/test_history_parse.py`）+ 10 条帧级
（`tests/test_protocol_history.py`）+ 8 条引擎恢复
（`tests/test_protocol_engine.py`，含默认开启、显式关闭、非前缀拒绝、
等长拒绝、seq 空间分歧、请求失败回退、delta+完整 `done` 组合）。

**限制**：恢复只针对主会话。协议层 `subscribe()` 现在不发送 `session_id`，桥内并不
存在线程回合；线程不是"待补的恢复分支"，而是尚未支持的会话形态（将来若支持线程，
需要把 `session_id` 同时传进订阅与 `chat_history()`）。历史页是否包含**仍在生成中**
的回合未验证（本实测都是在回合收尾后取到）。

## 复现

```powershell
python -m pytest tests/test_protocol_history.py tests/test_history_parse.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests tools
```