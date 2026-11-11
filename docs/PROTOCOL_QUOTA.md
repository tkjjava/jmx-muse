# 协议侧额度 / 用量取证（负证据版）

> 取证日期：2026-11-11（静态语料 `work/muse-bundles/` + Lead 真机只读探针
> `work/canvas_re/protocol-route-dump/`）。执笔：`quota-recon`（task-6）。
> 本文件只做两件事：**把"协议侧读不到周百分比"证到什么程度写清楚**，以及
> **钉住唯一能读到的会话字段**。所有结论分证据级别，推断与实测不混写。

一句话结论：

> **额度周百分比在协议侧不可读**——Noise 内层 418 条方法白名单里没有任何
> `usage` / `quota` / `billing` / `plan` / `credit` 命名的 RPC，4 条候选路径真机
> `GET` 全部 `404 route not found`（与对照路径同形）；前端额度面板确实存在，
> 但其取数组件**不在已下载的 220 个 chunk 里**（负证据），所以**不能**说
> "服务端绝无额度接口"，只能说"**内层路由表里没有，且静态语料未覆盖面板取数点**"。
> 唯一可走协议读出的是 `/api/session` 的会话字段，而它连内层也 404——真正读它
> 的是 Noise **外层** HTTP（`session_http` + cookie）。

---

## 0. 证据级别（本文件用词的定义）

| 级别 | 定义 | 本文件里的例子 |
|---|---|---|
| **真机实测** | Lead 在真机隧道里实跑得到的 status/body，落盘在 `work/canvas_re/protocol-route-dump/` | 5 条候选 GET 全 404（69 字节同形 body） |
| **实测字面量** | 语料里能逐字复核的原文，给出 `chunk@字符偏移` | `method:"subscription.callback"`、`"UJXDx":"{percent}% used"` |
| **仅注册表** | 418 条注册表里有条目，但**全语料找不到一处调用点** | `subscription.redeem_complimentary` |
| **未覆盖** | 该 UI 组件 chunk **不在** 220 个文件里；它引用词表 id，但代码里没有该 id 的引用 | 额度面板取数组件 |
| **推断** | 由间接证据推得，**不作为实现依据**，仅作解释 | "面板文案可能由服务端下发本地化串" |

本仓库既有词汇（`docs/FRONTEND_RE.md §0.1`）是 `observed / corroborated /
hypothesis / unverified`，对应关系：实测字面量→observed、仅注册表→corroborated、
未覆盖→unverified、推断→hypothesis。

---

## 1. 取证边界（先说清楚"没做什么"）

| 项 | 事实 |
|---|---|
| 静态语料 | `work/muse-bundles/`：**220 个 `.js` chunk，合计 32,631,831 字节（31.1 MB）**；部署 id `dpl_AmYcy8nhVKz7uDfYcPy4ww7ZnoNJ` / `dpl_3WKPwME7mcgKdL9XQtmQooWnmkiJ` |
| 偏移语义 | 全部是**单行 Turbopack 产物的字符偏移**（不是行号）。`chunk@12345` 只能用本语料复核，换构建即失效 |
| 真机证据 | 来自 Lead 2026-11-11 09:5x 的只读 `GET` 探针（`protocol-route-dump/summary.json`）；**本轮 `quota-recon` 未发任何真机请求** |
| 没做的事 | 未做"418 条 path 全矩阵 GET 扫"（只测 5 条候选 + 1 条对照 + 1 条健康对照），未爬取未下载的 chunk，未改任何运行时 |
| 语料完整性 | `work/chunk_map.tsv` 只有 221 行（表头 + 220 chunk），`work/buildManifest_check.txt` 为 `Not Found`——**构建清单拿不到**，chunk 列表来自页面 HTML 抓取，因此"面板取数组件未下载"是**很可能**而非"必然" |

---

## 2. 候选面清单（一页看完）

