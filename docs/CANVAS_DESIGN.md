# 无限画布（Canvas）设计与 API 契约（0.5.0 任务书）

> 目标：在 `muse-bridge` 上自研一套**无限画布** Web UI，视觉精良、零 npm 构建、
> 由 bridge 直接托管；协议上完全适配 muse——生成走 muse（当前浏览器驱动，
> 逐步切纯协议），画布文档可**本地保存**，并预留与 muse 原生 Spaces（`/spaces/v2`）
> 的导入/导出适配。
>
> 本文件是**实现契约**：后端与前端各自照此实现，不许单方面改字段。字段一旦上线，
> 只能加不能改语义；需要变更时在文末“变更记录”加一行。

## 1. 目录与归属

| 路径 | 归属 | 说明 |
|---|---|---|
| `muse_bridge/canvas/` | 前端 | 纯静态资产：`index.html`、`css/`、`js/`、`assets/`。**零外部依赖、零构建**，ES Module + `<script type="module">`。 |
| `muse_bridge/canvas_store.py` | 后端 | 画布空间存储（SQLite，复用 bridge 数据目录）。 |
| `muse_bridge/canvas_api.py` | 后端 | FastAPI `APIRouter`：`/canvas/api/*` + 托管 `/canvas` 静态目录（`/canvas/assets/*`）。 |
| `app.py` 挂载 | Lead | `from .canvas_api import router; app.include_router(router)`。 |
| `tests/test_canvas_store.py` / `tests/test_canvas_api.py` | 后端 | 离线测试（asyncio_mode=auto，假 store 不碰真库）。 |

硬约束（沿用仓库惯例）：
- Python 3.11、ruff line-length 88、select `E,F,I,UP,B,SIM`；LF-only（`tests/test_line_endings.py` 会扫 `.py`，前端 `.js/.css/.html` 不在扫射范围但也要 LF）。
- 不新增第三方 Python 依赖；前端**不引入任何 npm 包**、不引 CDN 运行时依赖（字体/图标可以内联 SVG 或本地静态文件）。
- 不把任何用户 cookie/token 写进画布文档或日志。

## 2. 视觉与交互目标（前端验收口径）

- 暗色主题，玻璃拟态（`backdrop-filter: blur(18px)` + 半透明面板 + 1px 内描边），无限点阵网格（随缩放变化密度）。
- 顶部悬浮工具栏：返回/空间名/保存状态/缩放控件/撤销重做/导出/分享入口。
- 左侧（可选）空间列表抽屉；右侧检查器面板（选中节点的 prompt、模型、尺寸、状态、操作）。
- 底部提示词输入条：输入 → 在画布中心或选中位置生成卡片，卡片立即出现并进入 `queued/generating` 状态。
- 画布交互：滚轮平移 + `Ctrl/Cmd+滚轮` 缩放（以光标为锚点）、空格拖拽平移、拖拽框选、多选、节点拖动/缩放/删除、`Del`、`Ctrl+D` 复制、`Ctrl+Z/Ctrl+Shift+Z` 撤销重做、双击空白快速新建、`Ctrl+0` 适配全部、`Home` 回原点。
- 卡片类型：`image`（成品，支持图片查看器/大图预览）、`prompt`（提示词占位卡）、`text`（便签）、`video`（视频成品，含播放）。
- 图片生成状态：`queued`（排队）、`generating`（骨架 + shimmer + 已耗时）、`done`（渐进加载）、`failed`（错误原因 + 重试按钮）。
- 性能：100+ 节点仍流畅；图片用 `decode()` 后绘制；节点 DOM 虚拟化按视口裁剪（只渲染视口内 + margin 的节点）。
- 可访问性：所有按钮有 `aria-label`，键盘可达；对比度 ≥ 4.5:1（正文）。
- 移动端触控不强制，但 pinch 手势要能用。

## 3. 后端 API 契约（`/canvas/api`）

所有响应 JSON；错误统一 `{"error": "...", "note": "..."}` + HTTP 4xx/5xx。认证沿用 bridge 现有 `require_key`（本地 keyless 时放行）。

