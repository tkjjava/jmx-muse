# 更新日志

## [0.4.2] - 2026-11-11（路线图 P0–P2 落地，未发布）

按 `docs/2026-11-11-roadmap.md` 执行 P0（暖池可靠性）、P1（聊天媒体语义）、
P2（可观测性）。**本轮把全量测试第一次跑成全绿**：1548 passed / 0 failed /
0 warnings（此前长期是 1525 passed / 2 failed / 5 warnings）。

### P0-A 暖池生命周期加固

- **初次连接失败/取消清理**：`acquire()` 过去先把槽位写进 `_SLOTS` 再
  `await client.connect()`，失败或取消后**没有任何清理**，留下一条半死连接 +
  脏引用（表现为"这个号之后每个回合都先付一次验活失败"）。现在用
  `try/except BaseException` 覆盖取消路径，失败时摘槽 + 关连接后原样抛出；
  摘槽前做**身份比较**（`slot.client is client`），不会误删后来者建的新槽位。
- **身份指纹覆盖完整 cookie**：`cookie_fingerprint()` 原本只取 value 前 12 字符，
  前缀相同的两套会话会被判成同一身份——真机语义是"拿旧会话打新账号"。改为
  对完整 name+value 集合求 sha256。
- **租用归属**：新增全局单调发号器 `_next_lease()` 与 `WarmSlot.lease_id`，
  `release(account, lease=None) -> bool` 可校验归属；晚归还的旧租用不再减掉
  新一代的引用。**为什么不用槽位内自增**：槽位会被 `drop` 重建，重建后从 1
  重来就会与旧租用撞号（这条是写测试时才发现的，见下）。
- **并发 acquire 明确拒绝**：同账号已持有租用时抛 `WarmPoolBusy` 而不是告警后
  共享游标——账号锁已保证同号串行，走到这里说明有人绕过锁，共享同一条订阅会
  让两个回合抢游标（数据正确性问题，不是性能问题）。

### P0-B 固定失败与警告裁决（三条都有结论）

- `tests/test_noise.py::test_protocol_constants_match_references`：断言
  `len(PROTOCOL) == 27`，实际 28。**测试字面量数错，实现无误**——`noise.py:199`
  按 `len > 32` 分支补零，28 与 27 同属"≤32"这一侧，判定的**前提**没变。
  改测试与 `noise.py:192` 的注释。
- `tests/test_proto.py::test_int64_signed_roundtrip_through_message[2**63]`：
  **测试参数越界**。2^63 与 -2^63 在 64 位补码下是同一条线上字节，任何实现都
  不可能让两者都原样往返——`proto.py:as_int64` 的文档字符串早就记着这个 oracle
  冲突。参数改为合法域上界 `2**63 - 1`；越界输入由
  `test_varint_out_of_64bit_range_rejected` 单独钉住。
- `tests/test_reasoning.py` 的 "coroutine was never awaited" 警告：夹具把
  Playwright 的 `page.locator()` 写成了 `async def`，而它是**同步**的。实现正确，
  夹具写错；改夹具为同步。
- 全量：**1548 passed / 0 failed / 0 warnings**，ruff 干净。

### P1-B 聊天媒体语义

- **回复归属缺口（真 bug）**：`protocol_engine._run` 里 `delta.text_append` 经
  `ours`（按 `chat_context.device_id` 过滤）判定，但媒体分支
  （`delta.presentation` / `message_done` / `tool_done` / `placeholder`）
  **完全没有这个过滤**。同一订阅上别的会话的 presentation 会被当成这回合的
  成品取回来——用户看到的是"问了 A，回来的附件属于上一个请求"。已补 `and ours`，
  并加 `test_foreign_device_media_is_ignored` 钉住。
- **仅媒体回复**：有附件无正文时正常交付（`collect_reply` 返回空串 + 附件），
  不因"没有文本"被判成失败。
- **部分失败**：已发出部分内容后再失败，异常照常冒泡，上层据此把
  `finish_reason` 标成 `length`（未说完），不伪装成正常 `stop`。
- **客户端断开**：生成器 `aclose()` 后取件被取消，不留半成品元数据。
- 新增测试见 `tests/test_chat_media_driver.py`（31 条）。

### P2-C 可观测性

- **新模块 `muse_bridge/observability.py`**：线程安全的计数 + 分阶段耗时
  （`bump()` / `timed()` / `snapshot()`）。纯进程内、不持久化、只记计数与秒数，
  不记 cookie / Bearer key / 提示词。
- **`/healthz` 的 `warm_pool` 补派生量**：新增 `leased_accounts`（按 `refs`
  真实算出）。`/healthz` 明确只放**池内状态**，不放失败率。
- **`/admin/observability`（需 admin key）**：分阶段计数与耗时。已埋点：
  `warm_acquire`（驱动构造耗时）、`protocol_driver_built` /
  `browser_driver_built`（协议优先是否真的生效）、`protocol_fallback`（回退次数）、
  `chat_media_fetch` / `chat_media_store` / `*_failed`、`media_download`、
  `media_sweep_runs` / `media_sweep_removed`。
- **媒体清理真正接上（真缺口）**：`MediaStore.sweep()` 早就写好，但**全仓只有
  测试调它**，生产路径零调用点——`data/media/` 只增不减，`MEDIA_TTL_SECONDS`
  形同虚设。新增 `media.sweep_forever()`（新常量
  `MUSE_MEDIA_SWEEP_INTERVAL`，默认 3600 s）并在 lifespan 里无条件起任务；
  构造放在 `try` 内，磁盘/权限异常只影响一轮而不是让任务静默死掉。
- **回退原因落日志（排障教训）**：`pool` 里协议 WS 失败后回退浏览器，根因只存在
  于内存中的 `fallback_note`，落到调用方时只剩"页面 45 秒没反应"的 504。已加
  WARNING 日志（只含异常类型与消息，不含 cookie）。这条是这一轮真实排障的直接
  产物，见下面的活网记录。
- 新增测试 `tests/test_observability.py`（10 条），含鉴权隔离、计数递增、
  清扫真跑真删、清扫异常不致命、lifespan 建/收清扫任务。

### 本轮活网验证（诚实记录）

- 上一次活网轮（14:0x–15:1x）offline 全绿 + 真机 chat 200/200、暖池命中
  （`warm_accounts: ["muse1"]`，`refs` 回 0）均已复现。
- ~~**本轮收尾时的活网 chat 复验未通过，且已定位为上游侧故障，非本轮改动回归**。~~
  **本条已被下面的"追记"推翻**：失败原因是排障探针的代理形状错误，不是上游故障；
  修正探针后活网全通。此段保留，作为误判的原始记录。
  证据链（`work/probe_protocol_direct.py`，绕开 bridge、只用**未经修改**的
  `protocol_client.py`，最后修改时间 13:40:43，早于本轮全部改动）：

  | 链路 | bootstrap（register/wake/notary） | `wss://hatch.metaaivm.com` 升级 |
  | --- | --- | --- |
  | mihomo `127.0.0.1:18000` | — | `ConnectionResetError` |
  | FlClash `127.0.0.1:7890` | 成功（`vm_state: RUNNING`） | `ConnectionResetError` |
  | 直连（无代理） | 成功（`vm_state: RUNNING`） | `ConnectionResetError` |

  三种互相独立的链路在同一处失败，且失败点在**拿到有效 `auth_token` 与
  `notary_token` 之后**的 WS 升级：bootstrap 全通、VM 是 `RUNNING`，只有
  `hatch.metaaivm.com` 这条 WS 被传输层重置。同一时段
  `/admin/accounts/{a}/refresh`（走 `MUSE_SESSION_PROXY` 打 `/api/session`）
  仍返回 200。旁证：`websockets` 在 `connection_lost` 里访问未初始化的
  `recv_messages`（`AttributeError`），是该库在握手早期失败时的已知噪音。
  一个待验的怀疑方向：`notary_token` 的 restriction 写的是**每 VM 的子域**
  （`<vm_id>.metaaivm.com`），而我们连的是共享的 `hatch.metaaivm.com`——
  若上游收紧了这个 hostname 校验，症状正是边缘直接重置。
- 结论：**该活网失败是环境/上游态，不计入本轮验收**；恢复后应重跑
  `work/verify_p0p2.py`（先 `work/refresh_account.py`）。本轮 P0–P2 的验收
  因此建立在**源码 + 离线测试**上，唯一达成的活网证据是端点可读性与计数真实性：
  `/healthz`、`/admin/observability` 均 200，且真实聊天中记录到
  `protocol_driver_built: 1` / `protocol_fallback: 1` / `warm_acquire` 耗时。

### 追记：上面那条"上游故障"结论**是错的**，已推翻（2026-11-11 16:4x）

那段结论建立在 `work/probe_protocol_direct.py` 的"三条链路一致失败"上，而那个
探针**自己就是故障源**：它把 `proxy` 传成了 `{"http": ..., "https": ...}`（requests
风格的形状），而 `HatchProtocolClient._proxy_url()` 只读 `self.proxy["server"]`，
所以取不到 `server` 就 `return None`，HTTP 与 WS **双双静默直连**。看起来"三种
代理表现一致"，其实是**三种情况都没用上代理**，全部走同一条被墙的直连。

产品代码三处构造都是对的（`protocol_driver.py:115`、`protocol_hub.py:242`、
`muse_spaces.py:607` 统一 `{"server": proxy}`），所以**产品从未受影响**——错的
只有那个排障探针。已按契约修正探针并重新取证：

| 检查项 | 修正前（探针形状错） | 修正后（`{"server": ...}`） |
| --- | --- | --- |
| `/api/session`（经 7890） | 200 | 200（`vm_state: RUNNING`，`vm_id: c2edbb14…`） |
| `/api/hatch/token`（真实 `build_headers`） | 403（因为走的是直连） | **200，拿到 token 与 notary** |
| `HatchProtocolClient.connect()` | `ConnectionResetError` | **成功，5.80s / 4.75s，两次** |
| 活网 chat | 504 | **200 / 14.33s，200 / 3.11s（暖池命中）** |

顺带纠正两个被误读的细节：

- **`/api/hatch/token` 的 403 不是"出口受限"**。仓库 `build_headers()` 带齐
  `Origin` / `Referer` / `Sec-Fetch-*`，用它的真实形状请求返回 200；只有我另拼的
  残缺头才 403。`session_http.build_headers` 是 **token 面的必要条件**，不是装饰。
- **`/api/session` 的真实字段在顶层**（`vm_id` / `endpoint_url` / `vm_name` /
  `vm_state`），`cvm_eligibility.defaultVm` 是空的。照 `defaultVm` 取 `vmAddress`
  会得到空串再诱发 403，这是取样位置错误，不是接口错误。

`notary_token` 的 restriction 确实写的是 per-VM 子域
（`hostname:c2edbb14-….metaaivm.com`），而实际握手连的是共享 LB
`hatch.metaaivm.com`——但对照实验证明**上游当前并没有收紧这个校验**：
LB 入口 101 升级 + Noise msg2（166 字节）+ msg3 + split 全通，而同一条 query
换成 per-VM 子域反而 `SSLV3_ALERT_HANDSHAKE_FAILURE`（TLS 层就不通，与旧结论一致）。
所以 `ws_url_from_endpoint` 默认 `use_endpoint_host=False` 的选择**是对的**，不必改。