| # | 候选 | 取证 | 级别 | 判定 |
|---|---|---|---|---|
| 1 | `GET /api/usage` | 404 `{"ok":false,"error":{"code":"not_found","message":"route not found"}}`（69 B） | 真机实测 | 内层**无此路由** |
| 2 | `GET /api/quota` | 同上，69 B | 真机实测 | 无 |
| 3 | `GET /api/billing/usage` | 同上，69 B | 真机实测 | 无 |
| 4 | `GET /api/me/usage` | 同上，69 B | 真机实测 | 无 |
| 5 | `GET /api/session` | 同上，69 B | 真机实测 | **`/api/session` 不在这张内层路由表里**（它是外层 HTTP，见 §5） |
| 6 | `GET /api/zzz-not-a-route`（对照） | 同上，69 B | 真机实测 | **404 是路由层判定，不是 ACL/权限层**（否则对照会与候选不同） |
| 7 | `GET /api/idea-cards`（健康对照） | 200，67,759 B JSON | 真机实测 | 隧道**通**、会话**有效**→ 候选 404 不是通道问题 |
| 8 | `subscription.callback` `POST /subscription/callback` | 注册表 1 条，**全语料调用点 0** | 仅注册表 | 支付回跳，非额度查询 |
| 9 | `subscription.redeem_complimentary` `POST /subscription/redeem-complimentary` | 注册表 1 条，**全语料调用点 0** | 仅注册表 | 兑换订阅，非额度查询 |
| 10 | `wallet.get` `GET /wallet` / `wallet.set` `POST /wallet` | 注册表各 1 条；调用点做 `payment_methods`/`payment_options` 归一 | 实测字面量 | **支付方式目录**（`payment_type`/`card_brand`/`card_last4`），**不含用量** |
| 11 | `/usage` 斜杠命令 | `{name:"/usage",category:"Session",description:"Toggle usage display",args:"on \| off"}` | 实测字面量 | **聊天 gateway 命令**，不发 HTTP 请求 |
| 12 | 额度面板文案（`{percent}% used` 等） | 46 份本地化词表各 1 条；`hk:"UJXDx"` 代码引用 **0** | 未覆盖 | 面板组件 chunk **未下载**，取数端点静态不可知 |
| 13 | `out_of_credits`（"Out of usage"） | 前端 agent 状态枚举成员，10 处字面量 | 实测字面量 | **"额度耗尽"布尔信号在 UI 侧存在**；下发来源未定位 |
| 14 | `hatch.settings.subscription.usage` | 设置页路由 id | 实测字面量 | 面板**入口存在**（但不能推断端点） |

**没有出现的**（全语料负计数）：`Weekly limit`=0、`Free plan`=0、`免费版`=0、
`每周限额`=0、`小写 weekly`=0、`reset_at`/`resetAt`/`estimated_expiry_at`=0、
`tokens_left`=0、`percentUsed`=0、`planName`/`plan_type`=0、`quota_exceeded`=0、
`allowance`=2（里程文案）、`entitlement`=8（`MediaKeySystem`）。

---

## 3. 锚点原文（逐条可复跑）

> 复核方式：`cd 'D:\deep seek\work\canvas_re'` 后用 `python -Xutf8 dump_ctx.py
> <chunk> <offset> --max N` 看上下文；`find.py` 按字面量找全部命中。
> 下面每条都标了 `chunk@偏移`，偏移是**该原文子串的起始字符下标**。

### 3.1 网关注册表：仅有的两条 subscription 与一对 wallet

| 锚点 | 原文片段 |
|---|---|
| `3qzcufyl9d94_.js@12048` | `{method:"subscription.callback",httpMethod:"POST",path:"/subscription/callback"}` |
| `3qzcufyl9d94_.js@12129` | `{method:"subscription.redeem_complimentary",httpMethod:"POST",path:"/subscription/redeem-complimentary",requestTimeoutMs:9e4}` |
| `3qzcufyl9d94_.js@28553` | `{method:"wallet.get",httpMethod:"GET",path:"/wallet"}` |
| `3qzcufyl9d94_.js@28607` | `{method:"wallet.set",httpMethod:"POST",path:"/wallet"}` |

注册表规模（同一 chunk，表头式统计）：**418 条方法条目、418 个唯一方法名、
419 条 path、377 个唯一 path**（`work/rpc_table.txt` 第 1 行即 `# rpc methods: 418`）。
方法名按 `usage|quota|billing|plan|credit|limit|subscription|entitle|allowance`
检索，**只命中 `subscription.callback` / `subscription.redeem_complimentary` 两条**。

这两条方法名在全语料各出现 **1 次**（就是注册表这一处，没有调用点）：

```powershell
cd 'D:\deep seek\work\canvas_re'
python -Xutf8 find.py "subscription.callback" "subscription.redeem_complimentary"
# 输出各自 hits=1，落点都在 3qzcufyl9d94_.js
```

### 3.2 `wallet.get` 是支付方式，不是用量

| 锚点 | 原文片段 |
|---|---|
| `0-hyrr_yu-la1.js@40306` | `id:"wallet.get",queryKey:...,noiseFn:async e=>...`（归一化 `payment_methods`） |
| `0-hyrr_yu-la1.js@40471` | `payment_methods`（`payment_options` / `payment_method` / `display_name` 的映射入口） |
| `0-hyrr_yu-la1.js@41279` | `sendRequest("wallet.get",{})` |

归一化后的形状只有支付信息：`{paymentType, paymentId, paymentMethodLabel,
cardBrand, cardLast4}`（`0-hyrr_yu-la1.js@40125` 的 `cardBrand`、`@40157` 的
`cardLast4`）。**没有** 百分比 / token 余额 / 周期重置字段。

