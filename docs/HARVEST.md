# 横向抄袭清单：上游 30 仓里「可以抄的工程」与「必须拒绝的坏习惯」

本文是方向三的产出物之一，配套代码 `muse_bridge/upstream_quirks.py`、测试
`tests/test_upstream_quirks.py`、漏洞清单 `docs/VULN_HARVEST.md`。
全部结论来自**静态阅读**：没有拉新仓库、没有对 muse.ai 发请求、没有起浏览器。

## 0. 一句话结论

上游真正值得抄的不是功能，而是**三条纪律**：

1. **额度/续期是记账问题，不是重试问题** —— 预扣、结算、失败补偿必须能幂等重放；
   续期只在上游 Cookie 到期的 12 小时窗口内动作。
2. **状态与进度有一条读侧补正规则** ——`succeeded/success/done → completed`、
   `processing` 进度下限 15、进度只增不减、重启把 in-flight 判失败。
3. **成品必须本地化 + 带 key 交付** —— 上游 `/v1/media/{name}` 无鉴权是跨三仓复现
   的公开缺陷，只能抄「正确修法」（lichao199208 分支），不能抄实现。

反过来，看到「看起来一样」的地方先别抄：

| 表面相似 | 实际差异 | 处理 |
| --- | --- | --- |
| 都是 OpenAI 兼容 | new-api 的 video status 示例写 `succeeded`，常量写 `completed`（`relaykit/dto/openai_video.go:8-14` vs `dto/video.go:27`） | 抄归一函数，不抄字段值 |
| 都有 progress | 上游报的是**里程碑** 0/5/10/15/20/25/40/100，不是百分比（`czg app.py:1281-1505`、`engine.py:1255-1258`） | 抄里程碑表，不插值 |
| 都有 media 下载 | 三家实现**都没有鉴权**（见 V-02），只有一家补了 | 抄修法，不抄路由 |
| 都有 keepalive | 上游是「全池 10 分钟扫一遍」（`jobs_keepalive.py:220-235`），我们是按需 + 手动 | 抄窗口判定，不抄定时器 |

## 1. 证据来源与读法

