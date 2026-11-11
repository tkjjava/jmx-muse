# Noise / hatch WS 建连与握手 —— 前端静态产物证据

> 只读复核。全部证据来自 `https://muse.ai/_next/static/chunks/*.js`（HTTP GET，未跑浏览器、
> 未开 CDP、未发聊天、未启动 bridge、未改 `muse_bridge/`）。chunk 是 Turbopack 单行压缩
> JS，**行号无意义，一律用字符偏移**（`文件@byteOffset`）。
>
> 本地落位：`D:\deep seek\work\muse-bundles\`（220 个 chunk，31.8 MB）；provenance 表
> `D:\deep seek\work\chunk_map.tsv`（文件名 / 字节数 / sha256 前 16 位 / 原始 URL 模板）。
> 采集脚本：`fetch_chunks.py`、`crawl_chunks.py`、`ctx.py`、`census.py`、`proto_dump.py`、
> `rpc_registry.py`、`paths.py`（均在 `D:\deep seek\work\`）。

## 0. 采集口径（为什么能拿到 app 区代码）

| 证据（文件@偏移 / 事实） | 结论 |
| --- | --- |
| `GET https://muse.ai/` 返回的 HTML 里只有 `/`（营销面）的 chunk 引用；`/_next/static/chunks/<name>.js?dpl=dpl_3WKPwME7mcgKdL9XQtmQooWnmkiT` | 公共面与 app 面是**两个 Vercel deployment**，chunk 集合不同，同名 chunk 在另一 dpl 下返回 9 字节 `Not Found` |
| 抓 `https://muse.ai/s/`（200，公开分享页）→ 其 HTML 里的 chunk 前缀是 `dpl_AmYcy8nhVKz7uDfYcPy4ww7ZnoNJ`（108 个 chunk 名） | 拿到 **app 区 deployment id**；`/login`、`/app`、`/chat` 全部 401/307，只有 `/s/` 是公开可取的入口 |
| 每个 chunk 尾部都有 `//# sourceMappingURL=<hash>.js.map` + `//# debugId=<uuid>` | sourcemap 元数据在，但 `.js.map` 在 CDN 上恒 404（9 字节 `Not Found`），只上传到了 Sentry → **没有 sourcesContent 可用**，本文结论全部靠压缩 JS 本体 |
| 8 个 chunk 名在两个 dpl 下都 404（`1f6qfjl0vznml.js`、`2mkxuvp3k3ljl.js` 等） | 旧构建残留名，不追 |

## 1. 裁定表：客户端常量 vs 前端证据

| 常量 | `muse_bridge/` 现值 | 前端证据 | 裁定 |
| --- | --- | --- | --- |
| `NOISE_WS_HOST` | `hatch.metaaivm.com` | `0tmgiplsgovnn.js@26706` `let r="hatch.metaaivm.com"`（模块 755854 导出 `q`），无 env 覆盖 | ✅ 正确 |
| `NOISE_WS_PATH` | `/v1/noise` | 同上模块紧邻导出 `"h",0,"/v1/noise"`；`3w55hkryr8wwj.js@19261` 处 `wss://${r.q}${r.h}` | ✅ 正确 |
| `MAX_PLAINTEXT_FRAME` | `65489` | `2knhq5aux36zv.js@11825` 发送侧 `Math.ceil(e.length/65489)`；`@6630` 接收侧 `s.length>65489` 直接抛 `payload too large for noise frame` | ✅ 正确 |
| `MAX_CHUNKS` | `256` | `@11825` `if(n>256)throw`；`@6532` `if(a<1||a>256)throw invalid totalChunks` | ✅ 正确 |
| `PROTOCOL` | `Noise_XX_25519_AESGCM_SHA256` | `2knhq5aux36zv.js@2700` 协议名原文；`@2599` `new Uint8Array(32)` 后 `set(name)` → **27/29 字节名补 0 到 32 字节再 mixHash** | ✅ 正确（客户端 `ljust(32,b"\0")` 已对上） |
| nonce | 4×`00` ‖ 8 字节大端计数器 | `@1252` `setUint32(4, Math.floor(e/0x100000000))`、`setUint32(8, e>>>0)`，`{iv, additionalData, tagLength:128}` | ✅ 正确 |
| 传输期 AD | `b""` | `@9749` `let U=new Uint8Array(0)`；`encryptWithAd(U,...)` / `decryptWithAd(U,...)` | ✅ 正确 |
| `msg1` 载荷 | `0a20`+32B nonce | `@53436` `Writer...uint32(10).bytes(e)`（field1=32B client nonce） | ✅ 正确 |
| `msg2` 最短长度 | `80`（`MSG2_MIN_LEN`） | `@4385` `if(e.length<96) throw "message 2 too short"` | ❌ 应为 **96**（32 re + 48 enc(s) + ≥16 tag） |
| `app_version` query | 未发送 | `3w55hkryr8wwj.js@33603` `app_version=${"1"}` 恒定发送 | ⚠ 缺参（服务端是否强校验未证实） |
| `atod_token` / `request_id` | `request_id` 有、`atod_token` 无 | `@33652` `request_id=`、`@33782` `atod_token`（`use_od` 分支） | ⚠ `atod_token` 只在 debug/OD 路径出现，普通连接不需要 |
| 服务端静态公钥 | 无（不 pin，握手后取 msg2 里的） | 见 §2.5：**前端也没有硬编码公钥**，靠 msg2 送 + attestation 绑 | ✅ 与真实实现同构（标准 VM 即 TOFU） |