### 3.3 `/usage` 是聊天斜杠命令（不是 HTTP path）

| 锚点 | 原文片段 |
|---|---|
| `0zr5ttz54gz63.js@337465` | `{name:"/usage",aliases:[],category:"Session",description:"Toggle usage display",args:"on \| off"` |
| `0zr5ttz54gz63.js@337793` | `sendGatewayCommand(`/usage ${n}`)` |

同文件里 `/usage` 出现在命令注册表与 `sendGatewayCommand` 调用，属于聊天 shell 的
"显示/隐藏用量"开关；它**不产生额度查询请求**，且路径不带 `httpMethod`。

### 3.4 额度面板存在：i18n 词表 + 设置路由 id

面板文案在 46 份本地化词表里各有 1 条（以英/中两份为例，其余语种同形）：

| 锚点 | 原文片段 |
|---|---|
| `1mx36gub0pttk.js@337097` | `"UJXDx":"{percent}% used"` |
| `1mx36gub0pttk.js@337043` | `"29jGF1":"Additional tokens"` |
| `1mx36gub0pttk.js@337072` | `"118ztI":"Never expires"` |
| `1mx36gub0pttk.js@337123` | `"3TXY4V":"Unlimited"` |
| `1mx36gub0pttk.js@336868` | `"20lihT":"Usage"` |
| `1mx36gub0pttk.js@336885` | `"4bN2BB":"Loading your usage"` |
| `1mx36gub0pttk.js@337144` | `"3x9t6x":"Loading subscription status"` |
| `1mx36gub0pttk.js@336634` | `"sBfnJ":"We couldn\'t load your usage. Please try again later."` |
| `273jgzbhnq-7z.js@182233` | `"UJXDx":"已使用{percent}%"` |
| `273jgzbhnq-7z.js@182196` | `"29jGF1":"额外 tokens"` |
| `273jgzbhnq-7z.js@182257` | `"3TXY4V":"无限制"` |
| `273jgzbhnq-7z.js@182087` | `"20lihT":"使用情况"` |
| `273jgzbhnq-7z.js@182103` | `"4bN2BB":"正在加载你的使用情况"` |
| `273jgzbhnq-7z.js@181978` | `"sBfnJ":"无法加载使用情况，请稍后重试。"` |
| `273jgzbhnq-7z.js@18007` | `"1ArUUn":"已用完。"`（agent 状态文案，见 §3.5） |
| `1vzyi952yno5v.js@58652` / `@58718` / `@58790` / `@58856` / `@58922` / `@58986` | `SETTINGS_SUBSCRIPTION_CANCEL/DOWNGRADE/MANAGE/REFUND/TOPUP/USAGE` = `hatch.settings.subscription.{cancel,downgrade,manage,refund,topup,usage}` |

**关键负证据**：文案有、引用没有。

```powershell
cd 'D:\deep seek\work'
(Select-String -Path 'muse-bundles\*.js' -SimpleMatch '"UJXDx"' | Measure-Object).Count   # 46
(Select-String -Path 'muse-bundles\*.js' -SimpleMatch 'hk:"UJXDx"' | Measure-Object).Count # 0
```

46 份词表都定义了这条消息，**没有任何代码 chunk 用 `hk:"UJXDx"` 去取它**——即渲染
面板的组件不在语料里。同理 `hk:"20lihT"` / `hk:"29jGF1"` / `hk:"sBfnJ"` 计数全是 0
（对照组：`hk:"1ArUUn"` 有 1 处真实调用，说明"hk 引用"这套机制本身是可命中的）。

### 3.5 额度耗尽的**状态**在 UI 侧存在

| 锚点 | 原文片段 |
|---|---|
| `1oio-h14b2iis.js@9210` | `case"out_of_credits":return n?e._("Out of usage.",null,{hk:"1ArUUn"}):e._("Out of usage",null,{hk:"4357yJ"})` |
| `1oio-h14b2iis.js@13521` | `{eventName:g.f.STATUS_LIMIT_REACHED_IMPRESSION,actionTarget:"status_limit_reached_chip",metadata:{surface:"chat",subsurface:"agent_state_label"}}` |
| `0heurq1eukk5i.js@28894` | `case"out_of_credits":return"static"`（状态 → 形象动画的映射） |
| `24thn7in_976_.js@509` | `"hatch:"!==t.protocol.toLowerCase()` |
| `24thn7in_976_.js@546` | `"devices"!==t.hostname.toLowerCase()`（CTA 深链归一：协议 `hatch:` + host `devices` + `/invoke/<command>`） |
| `24thn7in_976_.js@1167` | `"subscriptions.upgrade":{generalSubView:"plans",...}`（状态 CTA → 打开套餐页） |
| `24thn7in_976_.js@1277` | `"status_limit_reached_upgrade_link"`（埋点 actionTarget） |
| `24thn7in_976_.js@1337` | `"subscriptions.topup":{generalSubView:"buy_credits",...}`（状态 CTA → 打开买额度） |
| `24thn7in_976_.js@1453` | `"status_limit_reached_buy_usage_link"` |
| `24thn7in_976_.js@1615` | 日志串 `out-of-usage status CTA was unusable`（CTA 深链解析失败 / 命令不支持时的 warning，reason 有 `label_missing`/`url_unparseable`/`command_unsupported`） |

