# 协议收发、终止与资源清理加固

## 结果与适用范围

本轮修改 Noise 之上的 HTTP 复用、聊天终止裁决及生成预算，没有更换算法、密钥、
握手参数或已验收的请求字段。新增 37 个回归用例；完整套件 **1744 passed**，ruff 全绿。

2026-11-13 使用当前工作树直接连接上游，完成一次不经浏览器的短文本协议验收：

| 观测 | 结果 |
|---|---|
| 路径 | direct_noise_protocol |
| 引擎事件 | 2（文本增量和 done） |
| 文本长度 | 2 字符 |
| 明确终态 | done=true，client_terminal_confirmed=true |
| 从验收启动至首输出 | 21.589 秒，包含连接建立 |
| 总耗时 | 22.277 秒 |

只读打开现有账号库，密钥从用户环境读取，未在脚本、日志、报告中写入凭据。
未修改线上服务配置或重启服务，未对图像/视频做本轮活网生成验收。
首次验收前置检查发现本地 bridge 未监听；改为直接协议检查。
一次验收脚本自身把 proxy 字符串传给要求 dict 的接口而失败，纠正为 server 字段后成功。
这些失败没有冒充产品协议故障，也没有被计入通过结果。

## 已验证缺口与修复

### 请求与帧生命周期

| 原行为 | 修复 | 回放证据 |
|---|---|---|
| request 超时或取消只移除限额，响应体仍留存 | 异常出口移除响应，waiter 空集合及时删除 | timeout/cancel 后 responses、limits、waiters 均为空 |
| 超限设置 ended 后被 end_body=False 覆盖 | ended 单调保持，不再继续累积已结束响应 | 非终态大包立即报超限，其他 stream 仍可正常完成 |
| 迟到/未知响应用 setdefault 重建缓存 | 仅接受本地已登记且未结束的 stream | 迟到帧后屏障请求正常，缓存不复活 |
| send 已消耗 nonce 后中断，连接仍可能被复用 | 标记连接失效，失败在途等待者，要求新握手 | 后续发送被拒，原动作不自动重发 |
| subscribe 被取消后残留响应 | 所有异常出口清理未成功订阅的响应 | 保留任务引用取消，状态立即回收 |

加密回放用独立 AESGCM 对端生成密文，未调用生产 Cipher.enc/nonce 构建对端帧：
未知 stream 帧仍被解密并推进接收 nonce，后续有效响应使用下一 nonce 解密成功，
未知帧没有分配响应缓存。不能为了“忽略未知 stream”而跳过加密层计数推进。

发送中断后的上游执行结果可能不明确；本轮没有自动重放 POST /chat/stream。
正常请求超时只丢弃本地响应状态，没有臆造尚未验证的 wire cancel/reset 报文。
这不保证上游已经停止执行。

### 回合终止与归属

此前引擎把“已经收到正文、流在总超时之前 EOF”推定为成功。但客户端 idle、订阅
关闭、重连未恢复等路径都能 EOF，导致半段回复被标成 done。

现在只有显式 message_done 或客户端认可的本回合终态才能完成聊天。客户端每回合
重置 _turn_complete，仅在已收到本回合正文且命中已知状态时设置；空/未知
activity_code 不能当 online/idle。普通 EOF/idle 保留已发文本，随后抛不完整结果错误。

带 chat_context.device_id 且与本轮 node_id 不同的文本、媒体与完成事件，不得影响
本轮结果或生成宽限计时。缺少该上下文的旧事件仍按既有兼容规则接收，不能将此
检查描述为完整租户鉴权。明确 message_done 的 transcript-only 回复不再等静默超时。

### 清理与预算

- driver 字符串投影、driver 事件流、engine stream_chat、底层 read_stream 在提前返回、
  异常、取消、下游 aclose 时逐层确定关闭。正常结束不关闭共享 warm client。
- 聊天使用绝对剩余预算限制下一次 anext 等待；超时上下文不跨 yield，避免取消正在
  处理已收到数据的调用方。同步阻塞代码仍无法由 asyncio 强制抢占。
- 生成的总预算覆盖素材暂存、候选预取、事件读取与最终取件；明确完成后，无成品
  的静默等待同时受剩余总预算和 late-media grace 约束。
- 清理可能在预算到期后继续完成，不把“超时触发”描述为清理完成的严格墙钟上限。

## 可复现验证

在项目根目录运行（全部使用回放或假传输，不消耗上游额度）：

```powershell
python -m pytest tests/test_protocol_transport_lifecycle.py tests/test_protocol_engine_lifecycle.py tests/test_protocol_engine.py tests/test_protocol_driver.py -q -p no:cacheprovider
python -m ruff check muse_bridge tests
python -m pytest -q -p no:cacheprovider
python tools/doc_assertions.py
```

关键测试保留异步生成器强引用，检查 finally 已在返回前执行，不依赖垃圾回收制造
“看起来已关闭”。覆盖静默无事件、半段文本后静默、异设备完成/拒绝/媒体、上传和
取件阻塞、取消，以及不提供 aclose 的旧自定义迭代器兼容性。

## 本轮没有证明的内容

本轮没有恢复或绕过 confidential VM attestation、没有证明 resume 游标的服务器
包含/排除边界，也没有改变现有 delegation/CVM 硬停。订阅自动重连仍有历史证据中
标明的服务端补发不确定性。没有新增真实配额接口或编造每周百分比。

后续可继续对照新的静态 bundle 和合法会话抓包，验证 resume ACK/游标语义、取消
报文及配额来源；没有证据前不扩展 wire 字段。