## 2. 证据与结论

### 2.1 目标 URL 与 query

| 证据 | 结论 |
| --- | --- |
| `3w55hkryr8wwj.js@33403`–`@33782`：<br>`d=[vm_id=${encodeURIComponent(u)}, auth_token=${encodeURIComponent(t)}]; if(l?.notaryToken) d.push(notary_token=…); d.push(app_id=…); d.push(app_version=…"1"); d.push(request_id=…); if(use_od) d.push(atod_token=…); let h=r.q; return \`wss://${h}${r.h}?${d.join("&")}\`` | 线上 URL 形如<br>`wss://hatch.metaaivm.com/v1/noise?vm_id=<id>&auth_token=<jwt>[&notary_token=<t>]&app_id=hatch-web&app_version=1&request_id=<uuid>[&atod_token=<t>]`<br>**参数顺序**：`vm_id` → `auth_token` → [`notary_token`] → `app_id` → `app_version` → `request_id` → [`atod_token`] |
| `3w55hkryr8wwj.js@24069` `let i=t.default.env.NEXT_PUBLIC_ABRA_APP_ID??"hatch-web"` | `app_id` 就是 `hatch-web`（无覆盖时的默认值） |
| `2knhq5aux36zv.js@23927` 请求头 `key:"x-app-id", value:d.K`（即同一个 `hatch-web`） | 明文网关上走的那套 REST 也带 `x-app-id: hatch-web` |
| `2knhq5aux36zv.js@~25000` shared-agent（one-shot）分支：`n=new URL(y)` 后 `searchParams.set("vm_id"…)`、`"auth_token"`、`"app_id"`、`"request_id"`，**无 app_version/notary_token** | 存在第二条"共享代理"建连路径，参数更少；我们走的是 primary 路径 |
| `3w55hkryr8wwj.js@19261` 起 `s(e)`：envId → `wss://<envId>.metaaivm.com/`；URL → `wss://<host>/`；`<vm_id>.metaaivm.com` 是 per-VM 子域 | 客户端"per-VM 子域 TLS 不通"的实测与前端实现一致（前端也只是**可选**目标，默认仍是常量 LB） |
| `2uyfyf7-qx3vc.js@485` vm id 正则 `^[0-9a-f]{8}-…-[0-9a-f]{12}$|^[0-9a-f]{32}$`；`@452` 允许后缀 `.metaaivm.com`/`.test-only.metaaivm.com`；`10p6wdgvb-c8u.js@78100` 追加 `.willow606.com`/`.exe.xyz` | vm_id 必须是 UUID 或 32-hex；host 必须在三家后缀白名单里（`hatch` 本身不满足 UUID 规则，是域名不是 vm_id，别混用） |

