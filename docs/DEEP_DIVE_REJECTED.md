# 被拒四项的完整逆向：协议注册 / 卡池年龄验证 / 暖 Noise 池 / TLS 指纹 / 路由元数据

本文补上 §8.10 表里「看懂了但不采纳」四项的**机制层**说明。四项都只做
**静态源码逆向**（`work/muse2api_pure/`，浅克隆，只读，未运行对方项目），
所有行号可 `file:line` 复核。目的是：判断标准不靠"感觉风险大"，而靠看清它到底
做了什么、需要什么物料、成本多少、哪一段真值得移植。

复跑与来源：

```text
D:\deep seek\work\muse2api_pure\            lichao199208/muse2api（纯协议 + 注册 + 卡池）
D:\deep seek\work\muse2api_harvest\muse-cli\  nikships/muse-cli（402 条路由元数据）
```

---

## 0. 结论速览

| 项 | 机制本质 | 需要什么 | 与我们的缺口 | 决定 |
|---|---|---|---|---|
| 协议注册 | 13 步 HTTP 状态机 + 邮箱 OTP + 随机身份 + 地理门禁探测 | 邮箱物料（Graph/IMAP token）、美国住宅代理 | 我们只有"导入 cookie"，没有签发 cookie 的能力 | **不移植**；机制完整记录，供审计与误判排查 |
| 卡池年龄验证 | `/api/checkout/{init,credential,finalize}` + PTT JWE（ECDH-ES/A256GCM）+ `ADD_CARD`/`CREATE_CONTAINER` ticket | 可扣 $1 的卡源、卡数据加密存储 | 我们无卡、无支付 | **不移植**；PTT 封装格式记录在 §2.4，属于协议知识 |
| 暖 Noise 池 | 每账号一条常驻 WS + **单订阅多任务 fan-out**（按 `device_id`）+ 引用计数 + 空闲回收 | 无外部物料 | 我们每派单重新握手（约 3.3 s/次） | **值得移植**，但要改订阅模型；设计与步骤见 §3 |
| TLS 指纹伪装 | `curl_cffi` 的 libcurl-impersonate Chrome ClientHello + HAR 抄来的 Edge 156 头 | 可选依赖 | 我们 httpx + 手拼头，真机已通 | **按需移植**：默认关，只在边缘 403 时启用；见 §4 |
| 路由超时/上限 | 402 条路由里 24 条带 `requestTimeoutMs`、18 条带 `maxResponseBytes`，另有 idle timeout / 连接类 | 无 | 我们所有请求 120 s 默认、无响应体上限 | **可直接抄数据**；见 §5 |

---

## 1. 协议注册：完整链路

### 1.1 全景时序（OTP 注册路径）

来源 `app/signup/machine.py:806-1012`、`app/signup/protocol.py:1035-1620`。

```text
pending/failed
  ├─ POST /api/auth/native/restart        {resume:false}          → csrf_token, password_encryption
  ├─ POST /api/auth/native/send-otp       {contact_point, region} → flow, password_login_available
  ├─ (mail) 取 6–8 位验证码
  ├─ POST /api/auth/native/confirm-otp    {otp_code, remember_device} → tos.content_version
  ├─ POST /api/auth/native/register-completion
  │     {date_of_birth, tos_content_version, remember_device,
  │      first_name, last_name, display_name}     ← 缺名字会 4xx「Check the account details」
  ├─ POST /api/hatch/lease-vm             {vmType:"standard"|"confidential"}
  ├─ POST https://auth.meta.com/hatch/session/bootstrap/  form frl_access_token
  │     302 链 → OIDC → muse.ai，最后落 hatch_sess cookie
  ├─ POST /api/checkout/init              {amount,currency,sessionId,productId,receiverId,productData:[]}
  ├─ POST /api/ptt/encryption-key         {deviceId,paymentType:"ECP",uplSessionId,productId,otcSessionId,otcType:"card"}
  ├─ POST /api/checkout/credential        PTT JWE + 卡 first6/last4/expiry/zip/country
  ├─ POST /api/checkout/finalize          PTT ticket（CREATE_CONTAINER）+ credentialId
  ├─ POST /api/hatch/accept-tos           {saw_meta_ai_tos:false, client_request_timestamp, entry_point:"web_onboarding_flow", nux_type:"ROW", disclosure_version:"HATCH_ROW_v0", disclosure_text:"..."}
  ├─ POST /api/hatch/stateful-sharing     {}
  ├─ POST /api/hatch/vm/wake              {vm_id:uuid4, retry_count:0, connect_attempt_id:uuid4}
  ├─ recovery-start → 邮件码 → recovery-complete（随机强密码，写加密 secret）
  ├─ POST /api/auth/check                 {} → {ok, outcome}
  └─ 入池（cookie header 存库）
```

