# muse.ai / hatch-web 前端逆向报告（方向二·前端逆向）

**交付物**：本文件 + `muse_bridge/gen_params.py` + `tests/test_gen_params.py`。
**边界**：全程未连接真实 muse.ai、未起真实浏览器、未下载新仓库；只用工作区已有素材
（`work/muse-re/sources/`、`work/museapi-video/`、本仓 `docs/`）。反自动化只做静态
取证，不做任何绕过实践。
**复核日期**：2026-11-11（Python 3.14.7 / pytest 9.1.1）。

> **2026-11-11 追加（真机复核）**：上面的「全程未连接真实 muse.ai」只对**静态取证轮**
> 成立。同日另起一轮，在一台真机（真账号 + 已登录的 muse.ai 页面）上补做了活网观察：
> 额度面板部分写在本文件 **§3.6**（入口/菜单/文案与 `parse_quota_text` 的实际输出）；
> 聊天 DOM 的实测结构、WS 创建侧观察与成品提取的修复，分别见
> `docs/PROTOCOL.md` §4.1.1 / §1.5 / §4.5。原文其余小节按「静态轮的证据」保持不动，
> 只被真机改写的地方加了指针。

四问速答：

1. **生成参数白名单**：线上**没有**结构化生成参数字段。能进生产白名单的只有 3 个
   「文本约定」字段（`duration` / `aspect_ratio` / `model` 桥侧别名），全部
   `corroborated`；其余 13 个候选（`seed`、`--st` 家族、`--chaos`、`resolution`、
   `extra`…）只有单份二手来源，全部降级为 `hypothesis` 并在
   `normalize_params()` 里显式拒绝。
2. **RPC 方法名**：必须按三层区分（muse.ai HTTP / Noise 内层 HTTP / Gateway 点分
   方法），共整理 10 + 13 + 11 条（含「一组未复核端点」聚合行），逐条带等级与证据；
   `/chat/stream` 的 path 可 `corroborated`（且只接受 POST），但 body 形态两套实现
   互相矛盾，**未解**。
3. **配额**：没有任何结构化额度接口的证据（也未证伪）。Settings→Usage 面板的
   DOM `innerText` 是唯一已知来源；`quota.py` 的文本解析已是「事实上的结构化解析」
   最可行形态，可升级点 3 条（§3.4）。
4. **notary/反自动化**：证据链是 `session cookie → /api/hatch/noise-notary-token →
   endorsement(TTL/hostname/uri 绑定) → Noise RV proof/attestation → CVM 的 SNP
   attestation + 撤销表校验`。**cookie 才是认证边界**；本轮不碰、也不建议碰。

---

## 0. 口径与素材

### 0.1 验证等级（全仓统一，`gen_params.py` 里同一套）

| 等级 | 定义 | 本报告里的典型例子 |
|---|---|---|
| `observed` | 工作区里能直接打开的字节/字面量 | 真实 hatch-web chunk 里的 `fetch("/api/hatch/token", …)` |
| `corroborated` | ≥2 份**互相独立**的实现/观察记录给出同一结论 | `duration` 的 5/6/8/10（平台侧 + 上游文档） |
| `hypothesis` | 只有 1 份二手来源 | `--chaos`、`seed`、`resolution` |
| `unverified` | 零证据（既没证实也没证伪） | muse 是否有 `/api/billing` |

> 严格说，我们这一侧**没有任何一次自己的活网抓包**（`docs/PROTOCOL.md:475` 也这么写）。
> 因此 `observed` 只表示「工作区里有原始字节可复核」，不等于「本轮复现了那次请求」。
> 二手实现里声称「我抓到了 X」的，仍按 `corroborated`/`hypothesis` 计。

### 0.2 素材清单

| 素材 | 是什么 | 在本报告里的用法 |
|---|---|---|
| `work/muse-re/sources/manishkumar1601__muse-ai-api/recon/chunks/`（9 个文件，约 389KB） | hatch-web 真实构建产物的高信号 chunk 原文 | 唯一的一手字面量来源（`observed`） |
| 同仓 `recon/PHASE_0_REPORT.md`、`recon/signal_summary.json`、`docs/wire-protocol.md`、`docs/api-reference.md` | 同一项目的抓包/扫描记录（210/212 chunk 下载，其中 9 个留存） | `hypothesis`～`corroborated`，用于补 chunk 未覆盖处 |
| `work/muse-re/sources/lichao199208__muse2api/` | Python 纯协议实现（`app/muse_engine/*`）+ 自己的前端（`static/app.js`） | 独立的第二实现（`corroborated`） |
| `work/museapi-video/recon/upstream/` | muse2api 主线源码快照（`engine.py` / `app.py` / `README.md`） | 独立来源之一（`corroborated`） |
| `work/museapi-video/platform/` | 独立的 Node「视频工作台」，消费上游 REST | 独立的第三来源；参数面板 `ParamsPanel.jsx` 是 `--st` 家族的唯一出处 |
| `work/muse-re/sources/huwanbin__Muse-Canvas/muse2api/README.md` | 与上游 README 逐字相同的副本 | **不算独立证据**（同源复制，仅用于确认文本未变） |
| 本仓 `docs/PROTOCOL.md`、`docs/KNOWN_ORACLE_CONFLICTS.md`、`muse_bridge/*` | 本仓既有结论与代码 | 用来对冲突（§5），不作为外部证据 |

### 0.3 覆盖边界（先读这一节，再看结论）

- 210 个 chunk 里只有 **9 个**留存原文；`signal_summary.json` 是对全部 210 个的
  **固定信号词**扫描（`ChatMessage`/`GatewayRequest`/`notary_token`/…），不含
  `duration`/`aspect`/`resolution`/`usage` 这些词。
- 所以本报告能说的是：**已留存的 9 个 chunk 里没有**时长/画幅/分辨率/额度的字面量；
  这 9 个 chunk 之外的**生成参数 UI chunk 从未被下载**。
  据此**不能**断言「muse.ai 前端没有参数面板」，只能说「**未覆盖/未验证**」。
- 抓包日期是 2026-10-01，构建号 `dpl_AU2F73G98WTk6ngM4hDqSqzwsTaf`（`PHASE_0_REPORT.md`
  与 `docs/PROTOCOL.md:471` 都提到漂移风险）。今天（2026-11-11）的线上产物必然已变，
  chunk 文件名/偏移/端点都可能漂移 —— 本报告的证据等级只对**那一天的那一版**成立。

---

## 1. 生成参数

### 1.1 核心结论：线上没有结构化参数字段

