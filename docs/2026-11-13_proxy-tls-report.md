# 可选 WebSocket 传输的代理 TLS 与资源所有权

## 取证结论

检查标准库兜底客户端发现：wss 经 HTTP CONNECT 代理时，仅建立了 TCP 隧道，
随后直接写入包含 auth_token 查询串的 WebSocket GET，没有执行 TLS 握手。
这是兜底实现的缺陷，不是默认 websockets 后端本轮 HTTP 502 的已证实原因。

实际安装环境没有 websockets 时，协议客户端才自动选择该兜底实现。
项目依赖并不强制安装 websockets，因此这条路径有实际部署意义。

同时确认，opener 在 Upgrade 完成前若超时或取消，已取得的 StreamWriter 没有
统一异常清理；上层只有在 opener 返回后才给 client.ws 赋值，因此 client.close
无法补救该阶段的遗留连接。代理响应头读取原先也缺少等待上限。

已补齐代理隧道后的 TLS、整段打开预算、HTTP 头 64KiB 上限及各阶段异常/取消清理。
新增 19 条回归（含三条真实回环 TLS 集成），完整套件 **1799 passed**，ruff 全绿。
传输测试以 ResourceWarning 视为错误再次运行，19 passed，无资源警告。

## 真实上游诊断边界

2026-11-13 Asia/Shanghai，本轮两次补充 bootstrap-only 检查结果：

| 检查 | 后端 | session/token | 阶段 | HTTP | 耗时 |
|---|---|---|---|---:|---:|
| 1 | websockets | token_ok=true | websocket_upgrade | 502 | 2.819 秒 |
| 2 | websockets | token_ok=true | websocket_upgrade | 502 | 2.492 秒 |

未进入 Noise XX，未发送聊天或取消请求。HTTP 502 的生成方可能处于网关、代理
或后端链路，单凭状态不能归因。未切换账号、绕过证书校验、重启 VM 或改服务配置。
静态前端仍使用 hatch.metaaivm.com 的 /v1/noise；没有证据支持随意换主机。

随后链路恢复，使用当前默认 websockets 适配器，仅对自行发起的一次回合验证取消：

```json
{"ok":true,"via":"direct_noise_protocol","cancel_sent_after_own_text":true,"cancelled_ack":true,"observed_done_statuses":[],"terminal_event":false,"recognized_terminal":true,"elapsed_s":13.604}
```

这是正文之后的显式取消 ACK 与已识别终态，不是 observed message_done/interrupted，
也不保证所有后台工作已停止。该成功不能证明兜底 TLS 修复解决了默认后端的 502；
线程、跨客户端、取消 ACK 与实际后台停机的时序仍未核验。本轮不再发送额外聊天。

## 传输约束

安全顺序必须是：TCP 连接代理 → HTTP CONNECT 200 → TLS 握手与主机名验证 →
WebSocket Upgrade → Noise XX。wss 的 HTTP 请求及查询串只能在 TLS 成功后写出。
ws 保持不使用 TLS；这只是 scheme 的既有语义，不把 ws 当作 wss 的自动降级。

writer 在 opener 返回 WebSocket 对象之前由 opener 拥有；CONNECT、TLS、Upgrade
任一步失败或取消都必须关闭，成功返回后才把所有权交给 WebSocket 对象。

取消不自动重放聊天动作。兜底客户端仍不支持认证代理或 SOCKS，部署需要这些能力
时使用完整 websockets 实现，不静默丢弃凭据或协议类型。

## 修复后活网连接

将 opener 显式指定为标准库兜底后，经既有显式代理完成一次 bootstrap-only 验证：

```json
{"stage":"connected","backend":"stdlib_fallback","token_ok":true,"upgrade_ok":true,"handshake_ok":true,"ok":true,"elapsed_s":4.152}
```

该次没有发送聊天，证明修复后的兜底能在当前链路完成 TLS、Upgrade 与 Noise XX。
不把当前成功外推为永久可用，也不将它当作默认后端 502 根因证明。

## 可复现验证

在仓库根目录运行：

```powershell
python -m pytest tests/test_ws_fallback_proxy.py tests/test_ws_fallback_tls_integration.py -q -p no:cacheprovider
python -Xdev -m pytest tests/test_ws_fallback_proxy.py tests/test_ws_fallback_tls_integration.py -q -p no:cacheprovider -W error::ResourceWarning
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests
python tools/doc_assertions.py
```

控制流测试断言 CONNECT → TLS → GET 的顺序，TLS 失败/取消前绝无带 token 的 GET；
覆盖 CONNECT drain、TLS、Upgrade headers 等待点的超时/取消，以及 EOF/异常状态行。
真实回环测试临时生成 localhost SAN 证书并显式信任该证书，保持 CERT_REQUIRED 与
check_hostname；代理记录 TLS ClientHello 位于 GET 之前，独立服务端计算 Accept、
解析掩码帧，完成 Ping/Pong 与二进制回显。改用 127.0.0.1 时证书主机名不符，连接
必须拒绝，且服务端不收到 GET。测试不依赖公网、账号或产品凭据。

历史协议表曾引用当前树中不存在的 test_ws_fallback.py，现改为实际新增测试路径，
不再把缺失文件作为现状证据。关闭清理另有有限宽限，非任意同步代码的硬墙钟上限。