关键判据（不是"200 就算成功"）：

* `restart` 必须拿回 `csrf_token`，否则后续全 403；`machine.py:905-910` 专门处理
  `confirm-otp` 的 `csrfFailed` → 重新 `restart → send-otp` 自愈。
* `confirm-otp` 返回的 `tos.content_version` 必须原样回填到 `register-completion`，
  否则 ToS 版本不一致；`machine.py:912-918`。
* `checkout/init` 返回 403/404 **不是失败**：说明该账号 `ccvStatus: not_required`，
  走 `accept-tos` 即可（`machine.py:531-551`、`610-621`）。这是"是否需要年龄验证"
  的唯一可靠探测点，不能靠猜。
* `lease-vm` 在未验卡/未 accept-tos 时会 403；`machine.py:738` 注释明确。

### 1.2 「协议注册」到底依赖什么

`app/signup/machine.py:818`：`_bind_session_proxy(item, sess)` 绑定**每任务一条
sticky 住宅代理**（`{sid}` 占位符展开，`protocol.py:221-230`），整个流程出口 IP
不变；代理失效才换 SID（`protocol.py:982-1011`，最多 6 次）。这不是"多开"，
是让注册请求的出口看起来像同一个家庭用户。

邮箱物料：`app/mail/client.py:101-107` 的 5 组 token 端点/scope（Microsoft
consumers + graph + IMAP），配 `app/mail/client.py:187-283` 的 IMAP 与
`:328-367` 的 Graph 读信；取码正则 `:46-47`（6–8 位数字 + `code|otp|verif|验证码…`
上下文词）；等待预算 `signup_mail_wait_sec=180`、轮询 `5.0 s`（`config.py:184-194`）。

身份：`app/signup/identity.py:48-62` **随机合规生日**（21–55 岁窗口，`365*age+30`
/`+200` 天缓冲，避免边界恰好未成年）；名字从固定 44+42 名单里取
（`:15-31`）；密码 16 位强制四类字符（`:65-82`）。

### 1.3 反关联/指纹（注册路径）

* `app/signup/fingerprint.py:185-219` 的 `edge156-win` 人设：UA
  `Chrome/156.0.0.0 … Edg/156.0.0.0`、`sec-ch-ua` 三组件、`full-version-list`
  （Chromium 156.0.8078.80 / Edge 156.0.4314.58）、`platform-version "19.0.0"`、
  视口 1596×972、`caa-reg-entry-point: aymh`。
* **注册只允许这一个池**：`_registration_pool()`（`:288-293`）只返回
  `registration_default=True` 的项；`pick_fingerprint()`（`:315-327`）把任何
  未知/linux/mac 的 pin 一律重映射回 edge156-win；`_jitter()`（`:330-356`）最后
  还有硬保证：UA 不含 `Edg/` 或 `Windows NT` 就整条换回 edge156。
* 头部组合 `request_headers()`（`:113-151`）：`sec-ch-ua*`、`sec-ch-prefers-color-scheme`、
  `sec-fetch-{site,mode,dest}`、`origin/referer`、`x-hatch-csrf-token`、
  `x-hatch-waterfall-id`、`x-hatch-caa-reg-entry-point`。
* 两个 cookie 在 `_request()` 里被塞进会话（`protocol.py:964-968`）：
  `wd=<viewport>`、`dpr=<1|1.25|1.5>`——真实前端就靠这两个值上报视口，
  缺了等于自报"不是浏览器"。
* `waterfall_id` 每次 `restart` 重新生成 uuid4（`protocol.py:1040`，
  `fingerprint.py:359-362`）。
* `bootstrap`（`protocol.py:1178-1265`）是唯一允许走 httpx 而不是 curl_cffi 的
  一步：它需要 **跟随 302 + cookie jar**，且头必须改成
  `sec-fetch-site: cross-site` / `mode: navigate` / `dest: document`；
  否则 Meta 边缘回 "Sorry, this content isn't available right now"
  （`:1193-1206` 注释、`:1246` 检测）。

### 1.4 地理门禁（为什么必须先探测）

`onboarding_state()`（`:1267-1343`）读 `/access` HTML 里的 RSC 标记：
`isGeoGated`、`hasHatchAccess`、`waitlistStatus`、`ccvStatus`、
`hasRedeemedInviteCode`；HTML 是客户端壳时再 `POST /api/hatch/stateful-sharing {}`
探测：403 → 未放行，<400 → 已放行。**没放行的号所有 `/api/hatch/*`、
`/api/checkout/*` 都 403**，继续跑只会烧卡。这一条对我们有独立价值：如果未来
要判断"这个 cookie 是不是被地理封了"，这就是现成的探测法。