读法：前端把 `out_of_credits` 当成 agent 的一种状态并渲染 "Out of usage"、
埋 `STATUS_LIMIT_REACHED_*` 点、还能拿到"升级/买额度"的 CTA。这说明**"是否耗尽"
这个粗粒度信号在客户端存在**，但它是**状态机的一支**，不是可查询的数值接口；
**它的协议来源（哪个事件、哪个字段）在语料里定位不到**（负证据，清单见 §7）。

### 3.6 `/api/session` 实际被前端消费的字段

| 锚点 | 原文片段 |
|---|---|
| `0zza_pw6rs0s9.js@2935` | `fetch("/api/session?vm_state=live",{cache:"no-store"})` |
| `0zza_pw6rs0s9.js@28840` | `if(t(e))return"ELIGIBLE"===e.state\|\|"NOT_ELIGIBLE"===e.state`（`cvm_eligibility.state` 归一） |
| `0zza_pw6rs0s9.js@30966` | `expired:!i&&(l\|\|(e?.expired??!1))`（过期判定） |
| `0zza_pw6rs0s9.js@32357` | `AbortSignal.timeout(35e3)`（`/api/session` inventory 读取的 35 s 超时） |
| `0zza_pw6rs0s9.js@32790` | `throw Error("VM inventory is unavailable")`（`status` 不是 `assigned`/`none` 时判不可用） |

前端口径里 `/api/session` 消费的是 `status`（`"assigned"` / `"none"`）、`vm_state`、
`vm_type`、`vm_id`、`vm_name`、`endpoint_url`、`cvm_eligibility`、`expires_at` 一类
**会话/VM 字段**；**没有任何一处**从它读百分比、周重置时间或套餐名。

### 3.7 "周"字在语料里的全部落点（用于反证）

```powershell
cd 'D:\deep seek\work\canvas_re'
python -Xutf8 find.py '[Ww]eekly' 'Weekly limit' 'Free plan'
```

- 小写 `weekly`：**0 命中**（连 `weekly_percent` 这种键名都没有）。
- `Weekly`（大写开头）：6 处，全部是**提醒/周期文案**，例如
  `1mx36gub0pttk.js@106040` `"3FqCAI":"Weekly on {days} at {time}"`（提醒频率），
  与额度无关（同段没有 usage/quota 语义）。
- `Weekly limit` / `Free plan` / `免费版`：**0 命中**。

> 这一条直接决定了实现口径：既然客户端词表里没有 "Weekly limit resets on …"、
> 也没有 "Free plan"，真机面板上那两行**不可能是前端 i18n 拼出来的**。两种解释
> （都属**推断**，见 §8）：①服务端下发本地化好的整句；②真机构建与本语料存在
> 版本漂移。无论哪种，**静态语料里都不存在可解析的 weekly 字段**。

### 3.8 2026-11-11 11:1x 浏览器抓包：面板打开时**没有任何额度取数请求**

复跑：`python -Xutf8 work/canvas_re/probe_quota_panel_network.py`
（把系统代理临时切到可用的 FlClash `127.0.0.1:7890`，try/finally 恢复 18000；
重载用户已登录的 muse.ai 页面，装 fetch/XHR 钩子 + `Network.enable`，再让
bridge `/admin/accounts/muse1/quota` 打开设置→使用情况）。

**面板确实打开了**（bridge 200，文本与上次一致）：

```
设置 / 通用 / 连接器 / 钱包 / 安全存储 / 权限 / 消息渠道 / 设备 / 数据控制 /
帮助与支持 / 法律信息 / 退出 / 通用 / Meta 账户 / 密码、安全、个人详情 /
使用情况 / 免费版 / 每周限额将在 11月17日重置 / 已使用 7% /
额外的使用额度 / 从不过期 / 已使用 0%(剩余 10 亿个词元) / 升级 / 兑现邀请码
```

**但 Network.requestWillBeSent 里没有任何额度取数**（同窗口内全部 API）：

| 请求 | 性质 |
|---|---|
| `POST /api/falco`（多条） | 遥测 |
| `POST /`（body `{"settings"}` / `[{"includeAgreement":true}]`） | settings 打开埋点 |
| `GET /api/hatch/invite` | 邀请面板数据（见下） |
| `GET /api/checkout/stripe/publishable-key` | 收银台公钥 |
| `GET/POST` consent/status、hatch/vm/wake、hatch/stateful-sharing、feature-invites/mine | boot 期面 |