### 2.2 `auth_token` / `notary_token` 从哪来

| 证据 | 结论 |
| --- | --- |
| `3w55hkryr8wwj.js@31469` `POST /api/hatch/token`，body `{vmAddress, vmName?, force_refresh?}`，10s 超时（"Token request timed out after 10000ms"），响应 `{token, notary_token}`，只缓存在内存 Map | WS 用的 `auth_token` 来自 `/api/hatch/token`；`notary_token` 也常随该响应一起下来 |
| `2knhq5aux36zv.js@47133` `POST /api/hatch/noise-notary-token`，body `{vmName}`，8s 超时，响应 `{notaryToken}`，取不到就抛 `notary_unavailable` | 缓存里没有 notary token 时的**兜底端点**；两个端点并存 |
| `3w55hkryr8wwj.js@25463` `endorse("…", "/v1/noise")`：`notary_token` 若以 `delegation.` 前缀开头，必须先被背书成**路径域 = `/v1/noise`** 的令牌才能用；`@25515` 从 JWT `restrictions` 的 `timeout:` 条目读过期（默认 60s） | 机密 VM 的 `delegation.` 令牌是**路径绑定**的，且寿命短（≈60s），必须现取现用 —— 客户端"遇 `delegation.` 直接失败"的选择在协议上是合理的 |
| `2knhq5aux36zv.js@40211/40236` 握手器构造：`timeoutMs??1e4`、`maxMessageBytes??65535`；`@41951` `message_bytes>maxMessageBytes` 抛 `noise handshake message too large` | 握手总超时 10s、单条握手消息上限 65535；客户端应把这两条作为硬约束 |

### 2.3 Noise 配方（XX，AES-GCM，SHA-256）

| 证据 | 结论 |
| --- | --- |
| `2knhq5aux36zv.js@2700` `initialize()`：`let e=new TextEncoder().encode("Noise_XX_25519_AESGCM_SHA256")`，`@2599` 先 `new Uint8Array(32)` 再 `set(e)`，随后 `mixHash(new Uint8Array(0))` | `h₀ = SHA256( name‖00… )`（**名称补零到 32 字节**）；随后混一次空串。全量 220 个 chunk 里**没有 `prologue` 字样** → 无自定义 prologue |
| `@970` `function l(e,t,r)`：`i=HMAC(ck,ikm)`，`a=HMAC(i,[1])`，`l=HMAC(i,a‖[2])`，可选 `[3]` | HKDF 是 Noise 的 Extract + 链式 Expand，不是 RFC5869 的 `info/L` 版本 |
| `@1252` 12 字节 nonce 构造 + `{iv, additionalData, tagLength:128}`；`@~1300` `nonce exhausted`（MAX_SAFE_INTEGER 后拒绝）、失败后整条通道置毒 | AES-GCM-256，tag 128 位；计数器耗尽必须断连，不能回绕 |
| `@3946` `writeMessage1(e)`：临时密钥对 `(0,i.Y)()` → `mixHash(pub)` → `encryptAndHash(payload)` → 返回 `pub‖ct` | msg1 = `e`(32) ‖ enc(payload)（此时 key 为空 → 载荷明文），线上总长 = 32 + 34 = 66 |
| `@4338` `readMessage2(e)`：`e.length<96` 拒绝；`#p=e[0..32]`（远端临时公钥，先 mixHash）；`#g=decryptAndHash(e[32..80])`（32+16 = 服务端静态公钥）；再 `DH(e_priv, #g)` mixKey；`payload = decryptAndHash(e[80..])` | msg2 布局固定：`re(32) ‖ enc(s)(48) ‖ enc(payload)` |
| `@4895` `writeMessage3(e)`：`encryptAndHash(静态公钥)` → `mixKey(DH(st, re))` → `encryptAndHash(payload)` | msg3 顺序不可换（`se` 必须夹在两次 encrypt 之间） |
| `@5328` `split()` → 两个 CipherState；`@9947` 传输类构造函数 `(sendState, recvState)`，`#C=BigInt(1)` 起始 stream id | initiator 取 `split()` 第一把作**发送**；stream_id 从 1 开始递增 |

