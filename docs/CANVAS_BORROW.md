# CANVAS_BORROW —— muse 无限画布的开源借鉴清单与取舍（可直接指导前端实现）

**目标**：为 `muse-bridge` 自研一套完全适配 muse 的无限画布 UI。纯静态 HTML / ES Module / CSS，由 bridge 直接托管（`/canvas`），**不依赖 npm 构建**，零第三方运行时依赖，离线可开，Windows + Chrome 优先。
**调研日期**：2026-11-11（许可证与体积均为当日实抓；版本号随包注明，不随仓库漂移）。
**证据等级**沿用本仓口径：`observed`（直接读到许可证原文/文档原文）、`corroborated`（≥2 份独立来源）、`hypothesis`（单来源）、`未证实`（没查到）。

---

## 0. 结论先行（TL;DR）

1. **一个库都不装**。tldraw / Excalidraw / React Flow / Konva / Fabric / Pixi 全部是 npm 包 + 构建链产物，与"零构建静态托管"直接冲突；其中 tldraw 的生产使用还要 license key + 水印（见 §2）。
2. **渲染走"混合三层"**：DOM 卡片层（文字/毛玻璃/可编辑）+ 同矩阵的 SVG 连线层 + Canvas2D 缩略图/导出层。理由与数字见 §3.3。
3. **只抄"思路"不抄"代码"**：可安全借鉴的是 MIT/Apache 项目的**架构与交互模式**（相机模型、剔除、LOD、命令式撤销），落成我们自己的 2000 行级实现。
4. **红线**：tldraw（自定义许可，生产禁用）、ComfyUI 与 ComfyUI_frontend（GPL-3.0）、以及一切 AGPL 代码，**一行都不进仓**；Penpot（MPL-2.0）/AFFiNE（EE 混合）只读不抄。
5. **持久化按契约 `Doc` v1**（`version` + 稳定 id + 可独立写字段，天然为 CRDT 留位）；**JSON Canvas 1.0（MIT）只做 P3 的导入/导出转换层**，不改存储格式（§3.5/§8.3）。

---

## 0.5 与已有契约 `docs/CANVAS_DESIGN.md` 的关系（**先读这一节再往下**）

仓库里已经有一份**字段级实现契约**（`docs/CANVAS_DESIGN.md`，2026-11-11 定稿），以及已落地的后端 `muse_bridge/canvas_api.py`（`APIRouter(prefix="/canvas/api")`）、`muse_bridge/canvas_store.py`、`tests/test_canvas_*.py`。**本文不替代契约**：

| | `docs/CANVAS_DESIGN.md`（契约） | 本文（借鉴/取舍） |
|---|---|---|
| 管什么 | 字段、路由、上限、坐标约定、验收口径 | 借哪些开源思路、许可证红线、渲染与性能做法、视觉数值、坑 |
| 谁是真相 | **字段与路由的唯一真相** | 设计建议，冲突时**服从契约** |
| 变更方式 | 文末"变更记录"加一行 | 可直接改，但不得与契约冲突 |

**本文已按契约收敛的 5 处**（初稿曾写反，现已改）：

| 项 | 契约规定（服从） | 本文此前的写法 |
|---|---|---|
| 滚轮默认语义 | 滚轮 = **平移**，`Ctrl/Cmd + 滚轮` = 缩放（`CANVAS_DESIGN.md` §2） | 纯滚轮缩放（Figma 类）——已改 |
| 缩放范围 | `zoom ∈ [0.05, 8]`（§4 校验） | `[0.1, 8]`——已改 |
| 网格间距 | `gridSize = 28`（世界单位，§6） | 24——已改 |
| z 序 | 整数 `z`（§4） | 分数索引——降级为 P3 演进建议 |
| 静态目录 | `muse_bridge/canvas/`（`index.html`/`css`/`js`/`assets`，§1） | `static/canvas/`——已改 |

**另外发现三处需要 Lead 统一口径的地方**：

1. **任务状态：契约 §3.2 写的是 `queued|running|succeeded|failed`，代码是六态**。`muse_bridge/app.py:811-816` 的 `_normalize_state` 实际输出来自 `jobs.py:72-80` 的 `queued|processing|completed|failed|cancelled|unknown`（`succeeded/success/done` 只是被映射成 `completed` 的输入侧口语）。**前端按代码实现**（六态），并请 Lead 修正契约该行。
2. **两套状态不要混**：契约 §4 的 `data.status` 是**卡片显示态**（5 态 `idle|queued|generating|done|failed`），和任务六态是不同层次，映射表见 §4.1。前端只需保证"六态 → 五态"单向投影，不要把 `unknown` 直接漏进 UI。
3. **`canvas.js` 这个名字在契约里出现了两次不一致**：§7 的模块清单是 `js/app.js`，§8.1 的测试口径却写"返回 `text/html` 且包含 `canvas.js`"。建议全仓统一为 `js/app.js`，并让 Lead 顺手把测试断言一起改（当前 `tests/test_canvas_api.py` 里还没有这条断言）。

---

## 1. 候选项目对比（≥8 个，全部实抓许可证）

### 1.1 开源 / 源码可见项目