两套**互相独立**的实现把生成请求发到同一个 path，body 里都只有文本/会话/能力位，
没有任何 `duration`/`aspect_ratio`/`resolution`/`seed` 键：

- `lichao199208__muse2api/app/muse_engine/protocol_engine.py:308-336`
  → `{items:[{type:"text",text:prompt}], node_id, capabilities:{}, metadata?}`，
  `POST /chat/stream` 在 `:336`。
- `manishkumar1601__muse-ai-api/src/hatch/chat.ts:64-67,82`
  → `{message, node_id, capabilities:[...], timezone}`，`POST /chat/stream` 在 `:82`；
  侧聊再加 `session_id` + `metadata:{thread_is_dictation_used:false}`（`:150-154`）。
- 本仓 `muse_bridge/protocol_client.py:986-1001` 按第二种形态实现（有偏离说明
  `:973-978`）。

时长与画幅在两条路径上都是**自然语言文本**，不是字段：

- 上游浏览器路径 `work/museapi-video/recon/upstream/app.py:945`（`dur = r.duration or 6`）、
  `:974-976`（`时长严格为 {dur} 秒`）、`:988-989`（`画质规格：{resolution}`）、
  `:742/:990`（`extra` 原样插入提示词）。
- 纯协议路径 `…/lichao199208__muse2api/app/muse_engine/prompts.py:167-175`（时长默认 6）、
  `:201-236`（按 `16:9`/`9:16`/`1:1` 等字面量分支，`resolution` 拼进「画质规格」）。
- 本仓同样写文本：`protocol_generation.py:215-239`（`render_prompt()`）。

**推论**：所谓「生成参数白名单」不是服务端枚举校验，而是**桥自己的文本约定**。
即使把 `duration=7` 编进提示词，服务端也大概率照做（它根本不解析）—— 我们仍然用闭集
约束，是为了让成片形态可预期、让同一个参数不在三个文件里长出三套取值域。

### 1.2 生产白名单（= `gen_params.PARAM_SPEC`）

| 字段 | 类型 | 取值域 | 承载方式 | 等级 | 证据 |
|---|---|---|---|---|---|
| `duration` | int | `5, 6, 8, 10` | 提示词文本 | `corroborated` | `work/museapi-video/platform/config.js:37`；`work/museapi-video/recon/upstream/README.md:53`；`…/upstream/app.py:945,974-976` |
| `aspect_ratio` | str | `16:9, 9:16, 1:1, 4:3, 3:4` | 提示词文本 | `corroborated` | `platform/config.js:38`；`recon/upstream/README.md:49`；`lichao199208__muse2api/static/app.js:561`；`…/upstream/app.py:721-735` |
| `model` | str | `muse-image, muse-video` | 桥侧路由（不是线上字段） | `corroborated` | `platform/generate.js:83`；`lichao…/app/muse_engine/service.py:511-512`；`lichao…/static/app.js:561-562` |

三点必须说清：

1. 三个字段的「等级」指的是**文本约定**有 ≥2 份独立实现一致，**不是**服务端接受集。
   服务端到底把 `duration=8` 执行成几秒，`unverified`。
2. `model` 是**桥侧别名**：muse.ai 线上没有模型 id，图/视频由提示词与结果资源 kind
   区分。它的唯一作用是决定本仓取「图」还是「视频」成品（`expect`）。
3. 三个字段的取值域已收敛到 `gen_params.PARAM_SPEC`，测试
   `tests/test_gen_params.py::test_whitelist_parity_with_protocol_generation_constants`
   钉住它与 `protocol_generation.py:118-119` 的派生常量一致（该文件 `:68` 已直接
   `import PARAM_SPEC`），防漂移。

### 1.3 降级为 `hypothesis`（禁入生产路径）

| 字段 | 唯一出处 | 为什么不能进白名单 |
|---|---|---|
| `seed` / `--seed` | `work/museapi-video/platform/ParamsPanel.jsx:35` | 只有这一份 UI 自造；本仓 `protocol_generation.py:190,235-236` 仍有该字段（fal 入口 `app.py:1001-1027` 已改为拒绝），没有第二份独立证据 |
| `stylize` / `--st` | `ParamsPanel.jsx:18` | Midjourney 风格字符串；muse 侧零解析代码。范围 0..1000 步进 10 |
| `chaos` / `--chaos` | `ParamsPanel.jsx:19` | 同上，0..100 |
| `weird` | `ParamsPanel.jsx:20` | 同上，0..3000 步进 50 |
| `iw` | `ParamsPanel.jsx:21` | 同上，0..3 步进 0.1 |
| `cw` | `ParamsPanel.jsx:22` | 同上，0..100 |
| `sw` | `ParamsPanel.jsx:23` | 同上，0..100 |
| `quality` / `--q` | `ParamsPanel.jsx:24` | 同上，0.25..2 步进 0.25 |
| `resolution` | `recon/upstream/app.py:496,988-989` | 只有 muse2api 一支有；平台 UI 不暴露，无真机证据 |
| `extra` | `recon/upstream/app.py:498,742,990` | 语义是任意文本 = 等于允许绕过本白名单 |

关键判断：`--st`/`--chaos` 这一族**不是** muse 的协议参数，而是 `museapi-video`
自己的参数面板把「提示词装饰」写成了 CLI 风格字符串（`ParamsPanel.jsx:18-24` 的
`PARAM_SPECS` + `:27-37` 的 `buildExtra()`），上游只是把 `extra` 原样粘到提示词
（`app.py:742,990`）。**一份 UI、一份粘贴逻辑，就这两份，且后者不解析**。
把它们当「muse 参数」是层间混淆（§2.1）。

### 1.4 弱证据说明：视频侧的 `4:3 / 3:4`

`aspect_ratio` 的 5 值集合在**图片**侧证据充分：
`upstream/app.py:721-735` 五个分支 + `config.js:38` + `README.md:49` +
`lichao…/static/app.js:561`（`aspects: ['1:1','3:4','4:3','9:16','16:9']`）。

但**视频**侧：
`lichao…/static/app.js:562` 的视频模型只有 `['1:1','9:16','16:9']`，
上游 `README.md:53` 也只提 `9:16`/`16:9`；5 值全集只来自 `platform/config.js:38`
这一份（它与图片共用 `sizes`）。因此 **`4:3`/`3:4` 用于视频属于弱证据（单源）**。

本轮**不**给 `gen_params` 加「按 expect 细分 aspect 域」的交叉规则——那会引入一条
没有证据的新规则（同样是猜测）。现行做法：单域放行 + 本报告把弱证据标注出来，
等真机复核（§6 第 3 条）确认后再收紧。

### 1.5 与 `--st / --chaos` 的关系（任务点名项）