验收结论因此从"仅离线"升级为**离线 + 活网**：ruff 干净、1548 passed，
且真机 chat 两回合 200、暖池复用命中（`warm_acquire` 均 0.00023 s）、
`protocol_driver_built: 2` 且 `protocol_fallback` 未出现。

## [0.4.1] - 2026-11-11（真机活网轮，未发布）

- **聊天媒体交付（离线验证）**：协议聊天中的媒体引用不再静默丢弃；通过既有
  `fs_read` 取件并保存到 `MediaStore`，按路径去重、按文件签名识别 MIME，取件共享
  本回合剩余超时预算。SSE 使用 `choices[].delta.media`，非流式使用
  `choices[].message.media`，文本字段保持原样。媒体 URL 使用 `MUSE_PUBLIC_BASE`
  并继续要求 Bearer key；不把凭据放进 URL。非流式收集共用事件路径，并保留媒体
  元数据缓存。新增驱动与 API 契约测试；本轮未触发上游图片/视频生成，外部 relay
  是否保留扩展字段仍须针对实际部署验证。

> 0.4.0 的结论建立在"从未连过真实上游"之上。这一轮首次用一个真实 muse 账号
> 把整条链路跑通，下面每条都带活网证据；凡是仍然只有离线依据的，依旧标原等级。

- **逐路由预算：超时表 + 响应体上限（2026-11-11 13:3x）**：新文件
  `muse_bridge/route_limits.py`，数据抄自 `muse-cli` 的 402 条路由表（24 条
  `requestTimeoutMs` / 18 条 `maxResponseBytes`）。`request()` 未显式给
  `timeout` 时查表（`/api/dyi/export` 3900 s、`/chat/subscribe` 30 s、
  `/fs/thumbnail/*` 600 KiB、其余 120 s + 32 MiB）。上限在**接收路径**执法：
  `_append_body()` 追加前比较，超了停止累积并置 `limit_exceeded`，由
  `request()` 抛 `ProtocolError`——不在接收循环抛异常，避免一个超大响应把整条
  连接打断。订阅流不设上限（body 设计上持续增长）。测试
  `tests/test_route_limits.py` 7 条。
- **订阅流 idle 收尾（2026-11-11 13:3x）**：`read_stream(idle_timeout=...)` 新增
  参数，判定只认"body 有没有长新字节"，轮询空转不算；到点 `return` 并打 WARNING。
  开关 `MUSE_STREAM_IDLE_TIMEOUT`（默认 0=关）。驱动侧 `ProtocolEngine`
  按签名探测后才传（`accepts_kwarg`），窄签名的假件/旧实现不会被多传的 kwarg
  打成 TypeError。真机（idle=60）：两组 chat 200 / 11.8 s、11.7 s。测试
  `tests/test_stream_idle.py` 5 条 + `tests/test_engine_stream_extras.py` 10 条。
- **修 `/admin/accounts/{a}/refresh` 直连被打死（2026-11-11 13:4x）**：
  `session_http._OwnClient` 建 `httpx.AsyncClient` 时从不传 proxy，直连
  `/api/session` 被墙时报 `ConnectError`（refresh 502，`accounts_dispatchable`
  掉到 0）。新增 `MUSE_SESSION_PROXY`（未配时回落 `MUSE_PROTOCOL_PROXY`），
  `renew_session` / `wake_vm` 接受显式 `proxy=`，注入 client 时仍不套代理。
  真机：502 → **200 / 2.07 s**（`via=http`，`vm_state=assigned`），
  dispatchable 0 → 1。测试 `tests/test_session_proxy.py` 4 条。
- **暖 Noise 池（2026-11-11 14:0x）**：新增 `muse_bridge/protocol_hub.py` +
  `ProtocolDriver` 接线，跨派单复用同一条 Noise 会话。前提是既有代码已经支持
  跨回合复用订阅（`protocol_client.py:1737-1742` 的 `reused` + `_seek_to_end()`），
  所以这一层只做缓存/验活/引用计数，**不需要**参考实现的按 `device_id` fan-out
  （本仓账号锁保证同号串行）。开关 `MUSE_WARM_POOL`（默认关）+ `MUSE_WARM_POOL_TTL`
  （默认 300 s）。真机同进程三回合：`dispatch` 6.83 s → **0.00 s / 0.00 s**，
  `connect` 全程 0.00 s，`refs` 恒为 1。`lifespan` 退出时 `close_all()` 兜底。
  修掉两个真 bug：①验活把"订阅必须存在"当真（connect 后根本没有订阅，导致
  暖池 100% 不命中且每回合重连，探针 4.40 s `usable=False` → 修复后
  `warm acquire 0.000 s`）；②验活失败走重连时引用不归一（`refs` 会涨到 2 不降）。
  测试 `tests/test_protocol_hub.py` 23 条 + `tests/test_protocol_hub_wiring.py` 4 条。
- **暖池空闲清扫 + 可观测性（2026-11-11 14:3x）**：`sweep_forever()` 后台时钟
  （默认 60 s）由 `lifespan` 创建/取消，让 TTL 不再依赖"有请求到来"才生效；
  它自己吞异常继续下一轮，只对 `CancelledError` 放行。`/healthz` 新增
  `warm_pool: {enabled, accounts, refs}`——暖池的失败形态是"悄悄没命中"，
  不摊开状态就看不出到底复用上没有。真机：`accounts: []` → chat 200/12.16 s →
  `accounts: ["muse1"], refs: {"muse1": 0}`。
- **被拒四项的机制层逆向（2026-11-11 12:5x）**：新增
  [`docs/DEEP_DIVE_REJECTED.md`](docs/DEEP_DIVE_REJECTED.md)，把协议注册 13 步
  状态机（含 `register-completion` 的 ToS/生日字段、`/api/hatch/lease-vm`、
  `auth.meta.com` bootstrap 的 302 链）、卡池年龄验证（`/api/checkout/{init,
  credential,finalize}`、PTT 的 ECDH-ES/A256GCM `ADD_CARD` 与
  `CREATE_CONTAINER` ticket、卡池原子 claim 与 `fail_count>3` 策略）、暖 Noise 池
  （单账号单 loop、共享 `chat.subscribe`、按 `device_id` fan-out、引用计数与空闲
  回收）、TLS 指纹（curl_cffi impersonate + Edge 156 HAR 头 + `wd/dpr` cookie）、
  402 条路由的 `requestTimeoutMs`/`maxResponseBytes` 分布全部写清；并给出暖池与
  `max_bytes`/idle timeout 的可回滚移植步骤。源码只读，未运行对方项目。
- **真机造出第一个 Space，并验证导出（Spaces 成功路径，2026-11-11 12:5x）**：
  `POST /api/idea-cards/{id}/execute {"mode":"full"}` → `queued` → 20 s 后
  `GET /spaces/v2?include_constructing=true` 出现 `slug=welcome-to-muse`
  （`construction_status=constructing`、`has_server_actions=true`）；此时
  `export.html` 返回 400 `invalid_params`（"server-backed functionality…"）。
  等构建到 `succeeded` 后 **`has_server_actions=false`**，同一路由返回
  **200 / 5,311,894 B 单文件 HTML**；bridge 的 `GET /canvas/api/muse/export/
  welcome-to-muse` 同样 200 / 5,311,894 B。⇒ Spaces 可纯协议创建；导出前必须先看
  `construction_status`，构建中的 400 不是永久约束（旧结论已更正）。
  记为 §1.6.3。
- **VM 重启窗口改为窄重试（2026-11-11 12:4x）**：`/api/session` 的
  `200 {"status":"unavailable","vm_resolution_issue":{"kind":"retryable"}}`
  （muse-cli 记录的重启窗口）过去被一律当 `SessionUnconfirmed` 立即失败，会白白
  触发一次浏览器回退；现在 `session_http` 把 `retryable`/`vm_issue` 带出来，
  协议侧 `renew_with_retry()` 只对 `retryable` 做 2 次退避（1s/2s），其它非
  assigned 仍一次失败；`/admin/.../refresh` 不套重试、仍如实一次事实。
  测试 `tests/test_session_http.py` 4 组 + `tests/test_session_retry.py` 3 条。
- **`b64_json` 图片交付改为真 PNG（2026-11-11 12:3x）**：`/v1/images/generations`
  与 `/v1/images/edits` 在 `response_format=b64_json` 时把 WebP 成品先转 PNG 再
  base64（`media.png_deliverable`，Pillow 可选：缺席时原样返回）。原因：OpenAI
  该字段没有 mime，严格客户端按 PNG 解码，原样塞 WebP 会报 "not a PNG file"；
  上游 `muse2api` 也是"本地转真实 PNG 交付"。URL 端点不变（仍是原始 WebP +
  `X-Muse-Media-Mime`）。测试 `tests/test_media.py` 两条 + `tests/test_generation_api.py` 一条。
- **视频生成真机验收（已花额度，2026-11-11 11:0x）**：`probe_protocol_gen_video.py
  --live-confirm --skip-reference` 真跑文生视频，`video/mp4` **1280×720 /
  duration_ms=5000（约 5.0s）/ 2,963,296 B**，sha256
  `74873812370a8cb72311be48399c218baca5fb7cef7bb7194488b48ef797af82`，MP4 头
  `ftypisom` 合法；事件 20 条（`task.status`→`delta.message_start`→
  `delta.text_append`→`delta.presentation`）、进度 `[25,40,50,100]`、成品落盘
  `work/canvas_re/video-live1/probe-gen-video-product.mp4`。**"协议只能出图"的
  说法作废**；11:4x 又补跑了 `--via-driver`（6,858,917 B / 1280×720 /
  10.0s / sha256 `23de9087…e2d38`，落盘 `work/canvas_re/video-driver1/`），
  驱动 dict 确认含 `duration_ms`，"驱动丢弃时长"的旧说明作废；时长是上游软约束
  （提示词写 5s 实际出 10s）。证据写进 `docs/PROTOCOL.md` §1.6.1c，
  §8.8 #6/§8.9 同步删掉"视频未验收"。
- **断线 resume 真机验收（2026-11-11 11:2x，图像生成回合，已花额度）**：最后一块
  只有离线证据的协议面也补了真机实验——`probe_live_resume_image.py` 连上后发图像
  生成，在 `task.status` 之后、`delta.presentation` 之前**强制 close + abort 底层
  WS 传输**（模拟真实断线），`read_stream` 原地重连：`_reconnect_count=1`，带
  `after_stream_seq=322` / `after_chat_event_seq=0` 重订阅返回 200 且**没有触发
  降级重订**（subscribe 调用仅 2 次），随后尾部事件（model_lock.updated /
  agent.status / task.status / approvals.snapshot）与最终 `delta.presentation`
  照常到达（成品在重连之后才生成完）。⇒ "服务端接受 resume 字段"与"重连后继续
  推送尾部"从推断升级为实测；游标边界（seq=322 本身是否重发）仍未单独钉，本地
  `seq <= 游标` 去重继续兜底。证据 `work/canvas_re/resume-live2/resume-live2.json`；
  代码注释 `RESUME_EVIDENCE` 与 `docs/PROTOCOL.md` §1.6.1d/§8.8 #6/§8.9 同步更新。
  另有一次文本 v1 实验因"掐断时回答已整段到达本地缓冲区"导致 reconnect=0（实验
  设计无效，留档注明，不冒充结论）。