### 3.1 空间 CRUD

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| `GET /canvas/api/spaces` | — | `{"spaces":[SpaceMeta]}` |
| `POST /canvas/api/spaces` | `{"name": "..."}` | `{"space": SpaceMeta}` |
| `GET /canvas/api/spaces/{id}` | — | `{"space": SpaceMeta, "doc": Doc}` |
| `PUT /canvas/api/spaces/{id}` | `{"name"?: str, "doc": Doc}` | `{"space": SpaceMeta}` |
| `DELETE /canvas/api/spaces/{id}` | — | `{"ok": true}` |

`SpaceMeta`：
```json
{"id": "sp_ab12cd34", "name": "未命名画布", "created_at": 1794342067,
 "updated_at": 1794342067, "node_count": 3, "thumbnail": null,
 "source": "local"}
```
- `id`：服务端生成，`^sp_[a-z0-9]{8}$`；客户端只读。
- `source`：`local`（本库）或 `muse:<slug>`（从 muse Spaces 导入后的镜像）。
- `thumbnail`：可空；画布缩略图 URL 或 base64 dataURL（先行留空，前端自己根据 doc 渲染）。
- 未知 id → 404 `{"error":"not_found"}`。
- `PUT` 的 `doc` 必须在服务端做 schema 校验（见 §5）：不合法 → 400 `{"error":"invalid_doc","note":"<具体原因>"}`，**不落库**。

### 3.2 生成（复用现有端点，不新增代理）

画布前端直接调用 bridge 既有接口（同源、无需新契约）：
- 建任务：`POST /v1/images/tasks`（body 同 `/v1/images/generations`，`prompt/model/size/references...`）→ 返回 `task_id`。
- 轮询：`GET /v1/images/tasks/{task_id}` → `status` 取 `jobs.py` 的**六态**
  `queued|processing|completed|failed|cancelled|unknown`（`succeeded/success/done`
  只是输入侧口语，`_normalize_state` 会映射成 `completed`）；`url`、`error`。
- 图片成品 URL 形如 `/v1/media/<token>`。但 `/v1/media` 的 `require_key` **只认**
  `Authorization: Bearer`，`<img>` 与离屏 `drawImage` 发不了请求头，所以画布统一改走
  `GET /canvas/api/media/{token}`（见下）：同字节、真实 `Content-Type`、
  `Content-Disposition: inline`、支持 `Range`、**不要求鉴权**（token 本身是不透明能力串）。
- 若需要画布专属参数（节点 id、画布坐标），由前端在任务成功后写回节点 `data`，不污染上游任务表。

### 3.2.1 媒体代理

| 方法/路径 | 说明 |
|---|---|
| `GET /canvas/api/media/{token}` | 画布专用媒体出口。200 返回真实 mime + inline；`Range` → 206 + `Content-Range`；未知 token → 404 `{"error":"not_found","note":"媒体不存在"}`。目的只有两个：让 `<img>` 能显示、让导出 PNG 不被跨源 taint。

### 3.2.2 只读分享

| 方法/路径 | 鉴权 | 说明 |
|---|---|---|
| `POST /canvas/api/spaces/{id}/share` | Bearer | 创建/轮换分享 token：`{"share":{"token":"sh_<32 hex>","space_id","created_at","url":"/canvas/?share=..."}}`。同空间**只保留一个**活跃 token，再调用会作废旧链接。 |
| `GET /canvas/api/spaces/{id}/share` | Bearer | 查询当前分享；未分享回 `{"share": null}`（GET 无副作用）。 |
| `DELETE /canvas/api/shares/{token}` | Bearer | 撤销；幂等，未知 token 回 `{"ok": false}`。 |
| `GET /canvas/api/shares/{token}` | **免鉴权** | 读分享画布：`{"space","doc","share","read_only":true}`；无效/已撤销/空间已删统一 404。 |

语义与边界：

- token 是 128 位随机 capability，落 SQLite `canvas_shares`，**重启 bridge 仍有效**；
- 读口故意免 key：分享对象是没有 bridge key 的人；写权限仍只在 `/spaces/{id}`，
  不存在"凭 token 写"的入口；
- 前端 `?share=<token>` 进入**只读模式**：藏起空间菜单/保存/撤销/重做/设置/提示词/
  检查器，保留缩放、平移、选中、大图查看与 PNG 导出；写类 action 与写类快捷键
  统一弹「只读分享」拒绝（按钮隐藏不是安全边界）。**手势路径同样要守**（2026-11-11
  探针抓到漏网）：空白双击新建便签、节点拖动（含 z-order）、`[data-handle]` 缩放、
  节点操作按钮都必须检查 `readOnly`——双击那条曾经直接调 `_createTextNode`
  绕过 action guard，实测节点 323→324（`qa_canvas_readonly.py` 已回归为 323→323）。