fetch/XHR 钩子结论相同。**用 fetch response 体抓取实验**（`probe_quota_response_bodies.py`）
里，页面 boot 期唯一命中的额度相关响应是 `/api/hatch/invite`（1,629 B），内容只有
邀请营销/兑换文案（`code`、`invite_link`、`max_uses:30`、`use_count`、
`reward:null`、`redeem_invite_code_screen`），**不含 "已使用 7%" 这类用量数字**。

⇒ 订正 §3.7 的推断方向：每周用量 **既不是**面板打开时现取的 HTTP 接口，**也不是**
前端 i18n 拼的静态文案；它是**随页面 boot（RSC/HTML 或 WS 订阅状态）一起到达的
服务端状态**（同账号两次不同时间抓：4% → 7%，说明它随真实用量变化，不是写死文案）。
所以"面板取数点未定位"要改成：**面板不取数；数值在 boot 状态里**。协议侧仍然没有
可重放的额度端点，`quota_protocol.read_quota()` 返回 `None` 的口径不变；浏览器路径
（`MuseDriver.quota()` 抓面板文本）仍是唯一可用来源，且**它读到的是真实值**。

---

## 4. 为什么说"内层没有额度路由"而不是"没找着"

三条独立证据链：

1. **白名单是闭集**：418 条注册表条目里，方法名/路径关键词扫描只命中两条
   `subscription.*`（回跳与兑换）与一对 `wallet.*`（支付方式），**零 usage/quota/
   billing/plan/credit 命名**。这是"允许哪些内层路由"的**权威清单**——内层路由
   不存在于这张表就必然 404。
2. **真机 404 的形状与对照完全一致**：5 条候选都是 69 字节
   `{"ok":false,"error":{"code":"not_found","message":"route not found"}}`，
   `/api/zzz-not-a-route` 同形。这不是 401/403/ACL 拒绝（§8.4 的 verb+path ACL
   拒绝长这样：`matched route missing from endpoint ACL`），是**路由找不到**。
3. **隧道是健康的**：同一轮探针 `GET /api/idea-cards` → 200 / 67,759 B，说明
   会话、隧道、cookie 都正常，404 不是"没连上"或"cookie 失效"。

### 4.1 但必须保留的边界（不许越证）

- 220 个 chunk 是**抓页面 HTML**得来的，不是构建清单；**面板取数组件可能根本没被
  下载**（`hk:"UJXDx"` 引用为 0 就是这个判断的依据）。因此"服务端没有额度接口"
  **不在本轮结论里**；本轮只说：**内层 418 条路由表里没有** + **候选路径实测 404**
  + **静态语料未覆盖面板取数点**。
- `/api/session` 的内层 404 **不代表外层没有它**：前端是用页面 `fetch("/api/session")`
  （Noise 外层 HTTP + cookie）读它的。`muse_bridge/session_http.py` 走的正是外层。
- 没有做 418 条 path 的全矩阵 GET（只测 5 条候选 + 对照）。原因：真机探针要
  只读、最小化、且不能惊动限频；全矩阵属于"大规模探测"，超出本任务授权。

---

## 5. 模块契约：`muse_bridge/quota_protocol.py`

注入式、纯离线可测，**不碰网络**（client 由调用方给，测试用假件）。

### 5.1 公开对象

| 名字 | 类型 | 作用 |
|---|---|---|
| `read_quota(client, *, timeout=20.0)` | `async` | 额度门面：返回 `{plan, weekly_percent, reset_at, raw, session, source, found, quota_readable, note}` |
| `read_session_quota(client, *, timeout=20.0)` | `async` | 只做 `/api/session`：解析已知字段 + 负证据尾巴 |
| `parse_session_payload(payload)` | 纯函数 | payload → 已知字段 + `unknown_fields` + `raw`（**不做猜测性映射**） |
| `is_route_not_found(status, body)` | 纯函数 | 识别真机那种 404 形状（`code=not_found` 或 message 含 `route not found`） |
| `QuotaProtocolError` / `QuotaNoAccount` / `QuotaRouteNotFound` / `QuotaSessionUnauthorized` | 异常 | 失败分类：不编造空额度冒充成功 |
| `WEEKLY_QUOTA_NOTE` | `str` | 负证据原文（会写进返回值） |
| `QUOTA_CANDIDATE_PATHS` / `REGISTRY_METHOD_COUNT` / `KNOWN_SESSION_FIELDS` | 常量 | 与本文同源（4 条候选路径 / 418 / 已知字段集） |

### 5.2 返回契约（`read_quota`）