| # | 项目 | 仓库 / 站点 | 许可证（SPDX，实抓） | 商用 / 闭源项目可用？ | 核心渲染 | 包体积（min / gzip，bundlephobia 实测） | 零构建静态托管？ | 值得借鉴的点 |
|---|---|---|---|---|---|---|---|---|
| 1 | **tldraw** | [github.com/tldraw/tldraw](https://github.com/tldraw/tldraw) | **自定义许可**（GitHub 识别 `NOASSERTION`），非 SPDX、非 OSI 开源 | ❌ 生产环境**必须**商业/试用/爱好 license key；爱好 license 强制 "made with tldraw" 水印 | DOM（每个 shape 是 DOM 元素，剔除时 `display:none`）+ 内部几何索引 | 926.6 KB / **268.1 KB**（`tldraw@2.0.0`，13 个直接依赖 + React） | ❌ 需 React + 打包 | 相机模型、视口剔除、debounced zoom、几何缓存、LOD、SVG→PNG 导出管线（§3 大量引用） |
| 2 | **Excalidraw** | [github.com/excalidraw/excalidraw](https://github.com/excalidraw/excalidraw) | **MIT**（[LICENSE 原文](https://github.com/excalidraw/excalidraw/blob/master/LICENSE)） | ✅ 可闭源商用，保留版权与许可声明 | Canvas2D 全量重绘 + 隐藏 textarea 做文字编辑 | 2322.5 KB / **637.5 KB**（`@excalidraw/excalidraw@0.17.0`，peer React；0.18.1 包解包 46.8 MB） | ❌ React 组件 | 分数索引 z-order（fractional indexing）、手绘风 rough.js、协作/本地持久化取舍、hidden-textarea 文字编辑 |
| 3 | **React Flow (xyflow)** | [github.com/xyflow/xyflow](https://github.com/xyflow/xyflow) | **MIT**（[LICENSE 原文](https://github.com/xyflow/xyflow/blob/main/LICENSE)，API `spdx_id: MIT`） | ✅ | DOM 节点 + SVG 边（viewport transform 套在容器上） | 171.8 KB / **54.8 KB**（`@xyflow/react@12.3.0`，依赖 d3-zoom/d3-drag 等） | ❌ React | 视口 `{x,y,zoom}` 语义、MiniMap、Handle/Edge 抽象、边框选中与连线交互 |
| 4 | **Konva** | [github.com/konvajs/konva](https://github.com/konvajs/konva) | **MIT**（npm registry `"license":"MIT"`，[包页](https://www.npmjs.com/package/konva)） | ✅ | Canvas2D 场景图，每层独立 canvas + 独立 hit canvas | 182.7 KB / **53.4 KB**（`konva@9.3.0`；10.9.1 解包 1.91 MB） | ❌ 需打包（但有 UMD 单文件） | 分层与命中画布成本模型、`listening(false)`、shape cache、拖拽期跳过命中、移动端 canvas 内存上限（§3.3/§7 引用） |
| 5 | **Fabric.js** | [github.com/fabricjs/fabric.js](https://github.com/fabricjs/fabric.js) | **MIT**（[LICENSE 原文](https://github.com/fabricjs/fabric.js/blob/master/LICENSE)） | ✅ | Canvas2D 对象模型 + SVG→Canvas 解析 | 304.8 KB / **91.5 KB**（`fabric@6.6.0`） | ❌ | 对象级 transform/序列化模型、`toJSON/loadFromJSON` 的 schema 版本化写法 |
| 6 | **PixiJS** | [github.com/pixijs/pixijs](https://github.com/pixijs/pixijs) | **MIT**（[LICENSE 原文](https://github.com/pixijs/pixijs/blob/dev/LICENSE)） | ✅ | **WebGL/WebGPU** 批渲染 | 758.8 KB / **214.8 KB**（`pixi.js@8.6.0`，9 依赖） | ❌ | 纹理图集、`cull` 与批处理思路（我们只在 >1500 图片节点时考虑自写 WebGL，不引它） |
| 7 | **ComfyUI（前端）** | [github.com/Comfy-Org/ComfyUI_frontend](https://github.com/Comfy-Org/ComfyUI_frontend) | **GPL-3.0**（[LICENSE 原文](https://github.com/Comfy-Org/ComfyUI_frontend/blob/main/LICENSE)） | ❌ 传染性 copyleft，闭源分发即违约 | DOM 节点图（LiteGraph 血统）+ 节点内嵌 canvas 预览 | 未单独计量（Vue + TS 全量构建） | ❌ | **只读**：生成任务的"节点内进度/队列"UX（§4） |
| 8 | **ComfyUI（后端）** | [github.com/Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) | **GPL-3.0**（[LICENSE 原文](https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE)） | ❌ | —（Python 服务） | — | — | 只读：队列/历史/重试的 API 语义 |
| 9 | **InvokeAI** | [github.com/invoke-ai/InvokeAI](https://github.com/invoke-ai/InvokeAI) | **Apache-2.0**（[LICENSE 原文](https://github.com/invoke-ai/InvokeAI/blob/main/LICENSE)） | ✅（保留 NOTICE/声明；专利授权条款） | React + Canvas（Unified Canvas 分层/区域） | 大（React 全栈前端，需构建） | ❌ | 只读：Unified Canvas 的图层/区域/参考图工作流 |
| 10 | **Penpot** | [github.com/penpot/penpot](https://github.com/penpot/penpot) | **MPL-2.0**（[LICENSE 原文](https://github.com/penpot/penpot/blob/main/LICENSE)） | ⚠️ 可以，但**被抄文件**必须保持 MPL 并公开其修改（文件级 copyleft） | SVG/Canvas 混合（设计工具） | 大（ClojureScript + 构建） | ❌ | 只读：图层树/约束布局/导出面板的交互组织 |
| 11 | **AFFiNE** | [github.com/toeverything/AFFiNE](https://github.com/toeverything/AFFiNE) | **混合**：前端 MIT + `packages/backend` **EE License**（生产需订阅）+ 客户端产物按 MPL-2.0（[根 LICENSE](https://github.com/toeverything/AFFiNE/blob/canary/LICENSE)、[backend LICENSE](https://github.com/toeverything/AFFiNE/blob/canary/packages/backend/server/LICENSE)） | ⚠️ 部分（EE 部分闭源生产不可用） | BlockSuite/DOM | 大（monorepo） | ❌ | 只读：无限画布的 block/白板混合组织 |
| 12 | **JSON Canvas（规范）** | [jsoncanvas.org](https://jsoncanvas.org/) · [github.com/obsidianmd/jsoncanvas](https://github.com/obsidianmd/jsoncanvas) | **MIT**（站点明示，[spec/1.0](https://jsoncanvas.org/spec/1.0/)） | ✅ 可自由实现/导入/导出 | 不涉及渲染 | 0（只是 schema） | ✅ | **P3 做导入/导出适配**（不改存储格式，§3.5） |

> 体积口径：bundlephobia API 实测（`https://bundlephobia.com/api/size?package=<pkg>@<ver>`），min = minified、gzip = minified+gzip；含其声明的运行时依赖，不含 React 等 peer。**这些库即便"能 UMD 单文件引入"，也各自带自己的全局/构建假设，与本项目的"零依赖、可离线、无构建"约束冲突**，故只在 §5 里以"借思路"出现。

### 1.2 闭源产品（只能看 UX，不能拿代码/素材）

| 产品 | 链接 | 许可状态 | 我们看什么 |
|---|---|---|---|
| **Krea** | [krea.ai](https://www.krea.ai/) | 闭源商业产品（无公开许可） | 实时画布（realtime canvas）里"笔刷/参考图/参数"三栏并置；生成结果就地替换而非弹窗 |
| **Fal** | [fal.ai](https://fal.ai/) | 闭源商业平台（模型 API 为主） | 模型选择-参数-批量队列的紧凑面板；结果以 grid 呈现 |
| **Muse 自己的画布** | [muse.ai](https://muse.ai/) | 闭源（本仓 `docs/FRONTEND_RE.md` 有逆向记录） | spaces / idea-cards 的"卡片即会话"组织；生成结果回流到卡片 |

---

## 2. 许可证红线（逐条引原文）

### 2.1 tldraw：**生产环境不能用，除非买 key；爱好 license 强制水印**

`LICENSE.md` 原文（[链接](https://github.com/tldraw/tldraw/blob/main/LICENSE.md)）：

> "Production Environment" means any production deployment of the Software that operates on servers, cloud platforms, web applications, or where the software is used to provide functionality to end users, customers, or the public.
>
> **Conditions** … Not to use the Software in Production Environments. … **Not to disable, change, or interfere with the Software's License Key enforcement.** … Not to remove any copyright or other notices from the Software.
>
> **Technical enforcement**: The Software includes technical measures to verify License Key validity, detect deployment environments, enforce usage restrictions based on license type, and **ensure proper watermark display**.

官方文档（[tldraw.dev/community/license](https://tldraw.dev/community/license)）进一步明确：

> Under its default terms, the tldraw SDK license permits use **only in development**. … The SDK will work in production only when provided with a valid and active license key.
>
> Hobby license … **the "made with tldraw" watermark must be shown on the canvas**.
>
> While the tldraw SDK is source available, **it is not permissively licensed** … would not be Open Source by any definition.

商业条款（[tldraw.dev/pricing](https://tldraw.dev/pricing)）：100 天试用 / 商业 license / 非商业 hobby license 三档，商业 license 按价值定价。

**唯一的合法例外**（同一页原文，值得记住）：

> "Many of our examples and demos are available under the MIT license, and you should feel free to use this code **as if it were your own**; however, any source code or packages covered by the tldraw SDK license would not be Open Source by any definition."

即：`examples/` / demos 里的**示例代码是 MIT**，可以当参考甚至改写；但 SDK 本体（`tldraw`、`@tldraw/*` 包）不行。品牌与名称另有商标限制（[trademark guidelines](https://tldraw.dev/legal/trademark-guidelines)）。
另外 license key 是**客户端校验、可离线、可公开**的（原文："License keys are validated on the client. You can use them offline. They can be public."）——所以"内网/离线部署"**不是**绕开许可的理由。

**判定**：我们这套 `/canvas` 是给真实用户的生产 UI，**直接依赖 tldraw = 违约或付费 + 水印**。它的价值只在"读文档抄架构"（§3 里大量标注出处），代码、`@tldraw/*` 包、示例资源一律不进仓。

### 2.2 传染性 copyleft：GPL / AGPL 全线禁入

- **ComfyUI**、**ComfyUI_frontend**：均是 **GPL-3.0**（上文许可证原文链接）。把其代码并入我们的闭源/商业分发会触发整仓 GPL。
- **AGPL-3.0**：本项目明确排除（用户要求"不引入 AGPL"）。AGPL 的网络交互条款会把"通过 HTTP 提供 UI"也算作分发，风险高于 GPL。
- 结论：**GPL/AGPL 项目只允许"人读、写结论"，不允许 copy-paste 代码、不允许 vendor 进 `static/`**。

### 2.3 弱 copyleft（可读不可抄）

| 许可证 | 项目 | 抄代码的后果 | 我们的策略 |
|---|---|---|---|
| MPL-2.0 | Penpot | 被修改的**文件**须以 MPL 公开源码；可作为 Larger Work 分发 | 只读；不复制文件 |
| EPL-2.0 | elkjs（自动布局候选，**许可证未在本轮核实**） | 文件级 copyleft + 专利 retaliation | 不引入；自己写网格/流式布局 |
| Apache-2.0 | InvokeAI | 可闭源商用，但须保留 LICENSE/NOTICE 与修改声明，且含专利终止条款 | 只读；若真抄一段，须建 `NOTICE` |

### 2.4 图标 / 字体 / 示例美术

| 资源 | 许可 | 能否用 | 备注 |
|---|---|---|---|
| **Lucide 图标** | **ISC**（Feather 派生部分 MIT）— [LICENSE 原文](https://github.com/lucide-icons/lucide/blob/main/LICENSE) | ✅ 可直接 vendor SVG，保留版权声明 | 建议只挑 ~20 个图标内联成 `icons/*.svg`，免打包 |
| **Inter 字体** | SIL OFL 1.1 — [rsms.me/inter](https://rsms.me/inter/) · [OFL 全文](https://openfontlicense.org/) | ✅ 可嵌入/自托管，保留 OFL 与版权 | 若嫌体积，直接 `system-ui, "Segoe UI Variable"` 更稳（Windows 目标环境自带） |
| Excalidraw 手绘字体（Virgil 等） | 未逐一核实（其字体文件在仓库 `packages/excalidraw/dist`） | ⚠️ 不用 | 视觉风格相似≠可复制字体文件 |
| tldraw 示例/贴纸/截图 | 受 tldraw license + 商标政策约束（[trademark](https://tldraw.dev/legal/trademark-guidelines)） | ❌ | 连"看起来像 tldraw"的品牌化视觉都别做 |
| Krea / Fal / Muse 截图与生成图 | 闭源产品内容，版权归各自 | ❌ | 只能作为内部设计参考，不进仓、不外发 |

**一句话规则**：MIT / Apache-2.0 / ISC 可以"抄代码 + 保留声明"（Apache 还要带 NOTICE）；**CC0-1.0** 连声明都不强制；MPL / EPL 只"读"；GPL / AGPL / tldraw 自定义许可"只读、不抄、不 vendor"。

---

## 3. 交互与性能设计（逐条：可直接落地的做法 + 出处）

### 3.1 坐标系与视口：一个矩阵打天下

**状态模型（以契约为准，`docs/CANVAS_DESIGN.md` §6）**：`camera = {x, y, zoom}`，其中 `x/y` 是**视口左上角对应的世界坐标**：

```js
// 契约口径（唯一真相）：screen = (world - camera.xy) * zoom
const toScreen = (p, c) => ({ x: (p.x - c.x) * c.zoom, y: (p.y - c.y) * c.zoom });
const toWorld  = (p, c) => ({ x: p.x / c.zoom + c.x, y: p.y / c.zoom + c.y });

// 等价内部表示（本文后面所有推导用这个，可无损互转）：t = -camera.xy * zoom
//   screen = world * zoom + t
// CSS: transform-origin: 0 0; transform: translate(tx, ty) scale(zoom);
// 与上式等价，因为 CSS 变换从右往左作用到点上：p' = T · S · p
```

> tldraw 的相机是同一套语义（`x/y` 为页面空间偏移、`z` 为缩放，屏幕左上角对应的页面点是 `(-x, -y)`，[Camera system](https://tldraw.dev/sdk-features/camera)），但**字段名与契约不同，不要混用**。

**光标中心缩放（唯一需要背的公式）**：设光标屏幕坐标 `c`，缩放前 `zoom → zoom'`，要求光标下的世界点不动：

```js
// 契约写法（推荐）：光标的"世界点"保持不变，用新 zoom 反推 camera
const w  = toWorld(cursorScreen, camera);
const z2 = clamp(camera.zoom * factor, 0.05, 8);   // 契约 §4：zoom ∈ [0.05, 8]
camera.x = w.x - cursorScreen.x / z2;
camera.y = w.y - cursorScreen.y / z2;
camera.zoom = z2;

// 内部 t 写法（等价，便于直接写 CSS）：t' = c + (t - c) * (z2 / z)
// 两种写法都必须：先 clamp z2，再用"已 clamp 的 z2"算 camera/t，否则边界处会漂移
```

**为什么不用 `transform-origin` 动态设光标**：`transform-origin` 每次改都会触发重新计算基点，且和"容器内绝对定位的卡片"耦合；统一用 `t` 补偿更可控（d3-zoom 也是同一套 `k/x/y` 语义，见 [d3-zoom](https://d3js.org/d3-zoom) 的 `translateTo`：`tx = px - kx`）。

**DPR 适配（分两条路）**：

| 层 | DPR 处理 | 要点 |
|---|---|---|
| DOM 卡片层 | 不用管 | 浏览器按 devicePixelRatio 自动栅格化；**不要**给世界容器加 `will-change: transform`（会把位图钉在 1x，放大后发虚） |
| Canvas2D（缩略图/导出/截图） | 手动 | `canvas.width = round(cssW * dpr)`；`ctx.setTransform(dpr,0,0,dpr,0,0)`，再叠加相机矩阵；线宽用 `1/z` 反算，保证屏幕上恒为 1px（[devicePixelRatio](https://developer.mozilla.org/en-US/docs/Web/API/Window/devicePixelRatio)、[setTransform](https://developer.mozilla.org/en-US/docs/Web/API/CanvasRenderingContext2D/setTransform)） |

**边界处理**：世界坐标用 `float` 即可，但**卡片位置入模时统一 `Math.round` 到 0.5 网格**（避免 1px 边框落在半像素上发灰）；相机 `t` 允许小数（平滑）。缩放范围 **`zoom ∈ [0.05, 8]`（契约 §4 的校验区间）**；缩放档位可直接借 tldraw 的默认示例 `zoomSteps: [0.1, 0.25, 0.5, 1, 2, 4, 8]` 再加一个 `0.05` 下限——其文档写明"第一个值是 min zoom、最后一个是 max zoom，camera 即使没有 constraints 也会被 clamp 到这个区间"（[Camera options](https://tldraw.dev/sdk-features/camera)）。

### 3.2 平移/缩放的手感

| 输入 | 语义（**契约默认**，`CANVAS_DESIGN.md` §2） | 参数（可直接用） | 出处 |
|---|---|---|---|
| 鼠标滚轮（无修饰键） | **平移**（不是缩放；要改成"纯滚轮缩放"必须同时在契约变更记录登记） | `camera.x += deltaX / zoom`、`camera.y += deltaY / zoom`（世界位移才是 `屏幕位移 / zoom`） | 契约 §2；[WheelEvent.deltaMode](https://developer.mozilla.org/en-US/docs/Web/API/WheelEvent/deltaMode) |
| **Ctrl/Cmd + 滚轮 与 触控板 pinch** | 缩放，光标为锚点（Chrome 把 pinch 报成 `ctrlKey=true` 的 wheel） | `factor = 2^(-deltaY * 0.0015)`（d3 默认式同源），`zoom' = clamp(zoom * factor, 0.05, 8)` | [d3-zoom wheelDelta](https://d3js.org/d3-zoom)、[WheelEvent.ctrlKey](https://developer.mozilla.org/en-US/docs/Web/API/WheelEvent/ctrlKey) |
| 触控板双指滑动（无 ctrl） | 平移 | 同第一行；`deltaMode===1`（行模式）时 ×16 转 px | [WheelEvent.deltaMode](https://developer.mozilla.org/en-US/docs/Web/API/WheelEvent/deltaMode) |
| 空格 + 拖拽 / 中键拖拽 | 平移 | 抓取点固定：`camera.xy = cam0.xy - (p - p0) / zoom` | 契约 §2 |
| 双击空白 | 回正（`zoom=1` 或 fit） | 250 ms 过渡（d3 双击默认时长） | [d3-zoom duration](https://d3js.org/d3-zoom) |
| 惯性 | 仅对"拖拽甩出"生效 | 自研：`v *= 0.92 / frame`（60fps 下约 1.2s 衰减到 1%），`v < 0.02 px/ms` 停；tldraw 官方 `slideCamera` 的默认为 `friction: 0.1`、`speedThreshold: 0.01`、`speed ≤ 1` | [Camera system · Momentum scrolling](https://tldraw.dev/sdk-features/camera) |
| 缩放档位吸附 | 手势结束后吸附到最近档（`±3%` 内） | 200 ms `easeInOutCubic` | tldraw `EASINGS.easeInOutCubic`、`animation.duration`（同上） |

**三条硬规则**：

1. `wheel` 必须 `passive: false` + `preventDefault()`，否则 Chrome 会同时滚动页面（[addEventListener passive](https://developer.mozilla.org/en-US/docs/Web/API/EventTarget/addEventListener)）。
2. 指针事件统一用 **Pointer Events + `setPointerCapture`**，不要在 `mousemove`/`touchmove` 两套里各写一遍（[Pointer events](https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events)、[setPointerCapture](https://developer.mozilla.org/en-US/docs/Web/API/Element/setPointerCapture)）。
3. 相机动画一旦遇到用户输入立即取消（tldraw 明确"user input takes precedence"，同上），否则会出现"手在拖、动画在抢"的双写。

### 3.3 渲染策略：DOM / Canvas2D / SVG / WebGL 的取舍

| 维度 | DOM（我们的默认） | SVG | Canvas2D | WebGL(Pixi 级) |
|---|---|---|---|---|
| 文字排版/换行/编辑 | ✅ 原生，`contenteditable` 直接上 | ⚠️ 需 `<foreignObject>` | ❌ 全手写（measureText/断行） | ❌ |
| 毛玻璃 / 圆角 / 阴影 | ✅ `backdrop-filter`、`border-radius`、`box-shadow` | ⚠️ 部分 | ❌ 需自己画 | ❌ |
| 命中测试 | ✅ 原生 `elementFromPoint` / 事件冒泡 | ⚠️ 手写几何 | ❌ 手写（Konva 靠独立 hit canvas） | ❌ |
| 1k+ 节点 | ⚠️ 需剔除（tldraw 证明可行：10k shape 只渲染 50 个，[Performance](https://tldraw.dev/sdk-features/performance)） | ⚠️ 节点多则 DOM 树大 | ✅ | ✅ |
| 大量图片 | ✅ 浏览器解码 + 纹理缓存，配 LOD 降采样 | ✅ | ✅ | ✅ 最优 |
| 缩放清晰度 | ✅ 未提升合成层时按当前 scale 重栅格化 | ✅ 矢量 | ⚠️ 需按 DPR+zoom 重设 backing store | ✅ |

**决策：混合三层，按 z-index 从下到上**

```
┌ layer-grid   CSS 网格背景（1 个 div，background-size 随 z 变）      —— DOM
├ layer-edges  SVG（同相机矩阵；贝塞尔/正交路由；仅可见边）           —— SVG
├ layer-cards  DOM 卡片（绝对定位，世界坐标；可见集内才挂载）         —— DOM
├ layer-hud    DOM（选择框、对齐参考线、工具栏、参数面板）             —— DOM
└ layer-minimap Canvas2D（rAF 节流重绘）                              —— Canvas2D
```

**为什么不用 Canvas2D 画卡片**：卡片里是**可换行的中文提示词 + 可编辑文本 + 毛玻璃 + 圆角阴影**，Canvas2D 要自己实现断行、光标、选区、IME——这是 10× 工作量，且中文断行规则（`Intl.Segmenter` 只能帮一部分）极易翻车。tldraw 也用 DOM 走这条路（其 performance 文档写明"hides off-screen shapes by setting `display: none` on their **DOM elements**"）。

**什么时候才升级 WebGL**：单帧可见图片节点 > 400 且 1080p 下掉帧到 <45fps 时，只把**图片平面**换成一块自写 WebGL2（纹理图集 + 单次 draw call），卡片/文字仍是 DOM。PixiJS 的体积（758 KB / 215 KB gzip，9 依赖）与"零依赖"冲突，只作为该方案的参考实现（[pixijs](https://github.com/pixijs/pixijs)）。

**剔除（culling）落地**：

```js
// 1) 视口世界矩形（含 200 世界像素预取边距）
const view = {
  x: (-cam.tx) / cam.z, y: (-cam.ty) / cam.z,
  w: viewportW / cam.z, h: viewportH / cam.z,
};
const margin = 200 / cam.z;
// 2) 只对 AABB 相交的节点挂 DOM；其余 display:none（保留在 sceneMap 里，选中/编辑永不被剔除）
// 3) 剔除只在"相机变化 + 节点增删"时算，不在每帧算；用 rAF 合并成一次
```

tldraw 的阈值可以直接抄成配置：`debouncedZoomThreshold = 500`（shape 数超过 500 时，缩放过程中用"稳定 zoom"而非真实 zoom，避免每帧重算 stroke 宽度/LOD）、`maxShapesPerPage = 4000`（[Performance](https://tldraw.dev/sdk-features/performance)）。我们对应落成：`LOD_ZOOM_DEBOUNCE_N = 500`、`MAX_NODES = 4000`（超过就拒绝新建并提示"拆分 space"）。

**LOD（分级细节）**：三级，按"卡片屏幕尺寸 `w*z`"判，不按 zoom 判（同一 zoom 下大图和小卡需求不同）：

| 级别 | 触发 | 渲染 |
|---|---|---|
| `full` | `w*z ≥ 160px` | DOM 卡片：标题 + 参数 chips + 图片全质量（`?w=` 按 2 的幂降采样） |
| `compact` | `64px ≤ w*z < 160px` | 只留缩略图 + 一行标题；关阴影、关 `backdrop-filter` |
| `dot` | `w*z < 64px` | 纯色圆角块（无文字、无图片），或并入"聚合簇" |

图片降采样照 tldraw 的 `steppedScreenScale`（**四舍五入到 2 的幂**再乘 dpr）：4000px 图缩到屏幕 200px，scale 0.05 → 0.0625 → 请求 250px 宽（×dpr），内存与解码成本降一个量级（[Image export 同页的 LOD 说明见 Performance](https://tldraw.dev/sdk-features/performance)）。

**帧调度**：所有输入 → 改 `pending` 标记 → 单个 rAF 里「算相机 → 剔除 → 写 transform → 画 HUD/缩略图」。禁止在 `pointermove` 里直接写 DOM（一帧内多次写会强制同步布局）。`requestAnimationFrame` 语义见 [MDN](https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame)。

**离屏 canvas（两处，都是"别卡主线程"）**：

1. **导出**：大图（> 4000px × 4000px）在 `OffscreenCanvas` 里画完再 `convertToBlob()`，避免主线程长时间占用；浏览器不支持时退回普通 `<canvas>`（[OffscreenCanvas](https://developer.mozilla.org/en-US/docs/Web/API/OffscreenCanvas)）。
2. **缩略图/minimap**：固定 160×120 的离屏画布，每次只 `drawImage` 已有的小位图，不重绘世界（见 §3.7）。

**虚拟化 = "只挂可见 DOM"**：不是 `content-visibility` 那种"渲染但不画"，而是**物理上不挂载**（`:empty` 或 `display:none` 的回收池）。契约 §2 的"节点 DOM 虚拟化按视口裁剪"说的就是这件事。

**纹理/图片缓存**：

```js
// Map<token, {bitmap: ImageBitmap, w, h, bytes, usedAt}>，LRU 上限 120 MB
// 命中：直接 <img src=blobURL> 或 drawImage(bitmap)
// 淘汰：bitmap.close() + URL.revokeObjectURL(url)   ← 两条都要，少一条就泄漏
```

### 3.4 节点系统

**z-order**：契约用**整数 `z`**（`CANVAS_DESIGN.md` §4），≤2000 节点这样最省事：

- **置顶/置底**：`z = max+1` / `z = min-1`（O(n) 一次扫描）；
- **"上移一层"**：与相邻节点交换 `z`（O(n) 排序，可接受）；
- **批量置顶**：按选择顺序重新分配连续 `z`（一次 `renumber`，只在一次操作后跑）。

不要用 CSS `z-index: 999999` 这种土办法（会与 HUD 层打架）。**P3 之后**若真需要"高频中间插入 + 协同"，再换字符串分数索引（Excalidraw 同思路；参考实现 [fractional-indexing](https://github.com/rocicorp/fractional-indexing) 是 **CC0-1.0**，公有领域，可放心照抄算法），但那是**改契约**的动作，不是现在的活。

**命中测试**（因为卡片是 DOM，优先走原生）：

| 场景 | 做法 |
|---|---|
| 点击卡片 | 原生事件；卡片根节点 `pointer-events: auto`，空白处 `pointer-events: none` 交给画布 |
| 框选 | 世界空间 AABB 相交（快）；对**旋转过的图片卡**用 OBB-SAT（4 条分离轴，成本可忽略） |
| 连线端点 | Handle 是 12px 的 DOM 圆点，`pointer-events` 单独开；命中半径按 `12/z` 反算保持屏幕恒定 |
| 分组内命中 | 先测 group 子节点，再冒泡到 group；group 只作为"选择代理"，不参与 z 竞争 |

**框选**：`pointerdown` 在空白 → 记录起点世界坐标 → 每帧只更新 HUD 的矩形（不碰卡片）→ `pointerup` 时一次求交。**Shift 加选 / Ctrl 减选**；框选方向决定"包含"（左→右 全包含，右→左 相交即选，与 CAD 习惯一致）。

**吸附对齐**：阈值 `6 screen px`（换算成世界 `6/z`）；吸附源三类——其他卡片的 6 条边 + 2 条中线、网格线（24 的倍数）、等间距参考（相邻卡间距复刻）。命中后在 HUD 画 1px 亮线（`#7C5CFF`，opacity .6），并在 `pointerup` 才写入最终坐标（拖动过程只改 transform，不改模型）。

**连线**：

- 默认**三次贝塞尔**，控制点取两端 Handle 的法线方向 `±d`，`d = clamp(dist*0.35, 40, 180)`；
- **正交路由**只在"用户按 Alt"或"流程图模式"启用：`H`/`V` 两段折线 + 8px 圆角（用 4 段 arc 拼），不做完整 A*（节点少时 A* 的收益低于抖动成本）；
- 边只画**可见集内**的：端点都在视口外且线段 AABB 不与视口相交的边直接跳过（边数量通常是节点 1.5~2 倍，这一条能省 60%+ 的 SVG 节点）。

**分组/容器**：容器是**有 bounds 的特殊节点**，子节点用 `parentId` + 相对坐标（`x,y` 存相对父的偏移）。移动容器 = 改容器 transform，不动子节点（避免 N 次写入）；删除容器 = 先 detach 子节点到世界坐标，再删。tldraw 的 frame/parenting 语义可直接对照（[Parenting](https://tldraw.dev/sdk-features/parenting)、[Groups](https://tldraw.dev/sdk-features/groups)）。

### 3.5 状态模型：撤销、选择、schema、为 CRDT 留位

> **先说契约**：`Doc` 的字段、枚举、上限已经是定好的（`version: 1`、`nodes ≤ 2000`、单 doc ≤ 4 MB、`z` 为整数、`camera.zoom ∈ [0.05, 8]`、`type ∈ image|video|prompt|text`、`id` 形如 `n_[a-z0-9]{10}`，见 `docs/CANVAS_DESIGN.md` §4）。**本节不新增字段**，只讲"在这个契约内怎么把状态机制写对"，以及 P3 的演进方向。

**命令式撤销（command pattern）**：每个用户动作产出一个 `Command { do, undo }`；**拖动/缩放这类连续动作必须在 `pointerup` 合并成一条**，否则一次拖拽会塞进 200 条历史。

```js
// 历史栈上限 100；超出丢最旧
// 事务：begin('move') → 期间所有 set 记录 old/new → commit() 压栈一条
// 结构变更（增删节点）直接存"前/后快照 diff"（节点量小，比逐字段 patch 简单且不会漏字段）
```

tldraw 的 history 语义可对照（[History](https://tldraw.dev/sdk-features/history)）：真正的抓手是 **mark（停点）+ squash（把一段操作压成一步）**——"撤销单位是用户意图，不是字段写入"。契约里的 `Ctrl+Z/Ctrl+Shift+Z` 只要实现成"一次拖拽 = 一步"，体验就对了。

**选择状态**：**不进历史**（选中不是文档变更；契约也没有 selection 字段）。选择集放前端 `Set<id>`，派生值（包围盒、可执行的批量动作）用 memo 缓存，随选择集版本号失效。刷新后选择集可以丢弃，不要为它扩 schema。

**持久化 schema：契约是唯一真相，JSON Canvas 留到 P3 做"导入/导出适配"**

契约的 `Doc`（`version:1` + `nodes/edges/camera/meta`）已经够用，**不要**为了"兼容 Obsidian"去改存储格式。JSON Canvas 1.0 的正确用法是**转换层**：

```jsonc
// P3 建议：导出时把 Doc 转成 JSON Canvas；导入时反向转换。
// 契约的 Node.type: image|video|prompt|text  →  JSON Canvas 的 text|file|link|group
// 映射规则（建议，需先在契约"变更记录"登记）：
//   text   → text
//   image  → file（file 指向 /v1/media/<token>）
//   video  → file（mime 标注）
//   prompt → text（meta.muse.kind="prompt" 保留 prompt 原文与参数）
// 契约外的字段（例如将来的 group）一律走 meta.muse，保证 JSON Canvas 消费方能忽略
```

理由：JSON Canvas 1.0 是 **MIT**、可自由实现/导入/导出（[jsoncanvas.org](https://jsoncanvas.org/)、[spec/1.0](https://jsoncanvas.org/spec/1.0/)），把"导出成 .canvas 给 Obsidian 打开"作为**交付能力**比"把存储换掉"风险低得多。

**z 序**：契约用整数 `z`，≤2000 节点时完全够用（中间插入 = 后面 `z+1` 或整体重排，O(n) 可接受）。**只有在 P3 之后需要高频中间插入 + 协同**时，才换成字符串分数索引（参考实现 CC0，见 §3.4），届时先在契约登记新字段。

**迁移**：契约 `version` 只有 `1`。预留 `migrations[1] = (doc)=>doc` 这一层空壳即可，**不要**提前造 `v2` 字段；升级时按 `version` 逐级跑，升级后另存（不原地改）。Fabric 的 `loadFromJSON` 也是这套"版本号 + 转换"的思路（[fabric.js](https://github.com/fabricjs/fabric.js)）。

**为 CRDT 留位（本版不协作）**：现有契约已经满足三个前提——**稳定 id**（`n_...`，非数组下标）、**字段可独立写**（`data` 是扁平对象）、**服务端以 `updated_at` 收敛**。将来接 Yjs 时，一个 `Y.Map` per node + `Y.Array` 存 z 顺序即可（[Yjs docs](https://docs.yjs.dev/)）；现在不做 OT、不引任何 CRDT 依赖、**不加 `rev` 字段**（契约没有它，加了就是破坏"字段只加不改"的纪律）。

**存储位置**：契约是**服务端 SQLite 为准**（`canvas_spaces` 表 + `PUT /canvas/api/spaces/{id}`）。前端本地缓存用 **IndexedDB**（配额按站点总盘算，远大于 localStorage）；localStorage 只放 UI 偏好（主题、最后空间 id），因为它是 ~5 MB 级且同步阻塞（[Storage quotas](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_limits)）。**冲突**：以服务端 `updated_at` 为准，本地脏数据另存为"恢复副本"而不是覆盖。

### 3.6 自动布局

| 场景 | 布局 | 参数 |
|---|---|---|
| 新建卡片 | 网格流式 | 3 列，列宽 360，间距 24，`x = col*384`，`y = row*(h+24)` |
| 批量生成结果 | "接触表"（contact sheet） | 在父任务卡下方展开 2×2 / 3×3，间距 12，卡片宽 = 父宽/列数 |
| 一键整理 | 网格 + 分组保持 | 按契约的 `type`（image/video/prompt/text）分组，组内按 `data.createdAt`；组间 96px |
| 连线图整理 | 分层（Sugiyama 简化版） | 按入度拓扑分层，层内按 y 排序，层间距 120；**不引入 dagre/elkjs**（体积 + 许可证成本都不值） |

### 3.7 缩略图 / minimap 与导出

**minimap（Canvas2D，160×120 或按比例）**：

- 只画三类信息：节点矩形（按契约 `type` 上色：`prompt` `#7C5CFF`、`image` `#22D3EE`、`video` `#F59E0B`、`text` `#94A3B8`）、当前视口框（1px 白 40%）、可选边（不画，噪声大）；
- 缩放系数 `s = min(W/worldW, H/worldH)`，**不跟随主相机**（固定世界尺度，否则缩略图会"呼吸"）；
- 点击/拖动 = `centerOnPoint(worldFromMinimap)`；拖动时 30Hz 节流；
- 重绘挂在相机 rAF 上但**降频到 20fps**（人眼对缩略图不敏感）。

**导出 PNG：两条路，都不要 html2canvas。**

| 方案 | 保真度 | 成本 | 结论 |
|---|---|---|---|
| `html2canvas` | ❌ **明确不支持** `box-shadow`/`filter`/`mix-blend-mode`，`transform` 仅有限支持（[features 原文](https://html2canvas.hertzen.com/features)） | 中 | 直接排除：我们的玻璃拟态（`backdrop-filter` + 阴影）会被吃掉 |
| Canvas2D 直绘 | 中（文字用 `fillText`，需要自己断行） | 低 | **默认方案**：只画可见区域/选中区域，图片走已解码的 `ImageBitmap`（同源，不污染画布） |
| SVG 组装 → 栅格化（tldraw 路线） | 高（矢量文字、可嵌字体） | 高 | **"导出高清"选项**：把卡片渲染成 SVG（`foreignObject` 需把样式内联、`@font-face` 转 data URL），再 `drawImage` 到 canvas。tldraw 的完整管线与其坑都在 [Image export 文档](https://tldraw.dev/sdk-features/image-export)：字体先内联、样式再内联、媒体转 data URL、`pixelRatio` 默认 2、超限尺寸自动夹紧 |

**导出必须注意**：`canvas.toBlob()` 前若画过**跨源且未带 CORS 头**的图，画布被 taint，`toBlob` 直接抛 `SecurityError`（[CORS enabled image](https://developer.mozilla.org/en-US/docs/Web/HTML/CORS_enabled_image)、[toDataURL](https://developer.mozilla.org/en-US/docs/Web/API/HTMLCanvasElement/toDataURL)）。对策见 §7.6。

---

## 4. AI 画布 UX：5 个可直接抄的模式（配代码/文档链接）

> 这里只借"交互结构"，不借代码：ComfyUI 前端是 GPL-3.0，InvokeAI 是 Apache-2.0（要抄就得带 NOTICE），make-real 是 tldraw 生态（读，不抄）。

### 4.1 卡片自变形：提示词卡 → 任务卡 → 成品卡（**原位替换，不弹窗**）

同一个节点在生命周期里**只改 `data`**（`status`/`url`/`thumbUrl`/`taskId`），`id`、`x/y/w/h` 不变；`type` 可以从 `prompt` 切成 `image`（契约允许），但必须与 `data` 在同一次状态提交里完成（一次 undo 单元），不要留"半成品"中间态。用户的心智是"这张卡在生成"，而不是"我打开了另一个页面"。

```
prompt(card) --submit--> task(card: 进度环 + 取消 + 队列位) --done--> result(card: 图/视频 + 参数 chips + 重试)
                                                  \--fail--> error(card: 错误带 + 保留参数 + Retry)
```

- 参照：ComfyUI 的节点内进度条/队列（[ComfyUI_frontend](https://github.com/Comfy-Org/ComfyUI_frontend)，GPL，只读）；tldraw 的 make-real 直接在画布上圈选内容、再把 AI 生成结果作为画布内容放回原处（[make-real 仓库](https://github.com/tldraw/make-real)、[项目说明](https://tldraw.dev/blog/product/make-real-the-story-so-far/)、可在线体验 [makereal.tldraw.com](https://makereal.tldraw.com/)）。
- 我们的落点：`POST /v1/images/tasks`（带 `Idempotency-Key: cardId:attempt`）→ 轮询 `GET /v1/images/tasks/{id}`（1.5s 起，退避到 5s）→ `url` 回填。任务状态只有六个值 `queued|processing|completed|failed|cancelled|unknown`（`muse_bridge/jobs.py:72-80`），UI 直接映射，不要自己造词。

**六态 → 卡片五态的单向投影（照这张表写，别在视图里再判断）**：

| 任务六态（`jobs.py`） | 卡片 `data.status`（契约 §4） | 卡片表现 |
|---|---|---|
| `queued` | `queued` | 进度环 + "排队第 k 位"（`JOB_QUEUE_MAX=8`，超出给位次） |
| `processing` | `generating` | 骨架 + 已耗时秒表 + 取消按钮 |
| `completed` | `done`（同时写 `url`/`thumbUrl`/`width`/`height`/`mime`） | 图/视频 + 参数 chips + 重试入口 |
| `failed` | `failed`（写 `error`） | 错误带（按 `error.code` 分支，见 §4.5） |
| `cancelled` | `failed` + `error.code="cancelled"` | "已取消"灰态，可 Retry |
| `unknown` | `failed` + `error.code="unknown"` | "状态未知"，引导重试（**不显示成成功**） |

> `data.url` 契约注释写的是"一般是 `/v1/media/<token>`"。**建议存 token 或纯路径**，渲染时统一拼 `/canvas/api/media/<token>`（§7.6）——这样将来换代理/换端口只改一处。

### 4.2 批量变体：接触表 + "提升为主卡"

多选 N 张卡 → 一次提交 N 个任务（受 `JOB_QUEUE_MAX=8` 限流，超出要给"排队中第 k 位"而不是转圈）→ 结果在**发起卡下方**以 2×2/3×3 接触表铺开 → 单击任意一张 = 提升为主卡（其余折叠为"变体"角标）。

- 参照：Fal/Krea 的 grid 结果页（[fal.ai](https://fal.ai/)、[krea.ai](https://www.krea.ai/)）；InvokeAI 的 gallery/队列（[InvokeAI](https://github.com/invoke-ai/InvokeAI)，Apache-2.0）。
- 关键细节：变体图**不要**每张都建完整卡片 DOM；接触表用一个"组卡"内部的 CSS grid 承载缩略图，点开才升级为独立节点（省 N 倍 DOM）。

### 4.3 参数面板：右侧 dock，多选联动

- 单选 → 面板显示该卡参数；多选 → 面板顶部出现"应用到 3 张卡"的批量条，改动只标 dirty，点 Apply 才批量写（一次 undo）。
- 参数白名单现状：线上**没有**结构化生成参数字段，只有 `duration` / `aspect_ratio` / `model` 三个"文本约定"字段是 `corroborated`（见 `docs/FRONTEND_RE.md` §1）。UI 必须**只暴露这三个 + prompt**，其余参数一律 disabled 并标注"未证实"，否则就是把猜测做成产品。
- 参照：ComfyUI 的 widget 面板组织（只读）、Muse 自己的参数面板（`work/museapi-video/platform/ParamsPanel.jsx` 是本仓已存的第三来源）。

### 4.4 拖拽 reference 图：落到卡片边缘变"参考条"

- 拖入图片 → 卡片右缘出现 56px 高的参考条（横向 chips，hover 放大预览，× 移除）；
- 上传走 bridge 已有的 `POST /fs/write`（分片 append → stat 校验 → rename，任一步失败即 delete，见 `docs/PROTOCOL.md:811`），返回 `file_ref` 再进生成请求的 `images/files`；
- 视觉上"参考图属于这张卡"，而不是全局附件库——这是 Krea/Muse 的共同点（参考图跟 prompt 一起被看见）。

### 4.5 对比查看 + 失败重试

- **A/B 对比**：选中两张 → 空格键切换、按住 Shift 做"垂直擦除"对比（一个 `clip-path` 就够，别做同步双相机）；
- **放大查看**：单击图片卡 = 在 HUD 层开一个"灯箱"（同一个 `ImageBitmap`，不做二次解码），滚轮缩放用同一套相机公式，`Esc` 关闭；
- **失败重试**：错误带保留 `error.code`（`quota_exhausted` / `queue_full` / `generation_timeout` / `interrupted`，见 `muse_bridge/app.py` 的 `_submit_task`/`pool.generate_media` 映射），**不要**统一显示"失败了"。`quota_exhausted` 不提供 Retry（换号没用），只给"换账号/稍后"；`queue_full` 给"排队第 k 位"。

---

## 5. 我们该自己写什么、该借什么

| 能力 | 方案 | 理由 | 许可证风险 |
|---|---|---|---|
| 相机/视口（world↔screen、锚点缩放、DPR） | **自研**（~120 行） | 一个矩阵 + 两条公式；任何库都只是包装 | 无 |
| 手势（wheel/pinch/空格拖拽/惯性） | **自研**（~200 行） | 需要精细控制 ctrl 语义与惯性曲线；d3-zoom 的公式可抄（d3 是 **ISC**，且公式本身不受版权保护） | 无（若逐行抄代码需保留 ISC 声明） |
| 卡片渲染（DOM） | **自研** + CSS | 毛玻璃/中文排版/可编辑只有 DOM 能低成本做到 | 无 |
| 卡片组件样式 | **自研**，参考 Excalidraw/Krea 的视觉**结构**，不抄资源 | 视觉风格相似不构成侵权，但素材/字体/图标必须自有许可 | 低 |
| 视口剔除 / LOD / 纹理缓存 | **自研**（~250 行），阈值抄 tldraw 的公开数值 | 阈值是"经验参数"，文档里公开写着 | tldraw 文档可读；**代码不可抄**（自定义许可） |
| 连线（贝塞尔 + 正交） | **自研**（~180 行 SVG path） | 无库能同时满足"零依赖 + 我们的 Handle 语义" | 无 |
| 命中测试/框选/吸附 | **自研**（~300 行） | 与 DOM 卡片强耦合，库的命中模型反而碍事 | 无 |
| z 序 | **整数 `z`（契约 §4）**：置顶/置底/交换，批量 `renumber` | 契约已定整数；≤2000 节点时 O(n) 完全够用，不为"中间插入"提前上分数索引 | 分数索引留到 P3（参考实现 CC0，零风险） |
| 撤销/重做 | **自研** command stack（~150 行） | 需要"按用户意图合并"，库都给不了 | 无 |
| schema + 迁移 | **契约 `Doc` v1 为准**；JSON Canvas 1.0 只做 P3 的导入/导出转换层 | 契约管字段（改动只能加、要在变更记录登记）；规范 MIT 可自由实现，直接换来 Obsidian 互操作 | MIT，实现时在文档致谢规范 |
| 持久化 | **服务端 SQLite 为准**（`canvas_spaces`，已有 `canvas_store.py`）+ 前端 IndexedDB 缓存 | 避免 localStorage 5MB 坑；并发写以 `updated_at` 收敛 | 无 |
| minimap | **自研** Canvas2D（~80 行） | React Flow 的 MiniMap 是 React 组件，不可用 | 无（借视觉结构） |
| PNG 导出 | **自研** Canvas2D 直绘 + 可选 SVG 管线 | html2canvas 明确不支持我们的关键样式 | 无（SVG 管线思路参考 tldraw 文档） |
| 自动布局 | **自研**网格/分层（~100 行） | dagre（MIT）/elkjs（EPL-2.0，**本轮未核实**）的体积与构建成本都不值；我们只要网格 + 简单分层 | 无 |
| 图标 | **手写 SVG sprite**（契约 §7 的 `assets/icons.svg`）；不够用时再 vendor Lucide 子集（ISC） | 20 个图标内联，零依赖；契约倾向手写 | Lucide 为 ISC，需保留声明 |
| 字体 | **系统字体栈**（`system-ui`/Segoe UI Variable），可选 Inter（OFL） | 零下载、零许可风险 | OFL 若用 Inter |
| 协作（将来） | 契约已给稳定 id + 可独立写字段 + 服务端 `updated_at` 收敛；将来接 Yjs | 本版不做，但 schema 不留坑 | Yjs MIT（未引入） |
| **不引入** | tldraw / Excalidraw / React Flow / Konva / Fabric / Pixi / ComfyUI 前端 / 任何 AGPL | 与零构建冲突 + 许可红线 | — |

---

## 6. 视觉设计建议（可直接粘进 CSS 的具体数值）

### 6.1 暗色色板（violet 主色 + cyan 次色，避免"又一堆紫色"的模板感）

```css
:root{
  /* 底色：近黑，带一点点蓝，避免纯黑在 OLED 上"糊成一片" */
  --bg-0:#0B0C10;      /* 画布背景 */
  --bg-1:#101218;      /* 面板/侧栏 */
  --bg-2:#161923;      /* 卡片 */
  --bg-3:#1D2130;      /* 卡片 hover / 输入框 */
  --hair:rgba(255,255,255,.07);   /* 1px 分隔线 */
  --hair-strong:rgba(255,255,255,.14);

  --txt-0:#E8EAF0;     /* 主文字 */
  --txt-1:#A7AEBD;     /* 次文字 */
  --txt-2:#6E7686;     /* 占位/禁用 */

  --accent:#7C5CFF;    /* 主色：操作、选中、连线高亮 */
  --accent-soft:rgba(124,92,255,.16);
  --accent-line:rgba(124,92,255,.45);
  --cyan:#22D3EE;      /* 次色：图片/生成中 */
  --amber:#F59E0B;     /* 视频/队列中 */
  --green:#34D399;     /* 完成 */
  --red:#F87171;       /* 失败 */
}
```

### 6.2 字阶 / 间距 / 圆角

| token | 值 | 用途 |
|---|---|---|
| `--fs-11` | 11px / line-height 16 | 参数 chips、角标 |
| `--fs-12` | 12px / 16 | 元信息、时间 |
| `--fs-13` | 13px / 18 | 卡片正文（中文最小可读） |
| `--fs-14` | 14px / 20 | 面板标签、按钮 |
| `--fs-16` | 16px / 24 | 卡片标题 |
| `--fs-20` | 20px / 28 | 分区标题 |
| `--fs-28` | 28px / 36 | 空状态标题 |
| 字重 | 400 / 500 / 600 | 正文 / 强调 / 标题（不要用 700，暗底会"发胖"） |
| 字距 | 正文 `0`；标题 `-0.01em`；全大写标签 `0.04em` | — |
| 间距刻度 | `2 4 6 8 12 16 20 24 32 40 48 64` | 4 的倍数，禁止 5/7/9 这类奇数 |
| 圆角 | 卡片 14 / 面板 12 / 按钮 8 / chip 999 | — |

### 6.3 玻璃拟态参数（三档，按层级用，别全站一个 blur）

```css
/* 卡片（默认） */
.glass-card{
  background:linear-gradient(180deg, rgba(30,34,46,.86), rgba(22,25,33,.82));
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border:1px solid rgba(255,255,255,.10);
  border-radius:14px;
  box-shadow: 0 1px 0 rgba(255,255,255,.06) inset,
              0 8px 28px rgba(0,0,0,.45),
              0 1px 2px rgba(0,0,0,.5);
}
/* 浮层/参数面板：更厚、更暗，保证可读性 */
.glass-panel{
  background:rgba(14,16,22,.78);
  backdrop-filter: blur(28px) saturate(120%);
  border:1px solid rgba(255,255,255,.08);
  box-shadow:0 24px 64px rgba(0,0,0,.55);
}
/* 选中卡：不要换背景色，加一圈"外发光 + 内描边" */
.card.is-selected{
  box-shadow:0 0 0 1px var(--accent-line),
             0 0 0 4px var(--accent-soft),
             0 12px 32px rgba(0,0,0,.5);
}
```

> 性能提醒：`backdrop-filter` 在缩放/拖拽期间开销大——**手势进行中**把卡片换成纯色半透明（`backdrop-filter:none`），`pointerup` 后 120ms 恢复（[MDN backdrop-filter](https://developer.mozilla.org/en-US/docs/Web/CSS/backdrop-filter)）。

### 6.4 网格点/线

```css
/* 点阵：间距 = 契约 gridSize（默认 28 世界单位，见 CANVAS_DESIGN.md §6）；
   缩放时只改 background-size/position，不重画 DOM */
.canvas-grid{
  background-color:#0B0C10;
  background-image:radial-gradient(rgba(255,255,255,.085) 1px, transparent 1px);
  background-size:calc(28px * var(--zoom)) calc(28px * var(--zoom));
  background-position:var(--tx) var(--ty);
}
/* 过密/过疏时按 2 的幂阶梯缩放网格（契约 §6 的"2x/0.5x 阶梯"）：
   gridPx = 28 * zoom * step，step ∈ {0.25,0.5,1,2,4}，让 gridPx 落在 [14, 56] 之间 */
```

- 点 1px、透明度 **8.5%**；主网格线（每 5 格）透明度 **4%**，1px；
- `zoom ∈ [0.05, 0.4]` 只画主网格线；`zoom > 2.5` 隐藏主网格线（只剩点），避免"密恐"。

### 6.5 hover / active / focus

```css
.card:hover{ transform:translateY(-1px); border-color:rgba(255,255,255,.16); transition:transform 120ms var(--ease-out), border-color 120ms linear; }
.card:active{ transform:translateY(0) scale(.995); }
.btn:hover{ background:var(--bg-3); }
.btn:active{ transform:scale(.97); }
:focus-visible{ outline:none; box-shadow:0 0 0 2px rgba(124,92,255,.45), 0 0 0 1px rgba(255,255,255,.2) inset; }
```

### 6.6 动效时长与曲线

| 场景 | 时长 | 曲线 |
|---|---|---|
| hover / 颜色 | 120ms | `cubic-bezier(.2,0,.2,1)` |
| 面板开合 | 200ms | `cubic-bezier(.2,.8,.2,1)` |
| 卡片出现（生成完成） | 240ms | `cubic-bezier(.16,1,.3,1)`（"落定"感） |
| 相机动画（zoomToFit/双击回正） | 320ms | `cubic-bezier(.4,0,.2,1)`；tldraw 用 `easeInOutCubic` 亦可 |
| 弹窗/灯箱 | 180ms in / 140ms out | in `(.2,.8,.2,1)`，out `(.4,0,1,1)`（出场更快） |
| 进度环 | 线性 1.2s 循环 | `linear` |

命名：`--ease-out: cubic-bezier(.2,0,.2,1)`、`--ease-pop: cubic-bezier(.16,1,.3,1)`、`--ease-in-out: cubic-bezier(.4,0,.2,1)`。**所有动效在 `pointerdown` 期间禁用**（`.is-gesturing * { transition:none !important }`），否则拖拽会"追不上手"。

### 6.7 三个外部视觉参考（Dribbble / Awwwards）

| # | 链接 | 看什么 | 可信度 |
|---|---|---|---|
| 1 | [dribbble.com/tags/infinite_canvas](https://dribbble.com/tags/infinite_canvas) | 无限画布标签下的卡片密度、缩略图导航排版 | 抓取返回 HTTP 202 空 body（反爬），**内容未证实**，需人眼打开 |
| 2 | [awwwards.com/websites/infinite-canvas](https://www.awwwards.com/websites/infinite-canvas/) | 获奖站点的画布类信息层级与暗色质感 | 抓取 200（页面为 JS 渲染，列表内容未取到） |
| 3 | [awwwards.com/inspiration_search/infinite-canvas](https://www.awwwards.com/inspiration_search/infinite-canvas/) | 同上的检索视图，适合找具体案例 | 抓取 200，同上 |

> 补充（可 200% 确认的"活参考"）：[tldraw showcase](https://tldraw.dev/showcase)、[Excalidraw](https://excalidraw.com/)、[makereal.tldraw.com](https://makereal.tldraw.com/)。这三个可直接打开，且能对着截图量间距。

---

## 7. 必须避开的坑（每条：现象 → 机理 → 对策 → 出处）

### 7.1 DOM 节点过多导致卡顿

**现象**：节点到 300+ 后拖拽掉到 20fps，DevTools 显示 `Recalculate Style` / `Paint` 爆表。
**机理**：每个卡片 = 若干 DOM 节点 + 阴影/毛玻璃，合成与重绘成本随可见节点数线性上升；而**不可见节点不剔除就仍在样式计算里**。
**对策**：视口剔除（AABB + 200px 预取）→ 不可见 `display:none`（不是 `visibility:hidden`）；卡片加 `contain: layout paint style`；单卡节点数控制在 ~15 个以内；`maxShapesPerPage` 类比设 4000。
**出处**：tldraw 实测"10,000 shapes 可能只渲染 50 个"（[Performance](https://tldraw.dev/sdk-features/performance)）；tldraw 用 `display:none` 做剔除；Konva 的 `visible(false)` 同时跳过绘制与命中（[Konva perf](https://konvajs.org/docs/performance/All_Performance_Tips.html)）。

### 7.2 图片 blob 内存泄漏

**现象**：翻几十张图后内存持续上涨，`about:memory` 里 blob 不释放，标签页最终 OOM。
**机理**：`URL.createObjectURL` 创建的 blob URL 在文档生命周期内**不会**自动回收；`ImageBitmap` 也要显式 `close()`。
**对策**：每个 token 一条缓存记录，LRU 上限 120MB；淘汰时**同时** `URL.revokeObjectURL` + `bitmap.close()`；节点删除/切 space 时清空该 space 的缓存；滚动预取只保留 ±1 屏。
**出处**：[URL.revokeObjectURL](https://developer.mozilla.org/en-US/docs/Web/API/URL/revokeObjectURL)、[ImageBitmap.close](https://developer.mozilla.org/en-US/docs/Web/API/ImageBitmap/close)、Konva 的"每个 layer 两块 canvas"开销说明（[Konva perf](https://konvajs.org/docs/performance/All_Performance_Tips.html)）。

### 7.3 坐标抖动 / 亚像素模糊

**现象**：1px 边框在拖拽时"一闪一闪"，静止时发灰；卡片边缘出现 0.5px 亮线。
**机理**：世界坐标含小数 + `camera.x/y` 是浮点 → 屏幕位置落在半像素；CSS 会做抗锯齿，1px 线被摊成 2 个半透明像素。
**对策**：卡片位置入模 `round(x*2)/2`；边框改用 `box-shadow: 0 0 0 1px`（阴影不参与布局，亚像素表现更稳）；网格 `background-position` 用整数 CSS px；**相机平移本身不要取整**（会顿）。
**出处**：[Canvas 优化（避免半像素）](https://developer.mozilla.org/en-US/docs/Web/API/Canvas_API/Tutorial/Optimizing_canvas)、[devicePixelRatio](https://developer.mozilla.org/en-US/docs/Web/API/Window/devicePixelRatio)。

### 7.4 缩放时字体渲染发虚

**现象**：放大到 200% 后中文变糊，缩回 100% 又好。
**机理**：一旦容器被提升为合成层（`will-change: transform` / `translateZ(0)` / `backdrop-filter` 的某些组合），Chrome 只在 1x 位图上做缩放，不再按新 scale 重栅格化。
**对策**：世界容器**不加** `will-change`；手势结束后用一次 `transform` 微调（`+0.001px`）触发重栅格；`zoom > 2` 时对**少量可见**文字卡改用真实字号（`font-size: calc(13px * var(--zoom))`），而不是靠放大位图；导出走 SVG 管线（矢量）而不是截图。
**出处**：[will-change](https://developer.mozilla.org/en-US/docs/Web/CSS/will-change)（明确提醒"不要长期挂在大量元素上"）；tldraw 的 LOD 里"文字阴影在 zoom<0.35 关闭"是同类取舍（[Performance](https://tldraw.dev/sdk-features/performance)）。
**验收**：Day 1 结束前，把画布拖到 200% 截一张图，用 1:1 像素看中文边缘——模糊就是这条没做对。

### 7.5 触控/触屏冲突

**现象**：触控板双指滑动时页面跟着滚；触屏上拖卡片变成页面滚动；长按弹出系统菜单。
**机理**：浏览器默认的 `touch-action` / 手势识别会抢走 pointer 事件。
**对策**：画布容器 `touch-action: none`；`pointerdown` 后 `setPointerCapture`；`contextmenu` 在卡片上 `preventDefault`（但保留输入框的粘贴菜单）；`wheel` 用 `{passive:false}`。
**出处**：[touch-action](https://developer.mozilla.org/en-US/docs/Web/CSS/touch-action)、[Pointer events](https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events)、[setPointerCapture](https://developer.mozilla.org/en-US/docs/Web/API/Element/setPointerCapture)。

### 7.6 导出图片时画布被污染（tainted canvas）

**现象**：`canvas.toBlob()` 抛 `SecurityError: The canvas has been tainted by cross-origin data`。
**机理**：任何跨源图片只要**没有**带 CORS 头（或 `<img crossorigin>` 未设），就会把画布标记为不可读。
**对策**（按优先级）：
1. **同源**：图片全部走 `/canvas/api/media/{token}`（bridge 代理，和 UI 同源），根本不跨源；
2. 若必须跨源，`img.crossOrigin='anonymous'` + 服务端 `Access-Control-Allow-Origin`（且不能是 `*` 带凭据）；
3. 兜底：由服务端渲染导出（Python 侧拼图），前端只发坐标。
**出处**：[CORS enabled image](https://developer.mozilla.org/en-US/docs/Web/HTML/CORS_enabled_image)、[toDataURL](https://developer.mozilla.org/en-US/docs/Web/API/HTMLCanvasElement/toDataURL)。
**本仓特有缺口**：`require_key` 只读 `Authorization` 头（`muse_bridge/app.py:90-96`），`<img>` 发不了这个头。所以必须加 `/canvas/api/media/{token}` 同源代理（或改走 cookie session，见 §8.4 的方案 A/B），**不要**把 key 塞 query string（会进日志、进 Referer）。

### 7.7 localStorage 容量

**现象**：文档稍大就 `QuotaExceededError`，或写入时整页卡一下。
**机理**：localStorage 通常 5MB 级、同步 API、字符串化 JSON 双倍膨胀。
**对策**：文档进 IndexedDB（按站点配额，可到 GB 级）；localStorage 只存 `theme` / 最后相机 / 面板宽度这类 < 1KB 的偏好；写入前用 `navigator.storage.estimate()` 展示占用。
**出处**：[Storage quotas and eviction](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_limits)、[localStorage](https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage)。

### 7.8 html2canvas 这类"截 DOM"方案的隐性坑

**现象**：导出的 PNG 里毛玻璃变成纯色、卡片阴影消失、圆角外的背景色不对。
**机理**：它用自己实现的 CSS 渲染器，**明确不支持** `box-shadow`、`filter`、`mix-blend-mode`，`transform` 仅有限支持（[features 原文](https://html2canvas.hertzen.com/features)）。
**对策**：不用它。走 §3.7 的两条路（Canvas2D 直绘 / SVG 栅格化）。

### 7.9 其它两个容易踩的

- **rAF 里读布局**：在 `pointermove` 中读 `offsetWidth`/`getBoundingClientRect` 会强制同步布局，一帧多次读 = 掉帧；所有几何量预先在模型里算好。
- **`backdrop-filter` 叠太多**：可见卡片 > 30 张同时开 blur 会显著掉帧；手势期间统一关掉（§6.3）。

---

## 8. Lead 可直接执行的选型决定

### 8.1 技术栈（一句话）

> **零依赖、零构建、纯静态**：`muse_bridge/canvas/` 下 `index.html` + ES Module（`type="module"`，无 import map、无打包器）+ 三层 CSS；由 `canvas_api.py` 暴露的 `STATIC_DIR` 用 FastAPI `StaticFiles` 挂到 `/canvas`；数据面 `PUT/GET /canvas/api/spaces/{id}`（已落地，SQLite）+ 前端 IndexedDB 缓存；生成面直接复用 `/v1/images/tasks`、`GET /v1/images/tasks/{id}`、`/v1/videos`；参考图上传复用 `/fs/write`。**契约见 `docs/CANVAS_DESIGN.md`，本文只补"怎么做"。**

### 8.2 依赖清单（**目标为零**）

| 类别 | 依赖 | 说明 |
|---|---|---|
| 运行时 JS | **无** | 不用 React/Vue/Konva/Fabric/Pixi/d3/htm/preact |
| 构建工具 | **无** | 不装 node/pnpm；改 `muse_bridge/canvas/` 下的文件即生效 |
| 图标 | **手写 SVG sprite**（契约 §1/§7 的 `assets/icons.svg`）；如需更多图标，可用 Lucide 子集（ISC）并附 `LICENSE-lucide.txt` | 契约倾向手写，避免任何第三方资产 |
| 字体 | 系统栈 `system-ui, "Segoe UI Variable", "Microsoft YaHei UI"` | 不下载字体，避免 OFL 分包与首屏抖动 |
| 服务端 | 现状 FastAPI/uvicorn（已存在） | P1 后端已落地，只差 `app.py` 两行挂载，不加新 PyPI 包 |

### 8.3 目录结构（**按契约 §1 落位，不要另起目录**）

```
muse-bridge/
├─ muse_bridge/
│  ├─ canvas/                 # 前端静态资产（契约指定的根目录）
│  │  ├─ index.html           # 唯一 HTML；首屏只加载 js/app.js
│  │  ├─ css/
│  │  │  ├─ base.css          # §6 的主题变量、重置、布局
│  │  │  ├─ canvas.css        # 网格/层/相机/HUD/节点/连线
│  │  │  └─ ui.css            # 工具栏、抽屉、检查器、toast
│  │  ├─ js/
│  │  │  ├─ app.js            # 装配：store + view + renderer + 快捷键
│  │  │  ├─ view.js           # 相机（§3.1/3.2 全部公式）+ 命中/框选
│  │  │  ├─ store.js          # Doc 状态 + undo/redo（command stack）
│  │  │  ├─ nodes.js          # 四类节点视图（image/video/prompt/text）
│  │  │  ├─ api.js            # 空间 CRUD + 任务提交/轮询（六态映射）
│  │  │  ├─ storage.js        # IndexedDB 缓存 + 离线降级
│  │  │  ├─ ui.js             # 工具栏/检查器/空状态/快捷键面板
│  │  │  └─ export.js         # PNG 导出（Canvas2D 直绘）
│  │  └─ assets/icons.svg     # 手写 SVG sprite
│  ├─ canvas_api.py           # 已有：APIRouter(prefix="/canvas/api") + STATIC_DIR
│  ├─ canvas_store.py         # 已有：canvas_spaces 表 + Doc 校验
│  └─ app.py                  # Lead 加两行（见 8.4 Day 0）
└─ tests/test_canvas_api.py / test_canvas_store.py   # 已有，新增前端无自动化
```

> 契约给了 8 个模块的最小集；实现时可在 `js/` 内部再拆 `camera.js`/`render.js`/`hit.js`/`minimap.js`，但**入口保持 `app.js` 一个**，避免契约 §1 的归属混乱。
>
> **落盘快照（2026-11-11 04:49）**：已有 `index.html`（`index.html:211` 已经写死 `<script type="module" src="js/app.js">`）、`css/{base,canvas,ui}.css`、`assets/icons.svg`、`js/{store,storage}.js`；**仍缺** `js/app.js`、`view.js`、`nodes.js`、`api.js`、`ui.js`、`export.js`。所以入口名就是 `app.js`——契约 §8.1 里那处 `canvas.js` 是笔误，不要为它改名。

### 8.4 实施顺序（Day 0 / 1 / 2 / 3，可直接排期）

> 前提：**P1 后端已落地**（`canvas_api.py`、`canvas_store.py`、两个测试文件都在仓里）。所以 Day 0 只剩接线，Day 1–3 全是前端。

**Day 0 —— 接线（**已完成**，仅剩一个决策）**

| 动作 | 状态 | 依据 / 要点 |
|---|---|---|
| 挂路由 | ✅ 已完成 | `app.py:1481` `app.include_router(canvas_router)` |
| 挂静态 | ✅ 已完成 | `app.py:1485-1488` `app.mount("/canvas", StaticFiles(directory=STATIC_DIR, html=True))`，目录不存在时跳过挂载（不会让 app 起不来） |
| **顺序约束** | ✅ 已遵守 | **必须先 `include_router` 再 `mount`**：`StaticFiles(html=True)` 会把 `/canvas/api/spaces` 当静态路径去查，先 mount 就会 404（`app.py:1478-1480` 有注释） |
| **拍板媒体鉴权缺口** | ⬜ **待 Lead 决定** | 见下方 A/B 方案，决定后写进契约变更记录 |

**唯一需要决策的接口问题（媒体鉴权）**：`require_key` 只认 `Authorization: Bearer`（`app.py:90-96`），而 `<img src="/v1/media/<token>">` **发不了请求头**。两条路：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A（推荐）同源代理** | 在 `canvas_api.py` 加 `GET /canvas/api/media/{token}`，服务端用 `MediaStore().read(token)` 回 `Response`（带 `content-type`/`Range` 可选） | 一条路由解决"鉴权 + 不污染画布 + 可导出"；前端 `<img>` 零改造 | 多一次服务端读盘（本地文件，成本可忽略） |
| B 浏览器 session | `/canvas` 发 HttpOnly cookie（SameSite=Strict）+ 写操作 CSRF token，`require_canvas_key` 同时接受 cookie | 更符合"浏览器 UI"直觉 | 要改认证层，波及面更大；跨端调试变麻烦 |

> 不管选哪个，**都不要**把 API key 放进 query string（会进 access log / Referer）。

**Day 1 —— 骨架 + 手感（当天必须有可拖可缩的原型）**

| 时段 | 交付 | 验收 |
|---|---|---|
| 上午 | `index.html` + `base.css`（§6 变量）+ `canvas.css` 网格 | `GET /canvas` 见暗色点阵；无 console error；首屏 < 1s（契约 §8） |
| 上午 | `view.js`：契约坐标公式、锚点缩放、`zoom ∈ [0.05,8]`、DPR | 缩放时**光标下的点不动**；0.05 与 8 两端不漂 |
| 下午 | 手势：滚轮平移、`Ctrl/Cmd+滚轮`/pinch 缩放、空格/中键拖拽、惯性、双击回正 | 触控板双指不滚页面；双击 250ms 回正；惯性可停 |
| 下午 | 5 张假卡片（DOM）+ `contain: layout paint style` + 剔除桩 | 200% 缩放截图，中文边缘不糊（§7.4） |
| 收尾 | 灌 500 个假节点，`performance.now()` 记帧时 | 可见集裁剪后 pan ≥ 50fps；契约底线是 100+ 节点流畅 |

**Day 2 —— 节点系统 + 状态（当天必须能编辑并刷新不丢）**

| 时段 | 交付 | 验收 |
|---|---|---|
| 上午 | `store.js`：契约 `Doc` 模型、整数 `z`、增删改查、脏标记 | 新建/删除/置顶/置底正确；`nodes ≤ 2000` 时提交不报 400 |
| 上午 | 选择：单选/多选/框选/Shift 加减选；`view.js` 命中 | 框选 200 张卡 < 8ms |
| 下午 | `nodes.js` 四类卡 + 拖动/缩放/删除 + 吸附（6px 阈值） | 拖动只改 transform，`pointerup` 才写模型 |
| 下午 | undo/redo：拖动合并为一条；`Ctrl+Z`/`Ctrl+Shift+Z` | 一次拖拽 = 一次撤销；50 步不丢 |
| 收尾 | `api.js` + `storage.js`：`PUT /canvas/api/spaces/{id}` 保存、IndexedDB 兜底 | 刷新后从服务端恢复；断网时读缓存；400 错误原样展示 `note` |

**Day 3 —— 生成闭环 + 精修（当天必须能"提示词→图"并导出）**

| 时段 | 交付 | 验收 |
|---|---|---|
| 上午 | `api.js` 生成段：`POST /v1/images/tasks`（`Idempotency-Key` = `nodeId:attempt`）、1.5s→5s 退避轮询、**六态映射**、`queue_full`/`quota_exhausted` 分支 | 提交后卡片原地转"生成中"；失败卡保留参数可重试 |
| 上午 | 批量：多选 N 张 → N 个任务 → 接触表 → 点选提升 | 第 9 个显示"排队第 k 位"（`JOB_QUEUE_MAX=8`），不转圈死等 |
| 下午 | 检查器面板（只暴露 `duration/aspect_ratio/model` + prompt，见 `docs/FRONTEND_RE.md` §1）、参考图 chips（`/fs/write`） | 未证实参数 disabled + tooltip |
| 下午 | `export.js`（Canvas2D 直绘，2x）+ 可选 minimap | 导出图玻璃/阴影正确；同源媒体不 taint 画布 |
| 收尾 | 键盘：空格/Delete/Ctrl+D/Ctrl+Z/Ctrl+0/Home/Esc；空状态与骨架屏；1440×900 截图交 Lead（契约 §8.3） | 全键盘完成"新建→生成→导出" |

### 8.5 一句话风险提示（给 Lead 拍板用）

- **不要**为了"少写 200 行"引 tldraw：省下的 200 行会变成 license 谈判 + 水印 + React 构建链（§2.1）。
- **不要**用 html2canvas 导出：我们的视觉核心（毛玻璃/阴影）它明确不支持（§7.8）。
- **先拍板媒体鉴权**（§8.4 的 A/B 方案），否则 Day 3 的图片卡与导出会一起卡住。
- **状态枚举按代码、不按契约那一行**（§0.5）：六态，不是四态。

---

## 9. 未证实 / 待复核清单

| 项 | 状态 | 说明 |
|---|---|---|
| Dribbble/Awwwards 三个页面里的具体案例内容 | **未证实** | 抓取被反爬/JS 渲染挡住；链接可达（202/200），内容需人眼确认 |
| Konva 仓库 `LICENSE` 文件正文 | **corroborated** | GitHub API 返回 `NOASSERTION`（未识别），但 npm registry 明确 `"license":"MIT"`；以 npm 元数据为准 |
| elkjs / dagre 的许可证 | **未核实** | 本文未引用其条款，仅列为"不引入"候选 |
| Excalidraw 手绘字体（Virgil 等）的许可 | **未证实** | 未逐一核对字体文件许可，按"不用"处理 |
| tldraw 旧版本（v1）是否曾为 Apache-2.0 | **未证实** | 与本文结论无关（现版本为自定义许可），故不展开 |
| PixiJS 在"仅图片平面"场景的实际收益 | **hypothesis** | 需在 400+ 图片节点时做 A/B 实测才能定论 |
| muse 自身画布（spaces/idea-cards）的交互细节 | **hypothesis** | 仅有 `docs/FRONTEND_RE.md` 的逆向记录与二手观察，无一手截图证据 |
| Krea / Fal 的具体交互细节（栏位数量、就地替换等描述） | **hypothesis** | 基于产品页与公开演示的概述，未逐一截图比对；只作方向参考 |
| Excalidraw "hidden textarea 做文字编辑"的实现细节 | **hypothesis** | 是其公开架构特征（`TextWysiwyg` 类组件），本文未逐行读源码 |
| 契约 §8 测试口径里的 `canvas.js` | **observed 冲突** | `docs/CANVAS_DESIGN.md` §7 用 `js/app.js`，§8.1 写 `canvas.js`；建议统一为 `app.js` |
| 契约 §3.2 的任务状态四态 | **observed 冲突** | 与 `app.py:811-816`/`jobs.py:72-80` 的六态不一致，以代码为准 |

---

## 10. 参考链接汇总（外部，按主题）

**许可 / 项目**
1. https://github.com/tldraw/tldraw
2. https://github.com/tldraw/tldraw/blob/main/LICENSE.md
3. https://tldraw.dev/community/license
4. https://tldraw.dev/pricing
5. https://tldraw.dev/legal/trademark-guidelines
6. https://github.com/excalidraw/excalidraw
7. https://github.com/excalidraw/excalidraw/blob/master/LICENSE
8. https://github.com/xyflow/xyflow
9. https://github.com/xyflow/xyflow/blob/main/LICENSE
10. https://github.com/konvajs/konva
11. https://www.npmjs.com/package/konva
12. https://github.com/fabricjs/fabric.js
13. https://github.com/fabricjs/fabric.js/blob/master/LICENSE
14. https://github.com/pixijs/pixijs
15. https://github.com/pixijs/pixijs/blob/dev/LICENSE
16. https://github.com/Comfy-Org/ComfyUI_frontend
17. https://github.com/Comfy-Org/ComfyUI_frontend/blob/main/LICENSE
18. https://github.com/Comfy-Org/ComfyUI
19. https://github.com/Comfy-Org/ComfyUI/blob/master/LICENSE
20. https://github.com/invoke-ai/InvokeAI
21. https://github.com/invoke-ai/InvokeAI/blob/main/LICENSE
22. https://github.com/penpot/penpot
23. https://github.com/penpot/penpot/blob/main/LICENSE
24. https://github.com/toeverything/AFFiNE
25. https://github.com/toeverything/AFFiNE/blob/canary/LICENSE
26. https://github.com/toeverything/AFFiNE/blob/canary/packages/backend/server/LICENSE
27. https://github.com/lucide-icons/lucide
28. https://github.com/lucide-icons/lucide/blob/main/LICENSE

**规范 / 体积 / 文档**
29. https://jsoncanvas.org/
30. https://jsoncanvas.org/spec/1.0/
31. https://github.com/obsidianmd/jsoncanvas
32. https://bundlephobia.com/api/size?package=konva@9.3.0
33. https://bundlephobia.com/package/tldraw
34. https://tldraw.dev/sdk-features/performance
35. https://tldraw.dev/sdk-features/camera
36. https://tldraw.dev/sdk-features/image-export
37. https://tldraw.dev/sdk-features/history
38. https://tldraw.dev/sdk-features/parenting
39. https://tldraw.dev/sdk-features/groups
40. https://tldraw.dev/sdk-features/shape-indexing
41. https://tldraw.dev/sdk-features/culling
42. https://konvajs.org/docs/performance/All_Performance_Tips.html
43. https://reactflow.dev/learn/advanced-use/performance
44. https://reactflow.dev/api-reference/components/minimap
45. https://d3js.org/d3-zoom
46. https://docs.yjs.dev/
47. https://github.com/rocicorp/fractional-indexing
48. https://html2canvas.hertzen.com/features
49. https://github.com/tldraw/make-real
50. https://tldraw.dev/blog/product/make-real-the-story-so-far/
51. https://www.krea.ai/ · https://fal.ai/ · https://muse.ai/

**平台 / 浏览器行为**
52. https://developer.mozilla.org/en-US/docs/Web/API/Window/devicePixelRatio
53. https://developer.mozilla.org/en-US/docs/Web/API/CanvasRenderingContext2D/setTransform
54. https://developer.mozilla.org/en-US/docs/Web/API/Canvas_API/Tutorial/Optimizing_canvas
55. https://developer.mozilla.org/en-US/docs/Web/API/Window/requestAnimationFrame
56. https://developer.mozilla.org/en-US/docs/Web/API/Element/wheel_event
57. https://developer.mozilla.org/en-US/docs/Web/API/WheelEvent/ctrlKey
58. https://developer.mozilla.org/en-US/docs/Web/API/WheelEvent/deltaMode
59. https://developer.mozilla.org/en-US/docs/Web/API/Pointer_events
60. https://developer.mozilla.org/en-US/docs/Web/API/Element/setPointerCapture
61. https://developer.mozilla.org/en-US/docs/Web/CSS/touch-action
62. https://developer.mozilla.org/en-US/docs/Web/CSS/will-change
63. https://developer.mozilla.org/en-US/docs/Web/CSS/backdrop-filter
64. https://developer.mozilla.org/en-US/docs/Web/HTML/CORS_enabled_image
65. https://developer.mozilla.org/en-US/docs/Web/API/HTMLCanvasElement/toDataURL
66. https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_limits
67. https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage
68. https://developer.mozilla.org/en-US/docs/Web/API/URL/revokeObjectURL
69. https://developer.mozilla.org/en-US/docs/Web/API/ImageBitmap/close
70. https://developer.mozilla.org/en-US/docs/Web/API/OffscreenCanvas
71. https://openfontlicense.org/
72. https://rsms.me/inter/

**本仓内部依据（非外链，按文件:行号可复核）**
- `muse_bridge/app.py:90-96`（`require_key` 只认 Bearer）、`:811-816`（`_normalize_state` 六态归一）、`:844-870`（生成请求字段）、`:943-1046`（任务提交/轮询/视频）、`:1049-1079`（媒体 Range）、`:1478-1488`（canvas 路由挂载顺序）
- `muse_bridge/jobs.py:50-97`（队列 TTL/上限 8/六态/失败分类）
- `muse_bridge/canvas_api.py`（`APIRouter(prefix="/canvas/api")`、`STATIC_DIR`、`require_canvas_key`）
- `muse_bridge/canvas_store.py`（SQLite `canvas_spaces`、Doc 校验上限）
- `docs/CANVAS_DESIGN.md` §4（`Doc` v1/2000 节点/4MB/`data.status` 五态）、§6（坐标与 `gridSize=28`）、§7（模块划分）
- `muse_bridge/canvas/index.html:211`（`<script type="module" src="js/app.js">`，入口名证据）
- `docs/PROTOCOL.md:811`（`/fs/write` 三步上传）
- `docs/FRONTEND_RE.md` §1（仅 `duration/aspect_ratio/model` 可入白名单）