### 2.4 三条握手消息的 wire 格式（protobuf，非自造）

`2knhq5aux36zv.js` 内嵌了三段 base64 `FileDescriptorProto`，用 `proto_dump.py` 解出来：

| 证据（`@偏移` + 解码结果） | 结论 |
| --- | --- |
| `@53436` `Writer.create().uint32(10).bytes(e)`；`@53420` `s.r()` = `crypto.getRandomValues(new Uint8Array(32))` | **msg1 payload** = protobuf `field 1 (bytes) = 32 字节 client nonce`，即 `0a 20 ‖ nonce`；`@41487` 另有 `uint32(16).int32(1)`（field 2 = 1）的变体，只在 shared-agent/ticket 模式用 |
| `@51700` 区段解出 msg2 payload 解码器：`case 1: attestation(bytes)`、`case 2: int32 状态（1=unprovisioned / 2=provisioned）`、`case 3: vmNonce(bytes, 必须 32)`、`case 4: vmProof(bytes, 32, 仅 provisioned）`、`case 5: chipBinding{1:claimId string, 2:ownerNonce string}` | msg2 payload 是一张"挑战/attestation"包；标准 VM 根本不发这个结构（解码返回 `null` → `observed_type="standard"`） |
| `@50250` 起 label 常量：`hatch.noise-auth.key.v1`、`vm-proof-key`、`client-proof-key`、`hatch.noise-auth.vm-proof.v1`、`hatch.noise-auth.client-proof.v1`；`m(e,r)` = `Writer.uint32(10).string(notaryToken)` + `uint32(18|26).bytes(...)` | **msg3 payload** = protobuf `field1 = notary_token(string)`，`field2 = fresh rvKey`（unprovisioned）或 `field3 = proof`（provisioned） |
| 同区 `p()/g()`：`proof_vm = HMAC(HMAC(rvKey, "hatch.noise-auth.key.v1"‖"vm-proof-key"), "hatch.noise-auth.vm-proof.v1"‖handshakeHash‖clientNonce‖vmNonce)`；`proof_client` 用 `client-proof-key` 同构；用**常量时间比较**校验 `vmProof`，不匹配抛 `cvm_key_rejected` | msg3 的证明是"双 HMAC 链"，绑定 handshakeHash + 双方 nonce；标准 VM 直接发空 payload（`@44547` `let X=new Uint8Array(0)`，只有存在 challenge 才填） |
| `@6639`/`@11825` 用 `(0,f.f)(_, …)` 编 `ingress_rev_proxy.NoiseTransportFrame` | 传输帧是 protobuf，不是自定义二进制 |

### 2.5 服务端静态公钥到底从哪来（最关键的一条）