仓库已落在 `D:\deep seek\work\muse-re\`：

* `sources/<owner>__<repo>/`：GitHub 上的原始目录结构（muse2api 系、Muse-Canvas 等）；
* `_src/files/<owner>__<repo>/<path>/`：为避免路径非法，目录分隔符被压成 `__`
  （例如 `QuantumNous__new-api/model__quota_reserve.go` 对应上游
  `model/quota_reserve.go`），**行号保持原文件行号**，可直接 `file:line` 复核。

2026-11-11 12:2x 又补了一轮**整仓浅克隆**，用于协议侧交叉验证（只读源码/文档，
未运行对方项目）：`work/muse2api_pure/`（lichao199208 纯协议分支）、
`work/muse2api_harvest/czg/`、`work/muse2api_harvest/www222fff/`、
`work/muse2api_harvest/muse-cli/`。协议结论与取舍记在
`docs/PROTOCOL.md` §8.10，本文不重复。

验证等级（全篇统一）：

* `V1` 上游源码逐行直读，行号可复核，未执行；
* `V2` 跨两个以上仓库出现同一写法/同一缺陷，或本仓已有同型实现且被离线测试覆盖；
* `V3` 由 V1 推导出的边界与影响，上游没有明写，按最保守方向定。

复核某一行无需重读全文：

```powershell
# 例：确认 /v1/media 无鉴权
Select-String -Path 'D:\deep seek\work\muse-re\sources\czg86389-hub__muse2api\app.py' -Pattern '/v1/media' -Context 0,8
```

## 2. 块一：额度与会话续期（记账顺序）

| 上游 | 抄什么（证据） | 为什么不照搬 | 落到我们哪 |
| --- | --- | --- | --- |
| QuantumNous/new-api `model/quota_reserve.go:19-65`、`:161-238` | 预扣 → 落库 → 失败补偿三步；补偿脚本把 token 的 `RemainQuota` 加回、`UsedQuota` 减回（`:105-238`） | 依赖 Redis + Lua 原子脚本，以及钱包/订阅/令牌三套额度；本仓没有计费账本，也没有多用户 | 只抄「补偿可重放」这一条；本仓 `store.py:80-96` 已有原子替换，退款幂等键见 §7 待办 |
| new-api `service/task_billing.go:24-25`、`:263-283` | 提交时 `PreConsumeBilling`，完成/失败后 `SettleBilling`；`RefundTaskQuota` 同时退资金来源、令牌额度、用户/渠道用量 | 它的「任务」是数据库行 + 事务；本仓任务在加密 JSON 里，没有 SQL 事务 | 失败分类决定是否退款的动作语义（`jobs.py:145-174` 已实现「额度类不换号」） |
| songquanpeng/one-api `relay/billing/billing.go:11-21`、`:23-48` | 失败路径必须**显式** `ReturnPreConsumedQuota`；成功路径按真实用量 `PostConsumeQuota` | 全局 quota 数值模型，没有任务维度，也没有幂等键（重复退还只能靠调用方自觉） | 抄「显式退还 + 结算」的顺序，不抄数值模型 |
| work/museapi-video `worker.js:10-19`、`db.js:24-56` | 失败退款只做一次：`UPDATE jobs SET refunded=1 ... WHERE refunded=0`；完成后 `progress=100` | SQLite 单表 + SQL 原子条件更新；本仓存储是加密文件，没有条件更新 | 抄**幂等键**设计（`refunded` 布尔 + `finished_at`），不是抄 SQL |
| muse2api `app/jobs_keepalive.py:57-58`、`:186-217`、`:220-235`、`:244-264` | 续期三动作 `keepalive/renew/quota`；`expiring_ids(12*3600)` 只续 12h 内到期；到期未知用 `last_keepalive` 兜底；10 分钟一轮；未分配账号 `assign_pending_once(cooldown=90)` 自动 lease | 定时器模型针对「常驻进程 + 多线程池」；本仓是 FastAPI 单进程、手动 refresh，且全池扫描会放大代理账单/403 风控 | `upstream_quirks.renew_policy()` / `renew_stages()`；既有 `session_http.py:432-465` |
| muse2api `app/muse_engine/engine.py:534-624`、`:696-701` | 续期分三段：`renew` →（未分配）`lease-vm` → 再 `renew` →（已分配）`wake` → 再 `session` 确认 `RUNNING`；`x-hatch-session-refreshed:1` 才是真续期 | 浏览器侧 `Network.setCookie` 路径我们不走（HTTP-only）；`expires <= now+3600` 会被 Chromium 丢弃（`:636-640`） | 已实现于 `session_http.py`（`renew_session` / `wake_vm` / `injection_expires:240-252`）；策略判定抽成纯函数便于单测 |
| 本仓既有 `session_http.py:26-27`、`:91-105`、`:329-343` | 401=cookie 失效（重登）、403=出口/地区（**不判失效**）、429=限流冷却 | —— | `classify_renew_response()` 与既有异常类一一对应，不替代它们 |

**读出来的硬事实**（写进 `upstream_quirks.py` 的常量）：

* 窗口 = `12 * 3600` 秒，边界「刚好 12h」选择续（晚一秒可能撞 48h 硬过期，代价不对称，`V3`）；
* 到期未知且从未续过 → 必须探测，不能假设还早（`V3`，上游注释只写「避免漏掉」）；
* 407 是代理认证，换 cookie 无解；5xx 重试、其余 4xx 别重试。

## 3. 块二：任务状态机与进度

| 上游 | 抄什么（证据） | 为什么不照搬 | 落到我们哪 |
| --- | --- | --- | --- |
| czg/muse2api `app.py:1274-1277` | 生图准入控制：in-flight ≥ 8 就 429（注释写明「one Chromium worker，cap admission」） | 只覆盖 image；video 完全没有上限（见 V-05），不能一起抄 | `jobs.py:361-364` 已有同类上限（`MUSE_JOB_QUEUE_MAX`），语义是 queued 计数 |
| czg/muse2api `app.py:1499-1505` | 读侧归一 + 进度补正：`succeeded/success/done→completed`；`completed→100`；`processing→max(p,15)` | 补正写在**路由层**，每个读接口都要重复一遍，video/image 各一份，容易漂移 | `normalize_state()` / `progress_for_state()`（新增）；`app.py:680-685` 已有等价实现（本次不改） |
| czg/muse2api `engine.py:1255-1262` | 进度里程碑只报 20/25/40：`25 if seen_stop else 20`，agent 数增长覆盖为 40 | 这是页面观察量，不能当成百分比往 41、99 插值 | `milestone_for()` / `PROGRESS_MILESTONES` |
| czg/muse2api `app.py:2874-2877` | 启动钩子把 `queued/processing` 判 `failed`，文案「服务重启中断了任务」 | 文案写死中文、且不区分「可立即重提」与「上游失败」 | `jobs.py:515-544` `reconcile()` 已实现（判 `interrupted`），并已注明对应 `app.py:2873-2877` |
| new-api `model/task.go:17-44` | 任务状态常量 + `ToVideoStatus` 映射（queued/in_progress/completed/failed） | 它维护「内部 TaskStatus」和「对外 VideoStatus」两套枚举 + 映射表；本仓只有一套对外状态，抄双枚举会造出第二事实源 | 只取常量命名；`normalize_state` 的 `PUBLIC_STATES` 与 `jobs.JobStatus` 对齐 |
| new-api `relaykit/dto/openai_video.go:8-14`、`:33-36` | 对外 status 只有 5 个值：`unknown/queued/in_progress/completed/failed`；进度字段是 int | **不要抄** `SetProgressStr`：`TrimSuffix("%")` 后 `Atoi` 把错误丢给 `_`，`"40.5"` 静默变 0，进度倒退 | `normalize_progress()` 显式拒绝脏值，`advance_progress()` 只增不减 |
| novicezk/midjourney-proxy `enums/TaskStatus.java:10-26` | `NOT_START/SUBMITTED/IN_PROGRESS/FAILURE/SUCCESS` 的分层：**提交 ≠ 开工 ≠ 成功** | `order` 字段 `FAILURE(4)` 与 `SUCCESS(4)` 同值——上游自身 bug，不能当排序/比较依据 | 分层已体现在 `jobs.py` 的 `QUEUED/PROCESSING` + `note_progress`；本仓不引入 order |
| lichao/muse2api `app/muse_engine/service.py:1234-1241` | 与 czg 同款读侧补正（两仓独立实现同一规则 → 规则稳定，`V2` 交叉验证） | 同上：路由层重复实现 | 同一函数；测试里对拍 `jobs.public_status` |

**为什么进度必须单调**：SSE/轮询客户端把「进度回退」当任务被重新排队，会重置
自己的超时与展示（本仓 `jobs.py:460-465` 的注释同结论）。上游 new-api 那个
静默清零的解析错误正是回退来源之一。

## 4. 块三：媒体交付与成品回收

| 上游 | 抄什么（证据） | 为什么不照搬 | 落到我们哪 |
| --- | --- | --- | --- |
| czg `app.py:1822-1829`；bynne2602 `app.py:1535-1542`；bitgacon `app.py:1538-1545` | **只作为反面教材**：三份一字不差的「无鉴权 GET `/v1/media/{name}` + 文件名黑名单 + FileResponse」 | 文件名黑名单挡目录穿越够用，但**不挡任何人**：URL 泄露即成品泄露，且没有 key 绑定 | 本仓已修：`app.py:882-914` + `media.py:66` token 校验 |
| lichao `app/routers/v1.py:596-644`、`app/media_rewrite.py:15-39` | 正确做法：`media_auth_required` 时校验 `Authorization: Bearer`、拒绝无 key、`is_safe_media_name`、`FileResponse` 带 media_type | 不要抄「鉴权可关」：`media_auth_required=False` 的开关会把缺陷原样搬回来 | `media_url_policy(auth_required=False)` 直接抛 `MediaPolicyError` |
| czg `engine.py:1882-1894`、`:1896-1917` | 两路落盘：blob 直取 / download fallback；文件名 `uuid4().hex + ext`；「期望视频却拿到图片 → 拒收」 | `_pick_ext`（`:1921-1931`）只认 mp4/webm/png/jpg/webp/gif，`.mov/.3gp` 落进 `.mp4/.png` 猜测分支——本仓 `media.py:72-110` 已补 `3gp/quicktime` | `media.py` 已有；不抄扩展名猜测 |
| czg `app.py:995-1003` | `public_base` 非空返回**绝对 URL**（客户端平台会把相对路径解析到自己域名） | 它把 `name` 直接暴露成 URL 路径；我们换发 token | `media_url_policy(public_base=...)` 生成绝对 URL 且 token 必填 |
| `docs/PROTOCOL.md:176` | 上游直链约 **2 天**过期 | TTL 是文档级证据（`:474` 注明未实测），只能当上限 | `media.py:61` `MUSE_MEDIA_TTL` 默认 172800；`media_url_policy` 再把 TTL 夹到「上游剩余寿命」 |

**同一条硬结论**：直链不能交付，必须「先本地化、再带 key 换发」。这也是
`media_url_policy(upstream_direct=True)` 抛错而不是返回降级方案的原因。

## 5. 兼容层接口（新增 `muse_bridge/upstream_quirks.py`）

| 接口 | 语义 | 上游证据 | 验证等级 |
| --- | --- | --- | --- |
| `normalize_state(raw) -> str` | 读侧归一，认不出一律 `unknown` | czg `app.py:1499-1500`；new-api `openai_video.go:8-14`；mj `TaskStatus.java` | V1 + V2（与 `jobs.public_status` 对拍） |
| `PROGRESS_MILESTONES` | `(0,5,10,15,20,25,40,100)` | czg `app.py:1281/1288/1462/1468/1475/1505`、`engine.py:1255-1258` | V1 |
| `normalize_progress(raw) -> int \| None` | 解析进度；脏值/越界返回 `None` | 反面：new-api `openai_video.go:33-36` 静默清零 | V1 + V3 |
| `advance_progress(current, incoming)` | 只增不减 | `jobs.py:460-465`；czg 读侧补正 | V2 |
| `progress_for_state(state, reported)` | completed→100、processing→≥15、其余透传 | czg `app.py:1501-1505` | V1 |
| `milestone_for(stop_seen=, agent_grew=)` | 20/25/40 | czg `engine.py:1255-1262` | V1 |
| `renew_policy(expires_at=, last_renew_at=, now=)` | `renew_now` / `probe_unknown` / `defer` | `jobs_keepalive.py:186-217` | V1 + V3 |
| `renew_stages(assigned=, wake_vm=)` | `renew`→(`lease`→`renew`)→`wake` | `engine.py:608-650`、`:666-687` | V1 |
| `classify_renew_response(status)` | 401/403/407/429/5xx 各自处置 | `engine.py:562-568`；`session_http.py:329-343` | V1 |
| `is_media_token(token)` | 32 位小写 hex | `media.py:66` | V2 |
| `media_url_policy(...) -> MediaPolicy` | 拒绝上游直连/关鉴权；本地化 + 绝对 URL + TTL 夹取 | 见 §4；`media.py:59-61` | V1 + V2 |

## 6. 测试覆盖（`tests/test_upstream_quirks.py`，212 条）

对应任务要求的四条边界：

* 未知状态 → `unknown`：`test_normalize_state_unknown_for_non_strings`、
  `test_normalize_state_dat_not_guess_processing`；
* 进度单调不降：`test_advance_progress_is_monotonic`（含 100 之后不回退）；
* 续期三策略：`test_renew_policy_three_strategies`（11 组输入，含边界与未知兜底）；
* 媒体策略拒绝无鉴权路径：`test_media_policy_refuses_upstream_direct`、
  `test_media_policy_refuses_auth_off`、`test_media_policy_refuses_both_unsafe_options_at_once`。

另有对拍类：`test_normalize_state_agrees_with_public_status`、
`test_media_token_agrees_with_media_module`——两处规则一旦漂移立刻变红。

## 7. 未接线 / 不建议本轮做的事

四文件限制内**没有改动任何既有文件**，以下是有意留下的接线缺口，需要 lead 决策：

1. `app.py:680-685` 的 `_normalize_state` 与新函数行为一致，未替换；只在测试里对拍。
2. `app.py:688-694` 的 `_job_response` 仍拼相对 URL。要支持 `public_base` 需改
   `_job_response` 走 `media_url_policy(public_base=...)`——**会动既有文件**，超出本任务。
3. `jobs` 表没有 `refunded/settled_at` 幂等列。上游三家（new-api/one-api/museapi-video）
   的退款都以幂等键为前提；若本仓将来加计费，必须同时加幂等列，否则重复退款。
4. 本仓只有手动 `/admin/accounts/{account}/keepalive`（`app.py:1252-1270`），没有
   10 分钟自动循环。**若将来加自动循环，必须先用 `renew_policy()` 判窗口**，
   否则会每轮全池续一遍（上游明确避开的坑，`jobs_keepalive.py:186-188`）。
5. `/admin/*` 与 `/v1/*` 共用同一份 `MUSE_BRIDGE_API_KEYS`，且密钥表为空时
   `require_key` 直接放行（`app.py:45-56`）。这是上游 V-01 的本仓同型面，
   见 `docs/VULN_HARVEST.md` V-01「本仓受影响面」，需要 lead 决定是否拆密钥或拒绝空表启动。

## 8. 复核方式（可重跑）

```powershell
cd 'D:\deep seek\muse-bridge'
python -Xutf8 -m pytest tests/test_upstream_quirks.py -q
python -Xutf8 -m ruff check muse_bridge/upstream_quirks.py tests/test_upstream_quirks.py
python -Xutf8 -m pytest -q
```

上游行号复核：

```powershell
$r = 'D:\deep seek\work\muse-re'
# 状态归一 + 进度补正
Get-Content "$r\sources\czg86389-hub__muse2api\app.py" | Select-Object -Skip 1496 -First 12
# 里程碑
Get-Content "$r\sources\czg86389-hub__muse2api\engine.py" | Select-Object -Skip 1253 -First 10
# 续期窗口
Get-Content "$r\sources\lichao199208__muse2api\app\jobs_keepalive.py" | Select-Object -Skip 185 -First 33
# 媒体无鉴权
Get-Content "$r\sources\czg86389-hub__muse2api\app.py" | Select-Object -Skip 1820 -First 10
```

限制：所有「上游会怎么响应」的判断都是静态直读，没有真实网络执行；
`V3` 标记的边界（12h 相等、TTL 下限 1 秒、未知到期必须探测）是保守推导，
不是实测结论。真实行为要在拿到可用账号后按 `docs/PROTOCOL.md` 的实测流程复核。

## 9. 本次验收结果（测量时：2026-11-11）

```text
python -Xutf8 -m pytest tests/test_upstream_quirks.py -q
212 passed in 0.34s

python -Xutf8 -m ruff check muse_bridge/upstream_quirks.py tests/test_upstream_quirks.py
All checks passed!

python -Xutf8 -m pytest -q
2 failed, 1046 passed, 6 warnings in 11.73s
```

两条失败都是 `docs/KNOWN_ORACLE_CONFLICTS.md` 记录的已知 oracle 冲突
（`tests/test_noise.py::test_protocol_constants_match_references`、
`tests/test_proto.py::test_int64_signed_roundtrip_through_message[9223372036854775808]`），
与本任务的四个文件无关。全仓测试数量随其它并行任务增长，通过数只增不减。

## 10. 2026-11-11 真机复核（活网实测：成品交付形状）

> 对应 §2（额度）与 §4（媒体交付与成品回收）。上游对照表的结论**不变**；这一节只把
> 「我们自己的交付面到底长什么样」钉成实测值。素材：真账号 + 真 muse.ai 页面 +
> 本机已起的 bridge（只发请求、不重启进程）。

**实测（a）：同步生图接口**

```json
POST /v1/images/generations  ->  200
{ "created": <unix秒>, "data": [ { "url": "/v1/media/<token>" } ], "task_id": "task_…" }
```

任务随后 `completed`、`progress=100`。`url` 是**相对路径**——本机没有设
`MUSE_PUBLIC_BASE`，与 `app.py` 的 `public_base()` / `_absolute()` / `_job_response()`
（写作时 `:720-739`）当前行为一致。§4 里「绝对 URL」那条是 `public_base` 打开时的形态，
两者不是矛盾。

**实测（b）：`GET /v1/media/<token>` 的响应头**

| 项 | 实测值 | 与设计的对应 |
|---|---|---|
| 状态 | `200`；带 Range 时 `206` | `app.py:947-977` |
| `Content-Type` | `application/octet-stream` | **设计如此**（`media.py:300-324`：不给浏览器就地渲染） |
| `X-Muse-Media-Mime` | `image/webp` | 真实类型在这里，与元数据同源 |
| `Content-Disposition` | `attachment; filename="<token>.webp"` | `media.py:319` |
| `HEAD` | **405** | 只注册了 `GET`（`app.py:947`），没有 HEAD 路由；**符合现状**，不是缺陷 |

**实测（c）：同账号额度（免费版）**

`weekly_used_pct=2`、`weekly_reset=11月17日`。面板原文与解析过程见
`docs/FRONTEND_RE.md` §3.6，这里不重复。

**本节没有覆盖的**：上游直链 TTL（§4 仍按文档值 `172800` 计，**不是实测**）、
`/v1/media` 的并发与大文件断点续传行为、视频侧交付形状、以及配额扣减时点。
这些都还是文档级或单次实测，不要当"已验证"。