### 1.5 若要在本仓复刻（组件与接口，不写实现）

```text
signup_material:  email_addr, ms_client_id, ms_refresh_token     （加密存）
signup_proxy:     一条 sticky sid 串（{sid} 占位）
signup_identity:  first/last/dob 随机，生成后固定
signup_item:      status 状态机（pending→awaiting_otp→…→activated|pooled|failed）
signup_client:    restart/send_otp/confirm_otp/register_completion/lease_vm/
                  bootstrap/checkout_*/accept_tos/stateful_sharing/wake/
                  recovery_* / auth_check
```

成本估计（按对方代码规模，不含调试）：协议客户端 ~1.5k 行、状态机 ~600 行、
身份/指纹 ~250 行、邮件取码 ~400 行、卡池 ~600 行，另有 PTT ~400 行。
**物料成本才是主项**：可用住宅代理 + 邮箱 token 池 + 可扣款卡源，三者都不是
本项目已有资产；没有它们，代码写出来也只是空转。

---

## 2. 卡池 + 年龄验证

### 2.1 卡池模型（`app/cards/store.py`）

状态机：`available | in_use | used | failed | dead | disabled`（`:26`）。

* 取卡是**单条 SQL 条件更新**，天然原子（`:384-404`）：

```sql
SELECT * FROM cards WHERE status='available' ORDER BY fail_count ASC, created_at ASC LIMIT 200;
UPDATE cards SET status='in_use', attempts=attempts+1, used_by_item=?
 WHERE id=? AND status='available';   -- rowcount==1 才算抢到
```

* 过期卡在取卡时顺手标 `dead`（`:393-397`）。
* 失败可复用：`record_failure()`（`:487-510`）`fail_count+1`，**`>3` 才 disable**
  （`MAX_CARD_FAILURES=3`，第 4 次失败才禁）；`hard=True` 直接 `dead`。
* 成功 `mark_used()`（`:475-481`），异常 `release_card(status="failed")`（`:491`）。
* 非硬失败（网络/代理类）**放回 available 且不计失败**（`machine.py:466-484` 的
  `transient` 分支）——这点值得抄：把"这次没打成"和"这张卡坏了"分开记账。
* 卡数据加密：Fernet，密钥 = `sha256(signup_credentials_key or session_secret)`
  （`app/signup/crypto.py:14-19`）；卡号另有 `_pan_hash()` 做去重索引（`store.py:90-91`）。
* 导入格式（`:121-199`）：`|`、tab、`----`、逗号四种分隔；短格式
  `卡号|有效期|CVV|持卡人|ZIP|国家`，长格式多地址/电话/邮箱/IP；`MM/YY` 或 `MMYY`。

### 2.2 checkout 三步（年龄验证 = $1 授权，非真实扣款）

`protocol.py:1394-1522` 的请求体逐字段：

```text
POST /api/checkout/init
  amount "1.00", currency "USD",
  sessionId "upl_<ms>_<uuid4>", productId "766358168544755",
  receiverId "352501280694489", productData []

POST /api/ptt/encryption-key
  deviceId, paymentType "ECP", uplSessionId, productId,
  otcSessionId uuid4, otcType "card"
  → trustChain（EC P-256 leaf）+ id + platformTrustToken

POST /api/checkout/credential
  operation "CREATE", uplSessionId, orderId, amount, currencyCode,
  paymentContainerMode "LIVE", platformTrustToken, cardFirst6, cardLast4,
  expiryMonth "MM", expiryYear "YYYY", zip（US 只留 5 位数字）, countryCode,
  otcSessionId, otcType
  → credentialId；mutation.success=false → cardDeclined（带 title/description/code/failureStep）

POST /api/checkout/finalize
  selectedCredentialId, credentialType "CREDIT_CARD",
  payButtonClickId uuid4, paymentRequestId uuid4, platformTrustToken,
  orderId, amount, currencyCode, countryCode "US", paymentContainerMode "LIVE",
  otcSessionId, otcType
  → checkoutStatus；非 "SUCCESS" 一律 MuseProtocolError(402)
```

`checkout_max_card_tries=3`（`config.py:154-158`）——一次账号流程最多换 3 张卡。

### 2.3 PTT（platformTrustToken）封装格式

`app/cards/ptt.py:272-405` 是从真实 credential/finalize 抓包里恢复出来的：