- 真实字段名：**不存在**。它们只作为 `extra` 文本的一部分出现在上游 REST 的入参里，
  最终变成提示词字符串。muse 线上没有 `st`/`chaos`/`weird` 之类的结构化字段。
- 合法值范围：只有 `ParamsPanel.jsx:17-25` 里的 UI 范围（上表），没有任何服务端校验
  证据；因此合法值 = 本仓**不**接受（`normalize_params` 抛 `hypothesis` 错误）。
- 证据等级：`hypothesis`（单份二手）。
- 处置：登记在 `gen_params.HYPOTHESIS_FIELDS`；测试逐字段确认被拒绝
  （`tests/test_gen_params.py::test_every_hypothesis_field_is_explicitly_rejected`）。

### 1.6 生成参数的真机复核清单

1. 用真实账号发 `duration=7` 与 `duration=8` 各一条视频，比对成片时长 —— 验证服务端
   是否真的执行提示词里的「严格 N 秒」（服务端接受集 `unverified`）。
2. 发 `aspect_ratio="21:9"`（白名单外）看是否被「通用兜底句」接受/或产出异常画幅 ——
   验证兜底分支是否真的存在（`app.py:735` 只是代码推断）。
3. 视频 + `4:3`、`3:4` 是否成片（弱证据项，见 §1.4）。
4. `--st`/`--chaos` 文本加入前后，同提示词输出是否有可见差异 —— 判断这一族是否有效。
5. `seed` 是否有任何可观察影响（同 seed 复现性）—— 若有，再考虑提升等级。

---

## 2. RPC 方法名清单

### 2.1 先分清三层（混层是本轮最大的读数风险）

1. **muse.ai 域 HTTP**：普通 HTTPS，cookie 鉴权，`fetch()` 直接可见。主要用途是
   「拿 VM / 拿 token」，不是生成。
2. **Noise 内层 HTTP**：加密通道里的 `ServiceRequest{verb,path,headers,body}`
   （chunk `1w9duffuzanuc.js@13361` 明确 `verb: httpMethod, path`）。**ACL 按
   `verb+path` 判定**：同一 path 换 verb 就 403（`docs/PROTOCOL.md:419`）。
3. **Gateway 点分方法**：应用层 query/mutation 名（`artifacts.pin`、
   `connection.ping`），走 gateway 信封而不是内层 HTTP path；真实 wire 形状是
   protobuf（`PHASE_0_REPORT.md:87-93` 说明 schema 未取全）。

> 不要把第 3 层的名字当成内层 path，也不要把 `muse2api` 自己的 REST 字段
> （`duration`/`aspect_ratio`/`extra`）当成 muse 的字段。

### 2.2 muse.ai 域 HTTP（cookie 鉴权）

| 方法 | path | 主要入参 | 等级 | 证据 |
|---|---|---|---|---|
| POST | `/api/hatch/vm/wake` | `{vm_id, retry_count, connect_attempt_id?}` | `observed` | `2o6_a0psi6u-x.js@30409` |
| POST | `/api/hatch/lease-vm` | `{vmType}`；已有 VM 时 403 | `observed` | `1dva5p6eh25ps.js@45032`；403 语义见 `PHASE_0_REPORT.md:64-76` |
| POST | `/api/hatch/token` | `{vmAddress, vmName?, force_refresh?}` → `{token}` | `observed` | `24vdgjhlw22uj.js@9994` |
| POST | `/api/hatch/noise-notary-token` | `{vmName}` → `{notaryToken}` | `observed` | `1w9duffuzanuc.js@33635` |
| GET | `/api/session` | 无 body，`cache:"no-store"`；返回 VM 列表含 `vmState` | `observed` | `2o6_a0psi6u-x.js@9048`；`2b_2bs05jboin.js` 同 path |
| DELETE | `/api/hatch/saved-spaces` | `{shortcode}` | `observed` | `24vdgjhlw22uj.js@29017` |
| POST | `/api/auth/save-account` | 账号保存 | `observed` | `1dva5p6eh25ps.js@900` |
| GET | `/api/logout` | 登出 | `observed` | `1dva5p6eh25ps.js@29807` |
| GET | `/v1/noise`（升级为 WSS） | query: `vm_id, auth_token, notary_token?, app_id, request_id`（可选 `atod_token`） | `observed` | `24vdgjhlw22uj.js@12500-12535`；常量 `:2209`、`HATCH_APP_ID` `:736578` |
| POST | `/api/hatch/{invite,promotion,app,cloudflare,private,cvm,vesta,voice/voyager,voice/speech,voice/tts,voice/tts/stream}` | 未知 | `hypothesis` | 仅一处扫描记录：`PHASE_0_REPORT.md:83` + `signal_summary.json:157-339`；原 chunk 未留存，无法复核 |

补充：`/api/hatch/token` 的 JWT `kid=HATCH_EDGE_TO_VM_ADMISSION:1` 与
`notary_token` 的 `endorsement.v1.*` 形态见 `PHASE_0_REPORT.md:47-56`（单份记录 →
`hypothesis`）。部署号漂移提示见 `PHASE_0_REPORT.md:110`、`docs/PROTOCOL.md:471`。

### 2.3 Noise 内层 HTTP（加密通道）