- **浏览器侧活网抓包补上（§1.5b，关闭 §8.8 #5）**：attach-before-navigate 后
  `Network.webSocketCreated` 终于抓到浏览器真实连的 WS URL：常量
  `wss://hatch.metaaivm.com/v1/noise` + `vm_id` + `auth_token=muse-secret-edge.<EdDSA
  JWT>` + `notary_token=endorsement.v1.<JWT>` + `app_id=hatch-web` +
  `app_version=1` + `request_id`（uuid v4），参数顺序与
  `protocol_client._build_ws_url` 完全一致，`notary` 是标准 endorsement 而非
  `delegation.`（与"非机密 VM 才继续"的硬边界互证）。同窗口 `Network.webSocketFrame*
  事件出现 ⇒ 之前"帧事件为 0"是 attach 太晚、不是 worker 创建。未抓到浏览器自己的
  resume 订阅（重连不在窗口内），仍如实标空。
- **协议客户端代理口径修复 + 2 条回归测试**：`_ensure_http()` 现在显式
  `trust_env=False`。2026-11-11 真机踩坑：Windows 系统代理指向 18000（mihomo 节点
  全挂，controller delay 全 503），httpx 默认 `trust_env=True` 会把"直连本来可用"的
  `GET /api/session` 也塞进死代理，报 `ConnectError: SSL UNEXPECTED_EOF`，看起来像
  服务器出口故障。代理从此只认显式 `proxy=`，与 `session_http._OwnClient` 的口径
  一致。`tests/test_protocol_client.py` 新增
  `test_http_client_never_reads_system_proxy` /
  `test_http_client_uses_explicit_proxy_only` 钉住，避免同一份代码"能不能连"取决于
  本机代理健康度。
- **额度面板机制订正（浏览器抓包，`docs/PROTOCOL_QUOTA.md` §3.8）**：用可用代理
  重载已登录页面后让 bridge 打开 设置→使用情况，bridge 200 读到
  `免费版 / 每周限额将在 11月17日重置 / 已使用 7% / 额外的使用额度 0%(剩余 10 亿个
  词元)`——但**面板打开期间没有任何额度取数请求**（Network 只有 `POST /`(settings
  埋点)、`/api/hatch/invite`、`/api/checkout/stripe/publishable-key`、`/api/falco`），
  fetch 钩子同结论；抓 `/api/hatch/invite` 响应体只有邀请营销（`max_uses:30` 等），
  不含用量。⇒ 面板不是"现查 HTTP"，数值随页面 boot 的 RSC/HTML 或 WS 订阅状态到达
  （同账号两次抓 4%→7%，随真实用量变化）。协议侧"无额度端点、`read_quota()` 恒
  `None`"的口径不变；浏览器面板文本仍是唯一可用真实来源（文档同步删掉
  "面板取数点未定位/前端 i18n 拼出来"的旧推断）。
- **参考素材上传 → 生成（真机端到端打通，§1.6.1b）**：第一跑是失败留档——把
  `fs_write` 返回的 `sandbox://workspace/user/files/…` 直接塞进 `/chat/stream`
  的 `items[].path`，服务端 500 `failed to resolve file_ref path sandbox:/…`
  （单斜杠、文件不存在）。在 hatch-web 两处真实调用点定形状后修复：
  `protocol_client._stream_body` 带附件时改发
  `items=[{type:"text",text}, *file_ref]` 且**不发 message**（锚点
  `24thn7in_976_.js@97321`）；`protocol_engine._stage_references` 的
  `items[].path` 只在请求前剥掉 `sandbox://`，用裸 `workspace/user/files/…`
  （锚点 `07hpppij0ssd9.js@67023`，path=uploadPath）。修后真机 round2 成功：
  89,642 B WebP（sha256 `18100185…f22fb`）、进度 25/40/50/100、上传引用
  `sandbox://workspace/user/files/reference_0_735e2289.webp`；证据落盘
  `work/canvas_re/refs-live2/`。复跑
  `python -Xutf8 work/canvas_re/probe_protocol_gen_refs.py --live-confirm`
  （每次 2 次生图额度）。
- **断线重连 / 订阅 resume（离线落地，真机未验收）**：`protocol_client.connect()`
  变成幂等重连入口（先拆旧 receiver/心跳/WS，再续签 `/api/session`、换 token、
  重握手）；`subscribe(after_stream_seq=…)` 按前端 `Z` 夹取 + 游标单调 max
  传已消费 seq；`_resume_subscription()` 两段式降级（带字段被拒 → 退回 `{}`
  订阅 + 跳尾 + 本地 seq 去重），每回合 `max_reconnects=3`、退避 0.5/1/2s
  封顶 4s，401 不重试；`read_stream` 断线原地恢复并从新流继续。证据等级常量
  `RESUME_EVIDENCE` 写清"字段名强锚点 / 服务端接受与重放边界未证实"。
  新增 `tests/test_protocol_resume.py` 12 条（假 HTTP + 真 Noise 被动方假 WS +
  断线哨兵），249 passed。真机没制造过断线，**不许宣称"断线恢复已验证"**。
- **参考素材校验接线 + 有界内存（task-8）**：`_stage_references` 改为**先整批
  校验、后逐个 fs_write**：数量上限（含远端引用，`refs.MAX_REFERENCE_FILES`）、
  单件 40 MiB / 总量 100 MiB / 类型白名单 / 视频 120s 现在都在任何上传前生效；
  空载荷 data URL 由 `refs` 的空文件判定拒绝，不再变成假 `path_ref`；
  `upload_refs` 改为 FIFO 有界（`refs.MAX_REFERENCE_FILES * 8`），跨轮保留
  最近几轮（回显上传件不得当成品）。新增 `tests/test_protocol_refs_guard.py`
  12 条 + driver 元数据透传断言，定向 340 passed。
- **额度协议面静态逆向（负证据 + 一处更正级发现）**：Noise 内层 418 方法白名单
  **没有任何** `usage/quota/billing/plan/credit` 命名（`subscription.callback`/
  `redeem_complimentary` 仅注册表、0 调用点；`wallet.get` 是支付方式目录）；
  `/api/usage`、`/api/quota`、`/api/billing/usage`、`/api/me/usage` 真机 GET
  全 404 `route not found`（69 B，与对照同形）；前端额度面板存在但取数组件不在
  220 chunk 语料内（`"UJXDx"`=46 份词表、`hk:"UJXDx"`=0 引用）。**更正**：
  `/api/session` 也不在内层路由表（内层 GET 同 404），能读它的是外层
  `session_http.renew_session`；因此新增 `HatchProtocolClient.session_quota()`
  走外层 HTTP，`protocol_engine.quota()` 优先调用它（内层 `_request` 分支只留
  给离线 Fake 客户端），并配 `tests/test_protocol_engine.py` 防回退断言。真机只读
  验证 `work/canvas_re/probe_session_quota.py`：`200 / assigned / RUNNING /
  estimated_expiry_at=1794537469`（外层续签后的 hatch_vml 到期时间）。
  新增 `muse_bridge/quota_protocol.py`（303 行，注入式，额度字段恒 None、不
  猜映射）、`tests/test_quota_protocol.py`（22 条）、`docs/PROTOCOL_QUOTA.md`
  （14 候选表 + 43 条可复核锚点 + 7 条未证实项）。浏览器路径仍可读面板文本：
  `/admin/accounts/muse1/quota` 活网实测返回 `免费版 / 每周限额将在 11月17日重置 /
  已使用 4%`（协议侧仍拿不到该百分比）。
- **muse Spaces 只读适配（P3 落地）**：新增 `muse_bridge/muse_spaces.py`（787 行，
  只读协议客户端 + §2.1/§12.3 归一化 + 异常→HTTP 映射）与 `canvas_api.py` muse 段
  六个端点：`GET /muse/spaces`、`POST /muse/import`（只导入预览图/名称/链接，
  纯函数不写 muse/不写本地库）、`GET /muse/export/{slug}`（**恒
  `application/octet-stream` + `attachment` + `nosniff` + `CSP sandbox`**，
  绝不同源 inline HTML：导出页可读 localStorage 里的 bridge key 进而同调
  `/admin/*`、`/v1/*`、分享接口）、`GET /muse/idea-cards`、`.../{id}`、
  `.../media/{handle}`（免 Bearer 供 `<img>`，栅格 mime inline、显式排除 SVG，
  按文件头嗅探）。新增 `tests/test_muse_spaces.py` 73 条；canvas 相关 183 passed。
  真机（bridge 8788，重启加载新代码后）：`/muse/spaces` 200 `{"spaces":[]}`（该
  账号 0 Space）、`/muse/idea-cards` 200 **77,718 B / 7 分区**（Ideas、Productivity、
  Financial Management、Relationships、Health & Fitness、Shopping、More ideas）、
  详情 200 2,088 B、图标代理 200 `image/png` 1,045 B `inline`、`import` 404
  `{"error":"not_found","note":"muse 空间不存在：mb-canvas-demo"}`、`export` 404
  同样映射 not_found（错误体不含堆栈/cookie/代理串）。
- **画布「从 muse 导入」面板（前端真实入口，P3 可视）**：工具栏新增
  `#muse-import-btn`（只读分享下隐藏、action 守卫二次拦截），浮层面板两个 tab：
  「Spaces」列预览卡 + 导入按钮（按 `data.muse.spaceSlug` 判重，命中只居中不新建）、
  「灵感卡」7 分区网格（点一张 → 本地 text 便签，`data.muse` 记录
  `ideaCardId/title/iconUrl`，不伪造预览图）；懒加载 + 单 tab 失败可重试 +
  Esc/背景关闭复用浮层栈。无头 Chrome 验收
  `work/canvas_re/qa_canvas_muse.py`：**PASS**（7 分区 / 33 张卡 / 点卡后节点 +1
  / 面板关闭 / 页面 0 报错），截屏 `work/canvas_re/muse-panel-shot.png`。
  契约写入 `docs/CANVAS_DESIGN.md` §3.3/§10，路由列入 README。

- **无限画布（自研产品层，P1+P2）**：新增 `muse_bridge/canvas_store.py`（Doc v1
  校验 + SQLite `canvas_spaces` 表）、`muse_bridge/canvas_api.py`（`/canvas/api`
  的空间 CRUD + 媒体代理）、`muse_bridge/canvas/`（零构建 ES modules：13 个文件 /
  9,896 行，无任何第三方依赖、无构建步骤）。契约见 `docs/CANVAS_DESIGN.md`，逆向依据见
  `docs/CANVAS_PROTOCOL.md` / `docs/CANVAS_BORROW.md`。`app.py` 末尾按顺序
  `include_router` 再 `mount("/canvas")`（反过来 StaticFiles 会吞掉 `/canvas/api/*`），
  mount 带目录存在判断，缺前端目录也不会影响 `/v1/*`。
  - **媒体代理 `GET /canvas/api/media/{token}`**：把 bridge 的随机 media token
    反代成同源 URL，`<img>`/`drawImage` 不携带 Bearer 也能取图且不污染 canvas
    （上游直链不暴露）；保留 Range→206、`Content-Disposition:inline` 与真实
    `Content-Type`。活网实测：muse 出图 86,852 B WebP，代理 200 `image/webp inline`。
  - **画布视觉**（无头 Chrome 实测）：`#0b0d12` 底 + `radial-gradient` 点阵网格、
    `rgba(18,21,28,.72)` + `blur(18px) saturate(1.4)` 玻璃工具栏（52px / 999px 圆角）、
    300px 检查器、100% 缩放、47 个按钮 / 66 个内联图标、首屏 0 报错；Ctrl+滚轮
    1.00→1.43 以光标为锚缩放、普通滚轮平移、拖拽无异常；截图
    `work/canvas_re/ui-shot.png`（89.8% 暗像素、65k 种颜色）。
  - **生成闭环已打通（协议通道）**：在画布输入提示词 → `/v1/images/tasks` →
    （可选）`ProtocolDriver` 纯协议生成（见下条）→ 节点挂
    `http://127.0.0.1:8788/canvas/api/media/<token>` → 保存后
    `/canvas/api/spaces` 的 `node_count` 323→324（活网实测；节点 323→325、
    页面 0 报错，图片卡同源加载成功）。
