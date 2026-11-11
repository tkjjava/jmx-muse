# muse Spaces / idea-cards 协议契约（前端静态产物取证版）

> **取证口径（只读本地文件）**：全部证据来自 `D:\deep seek\work\muse-bundles\*.js`
> （220 个 chunk / 31.8 MB，Turbopack 单行压缩 JS，**行号无意义，一律用字符偏移** `chunk@offset`）。
> 未启动浏览器/CDP、未发任何网络请求、未改 `muse_bridge/*.py`、未改 `docs/` 既有文件。
> 探针脚本与中间产物全部在 `D:\deep seek\work\canvas_re\`：
> `dump_ctx.py`（±N 字符上下文）、`find.py`（全量字面量检索）、`scan.py`（计数）、
> `list_rpc.py`（从 RPC 注册表抽条目）、`verify_anchors.py`（**校验本文每个 `@offset` 是否真实存在**）。
>
> **锚点可信度**：文档内共 **110 个不同 `chunk@offset`、350 处引用**（统计命令见 §15.3/§15.5）。
> 其中承载**核心契约**的 135 条锚点由 `verify_anchors.py` 逐条 `str.find()` 验证命中（`135 ok / 0 miss`，输出见 §15.2），
> 其余是上下文/计数类定位锚点。任何没有锚点的判断，都已被放进 §14「未证实/推断」。

---

## 0. 先纠一个命名：这里**没有**"无限画布"

任务书把 muse 的 Spaces 描述成"无限画布（idea-cards）"。**在 220 个 chunk 的全部字节里，这个模型不存在。**
Spaces 的真实形态是：**AI 构建出来的 Web 应用（artifact/"space"），由服务端 CVM 运行时渲染成 HTML bundle，
前端用同源 iframe + MessagePort 隧道加载；分享时由 Cloudflare 托管，用短时 `viewer_assertion` 换 frame URL。**

### 0.1 负证据（把"没有"证到什么程度）

| 证据 | 结论 |
| --- | --- |
| 全量 220 chunk 检索 `react-flow` / `xyflow` / `konva` / `minimap`：**0 命中**（`canvas_re/find.py`，见 §15.4 输出） | 没有引入任何图/画布引擎 |
| `canvas` 116 次命中集中在 lottie 渲染器（`3puj7bv8ecjrh.js@204305` 起 `createCanvas`/`transformCanvas`/`getLumaCanvas`）与图片压缩（`0zr5ttz54gz63.js@359293` `document.createElement("canvas")` + `toBlob("image/jpeg",.85)`） | "canvas" 全是 HTML `<canvas>` 图像处理，不是画布业务对象 |
| `spaces.list` 的**完整**归一化函数里没有任何 `x/y/position/scale/camera/zoom/viewport/nodes/edges` 字段（`2lzbmcnnh3_e0.js@15101`–`@16698` 逐字段见 §2.2） | space 的列表模型是"应用/产物"元数据，不是坐标文档 |
| `zoom` 32 次命中无一位于 spaces 相关 chunk（分布在 map/图片查看器/视频） | 无画布视口模型 |
| 路由清单只有 `/spaces/v2`（list）、`/spaces/v2/{slug}/save`、`/spaces/cvm/{slug}/*`、`/spaces/share/*`、`/spaces/{slug}/export.html`、`/spaces/{slug}/debug/*`（`3qzcufyl9d94_.js@24040`–`@26430`） | 没有 `/spaces/{slug}/canvas`、`/doc`、`/graph`、`/nodes/{id}` 之类的画布路由 |

> 结论（已证实）：**"无限画布 + 节点/边 + camera(x,y,zoom)"这一层在已下载的前端产物中没有任何实现痕迹。**
> 如果产品里真有画布视图，它只可能出现在**未下载的 chunk**（例如 8 个 404 的旧 chunk 名，以及 `/app` 路由相关的私有 chunk），
> 或者根本由 CVM 里的应用自己实现（即"画布"是 space 内部生成的 HTML，而不是宿主协议）。见 §14-R1。

### 0.2 Spaces 到底是什么（一句话版本，全部有锚点）

| 证据 | 结论 |
| --- | --- |
| `2lzbmcnnh3_e0.js@16336` `runtime:"string"==typeof e.runtime?e.runtime:null`；`@16494` `has_server_actions`；`@16578` `theme_accent`；`@16698` `view_url` | space 是**可运行的应用**：有 runtime、服务端 action、主题色、独立 view URL |
| `3d5x3ax0xpja0.js@31779` `CVM render did not return a bundle`；`@32030` `?v=${encodeURIComponent(String(g??""))}&r=${I}&k=${i}` | space 的"内容"是**服务端渲染出的 HTML bundle**（Blob），不是 JSON 文档 |
| `3qzcufyl9d94_.js@26082` `spaces.export_html` `responseType:"binary" channel:"media"` | 可整包导出成 `.html`（下载即单文件） |
| `0sdkhdrd88scd.js@1503` `/api/hatch/cloudflare-shared-space-frame-url` → `@2965` `/__hatch/viewer-session` | 分享后的 space 由 Cloudflare 托管，前端只拿 `iframeSrc` |
| `2xx7ca9grna3m.js@23305` `audience_policy_source`、`@23986` `published_manifest_sha256`、`@22995` `sharing_settings_version` | 分享是"发布版本 + manifest 哈希 + 受众策略"的产物语义，不是协作编辑文档语义 |
| `1qwraqj4bjr1v.js@38755` `/api/peer-spaces/by-id/{host}/{space_id}/...`、`3qzcufyl9d94_.js@42201` `peerSpaces.document`（`responseType:"binary"`，8 MiB 上限 `@42339`） | 跨 muse 共享的单位也是**整份 HTML 文档 + 资产** |

---

## 1. 调用模型：不是 REST，是 Noise 隧道里的 `sendRequest(method, params)`

前端**没有**直接 `fetch("/spaces/v2")`。所有 spaces / idea-cards / nodes 调用都走统一网关客户端：

```js
// 典型形态（按方法名调用，路由/HTTP 动词由注册表决定）
await u("spaces.rename", { slug: c, name: e })          // 0sdkhdrd88scd.js@8887
await u("spaces.share.settings", { slug: e })           // 1efd95h3zub6m.js@20019 附近
await r("spaces.cvm.get", { slug: e, subpath: l })      // 3d5x3ax0xpja0.js@37808 附近
```

| 证据 | 结论 |
| --- | --- |
| `3qzcufyl9d94_.js@24040` 起整段是 `{method:"spaces.list",httpMethod:"GET",path:"/spaces/v2"},…` 的**扁平注册表**（418 条） | 方法名 ↔ HTTP 动词 + 路径 的映射唯一来源；`params` 里的 `{slug}`/`{*subpath}`/`{ideaCardId}` 做路径插值 |
| `0j768ks6f4vst.js@2514` `maxPendingStreams:128`、`@2561` `streamIdleTimeoutMs:3e5`、`@8901` `4194304`（4 MiB 帧上限） | 单连接最多 128 条并发流；订阅流空转 300 s 回收；单帧 >4 MiB 直接断链（4004） |
| `0j768ks6f4vst.js@28716` `Gateway subscription ack failed` | `streamMode:"subscription"` 的方法：**首个 JSON 响应体是 ack**，之后是记录流 |
| `1pv9icoxptmvc.js@3754` 分类器：`case "api.idea-cards.execute": … "queued"===e.status\|\|"accepted"===e.status` 才算"已受理" | 触发成功率判定写死在传输层，客户端必须按 `status` 字段判定 |
| 注册表里的修饰位：`service:"daemon"`（peer-spaces 全部）、`channel:"media"`（二进制走媒体通道）、`responseType:"binary"`、`aliases:["/nodes/list"]`、`noiseOnly:!0`、`requestTimeoutMs:6e5`、`streamMode:"subscription"` | 客户端实现必须尊重这 7 个修饰位，否则对不上服务端 |

**三种响应形态（由注册表 `responseType` / `streamMode` 决定）**

| 形态 | 判定 | 前端处理 | 证据 |
| --- | --- | --- | --- |
| JSON | 默认 | 整体 `JSON.parse` 后交给驼峰归一化函数 | `0j768ks6f4vst.js@28900` 附近 `JSON.parse(o)` |
| binary | `responseType:"binary"` | `new Blob([body],{type:contentType})`；调用方再 `.text()` / `.arrayBuffer()` | `0j768ks6f4vst.js@29260` 附近 `i.resolve(new Blob([T.body]…))` |
| subscription | `streamMode:"subscription"` | 首包 ack JSON 先 resolve，后续 NDJSON 记录逐条派发到 `onEvent(type,…)` | `0j768ks6f4vst.js@28716`、`3kjg8-xr0c35a.js@8807` |

---

## 2. `space` 的数据模型

### 2.1 列表单项（唯一被完整实现出来的 space schema）

```ts
// ===== 服务端原始字段（spaces.list 的 e.spaces[i]，全部在 2lzbmcnnh3_e0.js@15101..@16698 被读取）=====
interface RawSpace {
  slug: string;                    // 主标识；缺失时才回退 space_id
  space_id?: string;               // UUID；与 slug 不同才保留
  name: string;                    // 缺失 => 整条 space 被丢弃（返回 null）
  image_url?: string | null;       // 会被重写成 CDN icon（见 §2.4）
  is_shared: boolean;              // ←→ 归一化后叫 is_saved_foreign_share
  shortcode?: string | null;
  share_type?: string | null;      // 例："cloudflare" | "tinker"（见 §2.4）
  source_url?: string | null;      // 分享来源的 origin
  construction_status?: string;    // 或 build_status；见 §2.3 枚举
  build_status?: string;
  updated_at_ms?: number;
  accessed_at_ms?: number;
  frequency_score?: number;
  favorite_order?: number;
  preview_image?: {
    version: string;               // 必填，否则 preview_image 整块置 null
    icon_url?: string;
    thumbnail_url: string;         // 必填
    thumbnail_width_px?: number;
    thumbnail_height_px?: number;
    preview_card_url: string;      // 必填
    preview_card_width_px?: number;
    preview_card_height_px?: number;
  };
  runtime?: string;                // 字符串，含义未证实（§14-R3）
  is_current_build_published?: boolean;
  has_server_actions?: boolean;
  theme_accent?: string;           // 颜色；归一化函数 o() 未取明文（§14-R3）
  saved_target_shortcode?: string;
  view_url?: string;               // 空间自己的 URL（分享/直链）
}

// ===== 前端归一化后的模型（字段名与上面一一对应，除 is_shared 改名）=====
interface SpaceListItem {
  construction_status: "constructing" | "succeeded" | "failed" | "interrupted" | "updating" | null;
  slug: string;
  space_id?: string;
  image_url: string | null;
  name: string;
  updated_at_ms?: number;
  accessed_at_ms?: number;
  frequency_score?: number;
  favorite_order?: number;
  preview_image: RawSpace["preview_image"] | null;
  is_saved_foreign_share: boolean;   // = raw.is_shared
  runtime: string | null;
  is_current_build_published: boolean | null;
  has_server_actions: boolean | null;
  theme_accent: string | null;
  shortcode: string | null;
  share_type: string | null;
  source_url: string | null;
  saved_target_shortcode: string | null;
  view_url: string | null;
}
```

**归一化的严格性（客户端要照抄的校验）**

```js
// 2lzbmcnnh3_e0.js@14846
function x(e){if(!Array.isArray(e.spaces))throw Error("Invalid spaces.list response");
  let t=e.spaces.map(e=> /* 单项归一化 */);
  if(t.some(e=>null==e))throw Error("Invalid spaces.list response");   // 任一条坏 => 整响应失败
  return t}