| 方法 | path | 主要入参 | 等级 | 证据 |
|---|---|---|---|---|
| POST | `/api/ping` | `{presence:"active"}`（约 15s 超时） | `corroborated` | `lichao…/protocol_client.py:421`；`manishkumar…/docs/api-reference.md:155`（标为 observed）；本仓 `protocol_client.py:1096-1098`；点分名 `connection.ping` 见 `11qbbeqlkqufw.js@8462` |
| POST | `/api/nodes/register` | `{node_id, display_name, platform, commands_v2}` | `hypothesis` | 只有 `manishkumar…/src/hatch/chat.ts:48`、`:154` 与其抓包记录（`memory/05-10-2026-side-chat-routing.md:41`）；本仓 `protocol_client.py:929-942` 由该记录派生，**不计独立第二份** |
| POST | `/client/register-capabilities` | `{client_id, platform, display_name, version, capabilities}` | `hypothesis` | 只有 `chat.ts:52-55` 与 `api-reference.md:153`（同一项目）；本仓 `protocol_client.py:908-927` 派生。两处 `capabilities` 形态不同，见 §2.5 |
| POST | `/chat/subscribe` | `{after_stream_seq, after_chat_event_seq, capabilities}`（长流，200 头后持续写） | `corroborated` | `lichao…/protocol_client.py:501`；`chat.ts:68-69`；`api-reference.md:85` |
| POST | `/chat/stream` | 文本消息（body 两套方言，见 §2.5） | `corroborated`（path + 仅 POST）/ 接入形态未定 | `lichao…/protocol_engine.py:331,336`；`chat.ts:82,150`；`api-reference.md:98,173`（GET 同 path 403：ACL 按 verb+path） |
| GET | `/chat/history` | query `limit=40&transcript_mode=messages&session_id=` | `hypothesis` | 只有 `chat.ts:179` 与 `memory/05-10-2026-side-chat-routing.md:37`（同一项目） |
| POST | `/fs/read` | `{path, offset?, len?}`，单次 ≤1MiB，需循环到 eof | `hypothesis` | 素材里只有 `lichao…/protocol_client.py:526` 一份独立实现；本仓 `protocol_client.py:1155` 由它派生，**不计独立第二份** |
| POST | `/fs/write` | 分块写 | `hypothesis` | 同上：`lichao…/protocol_client.py:554,562`；本仓 `:1208,1226` 派生 |
| POST | `/fs/stat` | `{path}` | `hypothesis` | 同上：`lichao…/protocol_client.py:568` |
| POST | `/fs/rename` | `{from, to}` | `hypothesis` | 同上：`lichao…/protocol_client.py:572`；本仓 `:1124` 派生 |
| POST | `/fs/delete` | `{path, recursive}` | `hypothesis` | 同上：`lichao…/protocol_client.py:581` |
| POST | `/thread/new` | 未知 | `hypothesis` | 只有 `lichao…/engine.py:1400` |
| GET | `/v1/media/` | muse2api 自身的本地媒体路由（**不是 muse 的**） | `observed`（在 lichao 仓内） | `lichao…/engine.py:1746` |

> 评级规则再强调一次：**本仓自己的实现不算第二份独立证据**——它是从这些二手材料
> 写出来的。所以 `/fs/*`、`/api/nodes/register`、`/client/register-capabilities` 只能
> 到 `hypothesis`；`/chat/subscribe`、`/chat/stream`、`/api/ping` 因为有两份**互相
> 独立**的二手实现/抓包记录才够 `corroborated`。

> 内层请求/响应是 protobuf（`NoiseTransportFrame → ServiceRequest → ServiceFrame`），
> 但 `/chat/subscribe` 的 body 是 NDJSON 行（本仓 `protocol_client.py:1074-1093` 按行解析）。

### 2.4 Gateway 点分方法（应用层信封）

| 名称 | 形态 | 入参/出参要点 | 等级 | 证据 |
|---|---|---|---|---|
| `connection.ping` | 请求 | `{presence: "active"/"away"}`；明文兜底帧 `{type:"req", id, method, params}` | `observed` | `11qbbeqlkqufw.js@8462,8656,3277` |
| `client.invoke` | 事件 | `{command_id, …}` 命令下发 | `observed` | `11qbbeqlkqufw.js@27449` |
| `approvals.snapshot` | 事件 | 审批快照 | `observed` | `11qbbeqlkqufw.js@20052,27449` |
| `artifacts.subscribe` | 订阅 | `{collection, sort}` | `observed` | `24vdgjhlw22uj.js@25667` |
| `artifacts.snapshot` | 事件 | `{collection, sort, artifacts[], chunk_number, chunk_count, total_count}` | `observed` | `24vdgjhlw22uj.js@23838,24642` |
| `artifacts.pin` | 请求 | `{artifact_ref, pinned, before_artifact_ref?, after_artifact_ref?}` | `observed` | `24vdgjhlw22uj.js@19515` |
| `spaces.saved.delete` | 请求 | `{slug}` | `observed` | `24vdgjhlw22uj.js@29017` |
| `navigation.items.updated` / `.removed` | 事件 | `{artifact}` / `{artifact_ref}` | `observed` | `24vdgjhlw22uj.js@24992-25135` |
| `vm.enrollment_health` | 请求 | 无参；返回 CVM 注册状态（`UNENROLLED/ENROLLING/ENROLLED/NOT_APPLICABLE`） | `observed` | `1dva5p6eh25ps.js@44769-44881` |
| `chat.history` | 查询键 | `["shell-bootstrap", target, "chat.history", INITIAL_HISTORY_LIMIT, transcript].join(":")`——是 query cache key，**不是** RPC 名 | `observed` | `2o6_a0psi6u-x.js@27530` |
| 消息事件 | 事件 | `message.user` / `message.assistant`，字段 `{seq, occurred_at_ms, event_name, message_id, reply_to_message_id, role, content}` | `observed` | `24vdgjhlw22uj.js@35051` |

另外两个**观察**（不作为 RPC）：`hatch.chat` 是 trace/logger 标记而不是服务名
（`11qbbeqlkqufw.js@35712`）；`["hatch","library","directory"]` 是 queryKey 命名空间
（`24vdgjhlw22uj.js@3181`）。

### 2.5 两处未解分歧（真机复核项）

1. **`/chat/stream` body 方言**
   - A（`lichao…/protocol_engine.py:308-336`，`body = {"items": …, "node_id", "capabilities": {}}` 在 `:331`）：
     `items:[{type:"text", text}]` + `node_id` + `capabilities:{}`（+`metadata`）。
   - B（`manishkumar…/docs/api-reference.md:98,102-108` 自称「浏览器实际发送」；
     `chat.ts:64-67`；本仓 `protocol_client.py:986-1001`）：`message` + `node_id` +
     `capabilities:[...]` + `timezone`（+ 附件时 `items`、有 session 时 `session_id` +
     `metadata`）。
   - 两者都自称能工作/来自抓包；**path 可 `corroborated`（且只接受 POST），body 形态未定**。
   本仓 `docs/PROTOCOL.md:419` 记的是第三种 `{kind:"action", text, attachments?, node_id?}`
   —— 与现有代码 `protocol_client.py:986-1001` 不一致（§5 第 2 条）。
2. **`/client/register-capabilities` 的 `capabilities` 形态**
   - `api-reference.md:153` + `chat.ts:54-59`：对象
     `{data_sources, device_commands, hatch_app_commands, rendering}`。
   - 本仓 `protocol_client.py:919-925`：字符串列表 `["chat", …]`。
   - 两套都是二手；对象形态更像真实浏览器（该项目的抓包记录声称如此），但无第二份独立证据。

### 2.6 RPC 真机复核清单

1. 抓真实 hatch-web 的 `/chat/stream` 帧，裁定 §2.5 的方言 A/B（以及 PROTOCOL.md:419 的
   `kind:"action"` 说法是否过期）。