- **纯协议图像生成真机打通（本轮最大增量）**：`ProtocolDriver.generate` 从
  `NotImplementedError` 占位改成真实实现（引擎事件流 → `fs/read` 分页取件 →
  落盘 → 翻译成浏览器驱动同形 dict，`via="protocol"`）；`pool._build_driver` 让
  `capability="generation"` 与聊天共用同一次开关；`pool.generate_media` 增加
  **一次浏览器回退**（仅"可换号重试"的 `ProtocolGenerationError`；拒绝/额度不足不换链路，
  由 `_generation_fallback_allowed` 把关）。三层活网证据见 `docs/PROTOCOL.md` §1.6.1：
  引擎直连 181,726 B WebP；**`MUSE_CDP_URL` 指向无人监听端口仍 26s 出图**
  （685,900 B，`queued→processing→completed`）；画布 UI 全流程成功。
  离线测试：`tests/test_protocol_driver.py` 新增/改写 8 条（生成翻译、video 分派、
  非法 expect、空字节拒绝、派单选择、回退白名单），全仓 1254 passed + 2 条已知红。
- **只读分享（画布产品层补齐）**：`canvas_store` 新增 `canvas_shares` 表与
  `share/share_of/resolve_share/revoke_share`（128 位随机 token、同空间轮换、
  删空间连带清 token）；`canvas_api` 新增 `POST/GET /spaces/{id}/share`、
  `DELETE /shares/{token}`（Bearer）与 **免鉴权** `GET /shares/{token}`
  （capability URL 只读口，没有任何凭 token 写入口）。前端：工具栏分享按钮、
  `?share=<token>` 只读模式（隐藏空间菜单/保存/撤销/重做/设置/提示词/检查器，
  保留缩放平移选中、查看大图、导出 PNG；写类 action 与快捷键统一弹拒绝提示）、
  `.readonly-badge` 与只读样式。离线测试新增 10 条（store 6 + API 4，覆盖轮换、
  重启持久、撤销幂等、删除级联、免 key 读但写仍 401、GET 无副作用）。
  真机验收（headless Chrome）：只读页 323 卡片 / 240 张图 / 0 页面错误、
  8 个写入口全隐藏、Ctrl+D 被拒且节点数不变、导出 296 张卡片 2848×1610；
  编辑页点分享得到可用 token。全仓 1264 passed + 2 条已知红。
- **异步任务端点修复（真 bug，画布联调时暴露）**：`POST /v1/images/tasks`
  之前会建**两条**任务行——`app._submit_task` 一条，`pool.generate_media`
  又建一条；客户端轮询的那条永远停在 `queued`，真正干活的那条在任务表里没人看。
  现在 `generate_media` 接受 `job_id` 复用已有任务行，`_runner` 兜底把
  "生成前就抛"的早期失败也写进终态。活网复测：queued → processing → completed
  （28.1s），`media_token` 与 `url` 一起回填。新增两条契约测试钉住"只有一行任务"。
- **muse 侧"无限画布"结论**：muse 前端**没有**画布文档模型（`react-flow`/`konva`/
  `minimap` 全仓 0 命中；Space 字段表无坐标/节点/相机）。Space 是 AI 生成的
  Web 应用构件（CVM 渲染 HTML + 同源 iframe + MessagePort），`spaces.save` 只对
  已存在 artifact 生效（活网三连 404）。因此画布是自研产品层，"适配 muse"落在
  生成链路与 Space 列表/导出，不复制它的文档模型。证据：`docs/CANVAS_PROTOCOL.md`
  §2/§3、`docs/CANVAS_DESIGN.md` §10、`docs/FRONTEND_RE.md` §10。

- **出口治理（新增，非代码能力）**：muse 的网页/HTTP 直连可达，但
  `*.metaaivm.com` 的 Noise WebSocket 直连不通（TLS 握手失败/超时）。新增
  `docs/LIVE_RUNBOOK.md`：用本机 mihomo 加两条 `DOMAIN-SUFFIX` 规则
  （`muse.ai` / `metaaivm.com` → 代理，`MATCH,DIRECT` 其余直连）+ 系统代理指向
  `127.0.0.1:18000`。这是运行环境，不是仓库行为；关掉代理后页面回到
  `Muse 正在连接…`。
- **连接就绪门（真机修复）**：页面停在 `Muse 正在连接…` 时，旧代码会照常填字、
  点发送，消息根本没发出去，调用方等满 180 秒拿到 `timeout`，账号还被标
  `rate_limited`。新增中文标记识别（`_JS_HYDRATION` 同时认 `Connecting...` 与
  `正在连接`）+ `_send` 发送前最多等 12 秒；等不到抛 `ChannelUnavailable`
  （`BrowserUnavailable` 子类），HTTP 出口变 503 `browser_unavailable`、账号记
  可重试状态。活网复测：断代理 → **503 / 12.7 秒**；恢复代理 + refresh →
  `ready` → chat 200 `12+12等于24。`。
- **`POST /admin/accounts/{a}/wake` 改走纯 HTTP**（有导入 cookie 时）：真机上
  浏览器路径在页面「正在连接」时只会回 `{"ok": false}`，把原因藏掉；现在与
  keepalive 共用 `renew_session` + `wake_vm`，返回
  `{"ok":true,"refreshed":true,"woken":true,"via":"http","vm_state":"RUNNING"}`
  并自动落库新 cookie（活网 200 实测）。
- **额度读取失败重试一次**：bridge 重启后第一次 `/admin/accounts/{a}/quota`
  出现过 504（`quota_unavailable`），紧接着同一账号再请求就 200。原因是 dock 的
  "设置"入口是 hover 展开的按钮，冷启动第一下容易点在动画中的空位。改为
  `_open_quota_panel` + 关面板重开一次；活网复测两次均 200
  （`weekly_used_pct:2`、`weekly_reset:"11月17日"`）。
- **浏览器真实 WS 地址取证**：CDP 监听到页面建的连接（此前一直抓不到），
  确认 `wss://hatch.metaaivm.com/v1/noise?vm_id&auth_token&notary_token&
  app_id=hatch-web&app_version=1&request_id`，token 长度/前缀与
  `/api/hatch/token` 返回值一致。证据与脱敏字段见 `docs/WS_TARGET_EVIDENCE.md`。
- **纯协议通道首次真机打通**：Noise XX 握手、`/client/register-capabilities`、
  `/chat/subscribe`（NDJSON）、`/chat/stream` 全程不依赖浏览器，助手正文来自
  `delta.text_append.payload.text`。详见 `docs/PROTOCOL.md` §1.6 与
  `docs/PROTOCOL.md` §1.1 的 host 结论更正（per-VM `endpoint_url` 子域**不是**
  WS 目标，TLS 握手失败；LB `hatch.metaaivm.com` 才是）。
  - 客户端按真机 wire 重写：register 只发 `{"client_id"}`（字符串 capabilities
    会 400 `expected a map`）、subscribe 必须 `end_body=True` + `{}`（旧的
    `end_body=False` 服务端永不回响应头）、`/chat/stream` 四字段 body、
    订阅流按行增量消费 NDJSON；`_turn_ended` 多条件收尾（必须已收到正文）。
    活网：`HatchProtocolClient` + `ProtocolEngine` 不碰 CDP 拿到
    `13+13等于26。`（建连 3.1s、事件流 6.1s）。
  - **引擎收尾修正（同轮抓到）**：`protocol_engine._run` 原来只认
    `delta.message_done` 当 done，而真机序列没有它——流按 `agent.status` 收尾后
    引擎会抛"未收到完整结果"，把成功回答变成报错。改为"已收到正文且未到
    deadline 即算完成"；零正文仍然报错，不放宽成空回答。
  - **多轮**：同一客户端、同一订阅流连发 `14+14` / `15+15` →
    `14+14等于28。`（3.4s）/ `15+15等于30。`（2.4s），第二轮不回放上一轮正文
    （复用订阅先 `_seek_to_end`）。**仍未验证**：断线重连/resume（无重连实现）。
- **协议驱动接进派单路径（opt-in，0.4.1 主体）**：新增
  `muse_bridge/protocol_driver.py`（`ProtocolDriver` 适配浏览器驱动的鸭子接口：
  `status/wake_vm/touch_session/stream_reply_events/stream_reply/collect_reply/
  quota/close`，`generate` 故意 `NotImplementedError`）。`pool.dispatch()` 在
  `MUSE_PROTOCOL_DRIVER=1` + 账号有导入 cookie + `capability="chat"` 时用协议驱动，
  `ProtocolError` 时**回退浏览器**并在 note 记 `protocol_fallback`；生成
  （`capability="generation"`）**永远**走浏览器。响应头新增 `X-Muse-Driver`
  （`protocol`/`browser`），`/healthz` 新增 `chat_driver` 与
  `protocol_proxy_configured`，验收/排障不用翻日志。活网：流式与非流式
  `/v1/chat/completions` 均 `200` + `X-Muse-Driver: protocol` + 正确正文
  （`18+18等于36。` / `17+17等于34。`），无 `protocol_fallback`。
  - 顺带修掉两处接线前抓到的真 bug：`protocol_engine._request` 把 timeout 按位置
    传（真客户端是 keyword-only，额度路径会 TypeError）；`app.py` 没有映射协议
    异常（`ProtocolError`/`ProtocolGenerationError` 会从 SSE 流里逸出而不是
    `finish_reason=length`/`503`）。现在拒绝=502 不改账号、认证失败=401
    `session_expired`、限流/额度=429、超时=504，全部可审计。