| 证据 | 结论 |
| --- | --- |
| 全量 220 个 chunk 里没有"预置服务器公钥"常量：`grep` 不到 32 字节 hex/base64 常量被当作 `serverKey` 使用；`server_key_len` 只作为**遥测字段**出现（`0ebw_w1k2dpcu.js`） | 前端**没有硬编码** pin 的公钥，`/api/session` 也不下发公钥 |
| `2dx6awd8irs7j.js@10890` `fetch("/api/session",{cache:"no-store"})` 后只读 `{endpointUrl, vmName, vmId, vmState, migrationState, replacedVmId, cvmMigrationInventory, vmType, replaceAttemptId, vmStateSource}` | `/api/session` 的响应**没有公钥 / attestation 字段**，它只负责"把这台 VM 指到哪"（`endpointUrl`）与状态；公钥不可能从这里来 |
| `2knhq5aux36zv.js@42078`：`let H=await v(E.readMessage2(K),m(_))`，紧接着 `W=E.remoteStaticPublicKey()`；`@42296` `R.observedType = null==O ? "standard" : "confidential"` | 公钥就是 **Noise XX 的响应方静态公钥**，由 msg2 的第 2 段加密块送上来（`decryptAndHash` 解出）；`remoteStaticPublicKey()` 返回它 |
| `@42685` 校验调用：`verifier.verify({attestationPayload:z, serverStaticPublicKey:W, handshakeHash:P, clientNonce:…})`；`z = O?.attestation ?? H`；`F="standard"` 时 `O=null` → 没有 verifier → **不校验，直接接受 W** | 信任模型：<br>• 标准 hatch VM：msg2 送什么公钥就用什么（TOFU，仅 TLS 保护传输）<br>• 机密 VM（CVM）：靠 attestation 把 `W` 与 `hostData`/chip 绑定，不匹配即 `PubkeyMismatch`/`HostDataMismatch` 失败 |
| `29ud4s9wry4fx.js@28132` 方案名 `hatch:cvm-host-data:v1`；hostData 校验 = 字符串 **44 字符**、`^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$`（即 32 字节 base64） | 机密路径的绑定材料是 32 字节 hostData（base64 44 字符），不是任意长 blob |
| `0ebw_w1k2dpcu.js` 遥测：`has_rv_challenge`、`vm_identity_matches`、`vm_proof_matches`、`receipt_current`、`server_key_len`、`pubkey_mismatch`… 失败类 `PubkeyMismatch`/`HostDataMissing`/`MeasurementMismatch`/`Transparency*` | 校验是**多步**的（SNP 报告、VCEK、Merkle 透明日志、chip claim），标准 VM 全跳过 |
| `0zye9gcj9-_b5.js`（21KB）Ed25519 `crypto.subtle` + libsodium `crypto_sign_verify_detached` 兜底、小序点黑名单、ECDSA 校验、SHA-256 Merkle 根、`TransparencySignatureInvalid` | 上述校验的实现就在这一块（若要把 CVM 路径做全，从这里下手） |

**一句话**：`server static public key` 不是常量、不是 API 下发，而是 **Noise XX msg2 里被加密送来的 `s`**；
标准 VM 直接信它（TOFU），机密 VM 再用 attestation（hostData/chip/receipt）把它钉到硬件身份上。

### 2.6 帧层（传输期）

| 证据 | 结论 |
| --- | --- |
| `@11825` 发送侧：`n=Math.max(1,Math.ceil(e.length/65489)); if(n>256) throw "payload too large for noise framing"`；`chunk_id` = `crypto.getRandomValues(2×u32)` 拼的 int64；循环 `for(t=0;t<n;t++)` 切 `e.subarray(65489*t, min(...))` | 分块规则：**先切 65489 再逐块加密**，块序 `chunk_index` 从 0 连续；空载荷也要发 1 块（`n=1`） |
| `@6630` 接收侧：`if(s.length>65489) throw payload too large`；`@6532` `totalChunks 1..256`；同 chunk_id 最多 16 组待拼装；拼装预算 `0x1000000`（16MB）；重复/缺块/超预算都抛 | 客户端 `MAX_PLAINTEXT_FRAME=65489`、`MAX_CHUNKS=256` 完全对；**另外还有 16 组并发上限 + 16MB 装配预算**（客户端未实现，恶意对端可打爆内存，建议补） |
| `ingress_rev_proxy.NoiseTransportFrame` 字段（`@5693` 解出）：`1 chunk_id(int64)`、`2 chunk_index(uint32)`、`3 total_chunks(uint32)`、`4 payload(bytes)` | 与客户端 `_pack` 的字段号一致 |

### 2.7 内层 RPC（Noise 之内的"HTTP"）