```text
credential token = base64(json({"payload": "<ticket_b64>.<jwe>", "signatures": []}))

ticket = {"data": {"app_id": "2805739562851096", "auth_ticket_type": "PAN",
                   "cardholder_name": "", "credit_card": "$e2ee", "csc": "$e2ee",
                   "device_id": "DEVICE_ID",
                   "expiry_month": "MM", "expiry_year": "YYYY"},
          "nonce": uuid4, "op": "ADD_CARD", "ver": 1}

jwe = ECDH-ES + A256GCM，protected header:
      alg ECDH-ES, apu "", apv = b64u("fp:<b64u sha256(spki)>"), epk P-256 PEM
      AAD = protected || "." || ticket_b64
      CEK = ConcatKDF(SHA-256, Z, "A256GCM", "", raw "fp:..." 字符串)
      plaintext = {"csc": "...", "credit_card": "..."}

finalize token = base64(json({... "op": "CREATE_CONTAINER",
      "data": {"receiver_id": "352501280694489", "context_id": orderId,
               "container_mode": "LIVE",
               "components": [{"amount", "cred_id", "currency"}],
               "security_origin": "https://muse.ai"}, "nonce", "ver": 1}))
```

要点：PAN/CVV 只出现在 JWE 明文里，ticket 里是 `$e2ee` 占位；KDF 的 party V info
是 **原始 `fp:...` 文本**，不是它的 base64（`:293-305` 注释点明这是网页端真实喂法）。
这一段对我们唯一的价值是"认清边界"：**没有卡就没有这条链路**，而 PTT 的难点不在
加密（400 行可写完），在拿到 `trustChain` 与真实卡源。

### 2.4 若要复刻的组件

`CardStore`（SQLite 单表 + 原子 claim + fail_count 策略）、`PttClient`
（`/api/ptt/encryption-key` + JWE 封装）、`CheckoutFlow`（init→credential→finalize）、
`CardImporter`（四种分隔 + 过期归一 + Fernet 加密）。缺口同 §1.5：卡源是外部物料。

---

## 3. 暖 Noise 连接池（最值得抄的一项）

### 3.1 设计（`app/muse_engine/protocol_pool.py`，271 行）

* **每账号一个专属事件循环线程**（`:46-67`）：`asyncio.new_event_loop()` +
  daemon thread；跨线程调用走 `asyncio.run_coroutine_threadsafe`（`:69-73`）。
  理由：Noise WS 的 nonce 顺序不能跨线程交错，单 loop 天然串行。
* `_ensure_client()`（`:80-118`）复用条件四项全真：`client.ws` 存活、
  `not client.closing`、`cookie_fp` 相同、`proxy_key` 相同、`subscription_id` 非空。
  `cookie_fp` = 排序后 `(key, value[:12])` 的 hash（`:31-33`）——cookie 一换就重连，
  防止把旧会话借给新 cookie。
* **共享一条 `chat.subscribe`**（`:110`）：整账号只订阅一次，`_dispatch_loop()`
  （`:120-182`）是唯一读者：按 `\n` 切 NDJSON、从 `_scan_offset` 增量读
  `client.responses[sub_id].body`，逐行 `json.loads`。
* **fan-out 规则**（`:145-168`）：取 `payload.chat_context.device_id`；
  命中某 listener 的 node_id 就只投给它，否则广播给所有 listener，由各自过滤；
  `seq` 进 listener 自己的 `seen` 集合去重；队列 `maxsize=2000`，满了丢新事件
  而不是阻塞读循环。
* 引用计数 `_refs` + `_last_used`（`:184-191`）；`has_warm_worker()`（`:246-261`）
  给调度层判断"这个号是否已有热连接"；`drop_worker()`（`:238-243`）在账号删除/
  cookie 失效时整机回收。
* `client.reset_timings(); client._mark("reuse_hit")`（`:93-94`）——复用命中会重置
  计时并打点，便于统计"省了多少握手"。

### 3.2 为什么不能直接搬

我们的模型是**每回合自己订阅**：

* `read_stream()`（`protocol_client.py:1617`）在每次派单时 `subscribe()` 拿新的
  `stream_id`，然后把该 `stream_id` 的响应体从 offset 0 读到结束；
* 因此复用连接会引入两件事：旧订阅仍在推送（事件污染）与多个任务共用
  `_sub_stream_id`（互相踩游标）。
* 我们还有 resume 语义（`after_stream_seq` / `after_chat_event_seq`）绑定在
  "一次订阅"上；换成共享订阅后，游标要改成**每 listener 一份**。

### 3.3 移植方案（bridge 版，分四步，可回滚）