- **前端静态产物复核（握手/常量第二证据源）**：220 个 chunk 落盘
  `D:\deep seek\work\muse-bundles\`，逐常量对拍后确认
  `hatch.metaaivm.com` / `/v1/noise` / `65489` / `256` /
  `Noise_XX_25519_AESGCM_SHA256` / nonce 布局 / 传输期 AD=`b""` 全部一致；
  新证据是**服务端静态公钥不来自 `/api/session` 也不硬编码**，它由 Noise msg2
  的加密块送达（标准 VM 直接信任 = TOFU，机密 VM 再走 attestation/hostData 绑定）。
  据此补上 WS query 的 `app_version=1`。详见 `docs/NOISE_BUNDLE_NOTES.md`、
  `docs/FRONTEND_RE.md` §9。已知缺口：并发装配 ≤16 组/16MB 预算未实现。

- **画布分享面板补齐"链接管理"（2026-11-11 11:3x，真机 headless 验收）**：此前
  分享按钮是"点一下就 POST 轮换 token 并复制"，用户既看不到当前链接状态，也没有
  撤销入口（撤销只能调 API）。现在 `#share-panel` 面板做完整生命周期：未分享→
  引导生成；已分享→只读 URL 输入框 + 状态/创建时间/权限 + 「复制 / 重新生成 /
  撤销」；轮换与撤销都走二次确认（不可恢复且旧链接立即失效）；读取失败单独渲染
  错误态 + 重试。`api.js` 新增 `revokeShare(token)`（`DELETE
  /canvas/api/shares/<token>`，幂等）。真机验收
  `work/canvas_re/qa_canvas_share.py`（headless Chrome + 8788）：生成→读口 200、
  轮换后旧 token 404/新 token 200、撤销后新 token 404、面板回到未分享态、
  页面 0 异常，截屏 `work/canvas_re/share-panel-shot.png`（650,251 B）。排版几何
  另跑 `probe_share_panel_layout.py`：1440×900 / 900×640 / 640×720 三档视口下
  对话框均在视口内、无横向溢出、按钮不重叠且全可见（结果
  `work/canvas_re/share-panel-layout.json`）。新增
  `tests/test_canvas_share_ui.py`（4 条静态契约：DOM id、`data-share-action`→
  action 映射、`DELETE` 语义、`?share=` capability），防改名静默坏页。
- **只读分享手势漏洞修复（2026-11-11 11:5x，真机探针发现）**：`qa_canvas_readonly.py`
  在只读页 `?share=<token>` 下双击画布空白，节点数 323→324——`_onDoubleClick`
  直接调 `_createTextNode` 绕过了 action guard（按钮/快捷键的拒绝只覆盖了 action
  层）。同批把节点拖动（含 `bringToFront` 的 z-order 变更）、`[data-handle]` 缩放、
  节点操作按钮全部纳入只读拒绝；选中/平移/大图查看仍保留。修后探针 323→323、
  拒绝 toast 正常、token 不会被静默轮换、页面 0 异常。探针
  `work/canvas_re/qa_canvas_readonly.py`，截屏 `share-readonly-shot.png`。
- **`/admin/accounts/{a}/refresh` 改为浏览器可选（2026-11-11 11:5x，真机验收）**：
  此前 `refresh` 是唯一无条件 `MuseDriver` 的运维端点——浏览器没开就 503，与
  `keepalive`/`wake` 的"有 cookie 走纯 HTTP"口径不一致。现抽 `_renew_http` 统一
  错误映射，`refresh` 有 cookie 时只调 `session_http.renew_session`：不建
  `MuseDriver`、不碰 CDP，并把服务端新 `Set-Cookie` 落回加密列。真机：账号探针
  过期 `accounts_dispatchable=0` → `refresh` 返回
  `{"via":"http","refreshed":true,"vm_state":"assigned"}` → `/healthz`
  `accounts_dispatchable=1`；随后 `/v1/chat/completions` 17.1s、响应头
  `X-Muse-Driver: protocol`。无 cookie 的账号仍走原 CDP 分支（既有 503 语义不变）。
  新增 3 条离线契约（HTTP 优先且不构造浏览器 / unassigned 给原因 / 401 映射），
  `tests/test_generation_api.py`；README 增补"功能 × 是否需要浏览器"对照表，
  明确仍必须浏览器的只剩**额度面板**与**首次登录/attach/cookie 导出**。

## [0.4.0] - 2026-11-11 — 纯协议通道落地（未接真实上游）+ 长尾端点

- **纯协议通道**（本轮主体，全部离线验收）：Noise_XX_25519_AESGCM_SHA256 握手
  （`muse_bridge/noise.py`，与两份独立参考实现字节级对拍）、手写 protobuf wire
  解码/编码（`proto.py`）、三阶段 bootstrap + HTTP-over-Noise + NDJSON 事件流 +
  `/fs/*` 分块读写的客户端（`protocol_client.py`）、生成编排
  （`protocol_engine.py` / `protocol_generation.py`）。
  **本仓库从未连接真实 muse.ai**（无账号、无授权），因此每条结论都标了验证等级
  （observed / corroborated / hypothesis / unverified）；没证实的能力走
  `UNVERIFIED` 并显式抛带原因的错，不假装能跑。未证实端点（`/gen/*`、
  `/api/hatch/generate` 等）调用即抛，不会"发出去碰运气"。
- **重要边界**：生成的实际执行仍走浏览器（账号库里的凭据是 Chrome profile +
  CDP 端点，不是 cookie 串），纯协议层尚未接进 `pool.generate_media`。细节与
  接入前提写在 `docs/PROTOCOL.md` 第九章；任何文档都不许宣称已经替换浏览器。
- **无浏览器续期落地（可用的那一小块）**：新增 `POST /admin/accounts/{a}/cookies`
  导入 cookie（落 Fernet 加密列，响应只回 cookie 名字不回值）；`keepalive` 在有
  导入 cookie 时改走纯 HTTP（`session_http.renew_session` + `wake_vm`），返回值带
  `via:"http"|"browser"`，服务端回的新 cookie 自动落库。401/403/429/502 按
  `SessionError.status_code` 映射，不折成统一的 503。
- **参数白名单单一出处**：新增 `muse_bridge/gen_params.py`（`PARAM_SPEC` 只收
  observed/corroborated 字段；`seed/stylize/--st/--chaos/quality/...` 全在
  `HYPOTHESIS_FIELDS` 里显式拒绝）。fal 入口改为走 `gen_params`
  （`时长严格为 N 秒` / `画面比例严格 X`），不再自己拼 midjourney 风格
  `--duration/--aspect`；`protocol_generation` 的时长/比例常量改为从
  `PARAM_SPEC` 取值，不再有两套白名单。传 `seed` 现在回 400 并说明"未实证"，
  而不是悄悄编进提示词骗客户端。
- **上游兼容层**：新增 `muse_bridge/upstream_quirks.py`（纯函数、零 I/O）：
  `normalize_state`（succeeded/success/done→completed，未知→unknown）、
  `PROGRESS_MILESTONES`（上游是里程碑不是百分比，禁止插值）、`renew_policy`
  （defer/probe_unknown/renew_now，12 小时窗口）、`media_url_policy`
  （拒无鉴权直连）。每条带上游 文件:行号 与验证等级。
- 端点补齐：`POST /v1/images/edits`（JSON 变体；multipart 显式 400 并点名原因）、
  fal 队列兼容（`GET /v1/fal/models`、`POST /v1/fal/muse/{image,video}`、
  `GET /v1/fal/muse/requests/{id}[/status]`）、`GET /admin/tasks`、
  `GET /admin/tasks/{id}`、`POST /admin/tasks/clear`、
  `GET /admin/accounts/{account}/quota`、`POST /admin/accounts/{account}/keepalive`、
  `DELETE /v1/media/{token}`。
- 修复（本轮实测抓到的、都在入队/轮询主路径上）：
  - `_prompt_of` 不再静默丢多模态 `image_url`（改由 `_collect_references`
    收成参考素材，不再让图片凭空消失）；
  - 同步生成响应的 `task_id` 之前恒为 null，现在回真实 id；`b64_json` 之前对同一个
    token 读两次盘，现在读一次；
  - `list_tasks` 的排序 tiebreaker 从随机 `task_id` 换成 `rowid`（同一秒创建的两条
    任务会被随机排序，`limit=1` 拿到哪条不确定）；
  - **`_submit_task` 是同步函数却被 4 处 `await`**（`/v1/images/tasks`、
    `/v1/images/generations` 异步分支、`/v1/videos`、fal 提交）：`await dict` 直接
    TypeError→500，这四条入队端点此前根本跑不通；
  - **`_job_response` 从 `payload["state"]` 取状态**，而 `Job.to_dict()` 的键是
    `status`：每条任务的对外状态都被写成 `unknown`，客户端轮询永远看不到
    queued/completed。改为读 `job.state` 再过 `_normalize_state`；
  - **`protocol_generation.generate()` `await` 了异步生成器**：`_event_source` 是
    异步生成器函数，`await` 它必 TypeError，纯协议生成流程一步都走不动；
  - `TaskStore.clear(keep=N)` 的保留名额不再被活动任务占用：活动任务本来就不会被删，
    占掉 `keep` 只会让运维以为"清空没生效"；
  - `/v1/images/generations` 响应补 `width`/`height`（与 edits 同形状）；
  - **`protocol_engine.retryable()` 把 frozenset 传给 `isinstance`**：任何异常都
    TypeError，"这条失败该不该换号"变成新的崩溃点。改为 `tuple(NON_RETRYABLE)`；
  - **`_stage_references` 调不存在的 `refs.ext_from_mime`**：任何 data URL 参考素材
    在上传前就 AttributeError（参考图链路完全不可用）。改用
    `refs.REFERENCE_MIME_EXT`；
  - **0 字节上传恒失败**：`int(info.get("size") or -1)` 把 0 变成 -1，报错还自相矛盾
    （"声明 0，stat 说 0"）。改为显式判 None；
  - **`connect()` 对未知 `vm_state`（缺字段/STARTING）既不 wake 也不拒**：会带着一条
    没起来的 VM 继续换 token/握手，表现为随机超时。未知一律按"未 RUNNING"处理，
    wake 之后仍非 RUNNING 直接抛；
  - **`Idempotency-Key` 先建任务再查键**：重复请求会白建一条永远没人轮询的任务，
    而客户端只看到 409（既非幂等又泄漏任务）。改为**先查/占位再提交**，同键重放
    同一个任务；
  - **`protocol_engine` 的生成循环不在流内裁决**：真实 `read_stream` 是长连接，
    收到 `message_done` 不 break ⇒ 只能等满 timeout（或永远拿不到成品）。成品已到
    或宽限期满即跳出循环再统一裁决；
  - **未 `connect()` 就发请求**：`send_cipher` 是 None，在 `None.enc` 上抛
    AttributeError。改为协议层 `ProtocolError("尚未连接")`。
- **安全加固（漏洞表 V-01）**：管理面与调用面分钥匙——新增
  `MUSE_BRIDGE_ADMIN_KEYS`（设了则 `/admin/*` **只**认它，普通调用 key 进不来，
  两个作用域互不越界）；未设时退回 `MUSE_BRIDGE_API_KEYS` 并在 `/healthz` 的
  `admin_key_separated` 标 false。对应上游 czg/bynne/bitgacon 三份实现的同款缺陷。
- **对外绝对 URL**：新增 `MUSE_PUBLIC_BASE`，任务与同步响应里的媒体 url 变成
  `https://<base>/v1/media/{token}`；留空保持相对路径。
- 新增测试：`tests/test_protocol_regressions.py`（回归钉：retryable、data URL 上传、
  0 字节上传、未连接请求）、`tests/test_protocol_client.py`、`tests/test_protocol_engine.py`、
  `tests/test_protocol_generation.py`（含"未证实端点必抛"、"产物绝不从
  `workspace/user/` 回声"、"CDN 直链必抛"、"done 后 12 秒宽限"）、
  `tests/test_generation_api.py`（长尾端点契约 + cookie 导入 + 纯 HTTP keepalive）、
  `tests/test_gen_params.py`、`tests/test_upstream_quirks.py`。
