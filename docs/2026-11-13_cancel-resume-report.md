# 取消协议取证与订阅恢复无损消费

## 结论与证据等级

本轮从本地前端构建产物追出完整取消调用链，新增显式客户端 cancel_chat；同时
修复订阅恢复的首包丢失、逐行字节位置、去重游标及旧响应清理。新增 17 条回归，
完整套件 **1761 passed**，ruff 全绿。未修改运行中服务、账号库或新增取消 HTTP 端点。

取消载荷来自前端静态证据，并已通过本地 HTTP-over-Noise 帧测试；本轮两次活网
探针均在 connect 阶段失败，尚未发送提问或取消，没有 ACK。不能把静态取证和
帧回放称为部署端取消成功。本轮没有验证线程权限、跨设备取消范围或取消幂等性。

## 前端取消链路

以下偏移均为本地文件的 0-based UTF-8 字节偏移，而非字符位置。
产物目录为本地 work/muse-bundles，不打包进仓库。

| 产物 | 字节偏移 | 观测 |
|---|---:|---|
| 3qzcufyl9d94_.js | 32911 | chat.cancel → POST /chat/cancel，noiseOnly=true |
| 24thn7in_976_.js | 104255 | t4("chat.cancel", a2({}, route))，读取 result.cancelled |
| 24thn7in_976_.js | 22629 | a2 只给 thread 路由增加 session_id |
| 2knhq5aux36zv.js | 28794 | 非 GET 且非 empty 的请求用 JSON.stringify(params) 编码 |
| 0j768ks6f4vst.js | 7019 | 加密 HTTP 请求默认 service=daemon |
| 24thn7in_976_.js | 10127 | clear_busy_after_cancel 清本地 UI 忙态 |
| 05bqe9zbensof.js | 30416 | BrowserAgentStop 未确认取消时另尝试 durable task stop |

主产物 SHA256：

```text
24thn7in_976_.js
398374054b2fb608e031fbc3927e2015b3e0312df246a9eee781826026fd7ba6
3qzcufyl9d94_.js
5ff950d3a9a9c9d0009ff9dad64972128a33032e3e2a6d479fb54bb255f28f7d
2knhq5aux36zv.js
28d24289e82b1df17c679e1c0b785e66d8654d37ccb7ae1006143dfba5c396e7
0j768ks6f4vst.js
523f5ea0dcad86a8600caaa5e161bd9c6180a118e5b020d8e2cdd740da99f3ad
```

实际应用层载荷：

```json
{}
```

主会话使用上面两个字节的 JSON 对象，不是空 HTTP body。线程载荷只增加已有
thread session_id，不携带 node_id/message_id/task_id/transcript_mode。
不能从发送消息含 node_id 推断取消也支持它。

前端请求出错也会清本地 UI 忙态，因此 UI 停止不证明远端停机。
前端识别 message_done.status=interrupted，但尚未证明它必由 chat.cancel 产生。

## 显式客户端方法

已有连接的 HatchProtocolClient 可调用：

```python
result = await client.cancel_chat()
if result["cancelled"] is False:
    # 上游明确返回未取消；不能当成功。
    pass
```

线程调用必须传已有且明确归属的 session_id。非法空值在发送前拒绝；响应必须带
布尔 cancelled，缺失、数字、字符串不能伪装 ACK。HTTP/解析失败保留协议异常。

该方法不自动 connect、不自动重试、不自动清 UI，不改 _turn_complete，亦未绑定
到 close/aclose、warm release 或下游断开。主会话可能为多个客户端共享，服务端
作用范围未明确之前，无条件断开即远程取消会误伤其他任务。

## 恢复路径修复与回放

| 已复现问题 | 修复 | 回归情形 |
|---|---|---|
| 降级普通订阅后 seek_to_end 丢新尾部 | 从 offset=0 消费，按重连前固定 floor 去重 | 200 响应头 body 合并 ACK+旧 seq+新正文+终态 |
| 批末才写 scan_offset，yield/终态 return 提前退出 | 每条完整 LF 记录在 parse/drop/yield 前提交字节位置 | UTF-8 中文、CRLF、坏行、提前 aclose、半行续包 |
| 回放过滤前推进聊天游标 | ACK/旧 seq 拒绝后才记录游标 | 旧记录带 chat_event_seq=999 不污染后续 resume |
| 前奏重连未设置去重基线 | 读循环前保存 floor/skip_ack，两处前奏恢复均沿用 | 下一回合重连首包含旧正文/idle，不能抢先结束新回合 |
| 重连失败只清连接、不清旧订阅响应 | 恢复开始回收已知死订阅，保留其他请求响应 | 重连失败后旧 body 消失，无关响应还在 |

固定 floor 而非随当前最大 seq 移动，保留同批到达且高于断点的乱序新事件。
这证明本地交付和恢复状态保持一致，不证明服务器双游标的严格大于/包含边界。
没有发送不存在的 wire ACK，也没有声称保证上游 exactly-once。

## 复现命令

在仓库根目录运行：

```powershell
python -m pytest tests/test_protocol_cancel.py tests/test_protocol_resume.py tests/test_protocol_client.py -q -p no:cacheprovider
python -m ruff check muse_bridge tests
python -m pytest -q -p no:cacheprovider
python tools/doc_assertions.py
```

首次全量有既有媒体清扫测试的时序失败（90ms 窗口只执行一次清扫），该测试未修改。
定向复测通过，后续完整回归 1761 passed。活网取消探针两次失败分别约 12.072 秒、
9.426 秒；第二次确认 stage=connect、无取消 ACK、未见正文，不能用于推断取消 API
被拒或失效。未继续无界重试，也未把上轮短回复成功作为本轮取消成功证据。