| 证据 | 结论 |
| --- | --- |
| `hatch.noise` proto（`@7666` 解出）：<br>`ServiceRequest{1 service(ServiceType), 2 payload}`；`ServiceResponse{1 payload}`；`ServiceFrame{1 stream_id(int64), oneof: 2 request, 3 response, 4 body_chunk, 5 reset}`；`Header{key,value}`；`ApplicationRequest{1 verb, 2 path, 3 headers[], 4 body, 5 end_body}`；`ApplicationResponse{1 status(int32), 2 headers[], 3 body, 4 end_body}`；`BodyChunk{1 data, 2 end_body}`；`Reset{1 code, 2 reason}` | 加密通道里跑的是一层**自带 HTTP 语义的 RPC**：`verb`/`path`/`headers`/`body`/`end_body`。没有 `kind:"action"`、没有 `items`、没有 `node_id` —— 那几个猜测的字段在前端产物里**不存在** |
| `ServiceType` 枚举：`SERVICE_DAEMON=0, SENTINEL=1, VAULT=2, AUTHD=3, SSH_ACCESS=4, YOLK=5`；`Reset.Code`：`UNSPECIFIED=0, CANCELLED=1, TIMEOUT=2, PROTOCOL_ERROR=3, REFUSED_STREAM=4, INTERNAL_ERROR=5, SERVICE_UNAVAILABLE=6` | 请求的 `service` 选择决定落到哪个后端（默认 `daemon`）；取消/超时用 `Reset` 帧表达 |
| `2knhq5aux36zv.js@28402` 头构造：`content-type: application/json`（仅带 body 时）、`x-request-id: crypto.randomUUID()`、`x-app-id: hatch-web`、`accept-language`（`en-US` 或 `es-419`） | 请求头固定这 4 条；`x-request-id` 每请求唯一，可用于对账 |
| `@28881` `body = "GET"!==verb && requestBody!=="empty" ? encode(JSON.stringify(params)) : b""`；GET 时 params 走 query 串 | body 就是 **JSON**，不是 protobuf；GET 参数拼 path |
| `3qzcufyl9d94_.js@525` 起是**完整方法表**（用 `rpc_registry.py` 抽出 **418 条**）：每条形如 `{method, httpMethod, path, service?, noiseOnly, streamMode?, requestTimeoutMs?, maxResponseBytes?, requestBody?}`。表落在 `D:\deep seek\work\rpc_table.txt` | 内层 RPC 不是"猜名字"，有一条 418 项的静态注册表；`noiseOnly:true` 的方法只能走 Noise，明文网关不给 |
| 代表条目（各自文件偏移）：`chat.stream`=`POST /chat/stream`（`@34506`，`streamMode="subscription"`）；`chat.subscribe`=`POST /chat/subscribe`（`@34720`，`streamMode="subscription"`，`requestTimeoutMs=3e4`）；`chat.stream_events`=`POST /chat/stream-events`；`fs.write`=`POST /fs/write`（`@26802`）；`sessions.list`=`GET /api/session/list`；`session.start`=`POST /session/start` | 订阅型方法是"一条 ApplicationRequest + 服务端持续回 `body_chunk`"；`chat.subscribe` 3s 内无记录会被判超时并可能断连（`0j768ks6f4vst.js@2874`） |
| `0fxqa6f8kw5pm.js@2581` 上传实现：`/fs/write` body `{path, data_base64, overwrite, append, create_parent}`，按 **524288 字节**切片循环；`@3040` `/fs/stat` body `{path}`；`@3207` `/fs/rename` body `{source, destination, create_parent, overwrite}` | `/fs/*` 的 body 形状可直接照抄；上传分片 512KB、`overwrite` 仅首片、`append` 其后 |
| `0j768ks6f4vst.js@10893` 请求体可 **zstd 压缩**（`level:3`，输入/编码上限 `0x4000000`）；`@15095` 响应若带 `content-encoding: zstd` 则在客户端流式解码；窗口上限 `0x4000000`/`0x1000000`/`4194304`/`8389632` | 大响应是 zstd 流，不是明文 JSON；客户端若要支持 `chat.stream`/`fs.read` 大结果，必须实现 `content-encoding: zstd` |
| `0j768ks6f4vst.js@2874` 关闭码：`4006` fatal、`4008` 请求/订阅超时（`chat.subscribe` 固定 4008）、`4009` 其它超时、`4010` 共享订阅终止、`4011` 订阅记录非法 | 这 5 个 close code 是服务端语义信号，客户端应映射成可诊断错误而不是"连接断了" |

