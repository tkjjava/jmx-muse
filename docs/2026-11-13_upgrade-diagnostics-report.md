# WebSocket Upgrade 故障定位与握手取消清理

## 当前结果

2026-11-13 Asia/Shanghai，本轮两次真实连接检查均收到 WebSocket Upgrade HTTP 502。
Session 续签与 token 获取已通过，尚未进入 Noise msg1/msg2/msg3；没有发送聊天
或取消请求。这解释了上一轮只见 InvalidStatus 的阶段，但不能断言 502 由代理、
边缘网关还是后端 VM 产生，更不能说明取消接口本身失效。

本轮新增 10 条回归，完整套件 **1771 passed**，ruff 全绿。没有自动重放动作、
修改凭据、切换账号或重启 VM/线上服务。

## 真实观测

| 检查 | 耗时 | 阶段 | 异常类型 | HTTP | 聊天/取消 |
|---|---:|---|---|---:|---|
| 包装 opener 的脱敏诊断 | 3.973 秒 | websocket_upgrade | InvalidStatus | 502 | 未发送 |
| 修复后再次检查 | 4.032 秒 | websocket_upgrade | InvalidStatus | 502 | 未发送 |

日志只记录阶段、异常类型与整数状态码；未输出 WS URL、token、headers 或错误正文。
第二次不再依赖额外包装器，生产 ProtocolError 已携带 HTTP 502。

## 修复一：保留安全的 Upgrade 状态码

此前 _open_noise_ws 把失败一律变为“Noise WebSocket 连接失败（InvalidStatus）”，
导致 HTTP 401/429/502 等在协议日志里无法区分。

现在从 exception.response.status_code（当前接口）或 exception.status_code（旧接口）
提取 **100..599 的真正 int**。字符串、bool、超范围数值均忽略。异常消息和响应对象
绝不转字符串；这样既可定位 Upgrade 失败，又不会把 auth_token URL 或响应头带入日志。

没有把 WS 401 武断映射为 cookie 失效：握手目标的认证和 HTTP session 的认证不是
同一个已验证规则。此次也没有新增隐式重试策略。

## 修复二：取消半开 Noise 会话

原先握手异常出口只捕获 Exception；asyncio.CancelledError 继承 BaseException，
因此任务在 msg1 send、msg2 recv 或 msg3 send 时被取消，会跳过 teardown，WS 仍挂在
客户端。调用方未立即 close 时会遗留半开的密码会话。

现在握手出口捕获 BaseException，仅做资源清理后原样抛出：清握手标记和 cipher，
关闭并摘除 WS。不会吞外部取消，不会建立 receiver/heartbeat，也不会继续复用。

回归使用独立事件栅栏在三处握手等待点取消任务，在任务返回前断言 ws.closed、
client.ws=None、双 cipher=None、receiver/heartbeat=None；不依赖垃圾回收或最终
client.close 掩盖清理问题。

## 复现与后续边界

仓库根目录：

```powershell
python -m pytest tests/test_protocol_connect_diagnostics.py tests/test_protocol_client.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests
python tools/doc_assertions.py
```

本轮停止进一步活网重试。后续只有 Upgrade 成功后，才验证本次自行发起回合的
cancelled ACK 与 interrupted/idle 终态；既有的静态取消字段证据不等于活网取消成功。