- 新增文档：`docs/FRONTEND_RE.md`（前端逆向结论 + 证据表 + 验证等级）、
  `docs/HARVEST.md`（横向抄袭对照）、`docs/VULN_HARVEST.md`（上游漏洞表）。
- 校验：`python -Xutf8 -m pytest -q` = **2 failed, 1060 passed**，仅剩 2 条
  `docs/KNOWN_ORACLE_CONFLICTS.md` 记录的**故意**冲突（PROTOCOL 名长度上界、
  int64 边界回环），不是回归；`python -Xutf8 -m ruff check muse_bridge/ tests/`
  全净。

### 实测修复（真机）

2026-11-11 在一台真机（真账号 + 真 muse.ai 页面）上跑出来的三条：都有现场现象，
也有修完之后的复测。证据细节见 `docs/PROTOCOL.md` §4.1.1 / §4.5 与
`docs/FRONTEND_RE.md` §3.6。`driver.py` 本轮有并行改动，行号以引用时的树为准，
括号里的函数名是更稳的定位。

- **聊天气泡的角色判据排到通用类名之前**（`muse_bridge/selectors.py` 的
  `ASSISTANT_MESSAGES`，写作时 `:75-86`）：真机消息根容器带
  `data-message-role="user"|"assistant"`，而气泡类名 `hatch-chat-groupable-bubble`
  **用户与助手都命中**。旧顺序（气泡类名在前）会把用户提问当成回答，现场表现是
  非流式回答里出现 `你：` 回显前缀。现在最精确的正文容器
  `[data-hatch-assistant-message-body="true"]` 在首位，role 判据在前、通用类名在后；
  `[data-role="assistant"]` 在当前构建刷新后不命中，降为垫底兜底。
- **额度面板改真实鼠标点击 + 菜单项前缀匹配**（`muse_bridge/driver.py` 的 `quota()` /
  `_click_settings_entry()` / `_click_settings_menu()` / `_close_overlay()`）：
  `/admin/accounts/muse1/quota` 从 **504** 修到 **200**。入口
  `button[data-testid="hatch-dock-more"]`（`aria-label="设置"`）只认真实指针事件，
  JS `el.click()` 不触发它的 React 处理，必须先 hover 再 click；点开先出
  `[role=menu]`（`键盘快捷键 Ctrl+/` / `下载应用` / `报告问题` / `设置 Ctrl+,`），
  旧实现按 `textContent` **全等**匹配「设置」，而真实文本带 `Ctrl+,` 后缀 → 永远匹配不到，
  等超时即 504。现按前缀匹配，读完按 `Escape` 关掉对话框，否则遮罩挡住下一次输入。
- **`_JS_EXTRACT` 补右括号**（`muse_bridge/driver.py` 的 `_JS_EXTRACT`，写作时 `:1751`）：
  `(async (pair) => { … }` 少一个 `)`，Playwright `page.evaluate()` 抛 `SyntaxError`，
  `_js()` 把异常吞成 `None`，于是 `media_links()` 收上来的 `blob:` 源一律取不回——
  生图表现为 200 生成、500 交付（「已生成但未能取回文件」，由 `extract_media()` 抛出）。
  补上后同一 blob 取回 `image/webp`（371KB / 448KB），base64 正常。新增离线哨兵
  `tests/test_driver_js_syntax.py`（括号配平 + 必须是函数表达式或可拼接 `const` 片段）。

## 2026-11-07 - 多人部署安全阀：裸奔警告

- `MUSE_BRIDGE_HOST` 指到非回环且 `MUSE_BRIDGE_API_KEYS` 为空时，启动在
  stderr 打警告：任何能连到端口的人都能消耗共享账号池（`require_key` 在
  密钥表为空时直接放行）。多人使用的正确姿势写进警告文案：走 Muse-Relay
  统一鉴权与配额，或至少设上 API 密钥。默认行为不变，只出声。
- 注意 `MUSE_BRIDGE_KEY` 是凭据加密密钥、不是访问令牌，不参与该判断。
- `tests/test_exposure_warning.py` x4；harness +`exposure-warning-silenced`，
  19 项全部 LOAD-BEARING。

## 2026-11-06 — 发送没生效要早说，别等满 timeout 赖模型

- **缺陷（误归因）**：`_send` 点完发送（或回车兜底）后不做任何校验就进入轮询。
  消息没发出去时（发送按钮在 0.15 秒落定等待后仍禁用、回车在某个 UI 上变成换行、
  页面改版），代码要空转满整个 `timeout`（默认 180 秒）才抛
  `180 秒内没有产生新回答`——把"根本没发出去"误报成"模型慢"，运维会去查上游。
- 而代码手里早有判据：页面上连新气泡都没出现。现在新增
  `FIRST_REACTION_SECONDS`（env `MUSE_FIRST_REACTION_SECONDS`，默认 45 秒），
  同时满足三条证据（无新正文、无思考、无流式标记）时立即抛
  `发送后 N 秒页面没有任何反应——消息很可能没有发出去`，把 3 分钟挂起变成
  45 秒诊断。任一证据存在（流式标记在、思考已出）都会抑制这条早抛，避免把
  "模型确实慢"误判成"没发出去"。
- 新增 `tests/test_send_reaction.py` 三项（第一项修复前为红，另两项钉住抑制条件）；
  变异 harness 新增 `send-reaction-never-fires`，18 项全部 LOAD-BEARING。

## 2026-11-06 — 超时不能再把残篇当完整回答

- **缺陷（静默截断）**：`stream_reply_events` 的循环只有两个出口——静默期满
  `return`，或 deadline 到期。到期后唯一的抛错条件是
  `if not started and not last_content`，也就是**只要开头产出过内容，超时就会
  正常 return**，把半句话当完整答案交出去；`collect_reply` 拼起来返回，上层按
  `finish_reason=stop` 计费交付，客户端拿到一段看不出被截断的残篇。同一函数第
  293 行的注释自己写着"空页面超时要抛错而不是静默返回空"——作者反对静默，却漏了
  "流到一半超时"这条更隐蔽的路径。现在两种超时都抛 `TimeoutError`，错误信息区分
  "没有产生新回答" 与 "回答没有完成（已收到 N 字，残篇已丢弃）"。
- **否定结论（实证）**：排队清单里的"打字节奏纯 sleep 成本"不存在——
  `_send` 用 `box.fill(prompt)` 一次性原子填充，全流程只有一个 0.15 秒落定等待，
  没有逐字符输入。`pool.py` 那句"非流式收集无额外延迟"成立，未改代码。
- 新增 `tests/test_deadline_truncation.py` 三项（第一项修复前为红）；
  变异 harness 新增 `deadline-truncation-silent`，17 项全部 LOAD-BEARING。

## [0.3.14] - 2026-11-06

### 修复（第十六把尺子：派单是否如实记录 VM 状态）

- **唤醒失败却被记成"已唤醒"，账号此后永久跳过唤醒**：
  `dispatch` 里 VM 唤醒是尽力而为（失败不阻断派单），但失败之后代码
  **无条件**调用 `after_probe(..., vm_ready=True)` 与
  `store.mark_ready(..., vm_ready=True)`，把"还在睡"写成了"已唤醒"。
  而派单路径只在 `not record.vm_ready` 时才尝试唤醒——**谎报一次就等于
  此后每次派单都跳过唤醒，直接打到休眠中的 VM 上，账号会一直失败到有人
  手工 refresh**。失败还是静默的（`DispatchResult.note` 为空）。
  实测（真实 store）：
  ```
  派单前  vm_ready=False
  修复前  vm_ready=True  / state=READY        note=''
  修复后  vm_ready=False / state=VM_SLEEPING   note='…唤醒失败 RuntimeError: CDP 中途掉线'
  ```
  现在只在唤醒**确实成功**时记 `vm_ready=True`；失败则如实记为
  `VM_SLEEPING`（它同样在 `DISPATCHABLE_STATES` 里，下次派单会再试一次），
  并把失败原因随 `note` 交回调用方，不再静默。
  `ok=True`（唤醒失败不阻断派单）这条既有契约保持不变。

### 测试

- 全量 113 passed
- `tests/test_concurrency.py` 新增**双向**约束：
  `test_failed_wake_is_not_recorded_as_vm_ready`（失败必须记 VM_SLEEPING
  且 `note` 非空）与 `test_successful_wake_is_recorded_as_vm_ready`
  （成功必须记 vm_ready=True，否则每次派单都会重复唤醒）
- 变异台 **15 项**：新增 `wake-reports-awake`（还原成无条件
  `vm_ready=True` 后指定测试必红）
- 说明：hark-bridge 无此问题——它的派单不写就绪状态，就绪由
  `session_expires_at` 把关，因此本次不涉及 hark，也没有为了"对称"
  去编造对应改动。

## [0.3.13] - 2026-11-06

### 修复（第十五把尺子：自检工具是否说真话）

- **`doctor` 会把任何写库失败报成"检查 data 目录写权限"**：`check_store` 用的
  `TemporaryDirectory` 在 `with` 块退出时清理目录，但 `store.close()` 写在块**内**
  且只在成功路径执行。写库一旦抛异常，SQLite 句柄没关，Windows 上 `rmtree`
  就抛 `PermissionError: [WinError 32] ... probe.db`，**这个清理异常盖住了真正的
  失败原因**。实测：
  ```
  真实错误： RuntimeError 真正的原因：profile 序列化失败
  修复前报： PermissionError [WinError 32] ...probe.db
           提示：检查 data 目录写权限
  修复后报： RuntimeError 真正的原因：profile 序列化失败
  ```
  在排障工具里这是最坏的一种 bug：它把用户引向完全无关的方向。
  现改为 `mkdtemp` + `try/finally` 保证关闭，清理失败不再影响结论。
- **被跳过的检查项从报告里消失**：`muse.ai 登录态` / `端点路径` 在前置检查未过时
  不会执行，但输出里连一行都不出现，读者会当成"检查过、没问题"。
  现在显式打印 `⊘ 已跳过 <项目（原因）>`；并且只要有跳过项就不允许输出"全部通过"，
  退出码为 1——跳过不等于通过。

### 测试

- 全量 111 passed
- 新增 `tests/test_doctor_honesty.py`（3 项）：写库失败必须暴露原始异常、
  正常环境必须通过、任一检查失败 `_run` 必须返回 1
- 变异台 **14 项**：新增 `doctor-error-masked`（还原成不关连接 + 让 rmtree
  抛异常后，指定测试必红）

## [0.3.12] - 2026-11-06

### 修复（第十四把尺子：截断语义）

- **超时截断被当成正常完成**：`REPLY_TIMEOUT` 到点后，`pool` 层的 deadline 检查
  是静默 `return`，因此流式收尾无条件发送 `finish_reason: "stop"`。
  在 OpenAI 语义下 `stop` 表示"模型自己说完了"，而这里是**话被切断了**——
  下游会把半句话当成完整回复，审计与重试判断全部失效。
  同时 `app.py` 里原有的 `except TimeoutError` 是**死代码**：`pool` 从不抛它，
  那个分支从未执行过。
  现在 `pool` 在 deadline 到点时抛 `ReplyTimeout`（继承 `TimeoutError`，
  既有 `except TimeoutError` 的调用方语义不变），app 据此发送
  `finish_reason: "length"`。已产出的**内容照常全部交付**——只改收尾信号，
  不吞任何真实输出。
