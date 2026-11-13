# 兜底代理地址与失败状态边界完善

## 结果

本轮仅修改 ws_fallback.py 并新增 test_ws_fallback_boundaries.py，未联网、未修改凭据
或运行配置。新增 24 个用例；与现有代理/TLS 测试合计 43 passed。共享工作树完整
套件 1824 passed，ruff 和 diff 检查通过；全量数字包含同时进行的其他协议修改，
不能把全部新增用例归属于本文。

## 修复

| 已确认行为 | 修复后的约束 | 验证 |
|---|---|---|
| split("://") 丢弃 scheme，SOCKS/HTTPS 被误当 HTTP CONNECT | 只接受无认证 HTTP 代理；其余发送前拒绝 | 伪造 open_connection 断言零调用 |
| 用冒号切分 host/port，不支持 IPv6 | urlsplit 解析 bracket IPv6，端口限制 1..65535 | IPv6 代理、CONNECT 和 Host 方括号 |
| 带路径/查询串/片段的代理地址含糊解析 | 明确拒绝，不回显原串 | SECRET 不出现在异常文本 |
| 兜底 407/502 只有文字，无状态元数据 | WebSocketProtocolError.status_code 保留安全整数，并附 stage | 上层 ProtocolError 包装仍显示 HTTP 407 |

合法配置接受 host、host:port、http://host:port，以及方括号 IPv6。URL 尾部根路径
可用，其他路径不可用。认证代理、SOCKS、HTTPS proxy 继续需要完整 websockets
实现，本轮没有增加对这些协议的支持，也没有尝试以 HTTP 静默降级。

CONNECT 被拒时 stage=proxy_connect；Upgrade 被拒时 stage=websocket_upgrade。
状态码只保留真正的 int 且范围 100..599，不暴露原始状态行、响应头、正文或令牌。
这些是失败诊断元数据，不改变账号认证分类、不新增自动重试。

## 本地复现

仓库根目录运行：

```powershell
python -m pytest tests/test_ws_fallback_boundaries.py tests/test_ws_fallback_proxy.py tests/test_ws_fallback_tls_integration.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests
python tools/doc_assertions.py
```

新增测试复用实际 connect/HTTP 头解析路径，使用受控内存流并断言失败后 writer 已关。
既有可信证书回环测试同时通过，保证严格代理解析未破坏 CONNECT→TLS→Upgrade 路径。
本轮没有把本地回放称为新的上游实测，也未增加 wire 字段或扩大取消作用范围。