```text
Step 1  ProtocolSessionHub（新文件 muse_bridge/protocol_hub.py）
        - 单 asyncio 任务内运行（我们本来就是单 loop，不需要额外线程）
        - dict[account_id] -> SessionSlot{client, sub_id, offset, listeners, refs}
        - acquire(cookie_fp, proxy_key) → 复用或 connect+subscribe
        - release() 只减引用；空闲 > IDLE_SECONDS(300) 由后台任务 close
Step 2  单读者分发
        - 复用 protocol_client._read_line 的增量扫描逻辑（抽成公共函数）
        - 按 payload.chat_context.device_id 定向；无 device 的事件广播
        - 每 listener 独立 seq 去重 + 独立 queue(maxsize=2000)
Step 3  ProtocolDriver 改造
        - stream_reply_events(): acquire → register_listener(node_id) →
          消费 listener.queue；不再自己 subscribe
        - 事件结束判定沿用现有 presentation/done 规则
        - 保留旧路径：env MUSE_WARM_POOL=0 时走现在的每回合订阅（一键回滚）
Step 4  验收
        - 离线：假 WS 两条任务并发，断言事件不串、seq 不丢、关闭后无泄漏
        - 真机：同一账号连续 3 次文本请求，记录 connect 次数（应 1 次）与
          每次首字节延迟（应下降约握手时间 ≈3.3 s）
        - 断线：强制 close WS → 现有 resume 逻辑接管 → 断言 after_stream_seq 仍生效
```

风险与止损：共享订阅一旦串流，表现为"回答串台/重复增量"。止损手段是
**feature flag + 每任务 node_id 强过滤 + seq 去重**三件套；上线前先在
`MUSE_WARM_POOL=1` 下跑 20 次单账号回归，任何一次串台立即关 flag。

收益：真机握手约 3.3 s/次（§1.6 记录），暖池命中后每次省这段；另外
`/api/session` 续签请求也从每派单一次降为每连接一次。

---

## 4. TLS 指纹伪装

### 4.1 对方实现

`app/signup/transport.py`：

* 可选依赖 `curl_cffi.requests.AsyncSession`（`:24-30`），`_backend_curl()`
  （`:89-95`）在未安装时静默退回 httpx。
* `send()`（`:98-163`）：默认 `impersonate_target()` 读
  `signup_tls_impersonate`，默认值 **`"chrome"`**（`config.py:170-175`）；
  用 `AsyncSession(impersonate=target)` 发请求（`:180-203`），
  拿 `r.headers.multi_items()` 保留重复头。
* 失败策略（`:129-150`）：代理超时直接抛给上层换 SID；**非代理类失败退回 httpx**，
  不因为 TLS 库问题把整条注册打死。
* 文档说明（`:1-11`）：httpx/OpenSSL 的 JA3/JA4 + HTTP/2 SETTINGS 与真实浏览器不同，
  Meta 边缘会因此拒绝；curl_cffi 伪造 ClientHello 后仍需 HAR 抄来的头。

`app/signup/protocol.py:1178-1265` 的 `bootstrap` 反而**故意走 httpx**：
它需要 cookie jar + 自动跟随 302，curl_cffi 那层没接 jar 语义。

### 4.2 我们的现状与判断

* 外层 HTTP（`/api/session`、`/api/hatch/*`）走 httpx，头部见
  `muse_bridge/session_http.py:142-154`（UA 常量在 `:71-74`，是
  **Linux Chrome 135**，不是 Windows Edge）；已带 `Origin/Referer/Sec-Fetch-*`。
* Noise WS 走 `websockets` 库自己的 TLS（`protocol_client.py:1060-1097`），
  与 curl_cffi 无关。
* 真机事实（§1.6）：这种组合下 `/api/session` 200、WS 101 + Noise XX 握手成功、
  文本/图/视频全部跑通。**说明 muse 的 hatch 边缘当前不卡 JA3/JA4**，卡的是
  头部与 cookie。
* 何时才需要：出现"HTTP 403/挑战页但 cookie 有效"或"外层 `/api/session` 被
  Meta 边缘拒"时。那时再加，不预先背上 30 MB 的 libcurl-impersonate。

### 4.3 若要加的移植方案

```text
muse_bridge/tls_transport.py
  - try import curl_cffi; HAVE_CURL_CFFI
  - async def post_json(url, headers, json, proxy, impersonate) -> (status, headers, body)
  - 默认 impersonate = os.environ.get("MUSE_TLS_IMPERSONATE", "")  # 空 = 用 httpx
  - 失败回退：仅当异常不是代理类时退回 httpx，并记 WARNING
session_http.renew_session / wake_vm 改为调用该函数
测试：unittest.mock 掉 HAVE_CURL_CFFI 为 False/True 两条路径；
      断言 headers 原样透传、Set-Cookie 重复头不丢
```

若同时要抄指纹人设，至少要抄三样：`sec-ch-ua*` 全套、`wd`/`dpr` cookie、
`x-hatch-waterfall-id`；只换 ClientHello 不换头，等于穿西装穿拖鞋。