- hark 侧同类问题：`reader.poll_reply` 的轮询循环到期后直接落底返回，
  同样区分不出"完成"与"截断"；现已同样抛 `ReplyTimeout`，签名与 muse 对齐。

### 测试

- 全量 108 passed
- 新增 `tests/test_finish_reason.py`：截断流必须报 `length` 且内容不丢
- `tests/test_cancel.py` 的超时断言从"正常结束"改为"抛 ReplyTimeout"——
  旧断言固化的是缺陷本身
- 变异台 **13 项**：新增 `truncation-reports-stop`（把 `length` 改回 `stop`
  后指定测试必红）

## [0.3.11] - 2026-11-06

### 修复（第十三把尺子：启动路径 / 环境变量解析）

- **数值型环境变量写错 = 裸 traceback**：全部 9 处都是
  `int(os.environ.get(...))` / `float(...)` 直转。实测（修复前）：
  - `MUSE_BRIDGE_PORT=""` → `ValueError: invalid literal for int() with base 10: ''`
  - `MUSE_REPLY_TIMEOUT=180s` → 模块导入即失败，`app.py:22 ValueError: could not
    convert string to float: '180s'`——运营者得自己推断是哪个变量写错了
  - `MUSE_BRIDGE_PORT=八七八八` → 同样一段 traceback
  新增 `muse_bridge/env.py`：`env_int` / `env_float` / `env_positive_int` /
  `env_positive_float` / `env_port`，坏值统一变成**一行点名报错**（含变量名与当前值）
  并以 `SystemExit` 干净退出；**空串（含全空白）按"没配"处理**，与 `.env` 里 `VAR=`
  的直觉一致；超时/冷却/间隔必须 > 0；端口必须在 1..65535（`0` 会让系统随机分配端口，
  是个静默陷阱）。全部 9 处已改走这套解析。
- **文件被写成 `\r\r\n`（每行之间多一个空行）**：早前某次补丁没有先做换行归一化就
  回写 CRLF，导致 **6 个文件**实际交付出去时行尾是 `\r\r\n`：
  muse `state.py`、`README.md`、`README.en.md`；hark `pool.py`、`README.md`、
  `README.en.md`（共 1877 处）。markdown 里每行之间多一个空行意味着**每行都渲染成
  独立段落**。已在字节层面全量修复并复查残留为 0。
  发现方式：变异台的"锚点必须唯一"检查报 `found 0`——锚点跨行，而文件里行与行之间
  多了空行。**这个检查顺带成了文件格式的守门员。**

### 测试

- 全量 105 passed；新增 `tests/test_env.py`（7 项：坏 int / 坏 float 点名报错、
  空串按未配、EnvError 即 SystemExit、端口越界、必须为正、模块常量走同一套解析）
- 变异台 **11 项**：新增 `env-validation-gone`（吞掉坏值后点名报错那条测试必红）

## [0.3.10] - 2026-11-06

### 修复（第十二把尺子：注释承诺 vs 代码实现 → 落到运营工具）

- **冒烟脚本不认识 API 密钥**：`smoke.py` 只打 `/healthz`（开放）和 `/v1/models`
  （需要密钥）。服务端一旦配了 `MUSE_BRIDGE_API_KEYS`，冒烟就会拿到 401 并**抛出裸
  `urllib.error.HTTPError: HTTP Error 401` traceback、退出码 1**——运营者看到的是
  "冒烟挂了"，会去怀疑服务本身；CI 也会因为一个纯粹的配置问题变红。已实测复现：
  配密钥 + 退回修复前的 `_get` → 裸 401 traceback。
  现在冒烟读同一把 `MUSE_BRIDGE_API_KEYS`（取第一把）、带上 `Authorization`；真拿到 401 时给出
  明确诊断（"服务端启用了 API 密钥，请在同一环境里设置 MUSE_BRIDGE_API_KEYS 后重跑"）而不是 traceback。
  **未配密钥时行为一字未变**（不发送 Authorization 头）。
- 顺带把 `AuthRequired` 纳入 `wait_until_ready` 的异常集合，避免就绪轮询把它咽掉。

### 复核（查过，非缺陷）

- 两桥 `pool`/`reader`/`driver`/`session`/`state` 里带承诺语义的注释逐条核对：
  hark「成功即解除冷却」→ 成功路径确有 `_cooldown.pop()`；hark `doctor`「绝不碰正式库」
  → 确用 `tempfile.TemporaryDirectory()` + `close()`；muse `aclose`、`except TimeoutError`
  交半成品、`driver.close()` 只断开，均与代码一致。
- hark 的 `_cooldown` 条目在删号后靠 60s TTL 自愈（`_is_benched` 到期弹出），
  没有 muse 那种"永久忙"的后果 → 不加对称补丁。

### 测试

- 全量 98 passed；新增 `tests/test_smoke_auth.py`（5 项：无密钥不带头发送、
  有密钥带第一把、401 明确诊断、就绪轮询吞不掉）
- 变异台 **10 项**：新增 `smoke-auth-header-gone`（删掉请求头后"有密钥带头发送"必红）

## [0.3.9] - 2026-11-06

### 修复（第十一把尺子：锁与内存态的生命周期）

- **唤醒失败会漏锁，账号永久"忙"**：`dispatch` 抢到账号锁之后，`wake_vm()` 是**裸调用**
  ——注释写着"失败不阻断"，代码里却没有 except。CDP 在 status 与 wake 之间掉线（或页面
  崩溃抛 `TargetClosedError`）时异常直接逃出 dispatch，锁永不释放：该账号从此永久停在
  `忙`，`/healthz.accounts_busy` 一直显示它，所有请求对它 429，只能重启进程。
  现在唤醒失败降级为 `last_error` 不阻断派单，并且整段抢锁逻辑包进 try/finally：
  **只有成功交出去的调用才保留锁**，其余任何出口（continue / 未预期异常）都由 finally
  关驱动 + 放锁。漏锁这一类缺陷从此没有出口。
- **锁表随删号无界增长**：`_locks` 按账号名建条目、从不回收。新增 `pool.forget_account()`，
  `DELETE /admin/accounts/{账号}` 现在一并回收调度状态。

### 复核（查过，非缺陷）

- `DispatchResult.release()` 幂等（finally 里判 `locked()` 再释放，之后置 None），
  `ok=False` 的结果根本不持锁，故两处 `raise` 前不 release 是安全的。
- `/v1/chat/completions` 非流式路径：空消息 400 在抢锁之前、超时 504 在 try/finally 内
  （`finish()` 用 shield 保护，客户端断开也不会漏锁）——两条路径本就正确。
- hark 无账号锁（纯 HTTP 无状态），`_cooldown` 条目到期自动弹出，无同类泄漏。

### 测试

- muse **93 passed**（+3：唤醒失败仍完成派单并交锁、未预期异常必须放锁、删号回收调度状态）
- 变异台扩到 **9 项**：`wake-guard-gone`、`handoff-guard-gone` 各自删掉守卫后对应测试必红。

## [0.3.8] - 2026-11-06

### 修复（第十把尺子续：管理端点的错误映射）

- **`/admin/refresh` 与 `/admin/wake` 在 CDP 不可达时裸 500**：`touch_session()` /
  `wake_vm()` 直接 `await self.connect()`，不像 `status()` 那样把连接失败折成返回值，
  端点里也没有 except——同一个故障在 `/admin/attach` 是干净 503，在这两个端点是
  traceback 500。现在两者都映射为 503 `{"error": "browser_unavailable", ...}`，
  语义与 `attach` 一致；`driver.close()` 仍在 finally 里。
- 顺带清掉 `attach` 里 `try/finally: pass` 的死代码，并在 `connected=False` 提前
  返回前补上 `driver.close()`（此前该路径不关驱动）。

### 复核（查过，非缺陷）

- hark `/admin/accounts/{账号}/probe`：`session.probe_session()` 已捕 `httpx.HTTPError`
  并折成 `Probe(valid=False, note=...)`，不会裸 500。
- `status()` 内部已把 `BrowserUnavailable` 折成 `connected=False`，故 `/admin/attach`
  与派单路径本来就是正确语义。

### 测试

- muse **90 passed**（+1：refresh/wake 在 CDP 挂掉时必须 503）
- 变异台扩到 **7 项**：`admin-500-instead-of-503` 去掉刷新路径的 except 后该项必红。

## [0.3.7] - 2026-11-06

### 安全（第十把逆向尺：/admin/* 鉴权面）

- **写操作管理端点此前无鉴权**：`/v1/*` 要密钥，而 `DELETE /admin/accounts/{账号}`、
  `/admin/attach`、`refresh`、`wake` 全都裸奔——运营者"我配了 API key 所以安全"的
  心智模型是错的。默认只绑 127.0.0.1 所以不算暴露，但只要按可行做法把桥架到另一台
  机器（`MUSE_BRIDGE_HOST=0.0.0.0`），同网段任何人都能删号、把桥指到自己的 CDP 端点。
  现在这四个写端点与 `/v1/*` 共用 `MUSE_BRIDGE_API_KEYS`；`/healthz`、`/console`、
  只读 `/admin/accounts` 保持开放。
- **向后兼容**：`MUSE_BRIDGE_API_KEYS` 为空时 `require_key` 直接放行，默认行为一字未变；
  控制台新增"API 密钥"输入框（存 localStorage）并在 401 时明确提示。

### 测试

- muse **89 passed**（+3：闸门行为、四个写端点未鉴权必须 401、正确密钥可过闸门）
- 变异台扩到 **6 项**：`admin-auth-gone` 删掉 attach 的依赖后该项必红。

## [0.3.6] - 2026-11-06

### 修复（第九把逆向尺：故障恢复 / 账号生命周期）

- **瞬时故障永久摘号**：`RATE_LIMITED`（回复超时）与 `BROWSER_UNAVAILABLE`（CDP 一时
  连不上）此前被 `mark_failure` 写进 SQLite 且不计时——它们不在 `DISPATCHABLE_STATES`
  里，于是**永久离开派单池**，只能人工 `/admin/refresh` 复活。一个慢回复就把号摘掉；
  池子被逐个蚕食到空，之后每次请求 503。现在 `is_dispatchable()` 对这两态走冷却分支：
  `MUSE_BRIDGE_RETRY_COOLDOWN`（默认 60s，用失败时间戳 `updated_at` 计时）过后自动
  回池，跨重启依然有效。冷却期内不要求探针新鲜——真正失效的会话会在派单时的
  `status()` 探针里被改成 `SESSION_EXPIRED`（终态，仍需人工登录），不会误放。
- **muse 缺 lifespan**：进程退出时不关闭 SQLite 句柄（hark 有）。已补，语义对称。
- **文档与代码对齐**：`HEARTBEAT_INTERVAL_SECONDS` 与 `needs_vml_refresh()` 只有定义
  从未被调用——"30 分钟心跳续期"与"Hatch VML 48 小时潜行续期"在代码里不存在，注释已
  如实标注为未实现（会话续期目前只能人工触发）。

### 测试