| 键 | 值 | 说明 |
|---|---|---|
| `plan` | 恒 `None` | **未取证**的字段一律不映射；`weekly_percent` 同 |
| `weekly_percent` | 恒 `None` | 协议侧不可得（见 `note`） |
| `reset_at` | 恒 `None` | 同上 |
| `raw` | `/api/session` 原始 payload | 未知键原样保留，绝不丢 |
| `session` | 解析后的已知字段 | 含 `vm_state` / `vm_type` / `expires_at` / `cvm_eligibility` / `renewable` 等 |
| `quota_readable` | 恒 `False` | 调用方必须据此显示"协议侧不可得"，**不许渲染成 0%** |
| `note` | `WEEKLY_QUOTA_NOTE` | 负证据一句话 |

**硬不变量**：即便 payload 里出现 `weekly_percent: 42` 这种形似字段，也只进
`raw` 与 `unknown_fields`，`weekly_percent` 仍是 `None`（`tests/test_quota_protocol.py`
用 `test_unknown_fields_pass_through_without_guessing` 钉住）。

### 5.3 错误映射

| 情形 | 抛出 | 处置建议 |
|---|---|---|
| 没有 client / 没有 `request`（等价无 cookie） | `QuotaNoAccount` | 换号或先登录 |
| client 抛任意异常（连接层） | `QuotaProtocolError`（`__cause__` 保留原异常） | 当连接失败，不是额度问题 |
| `/api/session` 401 | `QuotaSessionUnauthorized` | cookie 失效，换号/重登 |
| 404 且 body 是 route-not-found 形状 | `QuotaRouteNotFound` | **不许重试**：内层没这条路由 |
| 其它 4xx/5xx | `QuotaProtocolError`（带状态码） | 原样上报 |
| 响应缺 status / body 非 JSON | `QuotaProtocolError` | 协议异常 |

### 5.4 真机现状必须写进调用方视野

`read_session_quota()` 发的是**内层** `GET /api/session`，而真机实测它 404
（`/api/session` 不是 418 路由表条目）。所以在真机上它通常抛 `QuotaRouteNotFound`。
真正能读 `/api/session` 的是 `muse_bridge/session_http.py`（Noise 外层 HTTP +
cookie）。本模块保留内层读取路径的理由只有一个：**把字段解析规则离线钉死**，
若将来服务端放开内层路由，接口不用改。**不要**把它当"现成可用的额度源"。

---

## 6. 落地文件与测试

| 文件 | 状态 |
|---|---|
| `muse_bridge/quota_protocol.py` | 新增（本模块，303 行） |
| `tests/test_quota_protocol.py` | 新增（22 个用例，**全绿**，离线假客户端，零网络/零浏览器） |
| `docs/PROTOCOL_QUOTA.md` | 新增（本文件） |
| `docs/PROTOCOL.md`、`muse_bridge/protocol_driver.py`、`muse_bridge/quota.py`、`muse_bridge/driver.py` | **未改动**（`protocol_driver.quota()` 的 `weekly_used_pct is None` 由 Lead 另行收敛） |

测试覆盖（22）：无 client/无 cookie 两类入口、client 异常包装、空 session 与字段
缺失、完整 session 解析、`estimated_expiry_at` 候选键、bool/float 不当整数、
未知字段透传、401/404(route-not-found)/404(其它)/503/缺 status/非 JSON 六类错误
映射、`read_quota` 门面键集、timeout 关键字转发、4 条候选路径与 418 注册表计数
与文档同源。

复核命令：

```powershell
cd 'D:\deep seek\muse-bridge'
python -Xutf8 -m pytest tests/test_quota_protocol.py -q
python -Xutf8 -m ruff check muse_bridge/quota_protocol.py tests/test_quota_protocol.py
python -Xutf8 -m ruff format --check muse_bridge/quota_protocol.py tests/test_quota_protocol.py
```

已知红测（**本任务不碰、保持原样**）：`tests/test_noise.py::test_protocol_constants_match_references`、
`tests/test_proto.py::test_int64_signed_roundtrip_through_message[9223372036854775808]`。

全量 `pytest -q`（2026-11-11 本轮跑的那一刻）：**1404 passed / 3 failed**——2 个是上面
列出的已知红测；第 3 个 `tests/test_protocol_regressions.py::test_stage_references_accepts_data_url`
是 `upload_refs` 与测试期望（`sandbox://` 前缀 vs 裸路径）不一致，**属并行改动/既有失败**，
与本任务新增的 3 个文件无关（本任务未改 `protocol_engine.py`）。

---

## 7. 未证实项（如实列出，不许拿推断上线）