---

## 5. 按路由的超时/上限元数据

### 5.1 数据形状（`work/muse2api_harvest/muse-cli/src/muse_cli/routes.json`）

402 条路由，字段出现次数：

| 字段 | 条数 | 含义 |
|---|---|---|
| `noiseOnly` | 144 | 只能走 Noise 内层；其余可走外层 HTTP |
| `requestTimeoutMs` | 24 | 该路由的请求预算，不是全局默认 |
| `maxResponseBytes` | 18 | 响应体上限，二进制路由为主 |
| `responseType: binary` | 22 | 媒体/导出类 |
| `channel: media` | 18 | 走媒体通道 |
| `streamMode: subscription` | 14 | 长连接订阅（`chat.subscribe` 等） |
| `responseIdleTimeoutMs` | 2 | 流空闲超时（`dyi.export` 3.9e6、`voice.speak` 30e3） |
| `connectionClass` | 2 | `shared_agent`（`shared_agents.remote.*`） |

### 5.2 实际分布

```text
requestTimeoutMs: 8000, 15000, 20000, 30000, 35000, 90000, 120000(×5),
                  150000, 180000, 360000, 600000(×5), 900000, 3900000
maxResponseBytes: 2048(×2), 16384(×2), 32768, 131072, 196608, 370688,
                  614400, 2097152(×2), 4194304, 7864320, 8388608(×4), 37748736
```

最长的两条：

| 路由 | timeout | 备注 |
|---|---|---|
| `POST /api/dyi/export` | 3,900,000 ms（65 min） | `channel=media`, `responseType=binary` |
| `POST /api/debug/bug-report-logs` | 900,000 ms（15 min） | `noiseOnly` |
| `POST /spaces/share/{slug}` 等 5 条 sharing | 600,000 ms（10 min） | 可走外层 |
| `POST /fs/write`（`peers.discretion.write`） | 360,000 ms（6 min） | `noiseOnly` |
| `POST /chat/subscribe` | 30,000 ms | `noiseOnly` |

最大的响应体是 `GET /api/email/messages/{id}/attachments/{i}` 的 **36 MiB**
（37,748,736 B）；`peerSpaces.*` 一律 8 MiB；`fs.thumbnail` 只允许 600 KiB。

### 5.3 我们现在的状态

* `request(self, verb, path, body, timeout)`（`protocol_client.py:1324-1343`）
  默认 **120 s**，调用点各自传 30/45/60（例：`_wait_headers(…,30.0)`、
  `renew_session` 内部 30 s）。
* 没有任何响应体上限：`_wait_end()` 会把 body 全收进内存（`:1141-1152`）。
  媒体走 `fs.read` 时如果上游回一个超大文件，我们没有硬闸。
* `read_stream` 有总超时（`timeout` 参数），但**没有 idle 超时**——一条僵死的
  订阅会一直等到总 deadline。

### 5.4 可落地的映射表（建议直接写进 `protocol_routes.py`）

只抄我们实际会用的路由，其余落默认域：

```python
ROUTE_LIMITS = {
    ("POST", "/chat/subscribe"):            {"timeout": 30.0},
    ("POST", "/chat/stream"):               {"timeout": 120.0},
    ("POST", "/api/nodes/register"):        {"timeout": 30.0},
    ("POST", "/api/checkout/init"):         {"timeout": 90.0},
    ("POST", "/api/ptt/encryption-key"):    {"timeout": 90.0},
    ("POST", "/api/checkout/credential"):   {"timeout": 120.0},
    ("POST", "/api/checkout/finalize"):     {"timeout": 120.0},
    ("GET",  "/fs/read"):                   {"timeout": 120.0},
    ("GET",  "/fs/thumbnail"):              {"timeout": 30.0, "max_bytes": 614_400},
    ("GET",  "/api/dyi/export"):            {"timeout": 3_900.0, "idle": 3_900.0},
}
DEFAULT_LIMITS = {"timeout": 120.0, "max_bytes": 8 * 1024 * 1024}
```

实现要点：

1. `request()` 先查表，调用点显式传 `timeout` 时以调用点为准（避免误伤）。
2. `_wait_end()` 累加 `len(body)`，超过 `max_bytes` 就抛
   `ProtocolError("response_too_large")` 并释放 response；不要读完再判断。
3. `read_stream()` 加 `idle_timeout`：`_wait_update` 的等待窗口 = min(0.5, idle)，
   连续 N 次无新行就判 idle 超时（N = ceil(idle/0.5)）。
4. 测试：假响应发 9 MiB → 断言抛 `response_too_large` 且未进内存；假订阅 3 s
   无事件 → 断言 idle 超时；正常路由仍按原 timeout。