- muse **86 passed**（+3：冷却回池、无失败时间戳不派单、SESSION_EXPIRED 永不自动恢复）
- 变异台扩到 **5 项**，新增 `retry-cooldown-gone`——删掉 `RETRYABLE_STATES` 后该项必红。
- 变异台作用域修正：此前只跑 `tests/test_reasoning.py`，改为跑整个 `tests/`（否则住在
  `test_core.py` 的钉子根本不会被变异触及）。

## [0.3.5] - 2026-11-06

### 新增

- **`tools/verify_tests_load_bearing.py` 变异验证台**：把桥复制到临时树，逐个删掉
  行为，要求指定测试变红——绿着的套件配被删掉的功能等于没测。muse 与 hark 各内置
  4 项变异（思考开关、陈旧思考外泄、思考帧、usage 计入思考 / hark 另含 search 兜底），
  全部证明"删掉特性 → 对应测试必红"。

### 逆向复核（查过，非缺陷）

- relay `read_usage` 的 `has_breakdown` 只要求 prompt/completion 两个整数同时在场，
  **不要求 token 细分**——本桥的三字段 usage 帧已足够让 relay 正常计价，无计费盲区。
- hark 每次 dispatch 都 `create_conversation`（新会话），history 天然只有本轮消息，
  不存在 muse 那种跨轮思考外泄的土壤。
- 两桥 `ChatRequest` 未设 `extra="forbid"`，真实 OpenAI 客户端的 `temperature`/
  `max_tokens`/`tools` 等额外字段被静默接受（兼容性正确，不会 422）。

## [0.3.4] - 2026-11-06

### 修复（第七把逆向尺：跨轮状态 × 思考容器）

- **muse 多轮对话旧思考外泄**：思考容器语义是"最后一段思考"——第二轮提问、新回答
  尚未开始时，容器里装的还是上一轮的旧思考；原判定
  `current == before and not reasoning_now` 因思考非空而不跳过，旧思考与空基线做差
  后被整段当成新思考发给客户端（`last_reasoning` 跨请求不共享，必现）。现在正文
  还是上一条消息时整轮跳过——正文是旧的，思考必然也是旧的。hark 无此问题
  （reasoning 按 triggeredByMessageId 消息级关联）。
- 同尺顺带确认：纯思考期每个 delta 都刷新 quiet 窗口（last_change），长时间思考
  不会被误判提前结束；hark 流式分支无 finally 是正确设计（无锁无连接无资源可漏）。

### 测试

- muse **83 passed**（+1：旧思考不外泄钉）

## [0.3.3] - 2026-11-06

### 修复（逆向 usage 链发现：0.3.1 的非流式修复只修了一半）

- **非流式 usage 漏算思考**：0.3.1 给非流式响应加了 `message.reasoning_content`，
  但 `_usage_of(prompt, text)` 只按正文估算——同一请求流式计费含思考（streamed_parts
  拼接）、非流式不含，计费不一致。现在非流式按 `reasoning + text` 估算，两通道对齐。
  有测试钉住（想想+答案 = 4 个汉字 token，漏算则 2）。
- relay 侧验证：非流式响应经 relay 是 `JSONResponse(payload)` 整对象透传，
  `reasoning_content` 原样直达客户端（`verify_reasoning_passthrough.py` 新增非流式
  场景，16/16 全绿）。

### 测试

- muse **82 passed**
- hark **100 passed**

## [0.3.2] - 2026-11-06

### 修复（进程级冒烟发现——ASGI 内存测试全 patched store，从未触达）

- **坏配置返回裸 500**：`store.ConfigError`（缺密钥/密钥损坏，文案里带生成指引）
  会从请求处理器直接炸出，客户端只看到 `Internal Server Error`。现在 app 级
  exception handler 把它映射为 **503 + 原文案 + `type: config_error`**，两桥一致，
  /v1/models 等其余端点不受影响。
- 进程冒烟同时确认：真实进程 `/v1/models` 200、模型列表非空、坏配置请求毫秒级
  明确失败（之前的 ReadTimeout 是冒烟脚本自身 stdout=PIPE 死锁的假象）。

### 测试

- muse **82 passed**（+1：坏配置 503 钉）
- hark **100 passed**（+1：坏配置 503 钉）

## [0.3.1] - 2026-11-06

### 修复（逆向审视：从交付行为倒推实现）

- **hark 事件接口丢失 search 兜底**：`stream_text_events` 的 ON 路径在 history 读空
  时会空转到超时，而 OFF 路径（经 `stream_text`）有兜底。已恢复兜底，OFF 路径提前
  return 防止双重触发（有测试钉住两种情况）。
- **muse 空 DOM 超时静默返回空**：`stream_reply_events` 的 `started` 判定过宽——
  空页面也置 `started=True`，超时后返回空串而不是 `TimeoutError`。现在只有思考先出
  或正文开始渲染才算上游已响应；空 DOM 超时以 TimeoutError 收场，非流式分支照旧
  映射为 504（有测试钉住）。
- **两桥非流式响应丢思考**：collect 走字符串接口，思考被丢弃。现在非流式响应的
  `message.reasoning_content` 在有值时出现（OpenAI/DeepSeek 语义），流式行为不变。
  muse 新增 `pool.collect_reply_events`，hark 新增 `pool.collect_reply_events`
  （非流式不经过打字机 sleep，无额外延迟）。

### 测试

- muse **81 passed**（+2：非流式思考、空 DOM 超时）
- hark **99 passed**（+3：search 兜底、OFF 防双兜底、非流式思考）

## [0.3.0] - 2026-11-06

### 新增

- **思考过程透传（默认关闭）**：`REASONING=1` 后，若上游带思考内容，桥以
  `delta.reasoning_content` 单独发出，正文仍走 `delta.content`；思考与正文各自独立
  做单调扩展 diff，互不污染。关闭时思考根本不被读取，输出与没有此功能时逐字节一致。
  relay 按字节透传该字段，无需改动。
  - muse：`MUSE_REASONING` 开关 + `MUSE_REASONING_SELECTORS` 字段/选择器候选（内置
    候选是对 muse 真实 DOM 的猜测，命中即用、命中不到就当没有思考）
  - hark：`HARK_REASONING` 开关 + `HARK_REASONING_FIELDS` 字段候选（协议文档记录的
    message 没有思考字段，从未被活网确认过）
- `tests/test_reasoning.py`：driver/pool/app 三层行为验证（两桥共 14 项）

### 修复

- **muse driver 的悬空委托**：`stream_reply` 曾委托给不存在的 `stream_reply_events`，
  任何真实调用都会 AttributeError——测试没抓到是因为桩 driver 都绕过了这条路径。
  现在事件接口真实存在，字符串接口经它实现，两条路径不再各自演化。

### 测试

- muse **79 passed**（+5 reasoning）
- hark **96 passed**（+9 reasoning）
- 默认关闭时 `test_stream_usage` 全部既有断言不动全过——关闭态行为未变。

## [0.2.0] - 2026-11-05

### 新增

- **token 估算改为按脚本区分**（`_estimate_tokens`）：原先一律 4 字符/token，汉字实际
  基本一字一 token，中文用量被少算 6-10 倍——挂在计量用的网关后面等于中文流量近乎免费。
  现在 CJK 一字一 token、拉丁仍按 4 字符（英文路径数值不变）。`total` 改为两部分之和，
  客户端拿 prompt + completion 一定能对上自己的数。
  **这是计费行为变更**：中文回答上报的 token 数会显著升高（约 4-8 倍），定价照旧。
- **流式 usage 帧**（`stream_options.include_usage`）：网关挂在前面时**只按上报的 usage
  扣 token 额度、没有字节兜底**——流式不报用量等于免费。`ChatRequest` 接收该字段，
  被要求时在 `[DONE]` 前补 `{"choices":[], "usage":{...}}`；没被要求时输出形状一字不变。
  流式与非流式共用 `_usage_of`，两条路径报同一个数。

### 测试

全量 **74 项通过**。


### 明确不含

注册 / 登录自动化、Noise 加密复刻 / notary 伪造、指纹伪装、账号共享。

---

## [0.1.0] - 2026-11-05

首个版本。

### 新增

- **账号级互斥锁**：同一账号（同一 Chrome 窗口）同一时刻只允许一个请求。
  两个请求同时往同一个输入框打字会让 DOM 互相污染、回复分不清归属。
  锁覆盖整个请求（含流式输出），通过 `DispatchResult.release()` 释放。
  - 有其它空闲账号时自动切换，不直接报错
  - 全部账号忙时返回 429 并说明「muse 只能串行」
  - 失败路径保证释放锁，不漏锁
  - `/healthz` 新增 `accounts_busy`
- **自检工具** `python -m muse_bridge.doctor`
  - 加密密钥、数据库（实测加密落盘）
  - playwright 依赖检测
  - Chrome CDP 端口连通性
  - muse.ai 登录态（前四项都通过才检查）
- **OpenAI 兼容接口**
  - `POST /v1/chat/completions`（非流式 + SSE 流式）
  - `GET /v1/models`（别名全部映射到 `muse-spark-1.3`）
- **CDP 驱动**
  - `connect_over_cdp()` 复用已登录 Chrome，不启动新实例
  - 只断开连接，不关闭用户浏览器
  - 延迟导入 playwright，未安装时给可操作报错
- **DOM 读取**
  - 选择器集中表 + 候选列表（`selectors.py`）
  - 文本单调扩展判定增量
  - 静默窗口 + 流式标记双重结束判定
  - 非单调文本整体重发（宁可重复不丢内容）
- **会话管理**
  - Fernet 加密 SQLite，密钥缺失抛错
  - `touch_session()` 走页面内 fetch 续期 `hatch_vml`（48h）
  - `wake_vm()` 唤醒休眠 VM
  - 9 状态状态机，探针 15 分钟新鲜度闸门
- **账号管理**
  - `POST /admin/attach` 连接 CDP 并入库
  - `POST /admin/accounts/{id}/refresh` 续期
  - `POST /admin/accounts/{id}/wake` 唤醒
  - `GET /admin/accounts` 脱敏快照

### 测试

### 修复

- **取消时锁泄漏**：客户端中途断开时任务处于 cancelled 状态，
  直接 `await release()` 会被立即取消，账号锁永久不释放。
  改用 `asyncio.shield` 保护收尾路径。
- **stream_reply 无超时兜底**：driver 自己有 deadline，但若其实现有 bug
  或前端一直显示"生成中"，`pool` 层会无限产出，把连接和锁永远占住。
  新增 pool 层超时保护与生成器显式关闭。
- 流式生成器被提前丢弃时未 `aclose()`，内部循环不会退出。

### 新增配置

- `.env.example`：所有环境变量与默认值的参考清单

### 测试

46 项，不启动真实浏览器。覆盖：

- 选择器表完整性
- 状态机（终态不派单、探针过期、VM 休眠可派单）
- 会话刷新窗口计算
- 心跳间隔必须短于刷新窗口
- 模型别名映射
- 账号轮转
- 派单失败落状态（browser_unavailable / session_expired）
- profile 加密落盘
- 单调扩展差分逻辑
- 非单调重绘处理

### 明确不含

- 注册 / 登录自动化
- Noise 加密复刻 / notary 伪造
- 指纹伪装
- 账号共享

---

作者：**君莫笑**
**永恒之兰**