- 删除空间连带删除其 token，撤销分享后旧链接 404。
- **前端链接管理 UI（2026-11-11 11:3x 补齐）**：工具栏「分享」不再"点一下就静默
  轮换 token"，而是打开 `#share-panel` 面板（`ui.js` `openSharePanel` /
  `renderSharePanel`）：
  - 未分享：引导「生成分享链接」；
  - 已分享：只读 URL 输入框（可整段选中复制）+ 状态/创建时间/权限说明 +
    「复制链接 / 重新生成 / 撤销分享」三动作；
  - 重新生成与撤销都先走 `ui.confirm()` 二次确认（不可恢复操作，且旧链接会立即
    失效）；撤销调用 `DELETE /canvas/api/shares/{token}`，成功后面板回到"未分享"；
  - 读取失败单独渲染错误态 + 重试按钮，不把失败吞成空状态；
  - 只读分享页（`?share=`）仍是点击即复制手里那条链接（没有写通道）。
  前端零构建：改 `js/*.js`、`index.html`、`css/ui.css` 后刷新页面即可。

### 3.3 muse Space 只读适配（P3 已落地，2026-11-11 10:2x）

> 实现：`muse_bridge/muse_spaces.py`（只读协议客户端）+ `canvas_api.py` muse 段
> （鉴权与既有画布 API 同源 `MUSE_BRIDGE_API_KEYS`）。前端不依赖它们也能完整用
> 本地画布；调用方 Bearer 头缺失时按既有语义 401。

| 方法/路径 | 契约 |
|---|---|
| `GET /canvas/api/muse/spaces?include_constructing=false` | `200 {"spaces":[SpaceListItem...]}`；字段按 `CANVAS_PROTOCOL.md` §2.1 归一化（缺失=null，`is_shared` → `is_saved_foreign_share`，不重排上游顺序）。无账号库/无 cookie → `503 {"error":"muse_unavailable","note":...}`；协议失败 → `502 muse_session_expired / muse_egress_blocked / muse_protocol_error / muse_invalid_response`；`429 muse_rate_limited`。 |
| `POST /canvas/api/muse/import` `{"slug":"<muse-slug>"}` | `200 {"node":{...}, "space":{...}}`：**只导入预览图/名称/链接**，不写 muse、不写本地库（前端拿 `node` 自行去重后 PUT）。无预览图时 `type:"text"`（w=360/h=160），链接在 `data.muse.viewUrl`；有预览图时 `type:"image"`（w=1200/h=630），`url/thumbUrl` 指向预览图，`data.muse` 带 `spaceSlug/viewUrl/shortcode/updatedAtMs/constructionStatus`。id=`n_`+10 位 hex，满足 §4 `NODE_ID_RE`。`include_constructing=true` 由服务端固定传，构建中的 Space 也能导入。`400 invalid_slug` / `404 not_found`。 |
| `GET /canvas/api/muse/export/{slug}` | `200 application/octet-stream` + `Content-Disposition: attachment; filename="<slug>.html"` + `X-Content-Type-Options: nosniff` + `Content-Security-Policy: sandbox` + `Cache-Control: private, no-store` + `X-Muse-Export-Mime`。**绝不 inline `text/html`**（导出 HTML 与本服务同源，可读 localStorage 里的 bridge key 进而同源调 `/admin/*`、`/v1/*` 与分享；即使某层忽略 disposition，octet-stream+nosniff 也会挡住解析）。不走 media store token（`/canvas/api/media/{token}` 对图片 inline，HTML 走那条等于换 token 重开同一 XSS 面）。文件名只由校验过的 slug 派生。 |
| `GET /canvas/api/muse/idea-cards` | `200 {"sections":[...],"viewerState":...,"pagination":...}`：muse Explore 灵感卡只读列表；`iconUrl`/`iconVariants[].url` 已改写成同源代理地址。 |
| `GET /canvas/api/muse/idea-cards/{ideaCardId}` | `200 {"card":{...含 items}}` 详情。 |
| `GET /canvas/api/muse/idea-cards/media/{handle}` | 免 Bearer（`<img>` 发不了头，与 `/canvas/api/media/{token}` 同构）；栅格 mime 白名单 inline（**显式排除 SVG**，其可执行脚本），SVG/HTML/未知 mime → octet-stream + attachment；按文件头嗅探优先于声明 mime。 |