优先级：`max_bytes` 与 `idle_timeout` 是**防炸**项，先做；逐路由 timeout 是
**体验**项，后做。

---

## 6. 采纳矩阵

| 项 | 决定 | 工作量 | 收益 | 依赖 |
|---|---|---|---|---|
| 路由 `max_bytes` + idle timeout | **已落地**（13:3x） | 0.5–1 天 | 防止超大响应/僵死订阅拖死派单 | 无 |
| 路由 timeout 表 | **已落地**（只抄真会调的 5 条 + 默认域） | 0.5 天 | 导出/长任务不再被 120 s 掐断 | 无 |
| 暖 Noise 池 | **已落地 + 生命周期加固**（flag 保护，14:0x / 15:0x） | 2–3 天 | 每派单省一次握手（真机 4.77 s → 0.000 s）；并发/归属/清理缺陷已修 | 无 |
| TLS 指纹 | **按需**（出现 403 再开） | 1 天 + 依赖 | 绕过边缘 JA3 判定 | curl_cffi 二进制 |
| 协议注册 | **不做** | 3–5 天 + 物料 | 无（需要邮箱/代理/卡三套外部资产） | 物料 |
| 卡池/年龄验证 | **不做** | 2–3 天 + 卡源 | 无 | 可扣款卡 |

## 7. 落地记录（2026-11-11 13:3x–13:5x）

前两项已实现，实现细节与真机证据写在 `docs/PROTOCOL.md` §8.11；这里只记"文档承诺
→ 代码事实"的对应关系，方便复核有没有走样：

| 文档里的设计 | 代码落点 |
|---|---|
| 逐路由预算表 | `muse_bridge/route_limits.py`：`ROUTE_LIMITS` + `PREFIX_RULES` + `limits_for()` |
| `request()` 查表 | `protocol_client.py` `request()`：`budget = limits.timeout if timeout is None else timeout` |
| 上限在追加时执法 | `protocol_client.py` `_append_body()`（`_receiver` 两处调用） |
| 超限的错误语义 | `ProtocolResponse.limit_exceeded` + `request()` 抛 `ProtocolError` |
| idle 判定只认新字节 | `read_stream()` 主循环的 `last_size` / `last_progress` |
| idle 开关 | `env.PROTOCOL_STREAM_IDLE_TIMEOUT`（`MUSE_STREAM_IDLE_TIMEOUT`，默认 0） |
| 驱动侧安全接线 | `protocol_engine.py` `accepts_kwarg()` / `stream_idle_timeout()` / `_stream_extras()` |
| 暖池复用/验活/引用会计 | `protocol_hub.py`：`acquire()` / `usable()` / `release()` / `evict_idle()` / `close_all()` |
| 驱动接入暖池 | `protocol_driver.py` `connect()` / `close()`（开关关时行为一字不差） |
| 进程退出兜底 | `app.py` `lifespan` 里的 `await protocol_hub.close_all()` |

与本文原设计的偏离（四处，都是实现时发现的真问题）：

1. **`_OwnClient` 从来没有代理**。本文只讨论了注册/协议链路的 TLS 与出口，没注意
   到 `/api/session` 这条运维链路是直连。13:4x 真机上它直接 `ConnectError` 把
   refresh 打成 502，`accounts_dispatchable` 掉到 0。已补 `MUSE_SESSION_PROXY`
   （回落到 `MUSE_PROTOCOL_PROXY`），修复后 refresh 200 / 2.07 s。
2. **`env_positive_float` 不接受 0**。idle 关闭态必须能表达成 0，用
   `env_positive_float` 会在 import 时抛 `EnvError`（默认值就是 "0"）。改用
   `env_float`，并在注释里写清"0 是合法值"。
3. **暖池不需要本文 §3.3 的 fan-out**。§3.3 抄的是参考实现的"一条订阅按
   `device_id` 分发给多 listener + 每 listener 一份 seq"。那套东西的前提是**同号
   并发**，而本仓 `pool.dispatch()` 有账号级锁，同号串行——照抄会引入一批本仓
   根本不需要的游标状态。实际落地只需要"缓存 + 验活 + 引用计数"。
4. **`connect()` 之后没有订阅**。§3.3 的验活假设"订阅存在才算活"，真机相反：
   订阅是 `read_stream(prompt=...)` 才建的。按原设计写会让暖池 100% 不命中，
   且每回合多烧一次重连（探针：cold 4.40 s `usable=False` → warm 2.78 s 全在重连）。

## 8. 落地记录续（2026-11-11 15:0x–16:1x，路线图 P0–P2）