2. 抓 `/client/register-capabilities` 的 `capabilities` 结构与取值枚举。
3. 验证 `/chat/history` 的 verb（GET 还是 POST）与 query 参数（只有一份来源）。
4. 验证 `/thread/new`、`/api/nodes/register` 的真实 body。
5. 补 `/api/hatch/{voice/*,cvm,vesta,…}` 的存在性与形状（当前是单份扫描记录）。
6. 验证内层 ACL 的完整 `verb+path` 表（已知反例：`/chat/send` 只收 `kind:"action"`，
   `GET /chat/stream` 403，见 `docs/PROTOCOL.md:419`、`protocol_client.py:973-978`）。

---

## 3. 额度 / 配额结构

### 3.1 结论：没有 JSON，只有 DOM 文本

- **没有任何结构化额度接口的证据**：9 个留存 chunk 里 `usage / credits / billing /
  quota / weekly / plan` 全部零命中；`signal_summary.json` 的 210-chunk 扫描词表里
  也没有这类信号词；`docs/PROTOCOL.md:478` 原文：**「30 个仓库里无人给出，判定为未知
  （不是不存在）」**。因此等级是 `unverified`（既未证实也未证伪），不是 `observed`
  的「不存在」。
- **已证的唯一读取路径**：浏览器打开「底部 Settings 菜单 → Settings 项 → 面板
  General → Usage 区块」，读 `[role=dialog]` 的 `innerText`。
  上游实现 `work/museapi-video/recon/upstream/engine.py:813-819`（面板文本样例）、
  `:834-844`（Settings 入口：`[data-testid="hatch-dock-more"]` 或 `aria-label`
  `Settings/設定/设置`）、`:861-865`（`[role=dialog],[aria-modal=true]` → `innerText`）、
  `:895-968`（解析）。
  `hatch-dock` 这组常量在真实 chunk `1dva5p6eh25ps.js@25509` 里存在（`HATCH_DOCK_*`），
  但 `data-testid="hatch-dock-more"` 这个**精确值**未在留存 chunk 中出现 →
  DOM 入口选择器仍是 `hypothesis`。
- **文本解析已被两处代码采纳**：上游 `engine.py:911-968` 与
  `lichao…/engine.py:1076-1089`（同源分支），本仓 `quota.py:62-120` 是第三份拷贝。
  三份是**同一血缘**，不构成 `corroborated`；等级 `hypothesis`（多份但同源）。
- 另一个 `unverified` 项：**配额扣减时点**（发提示词时扣，还是成品完成时扣）——语料零
  证据，本仓 `protocol_generation.py:50-51` 也照实标成 `[unverified]`。不要据此假设
  扣减时机，更不要在没有 429 的情况下推断「这一次没扣」。

> 2026-11-11 真机复核：上面第二条（DOM 入口选择器 `hypothesis`）已实测为 `observed`，
> 面板文案与 `parse_quota_text` 的实际输出见 **§3.6**。其余三级结论不变。

### 3.2 面板文本形态（样例）

```text
Free plan
Weekly limit resets on Sep 30
1% used
Additional tokens / Never expires / 0% used (2B tokens left)
```

（`engine.py:813-819` 的注释即此样例；本仓 `quota.py:1-28` 的模块文档解释了为什么
必须 NFKC 归一、为什么「单独的 plan 标签不算读到额度」。）

### 3.3 结构化字段与单位（`quota.parse_quota_text` 的输出）

| 字段 | 类型 | 单位/取值 | 证据 |
|---|---|---|---|
| `raw` | str | 面板原始文本（≤40 行） | `engine.py:922`；`quota.py:68-83` |
| `plan` | str | 自由文本，如 `Free plan`、`Pro 计划` | `engine.py:924-926`；`quota.py:44,87` |
| `weekly_reset` | str | **自由文本日期**（`Sep 30` / 中文「9月30日」），未归一成时间戳 | `engine.py:928-934`；`quota.py:39-43,91-93` |
| `weekly_used_pct` | int/float | 0..100（百分数，不是 0..1） | `engine.py:940-955`；`quota.py:47-49,101-104` |
| `extra_used_pct` | int/float | 0..100 | `engine.py:956-959`；`quota.py:107-116` |
| `extra_left` | str | **人类可读字符串**，如 `2B tokens left`（未拆数值/单位） | `engine.py:960-962`；`quota.py:111-113` |
| `extra_expires` | str | 目前只有 `"never"` 一种取值 | `engine.py:964-965`；`quota.py:117` |
| `found` | bool | `weekly_used_pct in out`；False 时调用方必须保留旧缓存 | `engine.py:967`；`quota.py:120` |

注意两个「单位陷阱」：百分比是 **0..100 的百分数**（`1% used` → `1`，不是 `0.01`）；
`extra_left` 是**未归一化的人类字符串**（`2B` 到底是 20 亿还是 2 万亿，取决于 muse 的
单位定义，我们没有任何证据）。

### 3.4 `quota.py` 现状与可升级点（本轮**不改**它）

结论：`quota.py` 的文本解析**已经是「结构化解析」的最可行形态**，逻辑与上游
`engine.py:911-968` 逐条对应（`quota.py:30-48` 的正则与 `:62-120` 的流程），
没有发现与证据冲突之处。可以升级但**本轮不做**的三点：

1. `extra_left` 拆成 `{value: 2, unit: "B", currency: "tokens"}`（需要先证 `B` 的定义）。
2. 加 `checked_at` 时间戳与 `source: "dom_innerText"` 字段，让缓存消费者知道新鲜度。
3. `weekly_reset` 自由文本 → 可选 ISO 日期（需要处理 `Sep 30` 跨年、无年份问题；
   上游也没做，属于新语义，建议先攒样本）。

失败路径的纪律照旧：`found=False` 时保留旧缓存，不写「0%」。

### 3.5 配额真机复核清单

1. 打开真实 Settings→Usage 面板，确认英文/繁体/简体三种文案是否仍与
   `engine.py:813-819` 一致。
2. 确认 `data-testid="hatch-dock-more"` 是否仍是入口（留存 chunk 未覆盖该字面量）。
3. 抓一次面板打开期间的全部网络请求，确认**真的没有** `/api/billing`、`/api/usage`
   之类的 JSON（当前只能证明「9 个 chunk + 30 个仓库没提到」）。
4. 取一次真实 `(2B tokens left)` 数值，与「Additional tokens」区块的百分比做一致性
   校验（判断 `B` 的语义与 `0% used` 矛盾时以哪个为准）。

### 3.6 2026-11-11 真机复核：额度面板（实测）