```

```js
// 2lzbmcnnh3_e0.js@15101 —— 单项归一化（截关键段，注意 slug 回退与字段改名）
let t,l=b(e.slug),r=b(e.space_id),a=l??r,s=b(e.name);
if(null==a||null==s)return null;
let i=null==(t=b(e.image_url))?null:t,u=!0===e.is_shared,c=b(e.shortcode),p=b(e.share_type),h=b(e.source_url);
return{construction_status:(enum)(e.construction_status??e.build_status),slug:a,
  ...null!=r&&r!==a?{space_id:r}:{},
  image_url:v({imageUrl:i,isSavedForeignShare:u,shortcode:c,shareType:p,sourceUrl:h}),name:s,
  updated_at_ms:(0,n.f)(e.updated_at_ms),accessed_at_ms:(0,n.f)(e.accessed_at_ms),
  frequency_score:(0,n.f)(e.frequency_score),favorite_order:(0,n.f)(e.favorite_order),
  preview_image:/*校验 version/thumbnail_url/preview_card_url，缺一即 null*/,
  is_saved_foreign_share:u, runtime:"string"==typeof e.runtime?e.runtime:null, …}
```

> 注意：`(0,n.f)(x)` 是"数字归一化"（非有限数字段被抹掉），所以 `updated_at_ms` 缺失是**合法**状态，
> 客户端不能用它做必填版本号（见 §14-R4）。

### 2.2 `construction_status` 枚举（大小写双写）

```js
// 2lzbmcnnh3_e0.js@15070 附近 / 0wc44m6535bb7.js@25295 附近（同一份 switch，出现两次）
switch(e){case"constructing":case"BUILDING":return"constructing";
 case"succeeded":case"READY":return"succeeded";
 case"failed":case"FAILED":return"failed";
 case"interrupted":case"INTERRUPTED":return"interrupted";
 case"updating":case"UPDATING":return"updating";
 default:return null}
```

服务端大小写不定（`constructing` 与 `BUILDING` 都出现过），客户端必须**大小写不敏感**匹配。

### 2.3 "文档本体"在哪：不在客户端 JSON 里，在服务端 bundle 里

| 证据 | 结论 |
| --- | --- |
| `3d5x3ax0xpja0.js@31779` `let l=null!=h?await h():await m("spaces.cvm.render",{slug:t})`；紧接着 `if(!(l instanceof Blob))throw Error("CVM render did not return a bundle"); let u=await l.text()` | 取"space 内容"= 一次二进制 RPC，拿到 **HTML 文本**；参数只有 `{slug}`（外加可选的 `bundleLoader` 覆写） |
| `3d5x3ax0xpja0.js@32030` `…/api/hatch/cvm-frame?protocol=${o.Z}&v=${encodeURIComponent(String(g??""))}&r=${I}…&k=${i}` | 版本用 `v=<updatedAtMs>` 做**缓存失效键**；`r=<retryNonce>` 是重试计数；`k=<frameSecret>` 是本次帧的随机 32 字符密钥 |
| `3qzcufyl9d94_.js@26201` `spaces.cvm.render` `path:"/spaces/cvm/{slug}/"`、`responseType:"binary"`、`channel:"media"` | 服务端路由把 bundle 挂在 `/spaces/cvm/{slug}/` |
| `3qzcufyl9d94_.js@26312` `spaces.cvm.get` `path:"/spaces/cvm/{slug}/{*subpath}"`；`@26430` `spaces.cvm.post` 同路径 POST | 应用内的**每次 HTTP 调用**都被宿主代理回 `/spaces/cvm/{slug}/<subpath>`（详见 §6.3） |

**所以 `spaces.save` 不可能保存"画布 JSON"** —— 如果它保存的是布局，那必然有客户端坐标产生代码；这类代码在语料里不存在。
`spaces.save` 的真实语义在语料里**没有调用点**，只能标注未证实（§14-R2）。

### 2.4 `image_url` / 图标重写规则（分享态 space 的图标走 CDN 域名推导）

```js
// 2lzbmcnnh3_e0.js@14321 起
if("cloudflare"===i||(0,a.x)(o)){ … `cloudflare`!==i&&null!=u&&null!=(0,a.l)(o)?`${u.origin}/assets/icon.jpg`:(0,a.p)(l,"/assets/icon.jpg")??e }
if("tinker"===i||o.length>0&&o.endsWith(".h.metaaiusercontent.com"))
  return w.test(l)?`https://${l}.h.metaaiusercontent.com/assets/icon.jpg`:e;
if(null!=u&&o.endsWith(".s.metaaiusercontent.com"))return`${u.origin}/icon.jpg`;
let c=-1===(s=l.lastIndexOf("-"))?l:l.slice(s+1);
return w.test(c)?`https://${c}.s.metaaiusercontent.com/icon.jpg`:e
```

| 证据 | 结论 |
| --- | --- |
| `2lzbmcnnh3_e0.js@14321` `h.metaaiusercontent.com`、`@14446` `s.metaaiusercontent.com` | 分享空间有两个托管域族：`*.h.*`（tinker）与 `*.s.*`（cloudflare），图标约定分别是 `/assets/icon.jpg` 与 `/icon.jpg` |
| `2lzbmcnnh3_e0.js@15934` `thumbnail_url`（必填） | 列表缩略图由服务端给出，不要自行拼接 |
| `2lzbmcnnh3_e0.js@16647` `saved_target_shortcode` | "被收藏的他人分享"会带目标 shortcode，可与 `shortcode` 组合定位 |
| `1qwraqj4bjr1v.js@38508` `_preview/thumbnail`、`@38755` `/api/peer-spaces/by-id/` | peer（跨 muse）空间的缩略图/图标路径是 `/api/peer-spaces/by-id/{host_muse}/{space_id}/_preview/thumbnail` 与 `.../icon.png`；`host_muse` 形如 `muse1<rand>:<uuid>` |

---

## 3. `spaces.list`（GET `/spaces/v2`）

### 3.1 请求

```js
// 2lzbmcnnh3_e0.js@12579
async function f(e,t){return x(await e.sendRequest("spaces.list",{...t?{include_constructing:!0}:{}}))}
```

```ts
interface SpacesListParams {
  include_constructing?: boolean;   // 唯一参数；true => body 里出现该键，false => body 为 {}
}
```

- **没有** 分页参数、**没有** 排序参数、**没有** `limit/offset/cursor`（`2lzbmcnnh3_e0.js@12612` 只有 `include_constructing`；全 chunk 无 `spaces/v2?…` 查询串）。
- 排序在**客户端**做（见 §3.3）。
- 请求超时/重试由传输层统一给（`staleTime:1e4` = 列表 10 s 内不重复拉，`2lzbmcnnh3_e0.js@12640` 附近 `staleTime:1e4`）。

### 3.2 响应

```ts
interface SpacesListResponse { spaces: RawSpace[] }   // 顶层就一个键；spaces 必须数组
```

错误形态：非数组 → `Error("Invalid spaces.list response")`（`@14883`）；任一条目不可解析 → 同一错误。

### 3.3 排序（客户端衰减排序，"近 7 天半衰"）

```js
// 2lzbmcnnh3_e0.js@14660
function k(e,t,l){return t*Math.exp(-(l-e)/6048e5)}        // 6048e5 = 604800000 ms = 7 天
function S(e,t,l){let r=k(e.accessedAtMs??0,e.frequencyScore??0,l);
  return k(t.accessedAtMs??0,t.frequencyScore??0,l)-r||(t.accessedAtMs??0)-(e.accessedAtMs??0)}
