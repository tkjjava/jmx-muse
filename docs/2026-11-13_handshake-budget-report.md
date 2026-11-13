# Noise 握手预算与取消活网证据

## 当前结果

2026-11-13 Asia/Shanghai，一次直接 Noise 协议请求成功：本次自行发起回合收到正文后
调用 cancel_chat，返回 cancelled=true，随后客户端识别终态，总耗时 20.981 秒。
未见 delta.message_done，因此未观察到 interrupted；此记录证明服务器接受取消 ACK，
不能证明 ACK 对应的实际停止时刻，也不能证明线程/跨设备取消范围。
未打印正文、账号名、token URL 或凭据。未修改运行服务与账号库。

本轮同时补齐从前端静态实现确认的握手约束。新增 7 条回归；在并行 WS 适配修改后的
当前工作树完整测试 **1780 passed**、ruff 全绿，定向握手及客户端回归 168 passed。

## 前端证据

本地 work/muse-bundles/2knhq5aux36zv.js，SHA256：

```text
28d24289e82b1df17c679e1c0b785e66d8654d37ccb7ae1006143dfba5c396e7
```

0-based UTF-8 字节偏移：

| 偏移 | 事实 |
|---:|---|
| 40211 | timeoutMs 默认 1e4 |
| 40236 | maxMessageBytes 默认 65535 |
| 41951 | msg2 收到后先检查消息大小，再 readMessage2 |

perform 在开始时记录 g=performance.now()，设定同一 deadline=g+timeoutMs；
initialize、writeMessage1、msg2 等待/处理、msg3、split 的异步步骤均取该 deadline
的剩余时间，而非每阶段重新计时。这与只限制 msg2 recv 不同：msg1/msg3 send
阻塞也必须受到同一预算约束。

## 实现

- ws_open_timeout=30.0 独立控制 WebSocket Upgrade，保持原有打开预算。
- handshake_timeout=10.0 控制整段 Noise 握手，以 asyncio.timeout 包住调用。
- msg2 在解密前检查长度 <=65535；传输期 WS max_size 不因此缩小。
- 超时及外部取消统一走半开会话清理；清理后才向调用方返回异常。
- 仅 budget.expired() 才标记“握手总预算到期”；底层自己抛出的 TimeoutError
  保持原样，不误判为当前预算超时。

该截止时间依赖 asyncio 合作调度，不能抢占同步密码运算。资源关闭可能超过预算，
不宣称严格墙钟截止。没有增加自动重试、动作重放或 CVM 证明旁路。

## 可复现验证

在项目根目录：

```powershell
python -m pytest tests/test_protocol_connect_diagnostics.py tests/test_protocol_client.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests
```

回归分别卡住 msg1 send、msg2 recv、msg3 send，验证总预算都可触发且 WS/cipher
在返回前已清理；对共享期限在 msg1 后立即到期的情况做确定性测试，验证不会在
msg2 新建计时器；长度边界 65535 可交给解码器，65536 不进入解密。

## 尚未证明

本次成功不解释此前 Upgrade HTTP 502 的根因，也不表示代理/边缘/VM 已永久恢复。
取消 true 不等于观察到了 interrupted，且取消与自然完成仍可能竞争。暂不把主会话
取消绑定普通 close/aclose，不扩大到其他会话或线程。