> 素材：真账号 + 真 muse.ai 页面，中文界面。往返一路只读 DOM、只按用户会按的按钮点
> （不注入脚本、不伪造请求）。指标是 `/admin/accounts/muse1/quota` 从 **504** 修到 **200**。
> 本节只写现场看到的东西；对 `B` 的语义、扣减时点这些**没有证据**的问题不做推断。
> 定位约定：`driver.py` 本轮有并行改动、行号会漂，所以下面给**函数名**而不是行号。

**入口：`hatch-dock-more` 只认真实鼠标事件**

- dock 侧栏的按钮是 `button[data-testid="hatch-dock-more"]`，`aria-label="设置"`。
- 它**不认** JS 的 `el.click()`（React 的指针处理只响应真实指针事件序列），必须先
  `mouse.move`（hover）再 `click`。这也是旧实现"点了没反应"的直接原因之一。

**入口之后还有一层菜单：全等匹配是 504 的根因**

点开入口先弹一个 `[role=menu]`，实测文本是：

```text
键盘快捷键  Ctrl+/
下载应用
报告问题
设置        Ctrl+,
```

要再点其中的「设置」项才进设置对话框。**菜单项的 `textContent` 带快捷键后缀 `Ctrl+,`**，
旧实现按全等匹配 `"设置"` → 永远匹配不到 → 等到超时 → 接口 504。
改成**前缀匹配**（`text == label` 或 `text.startswith(label + " ")`）后同一账号返回 200。
落点：`muse_bridge/driver.py` 的 `_click_settings_entry()`（入口，先 hover 再 click）与
`_click_settings_menu()`（菜单项文本判定；前缀匹配就在这个分支里）。

**设置对话框里「通用」页就带使用情况**（实测原文，中文界面）

```text
免费版
每周限额将在 11月17日重置
已使用 2%
额外的使用额度
从不过期
已使用 0%（剩余 10 亿个词元）
```

菜单里**不需要**再切到「Usage/用量」页：入口点进去默认就是「通用」，额度文本已经在
`[role=dialog]` 的 `innerText` 里。上游 `engine.py:813-819` 的四行英文样例（§3.2）
与这六行是**同一块面板的不同语言**，不是两套结构。

**解析输出（实测，`muse_bridge/quota.py:62-121`）**

```json
{"weekly_used_pct": 2, "weekly_reset": "11月17日", "found": true}
```

`plan` 没有被填：`_PLAN` 要求行尾是 `plan/計劃/计划/方案`，而真机文案是「免费版」——
这是**已知的未覆盖项**，不影响 `found`（`found` 只看 `weekly_used_pct`）。
`extra_used_pct` 同样没被填：`额外的使用额度` 不在 `_EXTRA_BLOCK` 的关键词表里，
所以第二个「已使用 0%」不会被当成额外额度区块。两处都**照实记录，不在本轮改解析器**。

**读完必须关对话框**

读额度成功后要按 `Escape` 关掉 `[role=dialog]`（`driver.py` 的 `_close_overlay()`，
连按两次再松手）：留着它，下一轮发消息点不到输入框（遮罩挡住 composer）。

**这一节把 §3.5 的哪几条结掉了**

| §3.5 条目 | 结论 |
|---|---|
| 1. 面板文案是否仍与 `engine.py:813-819` 一致 | **同一结构，语言不同**（中文原文见上），§3.2 的英文样例仍有效 |
| 2. `data-testid="hatch-dock-more"` 是否仍是入口 | **是**，`observed`（原为 `hypothesis`） |
| 3. 面板打开期间是否真的没有 usage/billing JSON | **未覆盖**：本轮没为该面板单独抓全量请求，仍是 `unverified` |
| 4. 真实 `(2B tokens left)` 数值与百分比的一致性 | **未做**：真机显示「剩余 10 亿个词元」（中文单位，不是 `2B`），`B` 的语义仍未定 |

---

## 4. notary / 反自动化（仅静态证据）

### 4.1 token 链（从 HTTP 到 Noise）

1. `POST /api/hatch/vm/wake` → `vm_id`（`2o6…@30409`）。
2. `POST /api/hatch/lease-vm` → 租用/续租（`1dva…@45032`）。
3. `POST /api/hatch/token` → 短期 JWT，`kid=HATCH_EDGE_TO_VM_ADMISSION:1`
   （`24vdg…@9994`；`PHASE_0_REPORT.md:68`）。
4. `POST /api/hatch/noise-notary-token`（body 只有 `{vmName}`！见下）→ `notaryToken`。
5. `wss://hatch.metaaivm.com/v1/noise?vm_id&auth_token&notary_token&app_id&request_id`
   （`24vdg…@12500-12535`）。

关键澄清：`PHASE_0_REPORT.md:39-43` 写「浏览器把公钥 POST 给 notary 端点」，但
chunk 字面量 **body 只有 `{vmName}`**（`1w9…@33635`）。公钥绑定发生在之后的
RV 凭据链路（`beginNoiseRvCredentialResolution` → `resolveHatchCvmNoiseNotaryToken`，
`1w9…@33635` 尾部；`24vdg…@3730-4020`）。所以「notary token 把 static key 绑到账号」
的结论成立，但**请求体形状以 chunk 为准**：`{vmName}`。

### 4.2 Noise 身份绑定与证明

- 客户端生成 32 字节 nonce，报错文案 `Noise client nonce must be exactly 32 bytes`；
  服务端 msg1 payload 用 protobuf `uint32(10).bytes(nonce)` 承载（`1w9…@22836-22975`）。
- 会话密钥与两类证明用固定 HMAC 标签（**`observed` 字面量**，`1w9…@22836`）：
  `hatch.noise-auth.key.v1`、`vm-proof-key`、`client-proof-key`、
  `hatch.noise-auth.vm-proof.v1`、`hatch.noise-auth.client-proof.v1`。
- RV（remote verification）链路：`Noise RV notary token is missing`；`rvKey` 与两个
  transcript hash 都要求 32 字节；`state:"unprovisioned"` 走 `{kind:"fresh", bytes:rvKey}`，
  `"provisioned"` 走 HMAC 证明（`1w9…@24080,25174`）。
- CVM/机密 VM：`delegation.` 前缀的 notary token 需要 `getHatchCvmNotarySigningKey(...)
  .endorse(token, "/v1/noise")`；缺 key 抛 `HatchCvmNotarySigningKeyUnavailableError`
  （`24vdg…@3730-4020`）。`endorse()` 的 uri 参数就是 `"/v1/noise"`，与
  endorsement 的 `uri-handle-prefix` 限制对应（`PHASE_0_REPORT.md:52`）。