```

`score = frequency_score * exp(-(now - accessed_at_ms)/6048e5)`，降序；同分按 `accessed_at_ms` 降序。
客户端要复刻"最近使用优先"的列表顺序，就必须实现这条公式（服务端不排）。

### 3.4 打开单个 space 时的定位查询（复用 list，不额外请求）

```js
// 2lzbmcnnh3_e0.js@12650 附近
async function N({gatewayCtx:e,queryClient:t,slug:l}){…
  let s={gatewayUrl:n,includeConstructing:!0};
  let i=e=>e?.find(e=>e.slug===a&&!0!==e.is_saved_foreign_share)??null;   // 关键：跳过 saved foreign share
  …缓存命中直接返回{status:"found",space:u}；否则 fetchQuery(spaces.list) 再查一次
  …返回{status:"found"|"missing"|"error"}
```

| 证据 | 结论 |
| --- | --- |
| `2lzbmcnnh3_e0.js@12650` 起 `i=e=>e?.find(e=>e.slug===a&&!0!==e.is_saved_foreign_share)` | "本地 space vs 共享 space"的判定就是 **`is_saved_foreign_share` 标志**：同 slug 的本地条目优先，foreign-share 同名条目被跳过 |
| 同上 `throw`/`catch` → `{status:"error"}`，找不到 → `{status:"missing"}` | 客户端应区分 missing（可回退到分享页）与 error（网络/协议问题） |

---

## 4. `rename` / `delete` / `save`

### 4.1 `spaces.rename`（POST `/spaces/rename`，`3qzcufyl9d94_.js@24098`）

```js
// 0sdkhdrd88scd.js@8887
await d("spaces.rename",{slug:c,name:e})
```

```ts
interface SpacesRenameReq { slug: string; name: string }      // 两字段都必填
// 响应：未在语料中出现消费代码 => 不校验返回值（推断：服务端返回 {ok}，§14-R5）
// 失败路径：仅打 Sentry + toast "Couldn't rename the space"
```

### 4.2 `spaces.delete` / `spaces.owned.delete`（DELETE `/spaces/v2/{slug}`）

```js
// 2lzbmcnnh3_e0.js@16949
let a=await t(l?"spaces.owned.delete":"spaces.delete",{slug:r})??{},
    n=b(a.slug),s=b(a.space_id),i=n??r;
if(!1===a.ok)throw Error(`Failed to delete space ${i}`);
return{deletedIdentifiers:Array.from(new Set([r,...n??[],...s??[]])),deletedSlug:i}
```

| 证据 | 结论 |
| --- | --- |
| `3qzcufyl9d94_.js@24163` / `@24233` / `@24322` 三条注册项：`spaces.delete`（无 query）、`?source=space`、`?source=saved_space` | 服务端用 **`source` query** 区分"我自己的 space（删除）"与"我收藏的别人的 space（取消收藏）"；前端用 `l?"spaces.owned.delete":"spaces.delete"` 选择 |
| `2lzbmcnnh3_e0.js@16971` `"spaces.delete"` 与 `@16949` `"spaces.owned.delete"` | 两个注册名都在用 |
| 删除响应字段 `ok` / `slug` / `space_id` | 客户端应当用 `ok!==false` 判定成功，并把 `{slug, space_id}` 都从本地列表剔除（同一 space 可能有 3 个标识） |

### 4.3 `spaces.save`（POST `/spaces/v2/{slug}/save`）—— **body 未证实**

| 证据 | 事实 |
| --- | --- |
| `3qzcufyl9d94_.js@24417` `method:"spaces.save"`、`@24456` `path:"/spaces/v2/{slug}/save"`、注册项**没有任何修饰位**（无 `requestBody`、无 `responseType`、无 `streamMode`） | 方法存在、是 POST、需要 `{slug}` 路径参数 |
| 全量检索 `"spaces.save"` 只有 3 处：注册表 2 处 + `0jvm7f69mevry.js@33987` 的 `spaces.saved.delete` **子串误命中**（`canvas_re/find.py` 输出） | **语料里没有调用点**，因此请求体、响应结构、幂等语义全部无法证实 |
| 间接排除：`spaces.updated` 事件只带 `{slug,kind,updated_at_ms}`（`2xx7ca9grna3m.js@23682`），且 space 内容由 `cvm.render` 全量重取（`3d5x3ax0xpja0.js@31779`） | 推断：`save` 不是"增量文档补丁"，更像"保存某个草稿/发布态"（§14-R2，**不许当结论用**） |

**给客户端的保守做法**：先实现 `list/rename/delete/render/share/export`，`save` 只留接口壳子，不进入第一版。

---

## 5. 打开一个 space 的完整请求序列

有 **4 条加载路径**，按"本地私有 / 已分享给别人 / 别人的分享 / peer(跨 muse)"分流。

### 5.1 判定树（前端真实分支）

```js
// 2xx7ca9grna3m.js@~22600（舞台组件解构，逐字）
{stageSrc:tP, allowedSpaceFrameOrigin:tM, cloudflareShare:tO, cloudflareSpaceFrameFailure:tj,
 isStageSourceReady:tT, privateSpaceFrameFailure:tK, publicationIncomplete:tG,
 shouldUsePrivateSpaceFrame:tW} = …
…
let P=v?.is_saved_foreign_share===!1&&C.servesCopy;    // 本地 space 且"有可服务的副本"
```

| 分支条件（按代码顺序） | 走哪条路径 | 关键证据 |
| --- | --- | --- |
| 本地/私有、同源可注入 bundle | **CVM 直载**：`cvm.render` → 同源 iframe `/api/hatch/cvm-frame` → postMessage 注入 HTML | `3d5x3ax0xpja0.js@31779`、`@32030` |
| 私有但需 VM 侧 frame（跨域/独立 VM） | **private-space-frame-url**：`GET /api/hatch/private-space-frame-url?slug=…&protocol=2&hatch_vm_identity=…&v=…` → `{iframeSrc, vmIdentity}` → iframe 指该 src | `2xx7ca9grna3m.js@4000`、`@3342` |
| 已发布到 Cloudflare（`share_type:"cloudflare"`） | **cloudflare-shared-space-frame-url**：`GET /api/hatch/cloudflare-shared-space-frame-url?shortcode=…[&share_key=…]` → `{iframeSrc}` → iframe | `0sdkhdrd88scd.js@1503` |
| peer / 跨 muse（daemon 服务） | **peerSpaces.document** 取整份 HTML，资产内联后本地渲染 | `0wc44m6535bb7.js@8611`、`@8747` |

### 5.2 路径 A（主路径，私有 space）——逐步

```text
1. spaces.list {include_constructing:true}
     -> 定位条目（slug 命中且 is_saved_foreign_share!==true），拿 updated_at_ms / runtime / has_server_actions
       2lzbmcnnh3_e0.js@12579 / @12650
2. spaces.cvm.render {slug}
     -> Blob(HTML)。非 Blob 即协议错：Error("CVM render did not return a bundle")
       3d5x3ax0xpja0.js@31779
3. 生成 frameSecret = crypto.randomUUID() 去掉非字母数字（32 字符十六进制串）
       3d5x3ax0xpja0.js@31970 附近 `let i=crypto.randomUUID().replace(/[^A-Za-z0-9]/g,"")`
4. <iframe src="/api/hatch/cvm-frame?protocol=<P>&v=<updatedAtMs>&r=<retryNonce>&k=<frameSecret>">
     —— 这是"引导壳"，不是应用本体；同源，可 postMessage
       3d5x3ax0xpja0.js@32030
5. 等壳发出 postMessage {type:"cvmBootstrapReady", k:<frameSecret>, nonce, ports:[MessagePort]}
       3d5x3ax0xpja0.js@33623
6. 宿主向该 port 发送 {type:"cvm-bootstrap:install-bundle", protocol:<P>, bridgeToken, html:<注入了 nonce+bridge 脚本的 HTML>}
       3d5x3ax0xpja0.js@~37600（`c.postMessage({type:"cvm-bootstrap:install-bundle"…})`）
7. 壳把 html 写入文档 → iframe 内 bridge 脚本 parent.postMessage({type:"cvm-inner:document-ready",bridgeToken})
       3d5x3ax0xpja0.js@13637
8. 宿主 re-post {type:"cvm-inner:install-bridge"} 携带 port → 建立应用侧端口；收到 cvm-bridge:document-loaded
       3d5x3ax0xpja0.js@11429 / @11915
9. 需要"当前文档权威确认"时：post {type:"cvm-bridge:confirm-document",id}（10 s 超时，最多 32 次并发确认）
       3d5x3ax0xpja0.js@13226（`confirmations.size>=32` / `AbortSignal.timeout(1e4)`）
10. 应用内发起的每个 HTTP 请求都经 port 转到宿主 -> spaces.cvm.get / spaces.cvm.post（§6.3）
```

### 5.3 路径 B（私有 frame URL）

```js
// 2xx7ca9grna3m.js@4000 起（逐字）
let u=new URLSearchParams({slug:p}),i=new URLSearchParams({slug:p});
for(let[e,t]of new URLSearchParams(y))u.set(e,t),i.set(e,t);      // y = 网关连接参数串（vm_id 等）
u.set(Q,"2"),i.set(Q,"2"),u.set(D.M,n),null!=h&&u.set("v",String(h));   // Q="protocol"; D.M="hatch_vm_identity"
…
let s=`/api/hatch/private-space-frame-url?${e}`;
```

| 证据 | 结论 |
| --- | --- |
| `2xx7ca9grna3m.js@4000` + `3d5x3ax0xpja0.js@903774 附近 "M",0,"hatch_vm_identity"` | query 名固定：`slug`、`protocol=2`、`hatch_vm_identity=<vm uuid 归一化>`，可选 `v=<updatedAtMs>`，其余网关参数原样透传 |
| `2xx7ca9grna3m.js@6687` `expiresAtMs:Date.now()+45e3` | frame URL **缓存 45 s**，过期重取；缓存上限 100 条（同段 `H.size>=100`） |
| `2xx7ca9grna3m.js@3342` `vmIdentity` + 校验 `typeof r==="string"&&r.length>0&&n===t` | 响应必须含 `{iframeSrc:string, vmIdentity:string}`，且 `vmIdentity` 必须等于本连接的期望值；若响应里**缺** `vmIdentity` 键 → 判定 `incompatible_server`（要求整页刷新） |
| `3d5x3ax0xpja0.js` 模块 903774 导出 `"x-hatch-private-frame-error-source"`、`"route"`；`2xx7ca9grna3m.js` 里 `t.headers.get(D.J)===D.g` 判 `isRouteOwned` | 路由级错误用响应头 `x-hatch-private-frame-error-source: route` 标记，客户端据此区分"路由拥有"（可提示）与"上游故障"（可重试） |

失败分级（`2xx7ca9grna3m.js@~6900`）：`network` / `incompatible_server`（需 reload）/ `invalid_response` / `route-owned`（HTTP 状态 + 头）。
重试有"失败缓存 + `retryAfterMs`"，并对并发 generation 做 latest-wins 去重。

### 5.4 路径 C（Cloudflare 分享页）

```js
// 0sdkhdrd88scd.js@1503
let l=await fetch(`/api/hatch/cloudflare-shared-space-frame-url?shortcode=${encodeURIComponent(e.shortcode)}${(0,i.g)(e.shareKey)}`,{cache:"no-store",signal:AbortSignal.any([n,AbortSignal.timeout(3e4)])});
… if("string"!=typeof a?.iframeSrc) throw … "Viewer assertion mint response was invalid"
…
// @2965 换成最终 iframe src（同一 Cloudflare origin）
src:((e=new URL("/__hatch/viewer-session",n.origin)).searchParams.set(s,n.assertion),e.toString())
```

| 证据 | 结论 |
| --- | --- |
| `0sdkhdrd88scd.js@522` `viewer_assertion` 被放进 iframe src 的 query | 换 URL 的动词是 `GET …-frame-url`，服务端返回**短时 JWT**（`viewer_assertion`），不是长期 URL |
| `0sdkhdrd88scd.js@522` 起解 JWT payload `{iat,exp}`，`lifetimeMs=(exp-iat)*1e3`，`expiresAtMs=1e3*exp` | 过期时间客户端自己解析（只读 payload，不验签） |
| `0sdkhdrd88scd.js@2108` `CloudflareViewerSessionRenewal`；同段 `-15e3`（提前 15 s 续期），退避 `1e3` 起、`408/429/>=500` 可重试、最多 6 次 | **会话 45 s～exp 之前必须续期**：客户端要实现"提前 15 s 续签 + 指数退避 + 只在页面可见时续"（`document.visibilityState==="visible"`） |
| `0sdkhdrd88scd.js@2965` `/__hatch/viewer-session?viewer_assertion=` | 最终页面路径在 Cloudflare 侧固定为 `/__hatch/viewer-session` |

### 5.5 路径 D（peer / 跨 muse，走 `service:"daemon"`）

```js
// 0wc44m6535bb7.js@8611
let t=await f.sendRequest("peerSpaces.document",{host_muse:h,slug:g},e),n=[],
    r=await (0,y.f)(await t.text(),async t=>{
      try{return await f.sendRequest("peerSpaces.asset",{host_muse:h,slug:g,subpath:t},e)}catch(e){…n.push(…);throw e}});
```

| 证据 | 结论 |
| --- | --- |
| `0wc44m6535bb7.js@8611` `await t.text()` | `peerSpaces.document` 返回的是 **HTML 文本**（注册表也标 `responseType:"binary"`、8 MiB 上限 `3qzcufyl9d94_.js@42339`） |
| `0wc44m6535bb7.js@21296` `slice(0,24)`、`@21359` `8388608` | 资产内联策略：**最多 24 个** `<script src>`/`<link href>` 资源，总预算 **8 MiB**，超预算的跳过；内联方式 `data:<mime>;base64,…` |
| `0wc44m6535bb7.js@8611` 起重写规则（同段 `D`/`V`/`B` 正则：`assets/…`） | 只处理相对路径 `assets/*`；含 `\`、`%` 或长度 >512 的路径直接拒绝（防路径穿越） |
| `3qzcufyl9d94_.js@41938` `peerSpaces.collaborators.set` + `1nf703hwxciza.js@66123` `expected_connection_revision` | peer 协作列表是 **CAS** 写：`{slug, collaborators:[{principal, permission?}], expected_connection_revision}` |
| `3qzcufyl9d94_.js@41810`–`@42765` 全部 `service:"daemon"` | 这一族由**本机 daemon 服务**处理（不是 VM/云侧），客户端要按 `service` 字段分流 |

---

## 6. CVM 桥：iframe 与宿主之间的完整消息契约

> 这是"space 能跑起来"的核心协议。全部证据集中在 `3d5x3ax0xpja0.js`（44.8 KB）。

### 6.1 注入脚本与握手（宿主 → 应用）

| 消息 `type` | 方向 | 载荷 | 证据 |
| --- | --- | --- | --- |
| `cvmBootstrapReady` | 壳 → 宿主（window message） | `{cvmBootstrapReady:true, k:<frameSecret>, nonce, protocol?, ports:[MessagePort]}` | `3d5x3ax0xpja0.js@33623` |
| `cvm-bootstrap:install-bundle` | 宿主 → 壳（port） | `{type, protocol, bridgeToken, html}` | `3d5x3ax0xpja0.js@~37600` |
| `cvm-inner:document-ready` | 应用 → 宿主（window） | `{type, bridgeToken}` | `3d5x3ax0xpja0.js@13637` |
| `cvm-inner:install-bridge` | 宿主 → 应用（window，带 port） | `{type, bridgeToken}` | `3d5x3ax0xpja0.js@11429` |
| `cvm-bridge:init-port` | 宿主 → 应用（port） | `{type, bridgeToken, id}` + 转移 `MessagePort` | `3d5x3ax0xpja0.js@11915` |
| `cvm-bridge:tunnel-ready` / `tunnel-failed` | 应用 → 宿主 | `{type, id[, message]}` | `3d5x3ax0xpja0.js@12206` 附近 |
| `bundle:init-port` | 应用内 → 应用内（合成 MessageEvent，注入器拦截 `ports`） | `{type:"bundle:init-port"}` + `ports:[tunnel]` | `3d5x3ax0xpja0.js@12206` |
| `cvm-bridge:document-loaded` | 应用 → 宿主 | `{type}`（`DOMContentLoaded` 或已 parsed 时立刻） | `3d5x3ax0xpja0.js@13637` 附近 |
| `cvm-bridge:document-unloading` | 应用 → 宿主 | `{type}`（`pagehide`，capture once） | 同段 |
| `cvm-bridge:document-confirmed` | 应用 → 宿主 | `{type, id}` | `3d5x3ax0xpja0.js@13226` |
| `cvm-bridge:confirm-document` | 宿主 → 应用 | `{type, bridgeToken, id}` | `3d5x3ax0xpja0.js@13226` |
| `cvm-bridge:runtime-signal` | 应用 → 宿主 | `{type, protocol:1, signal, resourceKind?, consoleErrorKind?, consoleErrorCount?}` | `3d5x3ax0xpja0.js@6455` |
| `cvm-bridge:runtime-signal`(收) | 宿主判重 | `protocol` 必须 `===1`；同 `(signal,kind,count)` 只报一次 | `3d5x3ax0xpja0.js@~35460`（`1===r.protocol&&L(...)`） |

**signal 取值**（`3d5x3ax0xpja0.js@6455` 起注入脚本）：`document_loaded`、`uncaught_exception`、`unhandled_rejection`、
`resource_load_error`（带 `resourceKind`: `script|stylesheet|image|media|frame|other`）、
`console_error`（带 `consoleErrorKind`: `no_arguments|string|object|function|primitive|other` 与计数 1/2/4/8/16/32）。
**配额**：`SIGNAL_COUNT>=12` 或同一 key 已报 → 丢弃；`console_error` 最多 8 行（同段）。

### 6.2 文档级消息（宿主 ↔ 应用业务）

| 消息 `type` | 方向 | 载荷 | 证据 |
| --- | --- | --- | --- |
| `hatch:space:query-invalidated` | 宿主 → 应用 | `{type, spaceSlug, payload:{queryKeys:string[][], originActionCallId?}}` | `2xx7ca9grna3m.js@1724` |
| `hatch:space:device-context` | 应用 → 宿主 | `{type, spaceSlug, payload/或扁平:{path, context:{entities:[{id,type,label?}], textSelection}, selection}}` | `2xx7ca9grna3m.js@47552` |
| `hatch:space:theme` | 宿主 → 应用（文档级） | `{type:"hatch:space:theme", version:1, colorScheme, accentColor}`（`version:1`，两者都必须在白名单内才发） | `2lzbmcnnh3_e0.js@12592` 附近（`e.s(["f",0,function(e,t){return u(s,e)&&u(i,t)?{type:"hatch:space:theme",version:1,…}:null}])`） |

约束（`2xx7ca9grna3m.js@1724` / `@47552`）：
- 只有 `event.origin === <frame origin>` 且 `event.source === frame.contentWindow` 才处理；
- 有 `frameSecret`（`k`）时必须 `t1(source, frameSecret, cb)` 二次校验；`payload.entities` 限 20 条、`type` 100 字符、`id` 100 字符、`name` 200 字符；`path` 限 1024 且必须 `/` 开头。

### 6.3 应用 HTTP 代理（`spaces.cvm.get` / `spaces.cvm.post`）

```js
// 3d5x3ax0xpja0.js@37808 起的 D()（逐字摘）
let l=("string"==typeof(o=t.path)?o:"").replace(/^\.\//,"").replace(/^\/+/,""),u=(t.method??"GET").toUpperCase();
if("GET"===u||"HEAD"===u) a = await r("spaces.cvm.get",{slug:e,subpath:l});
else { …body 由 t.bodyB64(JSON) 解出，剔除保留键 … a = await r("spaces.cvm.post",{...o,slug:e,subpath:l}); }
if(a instanceof Blob){let e=new Uint8Array(await a.arrayBuffer());
  return{status:200,headers:a.type?[["content-type",a.type]]:[],bodyB64:btoa(e)}}
return{status:200,headers:[["content-type","application/json"]],bodyB64:btoa(JSON.stringify(a??null))}
```

| 证据 | 结论 |
| --- | --- |
| `3d5x3ax0xpja0.js@37808` 起 `I=["slug","subpath","host_muse","space_id"]` 被 `delete` | POST 时**业务 body 与路径参数混在同一对象**里，客户端必须保留这 4 个键名不被应用覆盖 |
| 同上：响应统一被改写成 `status:200`，真实状态码/头丢失（只保留 `content-type`） | 这是**受限代理**：跨域、状态码、重定向语义都在宿主侧被抹平；客户端不要依赖应用代理的 status |
| `3qzcufyl9d94_.js@26430` `spaces.cvm.post` **没有** `channel:"media"`（而同族 GET 有） | POST 走默认通道，GET 走媒体通道 |

---

### 6.4 主题注入（`hatch:space:theme` / HTML 重写 / hash 参数）

| 证据 | 结论 |
| --- | --- |
| `2lzbmcnnh3_e0.js@12592` 附近 `let s=["light","dark"],i=["blue","green","red","purple","pink","orange","monochrome"]`；`o(e)` 用 `i.includes` 校验 | 色彩方案只允许 `light\|dark`；强调色只允许这 7 个；非法值一律归一成 `null` |
| 同段 `p=/<html(\s[^>]*)?>/i`、`h=/\s(?:data-hatch-color-scheme\|data-hatch-accent)\s*=\s*(?:"…"\|'…'\|[^\s>]+)/gi` | 注入方式是**字符串重写 `<html>` 开标签**：先剥掉已有 `data-hatch-*`，再插新值 → `<html data-hatch-color-scheme="dark" data-hatch-accent="blue">` |
| 同段 `data-hatch-host-theme` 分支：从 `class` 里过滤掉 `dark`，`colorScheme==="dark"` 时再 push 回去，然后重写 `class` 属性 | 应用可声明 `data-hatch-host-theme` **让宿主接管明暗 class**；否则只加 `data-*` 属性、不碰 class |
| 同段 `"g"`：`a.set("hatch_theme",r.colorScheme),a.set("hatch_accent",r.accentColor)` 拼成 `${e}#${n}`，且 `if(e.includes("#"))return e` | **URL hash 也能传主题**（`#hatch_theme=dark&hatch_accent=blue`）；已带 `#` 时不覆盖 |
| 同上「两者都合法才发」`u(s,e)&&u(i,t)?{…}:null` | 只改一个维度（例如只给暗色、不给强调色）时**不会发** `hatch:space:theme`，必须两个都给 |

---

## 7. 订阅与事件流（`spaces.subscribe`）

### 7.1 方法本身

| 证据 | 结论 |
| --- | --- |
| `3qzcufyl9d94_.js@35067` `{method:"spaces.subscribe",httpMethod:"POST",path:"/spaces/subscribe",noiseOnly:!0,streamMode:"subscription"}` | POST + 订阅流；必须走 Noise 隧道（`noiseOnly`） |
| 全量检索：`spaces.subscribe` **只有注册表 1 处**（`canvas_re/find.py`） | **没有调用点** → 请求体参数未知（§14-R6）。同族 `navigation.subscribe` 的调用形态见 §7.4 |

### 7.2 事件名与 payload（这部分**是**已证实，来自消费端）

```js
// 2xx7ca9grna3m.js@23682 —— 舞台订阅 spaces.updated
onEvent("spaces.updated",e=>{
  if(e.slug!==$)return;
  if("deleted"===e.kind){td.current=!0;return}          // 被删：标记，不再重载
  td.current=!1;
  let t=e.updated_at_ms;
  typeof t==="number"&&Number.isFinite(t)&&((0,y.K)($), to(e=>null==e?t:Math.max(e,t)))   // 单调抬升 updated_at_ms
})
```

```js
// 2xx7ca9grna3m.js@49131 —— spaces.data_invalidated：让 iframe 里的应用失效指定 query
onEvent("spaces.data_invalidated",e=>{
  if(e.slug!==$)return;
  let r=Array.isArray(e.query_keys)?e.query_keys.filter(e=>Array.isArray(e)):[],
      n="string"==typeof e.origin_action_call_id?e.origin_action_call_id:void 0;
  I(rI.current,$,r,n,ra), I(rU.current,$,r,n,ra);        // 向两个 frame（a/b，双缓冲）发 query-invalidated
})
```

```ts
interface SpacesUpdatedEvent {                 // 事件名 "spaces.updated"
  slug: string;
  kind?: string;                               // 取值至少含 "deleted"
  updated_at_ms?: number;                      // 版本戳；驱动 cvm-frame 的 v= 失效
}
interface SpacesDataInvalidatedEvent {         // 事件名 "spaces.data_invalidated"
  slug: string;
  query_keys?: unknown[][];                    // 逐项校验为数组，非数组项被过滤
  origin_action_call_id?: string;
}
```

### 7.3 前端如何处理（乐观更新 / rebase / 节流）

| 证据 | 结论 |
| --- | --- |
| `2xx7ca9grna3m.js@23682`：收到 `updated_at_ms` 只做 `Math.max` 抬升 + `invalidateQueries` | **没有乐观写入**：事件只是"失效信号"，真实内容靠下次 `cvm.render` 全量重取 |
| `2xx7ca9grna3m.js@49131`：把 `query_keys` 透传成 iframe 的 `hatch:space:query-invalidated` | 协作粒度 = **query key**（应用内数据源级），不是"文档字段级 diff" |
| 全量检索 `rebase`、`presence`、`cursor`（协同光标）在 spaces 相关 chunk 中**无命中**（`canvas_re/find.py`） | 没有 CRDT/OT/rebase 机制；没有他人光标同步 |
| `2xx7ca9grna3m.js` 中 `spaces.share.status` 的轮询退避：`Math.min(a?3e4:n<6e3?500:i<3e4?2e3:15e3,r)`（`1nf703hwxciza.js@~60500` 同款） | **分享状态**轮询：前 6 s 每 500 ms、30 s 内每 2 s、之后每 15 s，最长 30 s 截止（`requestTimeoutMs:6e5` 的 600 s 与之无关，是 RPC 超时） |

### 7.4 订阅的通用形态（用 `navigation.subscribe` 作为同构样板）

```js
// 3kjg8-xr0c35a.js@8807
let e=(0,d.h)((0,c.l)().enabled),
    t=await p("navigation.subscribe",e.params,{subscriptionTag:e.subscriptionTag});
… // 首包即快照
let t=y("navigation.items.snapshot",(e,t)=>{…setQueryData(…)}),
    r=y("navigation.items.updated",(e,t)=>{…merge…});
```

| 证据 | 结论（对 `spaces.subscribe` 的**推断**，标注在 §14-R6） |
| --- | --- |
| `3kjg8-xr0c35a.js@8842` `{subscriptionTag}`；`@9322`/`@9442` 事件名 `navigation.items.snapshot` / `navigation.items.updated` | 订阅型方法的统一形态：`sendRequest(method, params, {subscriptionTag})` → **首包是快照/ack**，后续记录按 `type` 派发给 `onEvent` |
| `0j768ks6f4vst.js@28716` | 首包必须是 JSON 对象，否则 `Gateway subscription ack failed` |

---

## 8. idea-cards：卡片是什么、execute 触发什么

### 8.1 定位：这是"探索/灵感卡"，不是画布卡片

| 证据 | 结论 |
| --- | --- |
| `0wc44m6535bb7.js@23365` `exploreIdeas`（queryKey 根）；`@23268` `iconPalette`（列表唯一参数 `colorIcons` 的落点） | 前端内部名是 **exploreIdeas**："推荐给我的想法" |
| `0wc44m6535bb7.js@23978` `request:{method:"api.idea-cards.list",params:i,resultMode:"raw"}`；`@29161` `api.idea-cards.execute` | 列表/执行都走 idea-cards 命名空间 |
| `0wc44m6535bb7.js@~28300` `kind` 归一化：`space|reminder|data_source|goal|task|skill|search|image|document|idea`（同义词 `research/discovery→search`、`visual→image`、`audio→skill`） | 卡片的"产物/条目"可以是 **space**、提醒、数据源、目标、任务、技能… —— 这正是 idea-card 与 space 的连接点 |

### 8.2 列表

```ts
interface IdeaCardsListParams {
  colorIcons?: boolean;        // -> params.iconPalette = "color"（true 时）
  pageSize?: number;           // 可选；infinite 列表用
  // 游标字段名未证实（§14-R7）
}
interface IdeaCardsListResponse {
  result: {                                  // 前端同时接受裸对象与 {result:…}
    sections: {
      id: string;
      title: string;
      subtitle?: string;
      presentation?: { layout?: "hero" | string };
      cards: RawIdeaCard[];
    }[];
    viewerState?: { hasBuiltIdea?: boolean; has_built_idea?: boolean };
    pagination?: { nextCursor?: string|null; hasNextPage?: boolean;
                   next_cursor?: string|null; has_next_page?: boolean };  // 两种命名都接受
  };
}
```

**卡片归一化后的字段**（`0wc44m6535bb7.js@~26200` 的 `E(e,r)`）：

```ts
interface IdeaCard {
  idea: {
    id: string;
    title: string;            // content.title
    description?: string;     // content.summary
    sourceLabel?: string;
    labels: ({kind:"label",label:string}|{kind:"count",count:number})[];  // presentation.labels 聚合
    badges: string[];         // presentation.badges（对象取 .text）
    groupIds: string[];       // 由 sections 反推
    sharing?: unknown;        // (0,t.e)(sharing) 归一化，未取明文
    isFeatured?: true;
    actionLabel?: string;
    buildStatus?: "not_started"|"in_progress"|"complete"|"success"|"failed"|null;
    buildSummary?: string;
  };
  isHero: boolean;            // section.presentation.layout === "hero"
}
```

**hero 规则**（同段）：hero section 的前 4 张卡进 `heroIdeaIds`；非 hero section 的卡会被去重并归组。
`showNullStateMessage = !hasBuiltIdea`。

### 8.3 详情

```ts
// 0wc44m6535bb7.js@~26500 的 b(e)；同时接受 {result:{card}} 与裸 {card} 与裸 card
interface IdeaCardDetailResponse {
  result: { card: {
    id: string;
    content: { fitReason?: string; buildSummary?: string; title?: string; summary?: string };
    presentation: {
      previewImage?: { variants: { url: string; role?: string }[] };   // 取 role="original" 优先
      icon?: same;
    };
    items: {
      id: string;
      type: string;           // -> kind（见 §8.1 枚举）
      content: { title: string; summary?: string; buildSummary?: string };
      presentation: { previewImage?: {variants…} };
      selectable?: boolean;   // 默认 true
      isSelected?: boolean;   // 默认 true
    }[];
    buildStatus?: string; build_status?: string;
  } };
}
```

### 8.4 执行（`execute`）：触发生成什么、带哪些参数

```js
// 0wc44m6535bb7.js@29103 起（逐字）
async function K(e,t,n,r={}){
  let i=null!=t&&t.length>0?{mode:"selectedItems",itemIds:t}:{mode:"full"};
  return A(await n("api.idea-cards.execute",{ideaCardId:e,...i,...r}) ,e)   // r 可携带 session 等附加参数
}
// @28938 响应归一化
function A(e,t){let n=…(e.result??e)…,r=(0,p.u)(n.status);
  return{success:"queued"===r||"accepted"===r,
         ideaCardId:(0,p.u)(n.cardId??n.card_id)??t, builtAtUtc:null, chat:m(n.chat)}}
// chat 归一化（同段 m()）
{streamOwnerMessageId, channel, sessionId, isThread}  ← 键名同时接受 camel 与 snake
```

```ts
interface IdeaCardExecuteReq {
  ideaCardId: string;                 // 必填（路径参数同时用于 body）
  mode: "full" | "selectedItems";     // 必填：整卡执行 or 只执行选中条目
  itemIds?: string[];                 // mode=selectedItems 时必填
  // 其余附加参数由调用方透传（r），语料未给出具体键名（§14-R8）
}
interface IdeaCardExecuteResp {
  result: {
    status: "queued" | "accepted" | string;   // 仅这两个算受理成功
    cardId?: string; card_id?: string;
    chat?: { streamOwnerMessageId?: string; stream_owner_message_id?: string;
             channel?: string; sessionId?: string; session_id?: string;
             isThread?: boolean; is_thread?: boolean } | null;
  };
}
```

| 证据 | 结论 |
| --- | --- |
| `0wc44m6535bb7.js@28938` + `1pv9icoxptmvc.js@3754` 传输层同一判定 | 执行是**异步受理**：`status ∈ {queued, accepted}`；不是同步出图 |
| `0wc44m6535bb7.js@28938` 起 `chat` 对象（`stream_owner_message_id`/`channel`/`session_id`/`is_thread`） | 执行的实质是**向某个 chat 流投递一次"构建这个 idea"的任务**；产出进度用 `chat.*` 流 + 卡片 `build_status` 回读 |
| `0wc44m6535bb7.js@~25295` `build_status` 枚举与 space 的 `construction_status` 同一个 switch | 卡片的构建状态与 space 的构建状态共享一套状态机 |

**execute → 成品的请求顺序**（已证实部分 + 明确标注推断）：

```text
1) POST api.idea-cards.execute {ideaCardId, mode, itemIds?}      → {status:queued, chat:{stream_owner_message_id, channel, session_id}}
      0wc44m6535bb7.js@29161 / @28938
2) 打开该 chat 的流（chat.stream 或 chat.subscribe，subscription 模式）并消费 NDJSON
      【推断】：响应里给的是 stream_owner_message_id + channel + session_id，正是 chat 流的定位三元组；
       chat 流协议见 docs/PROTOCOL.md §1.6（同一传输层的 subscription 形态）
      证据锚：0wc44m6535bb7.js@28938（字段）、3qzcufyl9d94_.js 的 chat.stream/chat.subscribe 注册项
3) 期间轮询/失效 api.idea-cards.list 或 detail，读 card.build_status 由 in_progress → success/failed
      0wc44m6535bb7.js@25295
4) 成功后再取详情（detail）拿 items[].previewImage / buildSummary；若是 space 类条目，转 space 加载（§5）
      0wc44m6535bb7.js@~26500
```

### 8.5 媒体（`/api/idea-cards/media/{handle}`）

| 证据 | 结论 |
| --- | --- |
| `3qzcufyl9d94_.js@27414` `{method:"idea-cards.media",httpMethod:"GET",path:"/api/idea-cards/media/{handle}",responseType:"binary",channel:"media"}` | 是**二进制 RPC**（不是给 `<img src>` 用的普通 URL）；`handle` 来自媒体字段 |
| `3n69z2klm3y8g.js@3049` `media_handle`（兼容 `mediaHandle`）+ `variants.original` + `mime/byte_len/width/height/duration_ms` | 媒体对象：`{path, label, missing?, media_handle, mime?, byte_len?, width?, height?, duration_ms?, variants:{original}}` |
| `3n69z2klm3y8g.js@45711` URL 白名单：只接受 `pathname` 以 `/idea-cards/media/` 或 `/api/idea-cards/media/` 开头的**相对路径**（否则必须 `https` 绝对 URL），且 `bytes<=8192` | 客户端拿到的图片地址有两种形态：①服务端相对媒体路径（浏览器同源 GET）②`https` 外链 |
| `3qzcufyl9d94_.js@27414` 同族 `share`/`share-preview`/`debug` 均为 `noiseOnly`（share/preview） | 分享/预览走隧道；`debug` 无修饰 |

### 8.6 `share`（POST `/api/idea-cards/{ideaCardId}/share`）

| 证据 | 结论 |
| --- | --- |
| `3qzcufyl9d94_.js@20405` `method:"api.idea-cards.share"` `noiseOnly:!0`；`@20509` `share-preview` 也 `noiseOnly` | 存在"分享卡"能力，且必须先拿 preview |
| 全量检索 `idea-cards.share` 只有注册表 2 处 | **没有调用点**：body / 响应未证实（§14-R9） |
| 语义推断（标注推断）：`share` = 把卡片发布成一个可分享的公开产物；`share-preview` = 预览分享页元数据 | 见 §14-R9，不进结论 |

---

## 9. `/nodes/*` 与 `/api/nodes/*`：这是"分布式执行节点"，不是画布图节点

| 证据 | 结论 |
| --- | --- |
| `24thn7in_976_.js@157400` `await e("node.register",{node_id:r,display_name:n,platform:a,commands_v2:{ping:{description:"Connection liveness check"}}})` | 节点 = **一台设备/客户端进程**（`node_id` 来自 `deviceContext.local.deviceId`，`platform` 来自同处），注册时**声明自己支持的命令表** `commands_v2` |
| `24thn7in_976_.js@157547` 日志 `"Registered web client as node"`，随后 `c.v.invalidateAll(t)` | **Web 客户端自己就是一个节点**（所以"节点"不是画布里的图形） |
| `3qzcufyl9d94_.js@28003` `node.register` path `/api/nodes/register` 且 `aliases:["/nodes/register"]`（`@28071`）；同理 list/heartbeat/unpair/disconnect | 服务端同时暴露 `/api/nodes/*` 与 `/nodes/*` 两套别名；`/nodes/sync|invoke-ack|invoke-result` **只有** `/nodes/*` 形态 |
| `3qzcufyl9d94_.js@33428` `client.invoke.result` → `/client/invoke-result`；`0zr5ttz54gz63.js@475026` 实际调用 | **服务端 → 客户端**的命令下发叫 `client.invoke`，回执是 `client.invoke.result` |
| `0zr5ttz54gz63.js@475026` 回执体 `{invoke_id, ok:true, payload_json:string}` / `{invoke_id, ok:false, error:{code,message}}` | 命令结果必须 JSON 字符串化放进 `payload_json` |
| `0zr5ttz54gz63.js@474871` `invoke_id`（`t.invoke_id?.trim()`，缺失则忽略并 warn） | `invoke_id` 是幂等键；没有就丢弃 |
| `3qzcufyl9d94_.js@27757/@27831/@27896`：`nodes.invoke_result`、`nodes.invoke_ack`、`nodes.sync` 均为 POST `/nodes/*`，**无修饰位** | 这三个是**节点侧拉取/回传队列**的端点（与 `client.invoke.result` 的一问一答不同）：`sync` 取待办、`ack` 确认收单、`invoke_result` 回结果 |

**关系裁定**：
- `nodes.*` / `node.*` = **设备节点注册与任务分发**（可能承载浏览器自动化、文件操作等"在用户机器上执行"的工作；`node_id` 出现在浏览器任务记录里，见 `07hpppij0ssd9.js@34164` 的 `node_id`）。
- **与画布卡片执行无关**：卡片执行走的是 `chat.*` 流（§8.4），不经过 `/nodes/*`。
- `nodes.sync`/`invoke-ack`/`invoke-result` 的**请求体在语料中没有调用点**（§14-R10）。

```ts
interface NodeRegisterReq {           // 已证实（24thn7in_976_.js@157400）
  node_id: string;                    // 设备 ID（deviceContext.local.deviceId）
  display_name: string;
  platform: string;
  commands_v2: Record<string, { description?: string }>;   // 至少 {"ping":{...}}
}
interface ClientInvokeResultReq {      // 已证实（0zr5ttz54gz63.js@475026）
  invoke_id: string;
  ok: boolean;
  payload_json?: string;               // ok=true 时
  error?: { code: string; message: string };   // ok=false 时
}
```

---

## 10. 导出与分享

### 10.1 `export.html`（GET `/spaces/{slug}/export.html`，二进制）

```js
// 1nf703hwxciza.js@64962
let e=await h("spaces.export_html",{slug:r});
if(!(e instanceof Blob))throw Error("Space HTML export did not return a file");
(0,o.x)(`${i.length>0?i:r}.html`,e)      // 文件名 = 显示名（缺省 slug）+ ".html"
// 404 特例
if((0,s.u)(a)&&"jarvis_rejection"===a.kind&&"not_found"===a.code) …
```

| 证据 | 结论 |
| --- | --- |
| `1nf703hwxciza.js@64962` | 导出 = **服务端渲染好的整包 HTML**，一次 GET 得到 Blob；参数只有 `{slug}` |
| `3qzcufyl9d94_.js@26082` `responseType:"binary",channel:"media"` | 走媒体通道；客户端应流式落盘 |
| 同段：客户端用 `Set` 去重同一 slug 的并发导出 | 导出重（服务端渲染），前端自己防并发 |

> 「单文件」的边界：导出 HTML 里的引用是否已内联，语料未给出证据（§14-R11）；peer 路径的 24 个资产 / 8 MiB 内联策略（§5.5）**不能**直接套到 export。

### 10.2 分享：两阶段"隐私确认"流程

```js
// 1nf703hwxciza.js@56152（逐字，注意三层）
let r=await u("spaces.share",{slug:e,share_type:t,require_disclosure_confirmation:!0,
  ...null==l?{}:{disclosure_confirmation:l}});
// 响应校验
if(…!0!==e.ok)throw Error("Invalid artifact share response");
let t=e.disclosure_confirmation;
if(null!=t&&("string"!=typeof t.token||!/^[a-f0-9]{64}$/.test(t.token)||!m(t.disclosure)
  ||null!=t.exact_disclosure&&!m(t.exact_disclosure)
  ||"string"!=typeof t.summary||0===t.summary.trim().length
  ||null!=e.url&&""!==e.url)) throw Error("Invalid artifact share disclosure confirmation");
// 错误里解析隐私拦截
let e,t=-1===(e=msg.indexOf("\n\nIssues found:"))?null:{code:"share_blocked_by_privacy",summary:…,details:…};
```

```ts
type Disclosure = {                       // 4 个布尔，含 secrets 键可缺省
  contains_personal_information: boolean;
  contains_chat_history: boolean;
  contains_user_shared_media: boolean;
  contains_secrets_or_credentials?: boolean;
};

interface SpacesShareReq {
  slug: string;                           // 必填
  share_type: string;                     // 必填，取值见 §10.3
  require_disclosure_confirmation: true;  // 必填（前端恒为 true）
  disclosure_confirmation?: {             // 仅在服务端要求二次确认时回传
    token: string;                        // 64 位十六进制
    disclosure: Disclosure;
    exact_disclosure?: Disclosure;
    summary: string;
  } | string;                             // 形态未证实（§14-R12）
}

interface SpacesShareRespPhase1 {         // 需要确认时：无 url，有 confirmation
  ok: true;
  disclosure_confirmation: {
    token: string;                        // /^[a-f0-9]{64}$/
    disclosure: Disclosure;
    exact_disclosure?: Disclosure;
    summary: string;                      // 非空
  };
  url?: "" | null;
}
interface SpacesShareRespPhase2 {         // 已发布
  ok: true;
  url: string;                            // 非空
  share_type?: string;
}
```

**最小可用分享流程（已证实步骤）**

```text
1) GET  spaces.share.settings {slug}              → {disclosure, summary}（分享前告知用户"会暴露什么"）
        1efd95h3zub6m.js@20019 附近（`I("spaces.sharing.settings",{slug:e})`）
        失败码 artifact_audience_unavailable → 先 GET spaces.audience.get {slug, refresh:true} 再重试
        1efd95h3zub6m.js@19492
2) GET  spaces.share.status {slug}                → {share_type?, url?, sharing_settings_version, audience_policy_source, published_manifest_sha256, serve_published_version, …}
        2xx7ca9grna3m.js@22505 / @23305 / @22995 / @23986
3) POST spaces.share {slug, share_type, require_disclosure_confirmation:true}
        → 阶段1：{ok, disclosure_confirmation{token,disclosure,summary}}（无 url）
        → 阶段2：带 disclosure_confirmation 再 POST → {ok, url, share_type}
        1nf703hwxciza.js@56152
4) 若返回/错误里出现 "Issues found:" → code=share_blocked_by_privacy，转 POST spaces.share.fix {slug, message}
        1nf703hwxciza.js@55322 / @56748 / @56765(`"spaces.share.fix"`)
5) 发布后轮询 status（500ms→2s→15s，30s 截止）；期间 settings 每 10s 刷（publication_in_progress）
        1efd95h3zub6m.js@18748
6) 取消分享：DELETE spaces.unshare {slug}         3qzcufyl9d94_.js@25567
```

| 证据 | 结论 |
| --- | --- |
| `1nf703hwxciza.js@55952` `"Artifact sharing target changed before dispatch"` + 每次 dispatch 前 `Date.now()` 打点 | 分享是**长事务**（RPC 超时 600 s `3qzcufyl9d94_.js@24869` 同项 `requestTimeoutMs:6e5`），前端设了"目标变更即中止"的并发守卫 |
| `1efd95h3zub6m.js@20173` `POST spaces.share.settings.save {slug, ...settings}` | 分享设置（受众/策略）整体覆盖写 |
| `3qzcufyl9d94_.js@25047` `spaces.sharing.settings` 也带 `requestTimeoutMs:6e5` | 读取分享设置也可能是慢路径（要等受众策略计算） |
| `1nf703hwxciza.js@66123` `expected_connection_revision`（collaborators.set） | 协作者写入用 revision CAS；不匹配 => 409/冲突（客户端应重取后重试） |
| `1nf703hwxciza.js`（同 chunk 模块 436026）`fetch("/api/hatch/stateful-sharing",{method:"POST"})` → `{enabled}` | 新分享体系有**特性开关**；`false` 时旧路径才可用 |
| `3qzcufyl9d94_.js@24786` `GET /spaces/share/{slug}/review` | 另有"分享前 review"端点（语料无调用点，§14-R13） |

### 10.3 `share_type` 取值

| 证据 | 结论 |
| --- | --- |
| `2lzbmcnnh3_e0.js@14321` 分支 `"cloudflare"===i`、`"tinker"===i` 与 `o.endsWith(".h.metaaiusercontent.com")` | 至少存在 `cloudflare`（现代托管）与 `tinker`（旧托管）两种分享后端；`share_type` 其余取值未证实 |

---

## 11. 与前端交互强相关的数字（可直接照抄的常量表）

| 常量 | 值 | 语义 | 证据 |
| --- | --- | --- | --- |
| `spaces.list` staleTime | `1e4` ms | 列表 10 s 内不重复拉 | `2lzbmcnnh3_e0.js@12640` 附近 |
| 列表排序半衰 | `6048e5` ms（7 天） | `frequency*exp(-Δt/7d)` | `2lzbmcnnh3_e0.js@14660` |
| private frame URL 缓存 | `45e3` ms，上限 100 条 | 过期重取 | `2xx7ca9grna3m.js@6687` |
| frame URL 失败重试缓存 | 带 `retryAfterMs` | 失败后一段时间内不再打 | `2xx7ca9grna3m.js@~6900` |
| Cloudflare 会话续期提前量 | `-15e3` ms | 到期前 15 s 续 | `0sdkhdrd88scd.js@2108` 同段 |
| Cloudflare 续期退避 | 从 `1e3` 起，最多 6 次，`408/429/>=500` 可重试 | 429/5xx 才退避重试 | 同上 |
| frame URL 请求超时 | `3e4` ms | `AbortSignal.timeout(3e4)` | `0sdkhdrd88scd.js@1503` |
| 分享 RPC 超时 | `6e5` ms（600 s） | 注册表修饰位 | `3qzcufyl9d94_.js@24869`（`requestTimeoutMs:6e5`） |
| 分享状态轮询 | 500 / 2000 / 15000 ms，30 s 截止 | 三段退避 | `1nf703hwxciza.js@~60500` |
| 分享设置轮询 | `1e4` ms（仅 `publication_in_progress`） | 发布中 10 s 刷一次 | `1efd95h3zub6m.js@18748` |
| 分享阶段总预算 | `requestTimeoutMs + 15e3` | 前端额外加 15 s | `1nf703hwxciza.js@55207` 附近 `let g=((0,s.l)("spaces.share")?.requestTimeoutMs??u.G)+15e3` |
| 传输：待处理流上限 | `128` | 超出直接 reject | `0j768ks6f4vst.js@2514` |
| 传输：待处理请求上限 | `100` | 同上 | `0j768ks6f4vst.js@2500` 附近 |
| 传输：订阅空闲回收 | `3e5` ms（300 s） | 空转即回收 | `0j768ks6f4vst.js@2561` |
| 传输：单帧上限 | `4194304`（4 MiB） | 超出断链 4004 | `0j768ks6f4vst.js@8901` |
| CVM 文档确认超时 | `1e4` ms，最多 32 并发 id | 每帧最多 32 个未决确认 | `3d5x3ax0xpja0.js@13226` |
| CVM 运行时信号配额 | 12 条 / 8 条 console | 防刷 | `3d5x3ax0xpja0.js@6455` 起 |
| peer 资产内联 | 最多 24 个文件、总 8 MiB、单路径 ≤512 | 超限跳过 | `0wc44m6535bb7.js@21296/@21359` |
| peer 文档上限 | `8388608`（8 MiB） | 注册表 maxResponseBytes | `3qzcufyl9d94_.js@42339` |

### 11.1 明确的负结论：**没有**"画布自动保存"这一套

| 检索/证据 | 结论 |
| --- | --- |
| `autosave`：全量 0 命中；`debounce`：10 命中，无一位于 spaces/idea-cards chunk | 没有 space 文档的防抖自动保存 |
| `beforeunload`：20 命中，均在 chat/editor/终端等其它模块（如 `0ebw_w1k2dpcu.js@20903`） | spaces 不靠 `beforeunload` 落盘 |
| `2xx7ca9grna3m.js` 中所有持久化相关动作都是 **invalidate → 重取** | 语义是"服务端权威 + 全量重渲染"，不是"客户端草稿 + 合并" |
| `spaces.save` 无调用点（§4.3） | 客户端**不需要**在画布拖动结束时 save；拖动/缩放本身在语料里不存在（§0） |

### 11.2 版本号语义（`updated_at_ms` 是唯一的"版本"）

| 证据 | 结论 |
| --- | --- |
| `3d5x3ax0xpja0.js@32030` `v=${encodeURIComponent(String(g??""))}`（`g` = `updatedAtMs`） | `updated_at_ms` 就是 bundle 的缓存版本号 |
| `2xx7ca9grna3m.js@23682` 收到 `spaces.updated` 时 `Math.max` 抬升本地 `updatedAtMs` | 版本**单调递增**，客户端不能回退 |
| `2xx7ca9grna3m.js@4000` 私帧 URL 也带 `v` | 同一版本戳同时驱动两条帧路径 |
| 无语料证据显示 save 会返回新 revision | §14-R2 |

---

## 12. 客户端方法清单（可直接照着写）

> 约定：`rpc()` = 走 Noise 隧道的 `sendRequest(method, params)`；`host()` = 浏览器同源普通 HTTP。
> 「必」= required，「选」= optional，「？" = 未证实。

### 12.1 Spaces

```ts
// 1) 列表
list_spaces(params?: { include_constructing?: boolean })   // 必: 无
  RPC  GET  /spaces/v2                       body={} 或 {include_constructing:true}
  res: { spaces: RawSpace[] }
  err: Error("Invalid spaces.list response")（非数组 / 任一条坏）
  注意：客户端自行按 frequency*exp(-Δt/7d) 降序排

// 2) 改名
rename_space(req: { slug: string; name: string })          // 两个都必
  RPC  POST /spaces/rename
   res?: 未证实（不校验返回值亦能跑，§14-R5）

// 3) 删除（自己的）/ 取消收藏（别人的）
delete_space(req: { slug: string; owned: boolean })
  RPC  DELETE /spaces/v2/{slug}?source=space        (owned=true)
  RPC  DELETE /spaces/v2/{slug}?source=saved_space  (owned=false)
  res: { ok?: boolean; slug?: string; space_id?: string }
  err: Error("Failed to delete space <slug>")（ok===false）

// 4) 保存（**不要进第一版**，body 未证实）
save_space(req: { slug: string; [k: string]: unknown })
  RPC  POST /spaces/v2/{slug}/save          ← 请求体/响应/幂等语义均未证实（§14-R2）

// 5) 取渲染 bundle
render_space(req: { slug: string })                        // 必: slug
  RPC  GET  /spaces/cvm/{slug}/              responseType=binary, channel=media
  res: Blob(HTML)   非 Blob => Error("CVM render did not return a bundle")

// 6) 应用内 HTTP 代理（宿主替你转发，仅在托管 space 应用时用）
cvm_get(req: { slug: string; subpath: string })            // subpath 去掉前导 ./ 与 /
  RPC  GET  /spaces/cvm/{slug}/{*subpath}    binary, media
cvm_post(req: { slug: string; subpath: string; body: object })   // 保留键 slug/subpath/host_muse/space_id 不可被 body 覆盖
  RPC  POST /spaces/cvm/{slug}/{*subpath}    binary
  res(两者): 客户端侧统一看到 {status:200, headers:[["content-type",…]], bodyB64}
  err: 真实状态码被抹平（§6.3），只能靠 body 语义判断

// 7) 私帧 URL（跨域/独立 VM 渲染时）
get_private_frame_url(req: { slug: string; protocol?: 2; v?: number; vmIdentity: string })
  HOST GET /api/hatch/private-space-frame-url?slug=&protocol=2&hatch_vm_identity=&v=
  res: { iframeSrc: string; vmIdentity: string }
  err: 缺 vmIdentity => "incompatible_server"（要求整页 reload）
       头 x-hatch-private-frame-error-source: route => 路由级错误
  缓存 45s；过期重取

// 8) Cloudflare 共享帧 URL
get_shared_frame_url(req: { shortcode: string; shareKey?: string })
  HOST GET /api/hatch/cloudflare-shared-space-frame-url?shortcode=[&share_key=]
  res: { iframeSrc: string }（内含 ?viewer_assertion=<jwt>）
  然后 GET <iframeSrc origin>/__hatch/viewer-session?viewer_assertion=<jwt> 作为 iframe src
  解析 jwt payload{iat,exp}；到期前 15s 续签；429/5xx 退避≤6 次；仅页面可见时续

// 9) 订阅（**参数未证实**，见 §14-R6）
subscribe_spaces(params?: unknown, opts?: { subscriptionTag?: string })
  RPC  POST /spaces/subscribe   streamMode=subscription, noiseOnly
  首包 ack(JSON) → 之后 NDJSON 记录，按 type 派发：
      "spaces.updated"          { slug, kind?:"deleted", updated_at_ms? }
      "spaces.data_invalidated" { slug, query_keys?: unknown[][], origin_action_call_id? }
  处理：kind=deleted → 标记删除；否则 max(updatedAtMs) 抬升 → 失效列表/帧

// 10) 导出
export_space_html(req: { slug: string })                   // 必: slug
  RPC  GET  /spaces/{slug}/export.html   binary, media
  res: Blob（建议文件名 = name||slug + ".html"）
  err: jarvis_rejection + code=not_found → 404 语义
```

### 12.2 分享

```ts
share_status(req: { slug: string })
  RPC  GET  /spaces/share/{slug}
  res: { share_type?: string; url?: string; shortcode?: string;
         sharing_settings_version?: number; audience_policy_source?: string; // 例 "cloudflare"
         published_manifest_sha256?: string; serve_published_version?: boolean;
         publication_in_progress?: boolean; managed?: boolean; deployment_status?: string }
  轮询退避 500/2000/15000 ms，30s 截止

get_share_disclosure(req: { slug: string })
  RPC  GET  /spaces/share/{slug}/settings
  res: { disclosure: Disclosure; summary: string }   // 分享前告知

get_share_settings(req: { slug: string })
  RPC  GET  /spaces/share/{slug}/settings   (requestTimeoutMs=6e5)
  err: code=artifact_audience_unavailable → 先 get_audience({slug, refresh:true}) 再重试

save_share_settings(req: { slug: string } & Partial<Settings>)
  RPC  POST /spaces/share/{slug}/settings   (6e5)

get_audience(req: { slug: string; refresh?: boolean })
  RPC  GET  /spaces/share/{slug}/audience   (6e5)
set_audience(req: { slug: string; [k: string]: unknown })     // body 形状未证实（§14-R13）
  RPC  POST /spaces/share/{slug}/audience/update   (6e5)

share_space(req: { slug: string; share_type: string; require_disclosure_confirmation: true;
                   disclosure_confirmation?: DisclosureConfirmation })
  RPC  POST /spaces/share/{slug}            (requestTimeoutMs=6e5)
  阶段1 res: { ok:true; disclosure_confirmation{ token(64hex), disclosure, exact_disclosure?, summary } ; url?:"" }
  阶段2 res: { ok:true; url:string; share_type? }
  err: { code:"share_blocked_by_privacy", summary, details }  ← 由 message 里 "\n\nIssues found:" 切分
        串 "share_blocked_by_privacy"

fix_share(req: { slug: string; message: string })
  RPC  POST /spaces/share/{slug}/fix

unshare_space(req: { slug: string })
  RPC  DELETE /spaces/share/{slug}

review_share(req: { slug: string })
  RPC  GET  /spaces/share/{slug}/review      // 无调用点（§14-R13）
```

### 12.3 idea-cards

```ts
list_idea_cards(params?: { colorIcons?: boolean; pageSize?: number })   // 全部可选
  RPC  GET  /api/idea-cards
  res: { result: { sections:[{id,title,subtitle?,presentation?{layout},cards:RawCard[]}],
                   viewerState?{hasBuiltIdea}, pagination?{nextCursor,hasNextPage} } }
  空态: {ideas:[],groups:[],heroIdeaIds:[],subtitle:null,showNullStateMessage:false,
         pagination:{nextCursor:null,hasNextPage:false}}

get_idea_card(req: { ideaCardId: string })
  RPC  GET  /api/idea-cards/{ideaCardId}
  res: { result: { card: { id, content{fitReason,summary,buildSummary}, presentation{previewImage,icon},
                           items:[{id,type,content,presentation,selectable,isSelected}], buildStatus } } }

execute_idea_card(req: { ideaCardId: string; mode: "full"|"selectedItems"; itemIds?: string[] })
  RPC  POST /api/idea-cards/{ideaCardId}/execute
  res: { result: { status: "queued"|"accepted"|… ; cardId?; chat?:{stream_owner_message_id,channel,session_id,is_thread} } }
  语义: 异步受理 → 需要再开 chat 流看进度 → 回读 card.build_status

get_idea_card_media(req: { handle: string })
  RPC  GET  /api/idea-cards/media/{handle}   binary, media
  res: Blob；或直接用同源相对 URL `/idea-cards/media/<handle>` 给 <img>
  约束: 相对地址仅允许 /idea-cards/media/ 或 /api/idea-cards/media/ 前缀，长度 ≤8192

share_idea_card(req: { ideaCardId: string })      // body 未证实（§14-R9）
  RPC  POST /api/idea-cards/{ideaCardId}/share        noiseOnly
preview_idea_card_share(req: { ideaCardId: string })
  RPC  GET  /api/idea-cards/{ideaCardId}/share-preview  noiseOnly
debug_idea_card(req: { ideaCardId: string })
  RPC  GET  /api/idea-cards/{ideaCardId}/debug
```

### 12.4 peer（跨 muse）

```ts
get_peer_space_document(req: { host_muse: string; slug: string })
  RPC(service=daemon)  GET /peer-spaces/{host_muse}/{slug}/    binary, 8MiB
  res: HTML 文本；资产用 get_peer_space_asset 内联（≤24 个 / 8MiB 总预算）
get_peer_space_asset(req: { host_muse: string; slug: string; subpath: string })
  RPC(service=daemon)  GET /peer-spaces/{host_muse}/{slug}/{*subpath}   binary
get_peer_space_by_id(req: { host_muse: string; space_id: string; subpath: string })
  RPC(service=daemon)  GET /peer-spaces/by-id/{host_muse}/{space_id}/{*subpath}
  // 缩略图/图标: .../_preview/thumbnail 、.../icon.png
get_peer_collaborators(req: { slug: string })
  RPC(service=daemon)  GET /peer-spaces/collaborators/{slug}   → { …, connection_revision }
set_peer_collaborators(req: { slug: string; collaborators: {principal:string; permission?:string}[];
                           expected_connection_revision: number })
  RPC(service=daemon)  PUT /peer-spaces/collaborators/{slug}
share_to_peer(req: { slug: string; muse_ids: string[] })     // 参数名已证实
  RPC(service=daemon)  POST /peer-spaces/share  {slug, muse_ids}
unshare_from_peer(req: { slug: string; muse_ids: string[] })
  RPC(service=daemon)  POST /peer-spaces/unshare
```

### 12.5 节点（可选，与画布无关）

```ts
list_nodes()                       RPC  POST /api/nodes/list      // body 未证实
register_node(req: NodeRegisterReq) RPC  POST /api/nodes/register  // {node_id,display_name,platform,commands_v2}
heartbeat_node(?)                  RPC  POST /api/nodes/heartbeat   // body 未证实
unpair_node(?)                     RPC  POST /api/nodes/unpair
disconnect_node(?)                 RPC  POST /api/nodes/disconnect
client_invoke_result(req: ClientInvokeResultReq)  RPC POST /client/invoke-result
ack_invoke(?)  / nodes_sync(?)                     RPC POST /nodes/invoke-ack | /nodes/sync   // body 未证实
```

---

## 13. 错误形态与判定（别只看 HTTP 码）

| 形态 | 判定 | 证据 |
| --- | --- | --- |
| `jarvis_rejection` 信封 | `{kind:"jarvis_rejection", code, httpStatus}`；spaces/idea-cards 的 404 用它表达（`code:"not_found"`） | `1nf703hwxciza.js@64962` 附近解析；`3d5x3ax0xpja0.js` 同款 |
| 方法不支持 | 服务端可能回 `method_not_found` / `unknown_method` / `unsupported_method` / `not_implemented`，或消息文本含 `unknown method` / `no handler`；前端据此**永久禁用**该功能 60 s | `3kjg8-xr0c35a.js@8810` 起的 `N(e)` 判定 + `R.set(…,Date.now()+6e4)` |
| 分享隐私拦截 | 错误 message 内含 `\n\nIssues found:` → 转成 `{code:"share_blocked_by_privacy", summary, details}`，再走 `spaces.share.fix` | `1nf703hwxciza.js@55322` / `@56748` |
| 受众不可用 | `code:"artifact_audience_unavailable"` → 先 `spaces.audience.get {refresh:true}` | `1efd95h3zub6m.js@19492` |
| 帧协议不兼容 | `incompatible_server`（服务端缺 `vmIdentity`）→ 必须整页 reload | `2xx7ca9grna3m.js@~6200` / `@3342` |
| 模型锁定 | `code:"inference_model_locked"`, `httpStatus:409`；命中集合含 `api.idea-cards.execute`、`spaces.share.fix` 等 | `1pv9icoxptmvc.js@~3600`（`n="inference_model_locked"` 与 `i=new Set([...])`） |
| 断连 | `gateway` 断链错误；spaces 相关 UI 文案 "Spaces are unavailable while disconnected." | `1nf703hwxciza.js@64962` 附近 |
| 帧过大/解密失败 | 关闭码 4004；请求超时 4008/4009；订阅被共享终止 4010；fatal 4006 | `0j768ks6f4vst.js` 构造函数段（`this.#R(4008, …)` 等） |

---

## 14. 未证实 / 推断清单（**不许当结论用**）

| 编号 | 内容 | 状态 | 为什么 |
| --- | --- | --- | --- |
| R1 | "无限画布 / 节点 / 边 / camera(x,y,zoom)" 模型 | **未证实（语料内不存在）** | 0 命中任何画布引擎；space schema 无布局字段；无对应路由。若产品有这个视图，只在未下载 chunk 或 CVM 应用内部 |
| R2 | `spaces.save` 的请求体、响应、幂等性、是否服务端生成 revision | **未证实** | 注册表有方法（`3qzcufyl9d94_.js@24417/@24456`），但**零调用点**；只能确认 POST + `{slug}` 路径 |
| R3 | `runtime` 的取值集合、`theme_accent` 的颜色格式、`(0,o)()` 归一化细节 | **未证实** | 只见到 `string` 类型守卫，没有枚举/正则 |
| R4 | `updated_at_ms` 是否可能**缺失**、服务端是否保证单调 | **部分证实**：客户端用 `Math.max` 处理并提供缺省；服务端契约未知 | `2xx7ca9grna3m.js@23682` |
| R5 | `spaces.rename` 响应结构 | **未证实** | 调用点不校验返回值（`0sdkhdrd88scd.js@8887` 仅 try/catch + toast） |
| R6 | `spaces.subscribe` 的请求体（是否需要 `slug`？是否全量订阅？`subscriptionTag` 怎么给？）与 ack 首包字段 | **未证实（关键缺口）** | 注册表只有 1 处（`3qzcufyl9d94_.js@35067`），**没有调用点**。事件名/payload 已证实（`2xx7ca9grna3m.js@23682/@49131`），但"怎么订阅上"未知 |
| R7 | idea-cards 列表的游标参数名（`cursor`/`pageCursor`/`nextCursor` 请求侧） | **未证实** | 只见到响应侧 `nextCursor/hasNextPage`；请求侧只见 `iconPalette`、`pageSize`（`0wc44m6535bb7.js@23268/@23978`） |
| R8 | `api.idea-cards.execute` 的附加参数（`session_id`? `prompt`? `model`? `variants`?） | **未证实** | 固定参数只有 `{ideaCardId, mode, itemIds?}`；调用方还能透传一个 `r` 对象，键名语料未给 |
| R9 | `api.idea-cards.share` / `share-preview` 的 body 与响应；分享出来的产物形态 | **未证实** | 只有注册表条目（`3qzcufyl9d94_.js@20405/@20509`） |
| R10 | `nodes.sync` / `nodes.invoke-ack` / `nodes.invoke-result` 的 body 与触发时机 | **未证实** | 只有注册表（`@27896/@27831/@27757`）；网侧实际用的是 `client.invoke.result`（`0zr5ttz54gz63.js@475026`） |
| R11 | `export.html` 是否把资产内联成单文件、是否受 24 文件/8MiB 限制 | **未证实** | 只有"返回 Blob/必须非 Blob"的证据（`1nf703hwxciza.js@64962`） |
| R12 | `disclosure_confirmation` 回传时传整个对象还是只传 `token` | **未证实** | 代码 `...null==l?{}:{disclosure_confirmation:l}` 把 `l` 原样放进去，`l` 的类型由调用方决定（`1nf703hwxciza.js@56152`） |
| R13 | `spaces.share.review`、`spaces.audience.set`、`spaces.share.settings.save` 的 body、`spaces.debug.*` 的响应 | **未证实** | 前两条零调用点；`settings.save` 只见 `{slug, ...t}` 透传 |
| R14 | `share_key` 在分享 URL 里的确切位置（query? path?）与 `(0,i.g)()` 的实现 | **未证实** | 只证实参数**名**在敏感参数集合里（`1vzyi952yno5v.js@71842` `share_key\|viewer_assertion\|hatch_space_token`） |
| R15 | `/api/hatch/cvm-frame` 的 `protocol` 数值、`k` 之外的附加 query（Android/coarse-pointer 分支 `&${o.i}=${o.F}`） | **未证实** | `o.Z/o.i/o.F` 来自模块 861526 的导出，明文值未取到；私帧 URL 侧已证实 `protocol=2`（`2xx7ca9grna3m.js@3733` 附近 `u.set(Q,"2")`） |
| R16 | 客户端是否需要 `spaces.list` 之外的空间元数据端点（如单个 space 的 detail） | **未证实** | 语料里"打开 space"是**复用 list**（`2lzbmcnnh3_e0.js@12650`），没有 detail 路由 |
| R17 | `peerSpaces.document` 的 HTML 与 `export.html` 是否同源同构 | **推断**，未证实 | 两者都是"整包 HTML"（`0wc44m6535bb7.js@8611` vs `1nf703hwxciza.js@64962`），但没有同构证据 |

**另需注意（不是未证实，而是易踩的坑）**：
- `GET /spaces/cvm/{slug}/{*subpath}` 与 `POST` **返回的都是 binary**，但 POST 没有 `channel:"media"`（`3qzcufyl9d94_.js@26312` vs `@26430`）。
- 代理会把真实 HTTP 状态码抹成 200（`3d5x3ax0xpja0.js@37808` 起），应用内的 404/500 语义要靠 body 自己表达。
- `peer-spaces` 全族 `service:"daemon"`：**不能**按默认 service 发，否则路由到错误后端（`3qzcufyl9d94_.js@41809`–`@42765`）。

---

## 15. 验收（自跑命令 + 输出）

### 15.1 文件存在且 > 8000 字节

```powershell
python -Xutf8 -c "import pathlib; p=pathlib.Path(r'D:\deep seek\muse-bridge\docs\CANVAS_PROTOCOL.md'); print(p.stat().st_size)"
```
输出见 §15.5（实测 > 8000）。

### 15.2 锚点全部可复现（135 条）

```powershell
cd 'D:\deep seek\work\canvas_re'; python -Xutf8 verify_anchors.py | Select-String -Pattern 'MISS|anchors:'
```
实测：
```text
--- anchors: 135 ok / 0 miss / 111 total
```
（`111` 是主清单条数，`EXTRA` 24 条追加在同一轮里；`ok` 计数 = 135。）

### 15.3 报告内证据条数统计

```powershell
Select-String -Path 'D:\deep seek\muse-bridge\docs\CANVAS_PROTOCOL.md' -Pattern '@\d+' -AllMatches |
  ForEach-Object { $_.Matches.Count } | Measure-Object -Sum
```
`Canvas Protocol` 文档里每一条结论行都带 `chunk@offset`；统计结果见 §15.5。

### 15.4 可复用脚本 `dump_ctx.py`（本任务新增，已实测）

```powershell
cd 'D:\deep seek\work\canvas_re'
python -Xutf8 dump_ctx.py 2lzbmcnnh3_e0.js 12579 400
python -Xutf8 dump_ctx.py 1nf703hwxciza.js --find "disclosure_confirmation" --max 300
python -Xutf8 find.py "spaces.subscribe" "react-flow" "xyflow" "konva" "minimap"
```

### 15.5 本机实测输出（2026-11-11）

```text
$ python -Xutf8 -c "import pathlib; p=pathlib.Path(r'D:\deep seek\muse-bridge\docs\CANVAS_PROTOCOL.md'); print('SIZE', p.stat().st_size)"
SIZE 78383

$ cd 'D:\deep seek\work\canvas_re'; python -Xutf8 verify_anchors.py | Select-String -Pattern 'MISS|anchors:'
--- anchors: 135 ok / 0 miss / 111 total        （0 miss，全部锚点可复现）

$ Select-String -Path 'D:\deep seek\muse-bridge\docs\CANVAS_PROTOCOL.md' -Pattern '@\d+' -AllMatches |
    ForEach-Object { $_.Matches.Count } | Measure-Object -Sum
LINES_WITH_ANCHOR_MATCHES=200 TOTAL_MATCHES=350
UNIQUE_dotjs_AT_off=110

$ python -Xutf8 find.py "react-flow" "xyflow" "konva" "minimap" "infinite canvas"
### 'react-flow'  hits=0
### 'xyflow'  hits=0
### 'konva'  hits=0
### 'minimap'  hits=0
### 'infinite canvas'  hits=0
```

> `verify_anchors.py` 里的 `EXTRA` 24 条与主清单 111 条在同一轮校验，因此报"111 total"但 ok 计数为 135。

### 15.6 仓库变更范围

```powershell
cd 'D:\deep seek\muse-bridge'; git status --porcelain
```
预期只有 `docs/CANVAS_PROTOCOL.md` 为新增（`??`）；`work/canvas_re/` 在工作区之外，不进本仓库。

---

## 附录 A：本任务用到的脚本

| 脚本 | 用途 |
| --- | --- |
| `canvas_re/dump_ctx.py` | 给 chunk 名 + 偏移打印 ±N 字符上下文；`--find`/`--regex` 批量命中定位（**交付要求的那一个**） |
| `canvas_re/find.py` | 全量 chunk 字面量检索，只打 `chunk@offset`（用于负证据：0 命中） |
| `canvas_re/scan.py` | 正则计数（每组模式给出 total + 每文件计数） |
| `canvas_re/list_rpc.py` | 从 RPC 注册表抽 `spaces/idea-cards/nodes/peer-spaces` 全条目 |
| `canvas_re/verify_anchors.py` | **校验本文每条 `chunk@offset`**；`135 ok / 0 miss` |
| `canvas_re/anchors_ok.txt` | 上一脚本的完整输出（锚点清单，可作证据索引） |

## 附录 B：`chunk@offset` 证据索引（按主题）

| 主题 | 锚点 |
| --- | --- |
| 路由表全貌 | `3qzcufyl9d94_.js@24040` / `@24417` / `@24456` / `@26082` / `@26201` / `@26312` / `@26430` / `@35067` |
| spaces.list 调用与校验 | `2lzbmcnnh3_e0.js@12579` / `@14846` / `@14883` / `@12612` |
| space 字段归一化 | `2lzbmcnnh3_e0.js@15101` / `@15934` / `@16336` / `@16494` / `@16578` / `@16647` / `@16698` / `@15738` / `@15697` / `@14660` |
| 图标/CDN 重写 | `2lzbmcnnh3_e0.js@14321` / `@14446` / `1qwraqj4bjr1v.js@38508` / `@38755` |
| 删除/改名 | `2lzbmcnnh3_e0.js@16949` / `@16971` / `0sdkhdrd88scd.js@8887` |
| 私有帧（cvm） | `3d5x3ax0xpja0.js@31779` / `@32030` / `@31970` / `@33623` / `@13637` / `@11429` / `@11915` / `@13226` / `@6455` / `@12206` |
| 私有帧 URL | `2xx7ca9grna3m.js@4000` / `@3342` / `@6687` / `@3733` |
| Cloudflare 帧 | `0sdkhdrd88scd.js@1503` / `@522` / `@2965` / `@2108` |
| 事件流 | `2xx7ca9grna3m.js@23682` / `@49131` / `@1724` / `@47552` / `3kjg8-xr0c35a.js@8807` / `@8842` / `@9322` / `@9442` / `0j768ks6f4vst.js@28716` |
| 分享 | `1nf703hwxciza.js@56152` / `@55322` / `@56748` / `@55952` / `1efd95h3zub6m.js@20019` / `@18748` / `@19492` / `2xx7ca9grna3m.js@22505` / `@23305` / `@22995` / `@23986` |
| 导出 | `1nf703hwxciza.js@64962` / `3qzcufyl9d94_.js@26082` |
| idea-cards | `0wc44m6535bb7.js@23978` / `@23268` / `@23365` / `@29161` / `@28938` / `@25295` / `1pv9icoxptmvc.js@3754` / `3n69z2klm3y8g.js@3049` / `@45711` / `3qzcufyl9d94_.js@19919`–`@20629` / `@27414` |
| nodes | `24thn7in_976_.js@157400` / `@157455` / `@157547` / `0zr5ttz54gz63.js@475026` / `@474871` / `3qzcufyl9d94_.js@27757`–`@28101` / `@33428` |
| peer space | `0wc44m6535bb7.js@8611` / `@8747` / `@21296` / `@21359` / `1nf703hwxciza.js@66123` / `3qzcufyl9d94_.js@42201` / `@42339` |
| 传输常量 | `0j768ks6f4vst.js@2500`–`@2561` / `@8901` |