**真机证据（2026-11-11 09:5x，见 `docs/PROTOCOL.md` §1.6.2）**：
`GET /spaces/v2` 200 `{"spaces":[]}`（该账号 0 Space）；`GET /spaces/v2/{slug}` 404
`space_unavailable`（单 Space 路由存在）；`GET /spaces/{slug}/export.html` 404
`web artifact was not found`，而 `/export` 是 `route not found`（只有 `.html` 形态）；
`GET /api/idea-cards` 200 / 67,759 B / 7 sections；detail 1,877 B；icon media
200 `image/png` / 1,045 B（PNG 魔数已验证）。

## 4. 画布文档 `Doc`（版本化，前端唯一真相）

```ts
interface Doc {
  version: 1;
  camera: { x: number; y: number; zoom: number };   // world→screen: screen = (world - camera.xy?) 见 §6
  nodes: Node[];
  edges: Edge[];
  meta: { theme: "dark"; background: "dots"; gridSize: number };
}

type NodeType = "image" | "video" | "prompt" | "text";

interface Node {
  id: string;            // ^n_[a-z0-9]{10}$
  type: NodeType;
  x: number; y: number;  // world 坐标，左上角
  w: number; h: number;  // world 尺寸（>= 80px）
  z: number;             // 叠放层级，整数
  data: NodeData;       // 见下
}

interface NodeData {
  prompt?: string;
  status?: "idle" | "queued" | "generating" | "done" | "failed";
  url?: string;          // 成品媒体 URL（一般是 /v1/media/<token>）
  thumbUrl?: string;
  width?: number; height?: number;
  mime?: string;
  error?: string;
  taskId?: string;
  model?: string;
  params?: Record<string, string | number | boolean>;  // 生成参数快照
  createdAt?: number; updatedAt?: number;
  text?: string;         // text 节点的正文
  muse?: { spaceSlug?: string; ideaCardId?: string; taskNodeId?: string };
}

interface Edge { id: string; from: string; to: string; }
```

约束（服务端校验，前端也自查）：
- `version` 必须为 `1`；`nodes` ≤ 2000；单 Doc JSON ≤ 4 MB。
- `id` 唯一；`edge.from/to` 必须指向存在的节点；`type` 必须在枚举内；`w/h` 有限且 > 0。
- `camera.zoom` ∈ [0.05, 8]；`camera.x/y` 有限。
- 未知字段**保留并透传**（后端按 dict 存 JSON，不做有损转换），以便未来扩展。
- 顺序无关；服务端保存时按 `updated_at` 记录。

## 5. 存储

- SQLite 表 `canvas_spaces(id TEXT PRIMARY KEY, name TEXT, doc_json TEXT, created_at INTEGER, updated_at INTEGER, source TEXT)`，建在现有 bridge 数据库（`store()` 同一个 DB 路径），用 `CREATE TABLE IF NOT EXISTS`；不碰其它表。
- 写入用单条 `INSERT ... ON CONFLICT(id) DO UPDATE`，`updated_at` 单调递增（秒级）。
- 并发：FastAPI 单进程 + SQLite WAL；每次操作独立连接/事务，不缓存全局写锁（但同一空间的并发 PUT 以最后写入为准）。
- 测试用内存 SQLite（`:memory:`）或 tmp_path 文件，不碰真实 `data/muse.db`。

## 6. 坐标约定（前后端必须一致）

- 世界坐标 `world`，屏幕坐标 `screen`，视口原点在画布容器左上角：
  `screen = { x: (world.x - camera.x) * zoom, y: (world.y - camera.y) * zoom }`
  `world  = { x: screen.x / zoom + camera.x, y: screen.y / zoom + camera.y }`
- 节点 `x/y` 是**世界坐标下的左上角**；渲染时 `transform: translate(world.x*zoom, world.y*zoom) scale(zoom)` 或统一父层 `translate(-camera.x*zoom, -camera.y*zoom) scale(zoom)`（前端实现任选一致即可）。
- 缩放锚点公式：保持光标下的世界点不动：
  `camera.x' = worldAtCursor.x - (cursorScreen.x) / zoom'`，y 同理。