| # | 项 | 现状 |
|---|---|---|
| 1 | 额度面板的**取数端点** | **未覆盖**：渲染组件的 chunk 不在 220 个文件里（`hk:"UJXDx"` 引用 0）。端点可能是内层 RPC、外层 HTTP、也可能是页面首屏内联数据；**都没证据** |
| 2 | 真机面板那两行（`Free plan` / `Weekly limit resets on …`）从哪来 | **推断**：不在 46 份 i18n 词表 → 要么服务端下发本地化整句，要么真机构建漂移。两者都意味着静态语料无可解析 weekly 字段 |
| 3 | `out_of_credits` 状态的**下发来源** | **未证实**：前端有渲染/埋点/CTA，但事件名与字段没在语料里定位到；协议层要不要消费它、怎么消费，无依据 |
| 4 | `expires_at` / `cvm_eligibility` / `expired` 的**服务端语义** | 只见到前端消费与归一化；`expires_at` 究竟是 VML 到期还是别的计时，未对拍 |
| 5 | 内层 `/api/session` 404 的**原因** | 只知"不在 418 路由表"，不知它是 ACL 之外、还是压根不提供内层版本 |
| 6 | 是否存在**其它命名**的额度面（`/v1/…`、`/yolk/…`、外层 HTTP 的其它 path） | **未穷举**。418 条按关键词扫过（0 命中）；外层只测了 5 条候选；全矩阵未做（见 §4.1） |
| 7 | 语料 vs 真机的**构建一致性** | `chunk_map.tsv` 的部署 id 与既有文档记录的旧构建不同；`buildManifest_check.txt` = `Not Found`，无法证明"语料 = 当前线上" |

---

## 8. 复跑命令（全部只读、离线）

```powershell
# 0) 语料规模
cd 'D:\deep seek\work'
(Get-ChildItem 'muse-bundles\*.js' | Measure-Object).Count          # 220
(Get-Content 'rpc_table.txt' -TotalCount 1)                        # # rpc methods: 418
Select-String -Path 'rpc_table.txt' -Pattern 'subscription\.|wallet\.'   # 4 条

# 1) 真机证据（Lead 落盘，不是本轮跑的）
cd 'D:\deep seek\work\canvas_re\protocol-route-dump'
Get-Content api_usage.txt, api_quota.txt, api_billing_usage.txt, api_me_usage.txt, api_session.txt, api_zzz-not-a-route.txt -Raw

# 2) 注册表/字面量定位
cd 'D:\deep seek\work\canvas_re'
python -Xutf8 find.py "subscription.callback" "subscription.redeem_complimentary" "wallet.get" "wallet.set" "UJXDx" "out_of_credits"
python -Xutf8 dump_ctx.py 3qzcufyl9d94_.js 12048 --max 400
python -Xutf8 dump_ctx.py 1mx36gub0pttk.js 337097 --max 600

# 3) 负证据计数
cd 'D:\deep seek\work'
(Select-String -Path 'muse-bundles\*.js' -SimpleMatch '"UJXDx"' | Measure-Object).Count
(Select-String -Path 'muse-bundles\*.js' -SimpleMatch 'hk:"UJXDx"' | Measure-Object).Count
(Select-String -Path 'muse-bundles\*.js' -SimpleMatch 'Weekly limit' | Measure-Object).Count
(Select-String -Path 'muse-bundles\*.js' -SimpleMatch '免费版' | Measure-Object).Count

# 4) 模块与测试
cd 'D:\deep seek\muse-bridge'
python -Xutf8 -m pytest tests/test_quota_protocol.py -q
```

自带锚点复核（把下面整段粘进 `python` 即可；需要 `muse-bundles` 语料）：

