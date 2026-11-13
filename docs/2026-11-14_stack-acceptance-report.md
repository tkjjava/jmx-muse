# 本机栈验收：muse-bridge 8788 + Muse-Relay 8765（2026-11-14）

本文件记录一次**真实栈**的端到端验收：不是直连协议探针，也不是隔离实例，而是
客户端 → relay → bridge → Noise 协议 → 上游的完整链路。只记录长度、状态、路由与
计数，不含正文、账号名、cookie、令牌。

## 运行态

| 组件 | 事实 |
|---|---|
| muse-bridge | `version=0.4.2`、`chat_driver=protocol`、`accounts_dispatchable=1/1`（复制自旧库的探针时间戳过期，先经 `refresh_account` 刷新）、暖池开启 |
| Muse-Relay | `VERSION=1.13.4`、路由 `muse-bridge` `enabled/configured/weight=100` |

拉起方式：bridge 用 `tools/restart_bridge.py`；relay 用隐藏进程 +
`python -m uvicorn server.app:app --host 127.0.0.1 --port 8765`（密钥从用户环境变量
继承，不写进命令行/文件）。

**已知环境坑**：`relay/scripts/start-stack.ps1` 是 UTF-8 无 BOM + 中文注释，在
Windows PowerShell 5.1 下会按 ANSI 解码并报解析错误（`$cmd = "cd ...` 处开始）。
本机用 PowerShell 5.1 直接调用该脚本失败，改为用隐藏进程分别拉起。这是脚本/环境
兼容问题，不是 relay 代码问题；未改交付包内的脚本。

## 非流式（临时 client key，用后即删）

```
POST http://127.0.0.1:8765/v1/chat/completions
{"model":"muse-1","stream":false,"messages":[{"role":"user","content":"只回复 OK"}]}
```

```json
{"status": 200, "content_len": 2, "finish_reason": "stop",
 "has_usage": true, "elapsed_s": 4.07}
```

relay 审计日志（脱敏字段）：

```json
{"event": "relay_request", "model": "muse-1", "endpoint": "chat",
 "route": "muse-bridge", "status": 200, "latency_ms": 4029,
 "tokens": 3, "streamed": false, "complete": true,
 "attempted": ["muse-bridge"]}
```

bridge 侧计数 `protocol_driver_built` 2→3、`scheduler_account_attempts` 2→3，
证明请求确实穿过 relay 落到 bridge 的协议驱动。

## 流式

```
POST http://127.0.0.1:8765/v1/chat/completions   {"stream": true, ...}
```

```json
{"status": 200, "chunks": 4, "content_len": 2, "done": true,
 "has_usage": true, "usage_keys": ["completion_tokens","prompt_tokens","total_tokens"],
 "elapsed_s": 6.14}
```

即 relay 的流式透传 + `stream_options.include_usage` 自动补 usage 在本机栈上工作正常，
客户端收到 `[DONE]`。

## 过程异常（如实记录，未归因）

第一次拉起 relay 后，`/healthz` 与 `/api/admin/keys` 均正常，但处理第一个
`/v1/chat/completions` 期间 relay 进程消失：无 traceback、日志文件为 0 字节、无
Windows 应用错误事件。随后改用隐藏进程方式重启，连续完成非流式+流式两次验收，
进程稳定。该退出**原因未查明**，可能是启动包装方式/环境清理所致；不宣称已修复，
后续若复现需带 `--log-level debug` 与进程退出码再查。

## 未验证

- relay 的流式 usage 在断线/重试路径上的行为；
- relay 多 key/多 worker 并发；
- 生产式守护（当前两者都是本机手动拉起）。