- 网格：点阵间距 `gridSize=28`（世界单位），渲染时按 `zoom*gridSize` 取整，过密/过疏自动 2x/0.5x 阶梯。
- 初始状态：`camera = {x:-200, y:-120, zoom: 1}`，`gridSize: 28`。

## 7. 前端模块划分（建议，不强制）

```
muse_bridge/canvas/
  index.html
  css/base.css          # 主题变量、重置、布局
  css/canvas.css        # 画布层、网格、节点、连线
  css/ui.css            # 工具栏、抽屉、检查器、提示条、toast
  js/app.js             # 入口：装配 store/router/renderer
  js/store.js           # 状态 + undo/redo + 订阅
  js/view.js            # camera / 坐标换算 / 命中测试
  js/nodes.js           # 各类节点视图（DOM）
  js/api.js             # bridge API 客户端（spaces CRUD / tasks 轮询）
  js/storage.js         # 离线 localStorage 适配（无 bridge 时）
  js/ui.js              # 工具栏/检查器/提示条/快捷键面板
  js/export.js          # PNG 导出（canvas draw + 图片缓存）
  assets/icons.svg      # 内联图标 sprite（手写，不引三方库）
```

## 8. 测试与验收

后端（必须）：
1. `tests/test_canvas_store.py`：CRUD、schema 校验失败不落库、updated_at 递增、4MB/2000 节点上限、并发写不丢数据。
2. `tests/test_canvas_api.py`（httpx `ASGITransport` 或 FastAPI TestClient，同 `tests/test_generation_api.py` 模式）：CRUD 全链路、404/400/413、`/canvas` 返回 `text/html` 且包含 `canvas.js`、静态资产 200 + `content-type` 正确、越界路径 404。
3. `ruff check` 全绿；`pytest tests/test_canvas_store.py tests/test_canvas_api.py tests/test_canvas_media_proxy.py -q` 全绿；全量 `pytest -q` 除 `docs/KNOWN_ORACLE_CONFLICTS.md` 两条已知红外无新增失败。

前端（必须）：
1. `index.html` 在浏览器打开（或 `GET /canvas`）无 console error，首屏 < 1s（本地无远程资源）。
2. 交互自测清单：平移、缩放（光标锚点）、框选、多选拖动、删除、撤销重做、双击新建、生成卡片（mock 或真实任务）、图片查看器、导出 PNG、刷新后恢复（localStorage 或 bridge）。
3. 视觉：截图 1 张（1440×900）保存到 `D:\deep seek\work\canvas_re\ui-shot.png`，Lead 亲自看。

> 口径修正：断言静态入口时用 `js/app.js`（`index.html` 里就是它），不要写 `canvas.js`。

## 9. 分阶段实施

1. **P1 后端骨架**：store + API + 挂载 + 测试（不依赖 muse）。
2. **P2 前端画布**：静态 UI + 本地存储 + 生成接线 + 导出。
3. **P3 muse Spaces 适配**：按 `docs/CANVAS_PROTOCOL.md` 落地 list/import/export（协议逆向完成后）。
4. **P4 真机验收 + 视觉打磨**。

## 10. muse 端"无限画布"逆向结论与 P3 修订（2026-11-11 04:5x 实测）

> 一句话：**muse 没有"无限画布"这种文档型产品**。用户说的 `Spaces` 是"AI 生成的
> Web 应用构件"（server-side CVM 渲染成 HTML，同源 iframe 装载 + Cloudflare 发布），
> 与 x/y/zoom/nodes/edges 无关。因此本项目画布是**自研产品层**，“适配 muse”落在
> 生成能力与 Spaces 列表/导出上，而不是复制它的文档模型。

证据（两条独立来源）：

1. 前端 chunk 全量取证（`docs/CANVAS_PROTOCOL.md`，78,999 B，135 个锚点校验通过）：
   - `2lzbmcnnh3_e0.js@15101`–`@16698` 的 Space 归一化字段表里**没有**任何坐标/节点/
     边/相机字段；`3d5x3ax0xpja0.js@31779` 显示内容由 `spaces.cvm.render {slug}`
     返回 **Blob(HTML)**，前端用 messagePort 隧道装进 iframe（`cvmBootstrapReady` →
     `install-bundle` → `document-ready` → `install-port`）；
   - 全仓 `react-flow|xyflow|konva|minimap|infinite canvas` 命中 **0**；
   - 无 autosave / debounce / beforeunload 落盘，唯一"版本"是服务端 `updated_at_ms`。