```python
import os, re
W = r"D:\deep seek\work\muse-bundles"
ANCHORS = [
    ("3qzcufyl9d94_.js", 12048, 'method:"subscription.callback"'),
    ("3qzcufyl9d94_.js", 12129, 'method:"subscription.redeem_complimentary"'),
    ("3qzcufyl9d94_.js", 28553, 'method:"wallet.get",httpMethod:"GET",path:"/wallet"'),
    ("3qzcufyl9d94_.js", 28607, 'method:"wallet.set",httpMethod:"POST",path:"/wallet"'),
    ("0-hyrr_yu-la1.js", 40306, 'id:"wallet.get"'),
    ("0-hyrr_yu-la1.js", 40471, 'payment_methods'),
    ("0-hyrr_yu-la1.js", 41279, 'sendRequest("wallet.get",{})'),
    ("0zr5ttz54gz63.js", 337465, 'name:"/usage",aliases:[],category:"Session"'),
    ("0zr5ttz54gz63.js", 337793, 'sendGatewayCommand(`/usage ${n}`)'),
    ("1vzyi952yno5v.js", 58986, 'SETTINGS_SUBSCRIPTION_USAGE:"hatch.settings.subscription.usage"'),
    ("1mx36gub0pttk.js", 337097, '"UJXDx":"{percent}% used"'),
    ("1mx36gub0pttk.js", 337043, '"29jGF1":"Additional tokens"'),
    ("1mx36gub0pttk.js", 337072, '"118ztI":"Never expires"'),
    ("1mx36gub0pttk.js", 337123, '"3TXY4V":"Unlimited"'),
    ("1mx36gub0pttk.js", 336868, '"20lihT":"Usage"'),
    ("1mx36gub0pttk.js", 336885, '"4bN2BB":"Loading your usage"'),
    ("1mx36gub0pttk.js", 337144, '"3x9t6x":"Loading subscription status"'),
    ("1mx36gub0pttk.js", 336634, '"sBfnJ":"We couldn'),
    ("273jgzbhnq-7z.js", 182233, '"UJXDx":"已使用{percent}%"'),
    ("273jgzbhnq-7z.js", 182196, '"29jGF1":"额外 tokens"'),
    ("273jgzbhnq-7z.js", 182257, '"3TXY4V":"无限制"'),
    ("273jgzbhnq-7z.js", 182087, '"20lihT":"使用情况"'),
    ("273jgzbhnq-7z.js", 182103, '"4bN2BB":"正在加载你的使用情况"'),
    ("273jgzbhnq-7z.js", 181978, '"sBfnJ":"无法加载使用情况，请稍后重试。"'),
    ("273jgzbhnq-7z.js", 18007, '"1ArUUn":"已用完。"'),
    ("1oio-h14b2iis.js", 9210, 'case"out_of_credits":return n?e._("Out of usage."'),
    ("1oio-h14b2iis.js", 13521, 'STATUS_LIMIT_REACHED_IMPRESSION'),
    ("0heurq1eukk5i.js", 28894, 'case"out_of_credits":return"static"'),
    ("24thn7in_976_.js", 509, '"hatch:"!==t.protocol.toLowerCase()'),
    ("24thn7in_976_.js", 546, '"devices"!==t.hostname.toLowerCase()'),
    ("24thn7in_976_.js", 1167, '"subscriptions.upgrade":{generalSubView:"plans"'),
    ("24thn7in_976_.js", 1277, '"status_limit_reached_upgrade_link"'),
    ("24thn7in_976_.js", 1337, '"subscriptions.topup":{generalSubView:"buy_credits"'),
    ("24thn7in_976_.js", 1453, '"status_limit_reached_buy_usage_link"'),
    ("24thn7in_976_.js", 1615, 'out-of-usage status CTA was unusable'),
    ("1xnfhkurwumwc.js", 12797, 'TOPUP_CHECKOUT_PRICE_IMPRESSION'),
    ("1c4ekfs5re47i.js", 79329, 'c.CREDITS_BUY_ADDITIONAL_USAGE_CLICK'),
    ("1in90c44e_xz9.js", 90112, '{"path":"/subscriptions"}'),
    ("1p2bflvmkjcje.js", 9488, 'if("/subscriptions"===e)return"hatch.settings"'),
    ("0zza_pw6rs0s9.js", 2935, 'fetch("/api/session?vm_state=live",{cache:"no-store"})'),
    ("0zza_pw6rs0s9.js", 28840, 'if(t(e))return"ELIGIBLE"===e.state||"NOT_ELIGIBLE"===e.state'),
    ("0zza_pw6rs0s9.js", 32357, 'AbortSignal.timeout(35e3)'),
    ("0zza_pw6rs0s9.js", 32790, 'throw Error("VM inventory is unavailable")'),
]
cache = {}
bad = []
for fn, off, needle in ANCHORS:
    if fn not in cache:
        cache[fn] = open(os.path.join(W, fn), encoding="utf-8", errors="ignore").read()
    text = cache[fn]
    if not text.startswith(needle, off):
        bad.append((fn, off, needle, text[off:off + len(needle)]))
print(f"{len(ANCHORS)} 条锚点，{len(ANCHORS) - len(bad)} 条命中")
for row in bad:
    print("MISS", row)
```

---

## 9. 变更范围与不做事

- 只新增 3 个文件（§6）；`docs/PROTOCOL.md` 由 Lead 汇总，本轮**未改**。
- 没有 commit / push；没有启动或重启 bridge；没有连浏览器；没有对真机发任何请求
  （所有真机数据来自 Lead 落盘文件）。
- 没有把"未覆盖"写成"不存在"，也没有把"推断"写成"实测"：本文件每条结论都带
  证据级别；`WEEKLY_QUOTA_NOTE` 与 `quota_readable=False` 就是把"读不到"编码进
  返回值，避免上层把 `None` 当 `0%`。

**给 Lead 的一句话**：协议侧周百分比不可读（内层无路由 + 候选实测 404 +
面板取数组件未覆盖）；能读的只有 `/api/session` 会话字段，而它在内层也 404，
真机可用路径是外层 `session_http`。