### 2.8 连接生命周期（前端观察到的顺序）

| 证据 | 结论 |
| --- | --- |
| `2knhq5aux36zv.js@25715` 区段：取 token → 循环重试（仅 primary 且首次失败时刷新凭据一次）→ `new WebSocket(n)`，`binaryType="arraybuffer"` → `onopen` → 握手 → 返回 `{ws, transport}` | 顺序：`/api/hatch/token` → WS open → Noise（init/msg1/msg2/msg3/split）→ 之后才开始发 RPC |
| `@40211` 握手阶段名 `["initialize","msg1Send","msg2Wait","msg2Process","msg3Send","split"]`，超时 10s，`handshakeHash` 作为 `split` 之外的会话标识 | 每个阶段的耗时都被单独记录；10s 是**整段握手**预算，不是单条消息 |
| `@~26800` `onClose` 回调带 `close_code`/`was_clean`；握手期关闭 → "WebSocket closed during handshake" | 握手期任何 close 都算失败 |

## 3. 仍未找到 / 不可证实

- **prologue**：220 个 chunk 无 `prologue` 字符串，唯一混入是空串。→ 无自定义 prologue（或恒为空），客户端不需要额外 mix。
- `/api/session` 响应字段（含 `endpoint_url`）的前端**结构**：只有调用点，没有服务端契约；不影响本结论。
- `chat.subscribe` **NDJSON/事件字段**（谁在里面发什么）：前端只有订阅调用与超时处理，记录 schema 未静态暴露。
- 心跳：前端没有显式 ping/pong 帧，只有 `requestTimeoutMs` + close code；服务端是否主动 ping 未证实。
- `atod_token` 发放端点（仅 debug/`use_od` 分支出现）。
- 服务端是否**强校验** `app_version=1`：无法从前端证实（前端恒发）。
- 机密 VM（CVM）路径的完整校验链（SNP 报告/VCEK/透明日志）只看到实现符号与失败枚举，未逐段读透。

## 4. 对 `muse_bridge/` 的差异清单（按优先级）

1. `MSG2_MIN_LEN` 由 `80` 改 **96**（`noise.py:72`），否则短 msg2 会在 `decrypt_and_hash` 阶段报 tag 失败而不是报"msg2 太短"。
2. WS query 补 `app_version=1`；顺序对齐 `vm_id, auth_token, [notary_token], app_id, app_version, request_id`（`protocol_client.py` 的 `query` 构造）。
3. 帧解码器补 **≤16 组并发装配 + 16MB 装配预算**（对应 `2knhq5aux36zv.js@6532`/`@7176`），防恶意对端内存放大。
4. 握手加 `maxMessageBytes=65535` 与 10s 总超时（当前若无，则与线上不一致）。
5. 若要支持 `fs.read`/`chat.stream` 大响应：实现 `ApplicationResponse` 的 `body_chunk` 拼接 + `content-encoding: zstd` 解码。
6. 若要做机密 VM：从 `0zye9gcj9-_b5.js`（Ed25519/ECDSA/Merkle）与 `29ud4s9wry4fx.js@28132`（hostData 44 字符）复刻校验链；否则维持"遇 `delegation.`/`confidential` 明确失败"。

## 5. 复现命令（本机）

```text
python -Xutf8 "D:\deep seek\work\ctx.py" 2knhq5aux36zv.js "Noise_XX_25519_AESGCM_SHA256"
python -Xutf8 "D:\deep seek\work\proto_dump.py" 2knhq5aux36zv.js        # 解内嵌 proto 描述符
python -Xutf8 "D:\deep seek\work\rpc_registry.py" 3qzcufyl9d94_.js      # 418 条内层 RPC 表
python -Xutf8 "D:\deep seek\work\paths.py"                              # chunk 内 path 字面量普查
python -Xutf8 "D:\deep seek\work\chunkmap.py"                           # 生成 chunk_map.tsv
```

`ctx.py` 依赖环境变量 `BEFORE`/`AFTER`（默认 200/800），打印 `文件@偏移` + 上下文窗口，
不打印整段压缩 JS。