2. 本机活网实测（纯协议，读为主，写只碰一次性 slug）：
   - `GET /spaces/v2?include_constructing=true` → `200 {"spaces":[]}`；
   - `POST /spaces/v2/mb-canvas-probe-20261111/save`（`{}`、`{name}`、`{name,version,camera,nodes,edges}`
     三连）→ **404 `{"ok":false,"error":{"code":"not_found","message":"shared artifact not found"}}`**
     —— 说明 save 只对"已存在的 artifact"生效，创建发生在 frame app 内部，不在网关；
   - `DELETE /spaces/v2/{slug}` → 404 `"web artifact not found"`（两套词汇：shared vs web artifact）；
   - `GET /api/idea-cards` → `200 {"ok":true,"result":{"sections":[...]}}`（Explore 灵感卡，
     不是画布节点；`execute` 返回异步 chat 三元组，触发一次 AI 搭建而非出图）。

P3 修订（原 §3.3 的 where 保留、what 缩水）：

- **保留**：`GET /canvas/api/muse/spaces`（列出用户 muse Spaces，字段按
  `CANVAS_PROTOCOL.md` §2 归一化）；`POST /canvas/api/muse/import` 改为
  **导入 Space 的预览图 / 名称 / 链接**作为画布卡片（`preview_image.thumbnail_url`），
  不承诺文档级双向同步；
- **保留**：`GET /canvas/api/muse/export/{spaceId}` 走 `/spaces/{slug}/export.html`，
  把 muse 成品 HTML 落到本地媒体，作为嵌入卡；
- **暂缓**：`spaces.save` 文档同步（body 无调用点、且创建流程在 frame app 内）；
  `spaces.subscribe`（参数未证实）。两者待未来用 CDP 抓一次 frame app 建立空间的全过程再定；
- **另辟**：`idea-cards.execute` 是一条"AI 建 Web 应用"的异步生成路径，可作为画布
  `text/prompt` 节点的第二类生成能力（与 `/v1/images/tasks` 并列），P3 再做。

**06:1x 补记（P3 状态重排）**：

- 生成侧不再是"浏览器专属"：纯协议图像生成已真机验收（`docs/PROTOCOL.md` §1.6.1），
  画布点「生成」经 `/v1/images/tasks` 落到 `ProtocolDriver`（`MUSE_PROTOCOL_DRIVER=1`
  + 账号有 cookie），失败的一次可重试回退到浏览器。活网证据：CDP 指向无人监听端口
  仍 26s 出图，画布节点 323→325、保存 324、页面 0 报错。
- 只读分享已落地（§3.2.2），含前端 `?share=` 只读模式；分享 token 落库、可撤销、
  删除空间连带清 token。11:3x 补：编辑态 `#share-panel` 提供链接管理（查看/复制/
  轮换/撤销，轮换与撤销二次确认），底部状态栏显示有效/未分享/读取失败三态。

**10:2x 补记（P3 Spaces 适配落地）**：

- §3.3 的三个端点已按只读口径实现（`muse_bridge/muse_spaces.py` + `canvas_api.py`），
  另补 idea-cards 列表/详情/同源图标代理；真机证据见 `docs/PROTOCOL.md` §1.6.2。
- 不做 `spaces.save` 文档同步（body 无调用点、创建流程在 frame app 内）；不做
  `spaces.subscribe`（参数未证实）。idea-cards `execute`（"AI 建 Web 应用"异步链）
  仍暂缓，作为画布 prompt 节点第二类生成的候选。
- 前端接入留给 P3 后续：画布 UI 目前仍只用本地画布 + `/v1` 生成 + 分享；muse
  Space 导入/灵感卡画廊尚未接按钮（后端已可用，前端不加按钮不影响本地画布）。

## 变更记录

- 2026-11-11：首版契约定稿（P1/P2 并行开发的唯一依据）。
- 2026-11-11 04:5x：修正任务状态六态口径、补媒体代理契约、追加 §10 muse 逆向结论与 P3 修订。
- 2026-11-11 06:1x：补 §3.2.2 只读分享契约；§10 补记纯协议生成真机验收与 P3 重排。
- 2026-11-11 10:2x：写实 §3.3 muse 只读适配契约（spaces/import/export/idea-cards，含导出
  attachment+nosniff+CSP 的 XSS 硬约束）；§10 追加 P3 落地补记。