- attestation：`activeAttestationVerifier` 分 `prod/debug/staging/off` 档，日志
  「SNP attestation verification rejected」（AMD SNP，`1w9…@33635`）。注意 chunk 里存在
  `readAttestationEnforcementOverride()`（`"default"→"off"`）——这是**客户端自带的调试
  开关**；我们只记录它存在，**不使用**（用它等于伪造信任根，`docs/PROTOCOL.md:487` 明确不做）。
- 镜像/撤销校验：Cloudflare 透明性证明 `trustedNamespaces:["prod.pc.revocation_list"]`，
  `maxAgeMs` 30 天；撤销表 `prod.pc.revocation_list` / `pc.revocation_list`，
  `maxAgeMs` 1 天 + `maxFutureSkewMs` 5 分钟；校验失败错误
  `MeasurementMismatch` / `RevocationEvidenceMissing` / `ArtifactRevoked`；X25519
  公钥必须 32 字节（`03etoehba_onc.js@32712-32879`、`1w9…@26330-26402`）。

### 4.3 结论与边界

- 这套设计的目的很直接：**让「Noise static key」= 某个 Meta 账号**，并且对机密 VM
  再加一层「我确实在跟某个可信镜像里的进程说话」。三者（notary endorsement、
  RV proof、attestation/撤销表）任一缺失，对端都会拒绝。
- **认证边界仍是 `muse.ai` 的会话 cookie**（`PHASE_0_REPORT.md:56` 原文
  「Session cookie is still the real auth boundary — no reversing escapes that」）。
  本轮没有任何「无 cookie 进入」的证据，也没有尝试。
- **不做的边界**（与本仓纪律一致）：不伪造 notary token、不绕过 CVM attestation、
  不复刻 Noise 以规避计费/配额、不做指纹伪装（`docs/PROTOCOL.md:484-489`）。

---

## 5. 与现有产品代码的关系（只报告，本轮**不改**产品代码）

> 写作期间工作树里有并行改动，下表按**当前工作树**为准（不是上一版文件的样子）。
> `muse_bridge/gen_params.py` 与 `tests/test_gen_params.py` 是本轮新增的两个文件，
> 产品代码的接线由并行改动完成；这里只记录「证据要求 vs 代码现状」。

| # | 位置 | 现状 | 处置 |
|---|---|---|---|
| 1 | `muse_bridge/app.py:1001-1027` | **已消解**。fal 入参不再拼 `--duration/--aspect/--seed`，改为 `gen_params.normalize_params()` + `render_prompt_clauses()`；白名单外的值在 `:1040-1045` 变成 400 并带中文原因（`seed` 也在 `HYPOTHESIS_FIELDS` 里被拒）。`tests/test_generation_api.py:207-209,212-217` 钉住「自然语言 + 拒绝 seed」 | 保持；这就是 §1.2 白名单的接线 |
| 2 | `docs/PROTOCOL.md:419` | **未消解**。记 `/chat/stream` body 为 `{kind:"action", text, attachments?, node_id?}`，与现有代码 `protocol_client.py:986-1001`（`{message,node_id,capabilities,timezone}`）及两套二手实现**都不一致** | 等真机抓包裁定；裁定前在文档里标注「三种说法并存，未定」 |
| 3 | `muse_bridge/protocol_generation.py:190,235-236` | **已消解**。`GenerationRequest` 的 `seed` 字段与「随机种子 N」渲染已删除（单份二手来源，与 `gen_params.HYPOTHESIS_FIELDS` 的结论相反）；`tests/test_gen_params.py::test_protocol_generation_has_no_seed_entry` 钉住"没有入口" | 若要恢复，先做真机复核（§6 第 5 条），并把等级写进 `PARAM_SPEC` |
| 4 | `muse_bridge/protocol_generation.py:68,118-119` | **已消解**。`VIDEO_DURATIONS`/`ASPECT_RATIOS` 改为 `from .gen_params import PARAM_SPEC` 派生，不再有第二套常量 | 保持；测试 `test_whitelist_parity_*` 继续防漂移 |
| 5 | `muse_bridge/quota.py:30-120` | **无冲突**。与上游 `engine.py:911-968` 逐条对应；风险在 DOM 文案漂移而非解析逻辑 | 保持；补 `checked_at`/`source`（§3.4） |

> 另外一对**不是**冲突、但容易被误读的地方：`protocol_generation.py:45-49` 把
> `duration/aspect/model/seed` 标成 `[hypothesis]`（seed 现已无入口），指的是它们**不是线上字段**
> （这正确）；而 `gen_params.PARAM_SPEC` 把 `duration/aspect/model` 标成
> `corroborated`，指的是**文本约定**有多份独立实现。两者说的是不同对象，
> 本报告 §0.1 与 §1.2 已把口径写清。

---

## 6. 只有二手证据、需真机复核的清单（汇总）

1. `duration`/`aspect_ratio` 是否真的被服务端执行（当前只有文本约定，服务端语义 `unverified`）。
2. `/chat/stream` body 到底是 `message` 还是 `items`，还是 `kind:"action"`（三说并存）。
3. 视频侧 `4:3`/`3:4` 是否可用（单源弱证据，§1.4）。
4. `--st`/`--chaos`/`--weird`/`--iw`/`--cw`/`--sw`/`--q` 是否对输出有任何影响（单源）。
5. `seed` 是否存在/可复现（单源）。
6. `resolution`（`1K/2K/720p/1080p`）是否被 muse 认（单源，且只有上游有）。
7. `/api/hatch/{voice/*,cvm,vesta,invite,promotion,app,cloudflare,private}` 的存在与形状
   （仅一处扫描记录，原 chunk 未留存）。
8. `/chat/history`、`/thread/new`、`/api/nodes/register` 的真实 body（各只有 1 份来源）。
9. `/client/register-capabilities` 的 `capabilities` 形态（两说）。
10. 额度：面板文案是否仍是那四行；是否真的没有任何 usage/billing JSON；
    `extra_left` 的 `B` 语义（§3.5）。
11. notary：`endorsement.v1.*` 的字段布局与 `notaryToken` 的请求体（报告说带公钥，
    chunk 只说 `{vmName}`，§4.1）。
12. 内层 ACL 的完整 `verb+path` 表（已知 `/chat/send` 只收 action、`GET /chat/stream` 403）。
13. CVM 档位（`prod/debug/staging`）的真实触发条件（只有客户端代码，无服务端证据）。

---

## 7. 未解问题

- 生成参数面板到底在不在 hatch-web 里？**未覆盖**（9/210 chunk），不能否定。
- 是否存在一个「生成时就把参数结构化的」内部端点（不在 `/chat/*` 里）？`unverified`。
- `/chat/subscribe` 事件 schema 的权威定义（chunk 只有 NDJSON 用法，schema 未取全，
  见 `PHASE_0_REPORT.md:87-93`）。