暖池第一版落地后，按 `docs/2026-11-11-roadmap.md` 做了一轮可靠性与可观测性加固。
这里同样只记"文档承诺 → 代码事实"，以及**与本文原设计的进一步偏离**。

| 设计点 | 代码落点 |
|---|---|
| 暖池失败/取消清理 | `protocol_hub.py` `acquire()` 的 `except BaseException` + `_discard_locked()`（身份比较） |
| 身份指纹覆盖完整 cookie | `protocol_hub.py` `cookie_fingerprint()`（完整 name+value → sha256） |
| 租用归属 | `WarmSlot.lease_id` + `_next_lease()` + `release(account, lease=None)` + `lease_of()` |
| 并发 acquire 明确拒绝 | `WarmPoolBusy`（`protocol_hub.py`） |
| 媒体归属过滤 | `protocol_engine.py` `_run()` 媒体分支的 `and ours` |
| 进程内指标 | `muse_bridge/observability.py`（`bump` / `timed` / `snapshot`） |
| 管理面读指标 | `app.py` `GET /admin/observability`（`require_admin`） |
| 池内状态派生量 | `app.py` `_warm_pool_snapshot()` → `warm_pool.leased_accounts` |
| 媒体清理入口 | `media.py` `sweep_forever()` + `MUSE_MEDIA_SWEEP_INTERVAL`；`app.py` lifespan 起任务 |
| 协议回退留痕 | `pool.py` 回退处的 WARNING 日志 + `protocol_fallback` 计数 |

与本文原设计的新增偏离（三处）：

5. **本文 §3.1 的池没有"租用归属"这个概念**。参考实现按 `device_id` 做 fan-out，
   每个 listener 独立持有一份游标，所以不需要区分"谁的引用"。本仓是账号级串行，
   一个槽位在任一时刻只该有一个持有者，于是引用的**归属**必须显式记账。第一版
   用槽位内自增代次，被测试打回：槽位会被 `drop` 重建，重建后从 1 重来，旧租用
   的归还号会撞上新一代。最终改成**全局单调发号器**，且不随槽位销毁复位。
6. **本文 §3.1 假设同号并发存在**（所以才要 fan-out）。本仓既然账号级串行，那么
   "第二次 acquire 同一账号"就只意味着有人绕过了锁——这是**错误状态**，不是需要
   兼容的用法。因此选择抛 `WarmPoolBusy` 而不是排队等待：等待会把一次明显的锁
   泄漏变成一次静默超时，更难查。这条偏离同时改掉了旧测试里"连续两次 acquire
   验复用"的写法（必须先 release）。
7. **本文没有讨论媒体归属**。媒体事件走的是 `delta.presentation` /
   `tool_done` / `message_done` / `placeholder`，而正文走的 `delta.text_append`
   一直有 `chat_context.device_id` 过滤。两者不对称是**真缺口**：同一订阅上别的
   会话的 presentation 会被当成这回合的成品取回。本文 §3 只谈连接复用，没覆盖
   事件归属这一层。

一处**已结清**的环境态（16:4x 复核，结论与原记录相反）：收尾时真机 chat 复验失败，
当时判为"上游重置"。**该判断是错的**，根因是排障探针自身。直连探针
（绕开 bridge、只用未修改的 `protocol_client.py`）在**三种链路**上失败方式一致——
mihomo `127.0.0.1:18000`、FlClash `127.0.0.1:7890`、无代理直连，全部
`Noise WebSocket 连接失败（ConnectionResetError）`。看似"上游全面故障"，实际是：
探针把 `proxy` 传成 `{"http":…, "https":…}`（requests 风格），而
`HatchProtocolClient._proxy_url()` 只读 `proxy["server"]`，取不到就 `return None`，
**HTTP 与 WS 双双静默直连**。三条链路因此跑的是同一条被墙直连，"一致失败"是必然的
——一致性本身就是探针 bug 的指纹，不是上游的证据。产品代码三处构造
（`protocol_driver.py:115`、`protocol_hub.py:242`、`muse_spaces.py:607`）统一
`{"server": proxy}`，**产品自始至终未受影响**。按契约修正探针后：
`/api/hatch/token` 返回 200 并取到 token/notary，`connect()` 两次成功
（5.80 s / 4.75 s），活网 chat `200 / 14.33 s` 与 `200 / 3.11 s`（暖池命中）。
`notary_token` 的 per-VM `hostname` 疑点也已排除：LB 入口 101 + Noise msg2（166 B）
+ msg3 + split 全通，per-VM 子域反而 TLS 失败，故 `use_endpoint_host=False`
保持默认。**流程约束**：探针必须与产品同契约；报"多链路一致失败"前，先在 opener
里回读 kwargs 证明代理真的用上了。


