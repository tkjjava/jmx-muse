# 中和 `interrupted` 终态：拒绝、失败与账号健康的分界

## 结果

此前引擎把所有非 `completed` 的 `delta.message_done.status` 一律当成模型拒绝
（`ProtocolModelRefusal`）。静态前端证据表明 `interrupted` 对应取消/中止：它既不是
模型拒绝，也不是账号故障。本轮新增独立终态分类，避免三件事被混为一谈：

- 不是模型拒绝（不向调用方声称"模型不肯生成"）；
- 不是可换号重试的失败（换号只会把同一被中止的结果再要一遍，可能重复消耗额度）；
- 不计入账号健康失败样本（账号没有过错，健康度保持中性）。

本主题新增 9 条回归（另有 2 条补充活网取消语义的回归），完整套件
**1881 passed**，ruff 与差异检查通过。

## 证据边界

`interrupted` 只来自前端静态实现识别（`delta.message_done.status="interrupted"`
时前端停止流并清理忙态）。本轮取得了一次活网取消样本：ACK 返回 `cancelled=true`，
但 ACK 后仍有已缓冲正文到达、最终以 `agent.status:online` 收尾，没有出现
`message_done`。因此不能声称"取消必然产生 interrupted"或"ACK 即上游已停"。
本轮的改动只是：**一旦服务端确实回报 interrupted，就按中止语义处理**，
不把它当成拒绝或成功。

## 行为

| 路径 | 修复前 | 修复后 |
|---|---|---|
| 聊天流 | 视为拒绝，先发 `refused` 再抛拒绝错误 | 已发出的正文保留，直接抛 `ProtocolInterrupted`，绝不补 `done` |
| 生成流 | 视为拒绝，媒体判定未完成 | 该事件的媒体不作为成品产出；立即抛 `ProtocolInterrupted`，不取件 |
| 异常分类 | `ProtocolGenerationError` 的可重试分支 | 加入 `NON_RETRYABLE`；`retryable()` 返回 False |
| 浏览器回退 | 满足条件时允许回退 | 作为终态错误，不触发浏览器回退 |
| HTTP 映射 | 504 `protocol_timeout` | 409 `protocol_interrupted`，账号状态不变 |
| 调度健康 | 记一次失败样本 | 中性：不产生失败样本 |

`interrupted` 之外的状态（缺失、`completed`、其它字符串）保持原有判定：
缺失按完成（兼容旧事件），`completed` 按完成，其它字符串仍按拒绝处理。
没有新增任何 wire 字段、取消报文或自动重试，也没有把客户端 `aclose()` 绑定远端取消。

## 可复现验证

在仓库根目录运行：

```powershell
python -m pytest tests/test_protocol_engine.py tests/test_protocol_interrupted_api.py tests/test_scheduler_wiring.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests
python tools/doc_assertions.py
```

新增用例覆盖：生成路径中断不取件、中断事件携带的媒体不出现在产出、异设备中断
不影响本回合、聊天路径保留部分正文且不补 done、客户端 EOF 确认不能覆盖显式中断、
409 映射不改账号状态、健康样本中性而普通异常仍记失败。

## 活网补充证据（2026-11-14 Asia/Shanghai）

在链路恢复后，对自行发起的一次回合做了取消时序核对（一次短文本请求 + 一次显式取消，
未打印正文、账号或凭据）：

```json
{"ok":true,"via":"direct_noise_protocol","cancel_sent_after_own_text":true,
 "cancelled_ack":true,"observed_done_statuses":[],"terminal_event":false,
 "recognized_terminal":true,"terminal_reason":"agent.status:online",
 "after_ack_text_events":8,"cancel_ack_elapsed_s":0.442,
 "post_ack_observation_s":0.095,"elapsed_s":17.916}
```

可确认的事实：

- `POST /chat/cancel` 返回布尔 `cancelled=true`，ACK 往返约 0.442 秒；
- **ACK 之后仍收到 8 个正文增量**，随后以 `agent.status` 回到 online 收尾；
- 没有出现 `delta.message_done`，因此没有观察到 `interrupted`；
- 客户端新的终态原因元数据记录了 `agent.status:online`。

结论：取消 ACK 不等于上游立即停止，已缓冲的正文可以晚于 ACK 到达。因此本地
`read_stream` 必须继续消费到终态、不能因发送过取消就提前收尾；本轮新增回归钉住
"取消请求不得中止本地订阅、不得改动完成状态或连接"。反过来也不能把 ACK 写成
"所有后台工作已停止"。

### 用仓库工具复现

上述结论已整理为可复现工具；它只读打开账号库、输出脱敏 JSON（无正文/账号/令牌）：

```powershell
python tools/protocol_cancel_timing.py --observe 8
```

工具对一次自行发起的短回合执行同样的流程，第二次运行结果：

```json
{"ok":true,"via":"direct_noise_protocol","cancelled_ack":true,
 "own_text_before_cancel":true,"ack_latency_s":0.487,
 "after_ack_text_events":13,"post_ack_observation_s":0.001,
 "terminal_reason":"agent.status:online","recognized_terminal":true,
 "saw_message_done":false,"done_statuses":[],"elapsed_s":10.757}
```

两次独立运行都复现了同一模式：ACK 后仍有已缓冲正文、终态为 `agent.status:online`、
没有 `message_done`。这加强了"ACK 与停止时刻解耦"的判断，但不改变下面的未验证项。

## 尚未覆盖

活网仍未观察到 `interrupted` 事件本身：无法确认取消必然产生它、其到达时延，以及
中断后是否还有晚到媒体。线程会话与跨客户端取消作用范围同样未验证。
本轮不对这些行为做承诺。