- Gateway 点分方法的**完整**名字表（当前 10 条是从留存 chunk 摘的，不是全量）。
- 构建号漂移对 chunk 偏移的影响（今天线上必然不同）。

---

## 8. 复现方式

在 `D:\deep seek\muse-bridge` 下：

```text
python -Xutf8 -m pytest tests/test_gen_params.py -q
python -Xutf8 -m ruff check muse_bridge/gen_params.py tests/test_gen_params.py
python -Xutf8 -m pytest -q
```

第一条须全绿；第二条须 `All checks passed!`；第三条在 `2 failed`（两条已知 oracle
冲突，见 `docs/KNOWN_ORACLE_CONFLICTS.md`）之外不得多红。

证据定位（chunk 是单行压缩 JS，行号无意义，用字符偏移）：

```text
python -Xutf8 "D:\deep seek\work\muse-re\_tmp\ctx.py" "/api/hatch/token" 260 2 \
  "D:\deep seek\work\muse-re\sources\manishkumar1601__muse-ai-api\recon\chunks\24vdgjhlw22uj.js"
```

`gen_params.py` 与本文的对应关系：`PARAM_SPEC` ← §1.2；`HYPOTHESIS_FIELDS` ← §1.3/§1.5；
`normalize_params()` 的拒绝策略 ← §1.1 的「约定而非服务端枚举」结论。

---

## 9. Noise / hatch WS 建连与握手（前端产物复核，2026-11-11）

本节只给结论索引；**证据、偏移、proto 字段、方法表全部在
[`docs/NOISE_BUNDLE_NOTES.md`](NOISE_BUNDLE_NOTES.md)**（同一批 chunk 的逐条 `文件@byteOffset` 引用）。

- 目标 URL 已闭合：`wss://hatch.metaaivm.com/v1/noise?vm_id&auth_token[&notary_token]&app_id=hatch-web&app_version=1&request_id[&atod_token]`
  —— 常量 host/path 与 `muse_bridge/protocol_client.py` 现值一致（`0tmgiplsgovnn.js@26706`、`3w55hkryr8wwj.js@33403`）。
- 帧层常量全部证实：`MAX_PLAINTEXT_FRAME=65489`、`MAX_CHUNKS=256`、nonce=4×`00`‖8B 大端、传输期 AD 为空、
  `Noise_XX_25519_AESGCM_SHA256`（名称补零到 32 字节后 `mixHash`）；另有 16 组装配上限与 16MB 预算未在客户端实现。
- `msg1`=`0a20`+32B nonce 正确；`msg2` 实际最短 **96**（客户端 `MSG2_MIN_LEN=80` 需改）；`msg3` 在标准 VM 下是**空载荷**，
  机密 VM 才是 `notary_token‖{rvKey|proof}`。
- **服务器静态公钥不在常量里、也不由 `/api/session` 下发**：它就是 Noise XX 的响应方静态公钥，由 msg2 第 2 段
  加密块送上来（`2knhq5aux36zv.js@42078`），标准 VM 直接沿用（TOFU），机密 VM 再用 attestation/hostData 钉住。
- `notary_token` 有两个来源：`/api/hatch/token` 响应字段，兜底 `POST /api/hatch/noise-notary-token`（`@47133`）；
  `delegation.` 前缀令牌必须按 `/v1/noise` 路径背书且寿命≈60s。
- 内层 RPC 是 `hatch.noise` 封装的一层 HTTP 语义（`verb/path/headers/body/end_body`），**不是** `kind:"action"`/`items`/`node_id`
  那套猜测；方法名有 418 条的静态注册表（`3qzcufyl9d94_.js@525`，导出见 `work\rpc_table.txt`）。
- sourcemap：每个 chunk 都有 `//# sourceMappingURL=` 但 `.js.map` 在 CDN 恒 404（只上传 Sentry），`sourcesContent` 不可得。

---

## 10. Spaces / idea-cards / 画布相关面（2026-11-11 复核，含活网实测）

本节的完整证据表在 [`docs/CANVAS_PROTOCOL.md`](CANVAS_PROTOCOL.md)（135 条已校验锚点），
活网实测记录在 [`docs/CANVAS_DESIGN.md`](CANVAS_DESIGN.md) §10。这里只给结论，便于和上文对读。

- **muse 没有"无限画布"文档模型**：全仓 `react-flow | xyflow | konva | minimap | infinite canvas`
  命中 0；Space 的归一化字段表里没有 x/y/zoom/nodes/edges/camera（`2lzbmcnnh3_e0.js@15101`）。
  Space = **AI 生成的 Web 应用构件**：`spaces.cvm.render {slug}` 返回 Blob(HTML)，前端同源
  iframe + MessagePort 隧道装载（`cvmBootstrapReady → install-bundle → document-ready → init-port`）。
- **Space 列表/分享**：`GET /spaces/v2[?include_constructing=true]`（活网 200 `{"spaces":[]}`）；
  私有 frame URL `/api/hatch/private-space-frame-url?slug&protocol=2&hatch_vm_identity[&v]`（45s 缓存）；
  分享用 Cloudflare `viewer_assertion` JWT；别人的 space 走 `peerSpaces.document`。
- **`spaces.save` 只对已存在 artifact 生效**：活网 `POST /spaces/v2/<新slug>/save`（空体/带
  name/带 doc）三连 → 404 `shared artifact not found`；`DELETE` → 404 `web artifact not found`。
  创建发生在 frame app 内部，网关层没有创建入口。**结论：不要指望用网关 API 建/存画布文档。**
- **Explore 灵感卡不是画布节点**：`GET /api/idea-cards` → `{"ok":true,"result":{"sections":[…]}}`
  （活网 200）；`POST /api/idea-cards/{id}/execute` 体 `{ideaCardId,mode:"full"|"selectedItems",itemIds?}`
  返回 `{status:"queued"|"accepted",cardId,chat:{stream_owner_message_id,channel,session_id,is_thread}}`，
  是**异步触发一次 AI 搭建**，要配合 `/chat/stream` 才有下文，不是出图任务。
- **`/nodes/*` 与画布无关**：它是设备节点注册与 `client.invoke` 命令分发。
- **本仓选择**：画布做成自研产品层（`muse_bridge/canvas/` 零构建 ES modules + `docs/CANVAS_DESIGN.md`
  的 Doc v1 契约），"适配 muse"落在复用生成链路（`/v1/images/tasks` → 六态轮询 → `/v1/media`）
  与后续 Space 列表/导出上，不复制 muse 的文档模型（它没有）。
