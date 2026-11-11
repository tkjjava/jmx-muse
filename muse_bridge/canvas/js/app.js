/**
 * app.js — 组装层：数据源启动、画布手势、生成调度、自动保存
 *
 * 这一层是唯一同时知道 store / view / UI 的地方，负责把"用户意图"翻译成
 * store 事务。这样拆分的原因：
 *   - view.js 只做坐标与裁剪，不碰数据；
 *   - nodes.js 只画卡片，不处理手势；
 *   - ui.js 只呈现面板，写入全部回调到 actions；
 *   - app.js 统一控制"什么时候提交事务"（拖动 pointerup、文本失焦），
 *     避免各个模块各自写历史导致撤销栈碎片化。
 *
 * 启动顺序刻意先探测 bridge 再加载空间：探测失败就整会话走 localStorage，
 * 但仍把失败原因 toast 出来；探测只给 1.5s，不让没后端的静态页面白屏。
 */

import {
  CanvasStore,
  createEmptyDoc,
  normalizeDoc,
  newNodeId,
  clamp,
  LIMITS,
} from "./store.js";
import {
  LocalBackend,
  makeDemoSpace,
  buildDemoDoc,
  makeArtwork,
  DEMO_NAME,
} from "./storage.js";
import { BridgeClient, resolveMediaUrl } from "./api.js";
import { CanvasView } from "./view.js";
import { NodeLayer } from "./nodes.js";
import { CanvasUI } from "./ui.js";
import {
  exportViewportPNG,
  exportFileName,
  downloadBlob,
  downloadUrl,
} from "./export.js";

const AUTOSAVE_MS = 800;
const POLL_MS = 1200;
const MOCK_DELAY = 2500;
const DEFAULT_CAMERA = { x: -200, y: -120, zoom: 1 };
const LAST_SPACE_PREFIX = "muse-canvas:last-space:v1:";
const BATCH_LIMIT = 6;

function hashString(value) {
  let hash = 2166136261;
  const text = String(value || "");
  for (let index = 0; index < text.length; index += 1) {
    hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
  }
  return hash >>> 0;
}

function ratioNumber(size) {
  const match = /^(\d+):(\d+)$/.exec(String(size || ""));
  if (!match) return 1;
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? width / height : 1;
}

function snippet(value, length = 34) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  return clean.length > length ? `${clean.slice(0, length)}…` : clean;
}

function truncate(value, length = 90) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  return clean.length > length ? `${clean.slice(0, length)}…` : clean;
}

function readLastSpace(mode) {
  try {
    return globalThis.localStorage?.getItem(`${LAST_SPACE_PREFIX}${mode}`) || "";
  } catch {
    return "";
  }
}

function writeLastSpace(mode, id) {
  try {
    globalThis.localStorage?.setItem(`${LAST_SPACE_PREFIX}${mode}`, id);
  } catch {
    /* 隐私模式禁用存储时只是失去"上次空间"记忆，不影响当前会话 */
  }
}

/** 只读分享入口：`/canvas/?share=<token>`（或在 `#` 后）。 */
function readShareToken() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("share") || "";
  return token.trim();
}

class CanvasApp {
  constructor() {
    this.dom = {
      stage: document.getElementById("stage"),
      world: document.getElementById("world"),
      grid: document.getElementById("grid"),
      guides: document.getElementById("guides"),
      marquee: document.getElementById("marquee"),
      sizeHint: document.getElementById("size-hint"),
    };

    this.store = new CanvasStore(createEmptyDoc());
    this.view = new CanvasView({
      stage: this.dom.stage,
      world: this.dom.world,
      grid: this.dom.grid,
    });
    this.nodes = new NodeLayer({
      world: this.dom.world,
      view: this.view,
      store: this.store,
    });
    this.api = new BridgeClient();
    this.backend = null;
    this.mode = "offline";
    this.modeNote = "";
    this._currentSpaceName = "";
    this._dirty = false;
    this._version = 0;
    this._saveTimer = 0;
    this._savePromise = null;
    this._jobs = new Map();
    this._genSeq = 0;
    this._active = null;
    this._raf = 0;
    this._spaceDown = false;
    this._bootStarted = false;
    this._exporting = false;
    this._marqueeStamp = 0;
    // 只读分享：只由 `?share=` 启动路径拉起。`_guard()` 包住的 action 全部是
    // 写操作；把它们在入口拦掉而不是依赖按钮禁用，是因为快捷键与命令面板
    // 不经过按钮。
    this.readOnly = false;
    this.shareToken = "";

    const guard = (fn) => (...args) =>
      this.readOnly ? this._denyReadOnly() : fn(...args);

    this.actions = {
      setZoom: (zoom) => this.setZoom(zoom),
      zoomBy: (factor) => this.zoomBy(factor),
      fit: () => this.fit(),
      home: () => this.home(),
      saveNow: guard(() => this.saveNow({ manual: true })),
      undo: guard(() => this.store.undo()),
      redo: guard(() => this.store.redo()),
      exportPNG: () => this.exportPNG(),
      shareSpace: () => this.shareSpace(),
      refreshShare: () => this.refreshShare(),
      createShare: () => this.createShare(),
      rotateShare: () => this.rotateShare(),
      revokeShare: () => this.revokeShare(),
      copyShare: () => this.copyShare(),
      renameSpace: guard((name) => this.renameSpace(name)),
      switchSpace: (id) => this.switchSpace(id),
      newSpace: guard(() => this.newSpace()),
      deleteSpace: guard((id) => this.deleteSpace(id)),
      generate: guard((options) => this.generate(options)),
      regenerate: guard((id) => this.regenerate(id)),
      deleteNodes: guard((ids) => this.deleteNodes(ids)),
      duplicateNodes: guard((ids) => this.duplicateNodes(ids)),
      deleteSelection: guard(() => this.deleteSelection()),
      duplicateSelection: guard(() => this.duplicateSelection()),
      downloadNode: (id) => this.downloadNode(id),
      loadDemo: guard(() => this.loadDemo()),
      clearCanvas: guard(() => this.clearCanvas()),
      newNoteAtCenter: guard(() => this.newNoteAtCenter()),
      // muse 只读适配（P3）：面板懒加载 Spaces / Explore 灵感卡；导入只写本地画布。
      listMuseSpaces: () => this.api.listMuseSpaces({ includeConstructing: true }),
      listMuseIdeaCards: () => this.api.listMuseIdeaCards(),
      importMuseSpace: guard((slug, space) => this.importMuseSpace(slug, space)),
      addIdeaCard: guard((card) => this.addIdeaCard(card)),
      centerOnWorld: (point) => this.centerOnWorld(point),
      retryBridge: () => this.retryBridge(),
    };

    this.ui = new CanvasUI({
      store: this.store,
      view: this.view,
      actions: this.actions,
    });

    this.view.onResize = () => this.scheduleRender();
    this.store.subscribe((event) => this._onStoreEvent(event));
    this._bind();
    this._ticker = setInterval(() => {
      if (!this._jobs.size) return;
      this.store.tick();
      this.scheduleRender();
    }, 420);
  }

  /* ── 启动 ───────────────────────────────────────────────────────────── */

  async start() {
    if (this._bootStarted) return;
    this._bootStarted = true;

    const shareToken = readShareToken();
    if (shareToken) {
      // 分享页不探测空间列表、不落 localStorage，画面只来自 token；
      // 失败时退回本地模式并明确说出原因，而不是白屏。
      try {
        await this._initShared(shareToken);
      } catch (error) {
        console.error("[canvas] 分享画布加载失败", error);
        this.ui.toast(`分享画布加载失败：${error.message}`, {
          type: "error",
          duration: 8000,
        });
        await this._initLocal(`分享链接不可用：${error.message}`);
      }
      this.store.setReady(true);
      this.ui.setEmpty(this.store.nodes.length === 0);
      this.ui.renderToolbar();
      return;
    }

    const probe = await this.api.probe({ timeout: 1500 });
    try {
      if (probe.ok) await this._initBridge(probe.spaces, probe.latency);
      else await this._initLocal(probe.reason);
    } catch (error) {
      console.error("[canvas] 初始化失败", error);
      this.ui.toast(`初始化失败：${error.message}`, {
        type: "error",
        duration: 7000,
      });
      try {
        await this._initLocal(`bridge 初始化失败：${error.message}`);
      } catch (fallbackError) {
        this.ui.toast(`无法加载画布数据：${fallbackError.message}`, {
          type: "error",
          duration: 8000,
        });
        this._applySpace(
          { id: "", name: "未命名画布", doc: createEmptyDoc() },
          [],
        );
      }
    }

    this.store.setReady(true);
    this.ui.setEmpty(this.store.nodes.length === 0);
    this.ui.renderToolbar();
  }

  async _initBridge(spaces, latency) {
    this.mode = "bridge";
    this.store.setMode("bridge");
    let list = Array.isArray(spaces) ? spaces : [];
    let targetId =
      list.find((space) => space.id === readLastSpace("bridge"))?.id ||
      list[0]?.id ||
      "";

    let createdDemo = false;
    if (!list.length) {
      const created = await this.api.createSpace(DEMO_NAME);
      targetId = created.id;
      const demo = buildDemoDoc();
      try {
        await this.api.saveSpace(created.id, { name: DEMO_NAME, doc: demo });
      } catch (error) {
        this.ui.toast(`演示数据写入 bridge 失败：${error.message}`, {
          type: "warn",
          duration: 6000,
        });
      }
      list = await this.api.listSpaces().catch(() => [created]);
      createdDemo = true;
    }

    const data = await this.api.getSpace(targetId);
    const record = {
      id: targetId,
      name: data.space?.name || "未命名画布",
      doc: data.doc || createEmptyDoc(),
    };
    const currentList = list.some((space) => space.id === targetId)
      ? list
      : [...list, { id: targetId, name: record.name }];
    this._applySpace(record, currentList);
    this.modeNote = `已连接 bridge（${latency || 0}ms）`;
    this.ui.setModeInfo({ mode: "bridge", note: this.modeNote });

    if (createdDemo) {
      this.ui.toast(`已连接 bridge，并创建演示空间「${DEMO_NAME}」`, {
        type: "success",
        duration: 4200,
      });
    } else {
      this.ui.toast(`已连接 bridge · ${currentList.length} 个空间`, {
        type: "success",
        duration: 2600,
      });
    }
  }

  /** 只读分享启动：画面来自 `/canvas/api/shares/<token>`（免 key，只读）。 */
  async _initShared(token) {
    const payload = await this.api.getSharedSpace(token);
    this.readOnly = true;
    this.shareToken = token;
    this.mode = "bridge";
    this.store.setMode("bridge");
    const record = {
      id: payload.space?.id || "shared",
      name: payload.space?.name || "分享画布",
      doc: payload.doc || createEmptyDoc(),
    };
    this._applySpace(record, [{ id: record.id, name: record.name }]);
    this.ui.setReadOnly(true, record.name);
    this.ui.setModeInfo({ mode: "bridge", note: "只读分享" });
    this.ui.toast("只读分享 · 可缩放 / 导出，不能编辑或保存", {
      type: "info",
      duration: 5200,
    });
  }

  async _initLocal(reason) {
    this.mode = "offline";
    this.store.setMode("offline");
    this.backend = new LocalBackend();
    if (this.backend.loadError) {
      this.ui.toast(this.backend.loadError, { type: "warn", duration: 6000 });
    }

    let spaces = this.backend.lists();
    let firstDemo = false;
    let id = this.backend.currentId;

    if (!spaces.length) {
      const demo = makeDemoSpace();
      this.backend.setSpaces([demo]);
      this.backend.setCurrent(demo.id);
      this.backend.persist();
      spaces = this.backend.lists();
      id = demo.id;
      firstDemo = true;
    } else {
      const remembered = readLastSpace("offline");
      if (remembered && spaces.some((space) => space.id === remembered)) {
        id = remembered;
      }
      if (!id || !spaces.some((space) => space.id === id)) {
        id = spaces[0].id;
      }
      if (this.backend.setCurrent(id)) this.backend.persist();
    }

    const record = this.backend.get(id) || this.backend.get(spaces[0].id);
    if (!record) throw new Error("本地空间数据为空");
    this._applySpace(record, spaces);
    this.modeNote = reason ? `bridge 未连接：${truncate(reason, 72)}` : "本机存储";
    this.ui.setModeInfo({ mode: "offline", note: this.modeNote });

    this.ui.toast(
      `bridge 未连接：${truncate(reason || "无响应", 60)} · 已使用离线模式`,
      { type: "warn", duration: 6400 },
    );
    if (firstDemo) {
      setTimeout(() => {
        this.ui.toast(
          `已加载演示空间「${DEMO_NAME}」· ${this.store.nodes.length} 张卡片`,
          { type: "info", duration: 3800 },
        );
      }, 700);
    }
  }

  _applySpace(record, spaces) {
    this._cancelAllJobs();
    const doc = normalizeDoc(record?.doc || createEmptyDoc());
    // 刷新页面/切换空间后，上一个会话里没跑完的生成任务已经不存在了。
    // 若原样保留 queued/generating，卡片会永远停在"生成中"且没有重试入口：
    // 有旧图的直接回到 done，prompt 回 idle，其余标 failed 并给重试文案。
    for (const node of doc.nodes) {
      const status = node.data?.status;
      if (status === "queued" || status === "generating") {
        node.data = {
          ...node.data,
          status: node.data?.url
            ? "done"
            : node.type === "prompt"
              ? "idle"
              : "failed",
          taskId: "",
          createdAt: 0,
          error:
            node.data?.url || node.type === "prompt"
              ? ""
              : "页面重新加载后生成任务已中断，请重试",
        };
      }
    }
    this._currentSpaceName = record?.name || "未命名画布";
    this.store.setWorkspace({
      spaces: Array.isArray(spaces) ? spaces : this.store.state.spaces,
      currentSpaceId: record?.id || "",
      mode: this.mode,
    });
    this.store.setDoc(doc, { resetHistory: true });
    this.view.setCamera(doc.camera || DEFAULT_CAMERA);
    this.view.setGridSize(doc.meta?.gridSize || 28);
    this.ui.applyUIState();
    this._dirty = false;
    this._version += 1;
    this.store.setSaveState("saved");
    this.ui.setEmpty(doc.nodes.length === 0);
    if (record?.id) writeLastSpace(this.mode, record.id);
    this.scheduleRender();
  }

  _onStoreEvent(event) {
    this.ui.onStoreEvent(event);
    switch (event.type) {
      case "change":
        this.ui.setEmpty(this.store.nodes.length === 0);
        this.markDirty();
        this.scheduleRender();
        break;
      case "camera":
        this.markDirty();
        this.scheduleRender();
        break;
      case "doc":
        this.ui.setEmpty(this.store.nodes.length === 0);
        this.scheduleRender();
        break;
      case "selection":
      case "history":
        this.scheduleRender();
        break;
      default:
        break;
    }
  }

  /* ── 保存 ───────────────────────────────────────────────────────────── */

  _spaceName() {
    return (
      this._currentSpaceName ||
      this.store.state.spaces.find(
        (space) => space.id === this.store.state.currentSpaceId,
      )?.name ||
      "未命名画布"
    );
  }

  markDirty() {
    if (!this.store.state.ready) return;
    this._dirty = true;
    this._version += 1;
    if (this.store.state.saveState !== "saving") {
      this.store.setSaveState("dirty");
    }
    this._scheduleSave();
  }

  _scheduleSave() {
    if (this.readOnly) return; // 只读分享没有写通道，别排队一次注定失败的 PUT
    if (this._saveTimer) clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => {
      this._saveTimer = 0;
      this.saveNow({ manual: false });
    }, AUTOSAVE_MS);
  }

  async saveNow({ manual = false, silent = false } = {}) {
    if (this.readOnly) {
      if (manual && !silent) this._denyReadOnly();
      return false;
    }
    if (!this.store.state.ready) return false;
    // 手动保存 / 切空间 / 隐藏页面时，先把输入事务提交，保证快照完整；
    // 普通 800ms 自动保存不打断正在输入的用户。
    if (manual || silent) this.ui.flushEdit();
    if (this._savePromise) {
      const ongoing = this._savePromise;
      if (manual && !silent) {
        const ok = await ongoing;
        if (ok) {
          this.ui.toast("画布已保存", { type: "success", duration: 2200 });
        }
      }
      return ongoing;
    }
    if (this._saveTimer) {
      clearTimeout(this._saveTimer);
      this._saveTimer = 0;
    }

    const id = this.store.state.currentSpaceId;
    if (!id) {
      if (!silent) {
        this.ui.toast("当前没有可保存的空间", { type: "warn" });
      }
      return false;
    }

    const version = this._version;
    const name = this._spaceName();
    const doc = this.store.doc;
    this.store.setSaveState("saving");

    this._savePromise = (async () => {
      try {
        if (this.mode === "bridge") {
          await this.api.saveSpace(id, { name, doc });
        } else {
          this.backend.save(id, { name, doc });
        }
        this._currentSpaceName = name;

        if (this._version === version) {
          this._dirty = false;
          this.store.setSaveState("saved");
        } else {
          this.store.setSaveState("dirty");
          this._scheduleSave();
        }

        try {
          await this._refreshSpaces();
        } catch (error) {
          // 空间列表刷新失败不影响"文档已写入"的结论
          console.warn("[canvas] 空间列表刷新失败", error);
        }

        if (manual && !silent) {
          this.ui.toast("画布已保存", { type: "success", duration: 2200 });
        }
        return true;
      } catch (error) {
        this.store.setSaveState("error");
        this._dirty = true;
        if (!silent) {
          this.ui.toast(`保存失败：${error.message}`, {
            type: "error",
            duration: 6400,
          });
        }
        return false;
      } finally {
        this._savePromise = null;
      }
    })();

    return this._savePromise;
  }

  /* ── 只读分享 ───────────────────────────────────────────────────────── */

  _denyReadOnly() {
    this.ui.toast("只读分享：当前画布不可编辑", { type: "warn", duration: 2600 });
    return false;
  }

  /**
   * 分享入口：编辑态打开「分享画布」面板（复制 / 重新生成 / 撤销），只读态
   * 只能复制手里这条链接。
   *
   * 为什么不在这里直接 POST 建 token：撤销/轮换是不可恢复操作，必须先让用户
   * 看到当前链接状态再决定；"点一下分享就换 token"会让已发出去的链接悄悄失效。
   */
  async shareSpace() {
    if (this.readOnly) {
      const url = `${window.location.origin}/canvas/?share=${encodeURIComponent(
        this.shareToken,
      )}`;
      await this._copyShare(url, "分享链接已复制");
      return;
    }
    if (this.mode !== "bridge") {
      this.ui.toast("离线模式不能分享：先连上 bridge", {
        type: "warn",
        duration: 4200,
      });
      return;
    }
    const id = this.store.state.currentSpaceId;
    if (!id) {
      this.ui.toast("当前没有可分享的空间", { type: "warn" });
      return;
    }
    this.ui.openSharePanel();
    await this.refreshShare();
  }

  _shareSpaceId() {
    const id = this.store.state.currentSpaceId;
    if (!id) {
      this.ui.toast("当前没有可分享的空间", { type: "warn" });
      return "";
    }
    return id;
  }

  _shareUrlOf(share) {
    return share?.url ? `${window.location.origin}${share.url}` : "";
  }

  async refreshShare() {
    const id = this._shareSpaceId();
    if (!id) return;
    this.ui.renderSharePanel(null, { loading: true });
    this.ui.setShareStatus("正在读取分享状态…");
    try {
      const share = await this.api.getShare(id);
      this.ui.renderSharePanel(share);
      this.ui.setShareStatus(
        share ? "分享链接有效（只读）" : "尚未分享",
        share ? "ok" : "",
      );
    } catch (error) {
      this.ui.renderSharePanel(null, { error: error.message });
      this.ui.setShareStatus("读取失败", "error");
      this.ui.toast(`读取分享状态失败：${error.message}`, {
        type: "error",
        duration: 7000,
      });
    }
  }

  async createShare() {
    const id = this._shareSpaceId();
    if (!id) return;
    try {
      const share = await this.api.createShare(id);
      this.ui.renderSharePanel(share);
      this.ui.setShareStatus("已生成分享链接", "ok");
      await this._copyShare(this._shareUrlOf(share), "分享链接已复制");
    } catch (error) {
      this.ui.setShareStatus("生成失败", "error");
      this.ui.toast(`创建分享链接失败：${error.message}`, {
        type: "error",
        duration: 7000,
      });
    }
  }

  async rotateShare() {
    const confirmed = await this.ui.confirm({
      title: "重新生成分享链接？",
      message: "旧链接会立即失效，已经拿到旧链接的人将打不开这张画布。此操作不可恢复。",
      confirmLabel: "重新生成",
      danger: true,
    });
    if (!confirmed) return;
    await this.createShare();
  }

  async revokeShare() {
    const share = this.ui.currentShare();
    if (!share?.token) {
      this.ui.toast("当前没有可撤销的分享", { type: "warn" });
      return;
    }
    const confirmed = await this.ui.confirm({
      title: "撤销分享链接？",
      message: "这条链接会立即打不开，且撤销后无法恢复；需要时可以重新生成一条新链接。",
      confirmLabel: "撤销分享",
      danger: true,
    });
    if (!confirmed) return;
    try {
      const ok = await this.api.revokeShare(share.token);
      this.ui.renderSharePanel(null);
      this.ui.setShareStatus(ok ? "已撤销分享" : "链接已失效", "ok");
      this.ui.toast(ok ? "分享已撤销" : "该链接已经失效", { type: "success" });
    } catch (error) {
      this.ui.setShareStatus("撤销失败", "error");
      this.ui.toast(`撤销分享失败：${error.message}`, {
        type: "error",
        duration: 7000,
      });
    }
  }

  async copyShare() {
    const share = this.ui.currentShare();
    const url = this._shareUrlOf(share);
    if (!url) {
      this.ui.toast("没有可复制的分享链接", { type: "warn" });
      return;
    }
    await this._copyShare(url, "分享链接已复制");
  }

  async _copyShare(url, label) {
    try {
      await navigator.clipboard.writeText(url);
      this.ui.toast(`${label}：${url}`, { type: "success", duration: 5200 });
    } catch {
      // 剪贴板不可用（权限被拒/非安全上下文）时不能假装成功：把链接直接摆出来。
      this.ui.toast(`复制失败，请手动复制：${url}`, {
        type: "warn",
        duration: 12000,
      });
    }
  }

  async _refreshSpaces() {
    if (this.mode === "bridge") {
      const spaces = await this.api.listSpaces();
      this.store.setWorkspace({ spaces });
    } else {
      this.store.setWorkspace({ spaces: this.backend.lists() });
    }
  }

  /* ── 空间切换 / 新建 / 删除 / 重命名 ─────────────────────────────────── */

  async switchSpace(id) {
    if (!id || id === this.store.state.currentSpaceId) return;
    await this.saveNow({ silent: true });
    try {
      let record;
      let spaces;
      if (this.mode === "bridge") {
        const data = await this.api.getSpace(id);
        record = {
          id,
          name: data.space?.name || "未命名画布",
          doc: data.doc || createEmptyDoc(),
        };
        spaces = await this.api.listSpaces();
      } else {
        record = this.backend.get(id);
        if (!record) throw new Error("找不到该空间");
        this.backend.setCurrent(id);
        this.backend.persist();
        spaces = this.backend.lists();
      }
      this._applySpace(record, spaces);
      this.ui.toast(`已切换到「${record.name}」`, {
        type: "success",
        duration: 2200,
      });
    } catch (error) {
      this.ui.toast(`切换空间失败：${error.message}`, {
        type: "error",
        duration: 6000,
      });
    }
  }

  async _createBlankSpace() {
    if (this.mode === "bridge") {
      const created = await this.api.createSpace("未命名画布");
      const doc = createEmptyDoc();
      try {
        await this.api.saveSpace(created.id, {
          name: created.name || "未命名画布",
          doc,
        });
      } catch (error) {
        console.warn("[canvas] 空白空间初始化保存失败", error);
      }
      return {
        record: {
          id: created.id,
          name: created.name || "未命名画布",
          doc,
        },
        spaces: await this.api.listSpaces().catch(() => [created]),
      };
    }
    const record = this.backend.create("未命名画布", createEmptyDoc());
    this.backend.setCurrent(record.id);
    this.backend.persist();
    return { record, spaces: this.backend.lists() };
  }

  async newSpace() {
    await this.saveNow({ silent: true });
    try {
      const { record, spaces } = await this._createBlankSpace();
      this._applySpace(record, spaces);
      this.ui.setEmpty(true);
      this.ui.toast("已新建空白空间", { type: "success", duration: 2200 });
      setTimeout(() => this.ui.focusPrompt(), 120);
    } catch (error) {
      this.ui.toast(`新建空间失败：${error.message}`, {
        type: "error",
        duration: 6000,
      });
    }
  }

  async deleteSpace(id) {
    const meta = this.store.state.spaces.find((space) => space.id === id);
    const confirmed = await this.ui.confirm({
      title: "删除空间",
      message: `「${meta?.name || "当前空间"}」共 ${meta?.node_count ?? "?"} 个节点，删除后无法撤销。`,
      confirmLabel: "删除",
      danger: true,
    });
    if (!confirmed) return;

    try {
      this._cancelAllJobs();
      if (this.mode === "bridge") {
        await this.api.deleteSpace(id);
      } else {
        this.backend.remove(id);
        this.backend.persist();
      }

      let spaces =
        this.mode === "bridge"
          ? await this.api.listSpaces()
          : this.backend.lists();
      let targetId = spaces[0]?.id || "";

      if (!spaces.length) {
        const { record, spaces: createdSpaces } = await this._createBlankSpace();
        this._applySpace(record, createdSpaces);
      } else {
        if (this.mode === "bridge") {
          const data = await this.api.getSpace(targetId);
          this._applySpace(
            {
              id: targetId,
              name: data.space?.name || "未命名画布",
              doc: data.doc || createEmptyDoc(),
            },
            spaces,
          );
        } else {
          const record = this.backend.get(targetId);
          if (!record) throw new Error("删除后找不到可用空间");
          this.backend.setCurrent(targetId);
          this.backend.persist();
          this._applySpace(record, spaces);
        }
      }
      this.ui.toast("空间已删除", { type: "success", duration: 2200 });
    } catch (error) {
      this.ui.toast(`删除空间失败：${error.message}`, {
        type: "error",
        duration: 6000,
      });
    }
  }

  async renameSpace(name) {
    const id = this.store.state.currentSpaceId;
    if (!id) return;
    const clean = String(name || "").trim().slice(0, 48);
    if (!clean) return;
    const previous = this._currentSpaceName;
    this._currentSpaceName = clean;
    this.store.setWorkspace({
      spaces: this.store.state.spaces.map((space) =>
        space.id === id ? { ...space, name: clean } : space,
      ),
    });

    try {
      if (this.mode === "bridge") {
        await this.api.saveSpace(id, { name: clean, doc: this.store.doc });
      } else {
        this.backend.save(id, { name: clean, doc: this.store.doc });
      }
      this.store.setSaveState("saved");
      await this._refreshSpaces().catch(() => {});
      this.ui.toast("已重命名空间", { type: "success", duration: 2000 });
    } catch (error) {
      this._currentSpaceName = previous;
      this.ui.toast(`重命名失败：${error.message}`, {
        type: "error",
        duration: 6000,
      });
      this.store.setWorkspace({
        spaces: this.store.state.spaces.map((space) =>
          space.id === id ? { ...space, name: previous } : space,
        ),
      });
      this.ui.renderToolbar();
    }
  }

  async retryBridge() {
    this.ui.toast("正在重试连接 bridge…", { type: "info", duration: 1800 });
    const probe = await this.api.probe({ timeout: 1500 });
    if (!probe.ok) {
      this.ui.toast(`仍然无法连接 bridge：${probe.reason}`, {
        type: "error",
        duration: 6000,
      });
      return;
    }
    await this.saveNow({ silent: true });
    try {
      await this._initBridge(probe.spaces, probe.latency);
    } catch (error) {
      this.ui.toast(`已连接但加载空间失败：${error.message}`, {
        type: "error",
        duration: 6000,
      });
    }
  }

  /* ── 视图操作 ───────────────────────────────────────────────────────── */

  _commitCamera() {
    this.store.setCamera(this.view.getCamera());
    this.scheduleRender();
  }

  setZoom(zoom) {
    if (!this.view.setZoom(zoom)) return;
    this._commitCamera();
  }

  zoomBy(factor) {
    if (!this.view.zoomAt(factor, { x: this.view.width / 2, y: this.view.height / 2 })) {
      return;
    }
    this._commitCamera();
  }

  fit() {
    this.view.fitNodes(this.store.nodes, { padding: 120, maxZoom: 2 });
    this._commitCamera();
  }

  home() {
    this.view.reset();
    this._commitCamera();
  }

  centerOnWorld(point) {
    if (!point) return;
    this.view.centerOnWorld(point);
    this._commitCamera();
  }

  /* ── 节点操作 ───────────────────────────────────────────────────────── */

  _createTextNode({ x, y, w = 220, h = 132, text = "在这里写下你的想法…" }) {
    try {
      const node = this.store.addNode({
        id: newNodeId(),
        type: "text",
        x,
        y,
        w,
        h,
        z: this.store.maxZ() + 1,
        data: { text, prompt: "", createdAt: Date.now(), updatedAt: Date.now() },
      });
      this.ui.setEmpty(false);
      this.scheduleRender();
      return node;
    } catch (error) {
      this.ui.toast(`创建便签失败：${error.message}`, { type: "error" });
      return null;
    }
  }

  newNoteAtCenter() {
    const center = this.view.screenToWorld({
      x: this.view.width / 2,
      y: this.view.height / 2 - 20,
    });
    const node = this._createTextNode({
      x: center.x - 110,
      y: center.y - 66,
      w: 220,
      h: 132,
      text: "双击空白处新建便签，选中后在右侧编辑正文。",
    });
    if (node) {
      requestAnimationFrame(() => this.ui.focusInspectorInput());
    }
  }

  deleteNodes(ids) {
    const valid = [...(ids || [])];
    for (const id of valid) this._endJob(id);
    this.store.removeNodes(valid);
    this.scheduleRender();
  }

  /* ── muse 只读导入（Space 预览卡 / Explore 灵感卡）────────────────── */

  /**
   * 把后端 `/canvas/api/muse/import` 返回的节点放进本地画布。
   *
   * 为什么去重看 `data.muse.spaceSlug` 而不是 node.id：节点 id 是后端每次随机
   * 生成的（`n_`+10 hex），同一个 Space 反复点导入会得到多张同源卡片；用 slug
   * 判重才是"这个 Space 已经在画布上了"的稳定判据。命中去重时只居中现有卡片，
   * 不再新建——重复卡片既不美观也会让保存体积膨胀。
   */
  async importMuseSpace(slug, space = null) {
    this.ui.flushEdit();
    const key = String(slug || "").trim();
    if (!key) {
      this.ui.toast("这个 Space 没有 slug，无法导入", { type: "warn" });
      return null;
    }
    try {
      const result = await this.api.importMuseSpace(key);
      const node = result?.node;
      if (!node?.id || !node?.type) throw new Error("bridge 返回的节点形状不合法");
      const existing = this.store.nodes.find(
        (item) => item?.data?.muse?.spaceSlug === key,
      );
      if (existing) {
        this.centerOnWorld({
          x: existing.x + existing.w / 2,
          y: existing.y + existing.h / 2,
        });
        this.ui.toast(`「${space?.name || key}」已经在画布上了`, { type: "warn" });
        return result;
      }
      this.store.addNode(node, { label: "导入 muse Space" });
      this.scheduleRender();
      this.ui.toast(`已导入 muse Space「${space?.name || key}」`, {
        type: "success",
        duration: 2600,
      });
      return result;
    } catch (error) {
      this.ui.toast(`导入 muse Space 失败：${error?.message || "未知错误"}`, {
        type: "error",
        duration: 6000,
      });
      return null;
    }
  }

  /**
   * Explore 灵感卡 → 一张文本卡片（便签）。
   *
   * 为什么不做成图片卡：灵感卡自带的 `iconUrl` 是 1 KB 级的缩略图标（72px），
   * 放大成卡片只会糊；卡片的真正价值是标题 + 一句话摘要，作为便签落进画布后
   * 还能被提示词复用（写进生成 prompt）。所以文本卡是诚实形态，不伪造预览图。
   */
  addIdeaCard(card) {
    const title = String(card?.title || "muse 灵感卡").trim();
    const summary = String(card?.summary || "").trim();
    const center = this.view.screenToWorld({
      x: this.view.width / 2,
      y: this.view.height / 2 - 20,
    });
    const node = {
      id: newNodeId(),
      type: "text",
      x: Math.round(center.x - 190),
      y: Math.round(center.y - 90),
      w: 380,
      h: 180,
      z: this.store.maxZ() + 1,
      data: {
        text: summary ? `${title}\n\n${summary}` : title,
        status: "idle",
        muse: {
          source: "muse-idea-card",
          ideaCardId: card?.id || card?.ideaCardId || null,
          title,
          iconUrl: card?.iconUrl || null,
        },
      },
    };
    this.store.addNode(node, { label: "导入 muse 灵感卡" });
    this.scheduleRender();
    this.ui.toast(`已把「${title}」加成便签`, {
      type: "success",
      duration: 2400,
    });
    return node;
  }

  deleteSelection() {
    const ids = this.store.selectedIds;
    if (!ids.length) return;
    this.deleteNodes(ids);
  }

  duplicateNodes(ids) {
    const valid = [...(ids || [])];
    if (!valid.length) return;
    const copies = this.store.duplicateNodes(valid);
    for (const id of copies) {
      const copy = this.store.node(id);
      // 复制"生成中"的卡片不会复制后台任务；把副本落回 idle，
      // 否则它会永远停在 queued，看起来像卡死。
      if (
        copy &&
        !copy.data?.url &&
        (copy.data?.status === "queued" || copy.data?.status === "generating")
      ) {
        this.store.updateNodeData(
          id,
          { status: "idle", taskId: "", createdAt: 0, error: "", batch: false },
          { history: false },
        );
      }
    }
    if (copies.length) this.scheduleRender();
  }

  duplicateSelection() {
    this.duplicateNodes(this.store.selectedIds);
  }

  downloadNode(id) {
    const node = this.store.node(id);
    if (!node) return;
    const url = node.data?.url || node.data?.thumbUrl || "";
    if (!url) {
      this.ui.toast("这张卡片还没有可下载的媒体", { type: "warn" });
      return;
    }
    const isVideo = node.type === "video" && node.data?.url;
    const extension = isVideo
      ? "mp4"
      : url.includes("image/svg") || url.startsWith("data:image/svg")
        ? "svg"
        : "png";
    const name = `muse-${this._spaceName()}-${node.id}`.replace(
      /[\\/:*?"<>|\s]+/g,
      "-",
    );
    downloadUrl(url, `${name}.${extension}`);
    this.ui.toast("已开始下载", { type: "success", duration: 2000 });
  }

  async exportPNG() {
    this.ui.flushEdit();
    if (this._exporting) {
      this.ui.toast("正在导出，请稍候…", { type: "warn" });
      return;
    }
    this._exporting = true;
    this.ui.toast("正在导出当前视口…", { type: "info", duration: 1600 });
    try {
      const result = await exportViewportPNG({
        view: this.view,
        doc: this.store.doc,
        pixelRatio: 2,
      });
      downloadBlob(result.blob, exportFileName(this._spaceName()));
      this.ui.toast(
        `已导出 ${result.exported} 张卡片（${result.width}×${result.height}）${
          result.skipped ? `，${result.skipped} 张图片跳过` : ""
        }`,
        {
          type: result.skipped ? "warn" : "success",
          duration: 4200,
        },
      );
    } catch (error) {
      console.error("[canvas] 导出失败", error);
      this.ui.toast(`导出失败：${error.message}`, {
        type: "error",
        duration: 6000,
      });
    } finally {
      this._exporting = false;
    }
  }

  async loadDemo() {
    if (this.store.nodes.length) {
      await this.saveNow({ silent: true });
      const confirmed = await this.ui.confirm({
        title: "加载演示空间",
        message:
          "会用 300+ 张卡片的演示画面替换当前画布。当前画布已先保存，但仍建议先复制一份。",
        confirmLabel: "替换画布",
        danger: true,
      });
      if (!confirmed) return;
    }
    const doc = buildDemoDoc();
    this.store.setDoc(doc, { resetHistory: true });
    this.view.setCamera(doc.camera || DEFAULT_CAMERA);
    this.view.setGridSize(doc.meta?.gridSize || 28);
    this.ui.setEmpty(false);
    this.markDirty();
    this.scheduleRender();
    this.ui.toast(`已加载演示空间「${DEMO_NAME}」· ${doc.nodes.length} 张卡片`, {
      type: "success",
      duration: 3200,
    });
  }

  async clearCanvas() {
    if (!this.store.nodes.length) {
      this.ui.toast("当前画布已经是空的", { type: "info" });
      return;
    }
    const confirmed = await this.ui.confirm({
      title: "清空画布",
      message: `会删除当前空间里的 ${this.store.nodes.length} 张卡片。此操作会留在撤销栈外，请确认已保存。`,
      confirmLabel: "清空",
      danger: true,
    });
    if (!confirmed) return;
    this._cancelAllJobs();
    const doc = createEmptyDoc();
    doc.camera = { ...this.store.doc.camera };
    this.store.setDoc(doc, { resetHistory: true });
    this.view.setCamera(doc.camera);
    this.ui.setEmpty(true);
    this.markDirty();
    this.scheduleRender();
    this.ui.toast("画布已清空", { type: "success", duration: 2200 });
  }

  /* ── 生成 ───────────────────────────────────────────────────────────── */

  _promptNode({ prompt, model, size, x, y, w, h, batch }) {
    const node = {
      id: newNodeId(),
      type: "prompt",
      x,
      y,
      w,
      h,
      z: this.store.maxZ() + 1,
      data: {
        status: "queued",
        prompt,
        model,
        params: { size },
        width: 0,
        height: 0,
        error: "",
        batch: Boolean(batch),
        createdAt: Date.now(),
      },
    };
    return this.store.addNode(node, { select: false, label: "新建生成卡片" });
  }

  generate({ prompt, model, size } = {}) {
    this.ui.flushEdit();
    const text = String(prompt || "").trim();
    if (!text) {
      this.ui.toast("先写一句提示词，再点生成", {
        type: "warn",
        duration: 3000,
      });
      this.ui.focusPrompt();
      return;
    }
    if (text.length > 2000) {
      this.ui.toast("提示词太长了，请控制在 2000 字以内", { type: "error" });
      return;
    }

    const selected = this.store.selectedNodes();
    const specs = [];
    const batch = selected.length > 1;

    if (batch) {
      if (selected.length > BATCH_LIMIT) {
        this.ui.toast(`批量生成最多 ${BATCH_LIMIT} 张，已取前 ${BATCH_LIMIT} 张`, {
          type: "warn",
          duration: 3200,
        });
      }
      for (const node of selected.slice(0, BATCH_LIMIT)) {
        specs.push({
          x: node.x,
          y: node.y + node.h + 16,
          w: Math.round(clamp(Math.max(node.w, 190), 190, 260)),
          h: 150,
        });
      }
    } else if (selected.length === 1) {
      const node = selected[0];
      const x = node.x + node.w + 20;
      const y = node.y;
      specs.push({ x, y, w: 240, h: 154 });
      this.view.ensureVisible({ x, y, w: 240, h: 154 }, 90);
      this._commitCamera();
    } else {
      const center = this.view.screenToWorld({
        x: this.view.width / 2,
        y: this.view.height / 2 - 30,
      });
      specs.push({ x: center.x - 120, y: center.y - 77, w: 240, h: 154 });
    }

    const created = [];
    this.store.beginTransaction("生成提示词卡片");
    try {
      for (const spec of specs) {
        created.push(
          this._promptNode({
            prompt: text,
            model,
            size,
            x: spec.x,
            y: spec.y,
            w: spec.w,
            h: spec.h,
            batch,
          }),
        );
      }
    } catch (error) {
      this.store.commitTransaction();
      this.ui.toast(`创建生成卡片失败：${error.message}`, { type: "error" });
      return;
    }
    this.store.commitTransaction();

    const ids = created.map((node) => node.id);
    this.store.setSelection(ids);
    this.ui.setEmpty(false);
    this.scheduleRender();

    for (const [index, node] of created.entries()) {
      this._startGeneration(node.id, {
        prompt: text,
        model,
        size,
        batch,
        seed: (hashString(text) + index * 977 + 31) >>> 0,
      });
    }

    this.ui.toast(
      this.mode === "offline"
        ? `已提交 ${ids.length} 张生成 · 离线演示约 2.5 秒`
        : `已提交 ${ids.length} 张生成任务`,
      { type: "info", duration: 2600 },
    );
  }

  regenerate(id) {
    const node = this.store.node(id);
    if (!node) return;
    if (this._jobs.has(id)) {
      this.ui.toast("这张卡片正在生成中", { type: "info" });
      return;
    }
    if (node.type === "text") {
      this.ui.toast("文本便签不需要重新生成", { type: "warn" });
      return;
    }
    const prompt = String(node.data?.prompt || "").trim();
    if (!prompt) {
      this.ui.toast("这张卡片没有提示词，无法重新生成", { type: "warn" });
      return;
    }
    this.store.updateNodeData(
      id,
      { status: "queued", error: "", createdAt: Date.now() },
      { history: false },
    );
    this._startGeneration(id, {
      prompt,
      model: node.data?.model || "muse-image",
      size: node.data?.params?.size || "1:1",
      batch: Boolean(node.data?.batch),
      seed: (hashString(prompt) + (this._genSeq += 1)) >>> 0,
    });
    this.ui.toast("重新生成已提交", { type: "info", duration: 2200 });
  }

  _startGeneration(id, options) {
    const node = this.store.node(id);
    if (!node) return;

    const job = { cancelled: false, timers: [], errors: 0, taskId: "" };
    this._jobs.set(id, job);
    this.ui.setGenerating(this._jobs.size);

    this.store.updateNodeData(
      id,
      { status: "queued", error: "", createdAt: Date.now() },
      { history: false },
    );

    if (this.mode === "offline") {
      this._startMock(id, options, job);
    } else {
      this._startBridge(id, options, job);
    }
  }

  _startMock(id, options, job) {
    const step1 = setTimeout(() => {
      if (job.cancelled || !this.store.node(id)) return;
      this.store.updateNodeData(
        id,
        { status: "generating", createdAt: Date.now() },
        { history: false },
      );
      this.scheduleRender();

      const step2 = setTimeout(() => {
        if (job.cancelled || !this.store.node(id)) return;
        if (/fail|失败|报错/i.test(options.prompt)) {
          this._failGeneration(
            id,
            "演示失败：提示词包含 fail / 失败，用于展示失败与重试状态",
          );
          return;
        }
        const ratio = ratioNumber(options.size);
        let width = 720;
        let height = 720;
        if (ratio >= 1) height = Math.max(240, Math.round(720 / ratio));
        else width = Math.max(240, Math.round(720 * ratio));
        const artwork = makeArtwork({
          seed: options.seed,
          width,
          height,
          hue: options.seed % 360,
          detailed: true,
        });
        this._finishGeneration(id, {
          url: artwork,
          width,
          height,
        });
      }, MOCK_DELAY);
      job.timers = [step2];
    }, 280);
    job.timers = [step1];
  }

  _startBridge(id, options, job) {
    this.api
      .createImageTask({
        prompt: options.prompt,
        model: options.model,
        size: options.size,
        n: 1,
      })
      .then((task) => {
        if (job.cancelled || !this.store.node(id)) return;
        job.taskId = task.id;
        this.store.updateNodeData(
          id,
          {
            status: task.status === "queued" ? "queued" : "generating",
            taskId: task.id,
          },
          { history: false },
        );
        if (task.status === "succeeded") {
          if (task.url) {
            this._finishGeneration(id, {
              url: resolveMediaUrl(task.url),
              width: task.width,
              height: task.height,
            });
          } else {
            this._failGeneration(id, "任务成功但没有返回图片 URL");
          }
          return;
        }
        if (task.status === "failed") {
          this._failGeneration(id, task.error || "生成任务失败");
          return;
        }
        if (task.status === "cancelled") {
          this._cancelGeneration(id, task.error || "任务已取消");
          return;
        }
        this._pollBridge(id, task.id, job);
      })
      .catch((error) => {
        if (job.cancelled) return;
        this._failGeneration(id, error.message || "生成请求失败");
      });
  }

  _pollBridge(id, taskId, job) {
    if (job.cancelled) return;
    const timer = setTimeout(async () => {
      if (job.cancelled || !this.store.node(id)) return;
      try {
        const task = await this.api.getTask(taskId);
        if (job.cancelled) return;
        job.errors = 0;
        if (task.status === "succeeded") {
          if (task.url) {
            this._finishGeneration(id, {
              url: resolveMediaUrl(task.url),
              width: task.width,
              height: task.height,
            });
          } else {
            this._failGeneration(id, "任务成功但没有返回图片 URL");
          }
          return;
        }
        if (task.status === "failed") {
          this._failGeneration(id, task.error || "生成任务失败");
          return;
        }
        if (task.status === "cancelled") {
          this._cancelGeneration(id, task.error || "任务已取消");
          return;
        }
        this.store.updateNodeData(
          id,
          { status: task.status === "queued" ? "queued" : "generating" },
          { history: false },
        );
        this._pollBridge(id, taskId, job);
      } catch (error) {
        if (job.cancelled) return;
        job.errors += 1;
        if (job.errors === 1) {
          this.ui.toast(`轮询生成任务失败：${error.message}`, {
            type: "warn",
            duration: 4200,
          });
        }
        if (job.errors >= 3) {
          this._failGeneration(id, `轮询任务连续失败：${error.message}`);
          return;
        }
        this._pollBridge(id, taskId, job);
      }
    }, POLL_MS);
    job.timers = [timer];
  }

  _finishGeneration(id, { url, width, height }) {
    const node = this.store.node(id);
    if (!node) {
      this._endJob(id);
      return;
    }
    const current = node.data || {};
    const configured = ratioNumber(current.params?.size || "1:1");
    const ratio =
      width > 0 && height > 0 ? width / height : configured || 1;
    const targetW = Math.max(180, Math.round(node.w));
    const targetH = Math.round(clamp(targetW / (ratio || 1), 120, 620));

    this.store.updateNode(
      id,
      {
        type: "image",
        w: targetW,
        h: targetH,
        data: {
          ...current,
          status: "done",
          url,
          thumbUrl: url,
          width: width || targetW,
          height: height || targetH,
          error: "",
          completedAt: Date.now(),
        },
      },
      { history: false },
    );
    this._endJob(id);
    this.scheduleRender();

    if (!current.batch) {
      this.ui.toast("图片已生成", { type: "success", duration: 2400 });
    }
  }

  _failGeneration(id, message) {
    const node = this.store.node(id);
    if (!node) {
      this._endJob(id);
      return;
    }
    const current = node.data || {};
    this.store.updateNode(
      id,
      {
        type: "image",
        data: {
          ...current,
          status: "failed",
          error: String(message || "生成失败，没有返回原因"),
          url: "",
          thumbUrl: "",
        },
      },
      { history: false },
    );
    this._endJob(id);
    this.scheduleRender();
    this.ui.toast(`生成失败：${message}`, {
      type: "error",
      duration: 7000,
      actionLabel: "重试",
      onAction: () => this.regenerate(id),
    });
  }

  /**
   * 任务被取消（bridge 侧客户端断开 / 主动 cancel）。与 `_failGeneration` 分开：
   * cancelled 是终态但不是错误，节点保留提示词与重试入口，不显示"生成失败"。
   */
  _cancelGeneration(id, message) {
    const node = this.store.node(id);
    if (!node) {
      this._endJob(id);
      return;
    }
    const current = node.data || {};
    this.store.updateNode(
      id,
      {
        type: "image",
        data: {
          ...current,
          status: "cancelled",
          error: String(message || "任务已取消"),
          url: "",
          thumbUrl: "",
        },
      },
      { history: false },
    );
    this._endJob(id);
    this.scheduleRender();
    this.ui.toast(`生成已取消：${message}`, {
      type: "warn",
      duration: 5200,
      actionLabel: "重试",
      onAction: () => this.regenerate(id),
    });
  }

  _endJob(id) {
    const job = this._jobs.get(id);
    if (job) {
      job.cancelled = true;
      for (const timer of job.timers) clearTimeout(timer);
      this._jobs.delete(id);
    }
    this.ui.setGenerating(this._jobs.size);
  }

  _cancelAllJobs() {
    for (const id of [...this._jobs.keys()]) this._endJob(id);
  }

  /* ── 渲染 ───────────────────────────────────────────────────────────── */

  scheduleRender() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this.nodes.render();
      this.ui.updateMinimap();
    });
  }

  /* ── 指针手势 ───────────────────────────────────────────────────────── */

  _bind() {
    const stage = this.dom.stage;

    stage.addEventListener("pointerdown", (event) => this._onPointerDown(event));
    stage.addEventListener("wheel", (event) => this._onWheel(event), {
      passive: false,
    });
    stage.addEventListener("dblclick", (event) => this._onDoubleClick(event));
    stage.addEventListener("click", (event) => {
      // 键盘触发（Enter/Space）的按钮点击没有 pointerdown 细节
      if (event.detail !== 0) return;
      const actionEl = event.target.closest("[data-node-action]");
      if (!actionEl) return;
      const id = actionEl.closest("[data-node-id]")?.dataset.nodeId;
      if (id) this._runNodeAction(id, actionEl.dataset.nodeAction);
    });

    window.addEventListener("pointermove", (event) => this._onPointerMove(event));
    window.addEventListener("pointerup", (event) => this._onPointerUp(event));
    window.addEventListener("pointercancel", (event) => this._onPointerUp(event));
    window.addEventListener("keydown", (event) => this._onKeyDown(event));
    window.addEventListener("keyup", (event) => {
      if (event.key === " ") {
        this._spaceDown = false;
        stage.classList.remove("is-space-pan");
      }
    });
    window.addEventListener("blur", () => {
      this._spaceDown = false;
      stage.classList.remove("is-space-pan");
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.saveNow({ silent: true });
    });
    window.addEventListener("pagehide", () => {
      if (this.mode === "offline" && this._dirty) {
        const id = this.store.state.currentSpaceId;
        if (id && this.backend) {
          try {
            this.backend.save(id, {
              name: this._spaceName(),
              doc: this.store.doc,
            });
          } catch (error) {
            console.warn("[canvas] pagehide 保存失败", error);
          }
        }
      }
    });
  }

  _runNodeAction(id, action) {
    if (action === "delete") this.deleteNodes([id]);
    else if (action === "retry") this.regenerate(id);
  }

  _onPointerDown(event) {
    if (this.ui.hasOverlay()) return;
    if (event.button !== 0 && event.button !== 1) return;
    // 画布上的任何新手势都要先结束输入事务，否则拖拽会和文本编辑共用
    // 同一个 store 事务，撤销栈会出现"半条编辑 + 半条移动"。
    this.ui.flushEdit();

    if (event.button === 1 || this._spaceDown) {
      event.preventDefault();
      this._beginPan(event);
      return;
    }
    if (event.button !== 0) return;

    const actionEl = event.target.closest("[data-node-action]");
    if (actionEl) {
      event.preventDefault();
      event.stopPropagation();
      if (this.readOnly) {
        // 只读分享里节点操作（删除/复制/下载等）不是全部安全，统一拒绝更省心；
        // 大图查看走双击路径，仍可用。
        this._denyReadOnly();
        return;
      }
      const id = actionEl.closest("[data-node-id]")?.dataset.nodeId;
      if (id) this._runNodeAction(id, actionEl.dataset.nodeAction);
      return;
    }

    const handleEl = event.target.closest("[data-handle]");
    if (handleEl) {
      if (this.readOnly) {
        this._denyReadOnly();
        return;
      }
      const nodeEl = handleEl.closest("[data-node-id]");
      const node = nodeEl ? this.store.node(nodeEl.dataset.nodeId) : null;
      if (node) {
        event.preventDefault();
        event.stopPropagation();
        this._beginResize(event, node, handleEl.dataset.handle);
        return;
      }
    }

    const nodeEl = event.target.closest("[data-node-id]");
    if (nodeEl) {
      const node = this.store.node(nodeEl.dataset.nodeId);
      if (!node) return;
      event.preventDefault();

      if (event.shiftKey) {
        this.store.toggleSelection(node.id);
        if (!this.store.isSelected(node.id)) return;
      } else if (!this.store.isSelected(node.id)) {
        this.store.setSelection([node.id]);
      }

      // 只读分享允许选中/查看，但不允许拖动或改 z-order（都会改本地 doc）。
      if (this.readOnly) return;
      this.store.bringToFront(this.store.selectedIds);
      this._beginDrag(event, node);
      return;
    }

    // 空白处：框选；Shift 保留已有选择
    this._beginMarquee(event);
  }

  _beginPan(event) {
    this._active = {
      type: "pan",
      pointerId: event.pointerId,
      sx: event.clientX,
      sy: event.clientY,
      camera: this.view.getCamera(),
    };
    this.dom.stage.classList.add("is-panning");
    event.preventDefault();
  }

  _beginMarquee(event) {
    const local = this.view.localPoint(event.clientX, event.clientY);
    const append = Boolean(event.shiftKey);
    this._active = {
      type: "marquee",
      pointerId: event.pointerId,
      sx: local.x,
      sy: local.y,
      append,
      base: append ? this.store.selectedIds : [],
    };
    if (!append) this.store.clearSelection();
    this.dom.marquee.hidden = false;
    this.dom.marquee.style.left = `${local.x}px`;
    this.dom.marquee.style.top = `${local.y}px`;
    this.dom.marquee.style.width = "0px";
    this.dom.marquee.style.height = "0px";
    this.dom.stage.classList.add("is-marquee");
    this._marqueeStamp = 0;
  }

  _beginDrag(event, node) {
    const ids = this.store.selectedIds;
    const origins = new Map();
    for (const id of ids) {
      const item = this.store.node(id);
      if (item) origins.set(id, { x: item.x, y: item.y });
    }
    if (!origins.size) return;

    this._active = {
      type: "drag",
      pointerId: event.pointerId,
      sx: event.clientX,
      sy: event.clientY,
      primary: node.id,
      ids,
      origins,
      moved: false,
    };
    this.store.beginTransaction(ids.length > 1 ? "移动多张卡片" : "移动卡片");
    this.nodes.setDragging(ids, true);
    this.dom.stage.classList.add("is-panning");
  }

  _beginResize(event, node, handle) {
    this.store.setSelection([node.id]);
    this.store.bringToFront([node.id]);
    this.store.beginTransaction("调整卡片尺寸");
    this._active = {
      type: "resize",
      pointerId: event.pointerId,
      sx: event.clientX,
      sy: event.clientY,
      nodeId: node.id,
      handle,
      original: { x: node.x, y: node.y, w: node.w, h: node.h },
      last: { x: node.x, y: node.y, w: node.w, h: node.h },
    };
    this.nodes.element(node.id)?.classList.add("is-resizing");
    this.dom.stage.classList.add("is-panning");
    this.dom.sizeHint.hidden = false;
    const screen = this.view.worldToScreen({
      x: node.x + node.w,
      y: node.y + node.h,
    });
    this.dom.sizeHint.style.left = `${screen.x}px`;
    this.dom.sizeHint.style.top = `${screen.y}px`;
    this.dom.sizeHint.textContent = `${Math.round(node.w)} × ${Math.round(node.h)}`;
  }

  _onPointerMove(event) {
    const active = this._active;
    if (!active || event.pointerId !== active.pointerId) return;

    if (active.type === "pan") {
      const dx = event.clientX - active.sx;
      const dy = event.clientY - active.sy;
      this.view.setCamera({
        x: active.camera.x - dx / active.camera.zoom,
        y: active.camera.y - dy / active.camera.zoom,
        zoom: active.camera.zoom,
      });
      this.scheduleRender();
      return;
    }

    if (active.type === "drag") {
      const dx = event.clientX - active.sx;
      const dy = event.clientY - active.sy;
      if (!active.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
      active.moved = true;

      const zoom = this.view.zoom;
      const primaryOrigin = active.origins.get(active.primary);
      const primary = this.store.node(active.primary);
      if (!primaryOrigin || !primary) return;
      let nx = primaryOrigin.x + dx / zoom;
      let ny = primaryOrigin.y + dy / zoom;
      let guides = [];

      if (this.store.state.ui.snap && !event.altKey) {
        const snapped = this._snapPosition(
          primary,
          nx,
          ny,
          new Set(active.ids),
        );
        nx = snapped.x;
        ny = snapped.y;
        guides = snapped.guides;
      }

      const offsetX = nx - primaryOrigin.x;
      const offsetY = ny - primaryOrigin.y;
      for (const id of active.ids) {
        const origin = active.origins.get(id);
        if (!origin) continue;
        this.store.setNodePosition(id, origin.x + offsetX, origin.y + offsetY);
      }
      this.nodes.updateGeometry(active.ids);
      this._renderGuides(guides);
      this._scheduleMinimap();
      return;
    }

    if (active.type === "resize") {
      const dx = (event.clientX - active.sx) / this.view.zoom;
      const dy = (event.clientY - active.sy) / this.view.zoom;
      const original = active.original;
      let x = original.x;
      let y = original.y;
      let w = original.w;
      let h = original.h;
      const handle = active.handle;

      if (handle.includes("e")) w = original.w + dx;
      if (handle.includes("s")) h = original.h + dy;
      if (handle.includes("w")) {
        w = original.w - dx;
        x = original.x + dx;
      }
      if (handle.includes("n")) {
        h = original.h - dy;
        y = original.y + dy;
      }

      if (w < LIMITS.minSize) {
        if (handle.includes("w")) x = original.x + original.w - LIMITS.minSize;
        w = LIMITS.minSize;
      }
      if (h < LIMITS.minSize) {
        if (handle.includes("n")) y = original.y + original.h - LIMITS.minSize;
        h = LIMITS.minSize;
      }

      if (event.shiftKey) {
        const aspect = original.w / original.h;
        if (handle.includes("e") || handle.includes("w")) {
          h = w / aspect;
          if (handle.includes("n")) y = original.y + original.h - h;
        } else {
          w = h * aspect;
          if (handle.includes("w")) x = original.x + original.w - w;
        }
        if (w < LIMITS.minSize) {
          w = LIMITS.minSize;
          h = w / aspect;
        }
        if (h < LIMITS.minSize) {
          h = LIMITS.minSize;
          w = h * aspect;
        }
      }

      const node = this.store.node(active.nodeId);
      if (!node) return;
      this.store.updateNodeGeometry(
        active.nodeId,
        { x, y, w, h },
        { history: false },
      );
      this.nodes.updateGeometry([active.nodeId]);
      active.last = { x, y, w, h };

      const screen = this.view.worldToScreen({ x: x + w, y: y + h });
      this.dom.sizeHint.style.left = `${screen.x}px`;
      this.dom.sizeHint.style.top = `${screen.y}px`;
      this.dom.sizeHint.textContent = `${Math.round(w)} × ${Math.round(h)}`;
      this._scheduleMinimap();
      return;
    }

    if (active.type === "marquee") {
      const local = this.view.localPoint(event.clientX, event.clientY);
      const rect = {
        x: Math.min(active.sx, local.x),
        y: Math.min(active.sy, local.y),
        w: Math.abs(local.x - active.sx),
        h: Math.abs(local.y - active.sy),
      };
      this.dom.marquee.style.left = `${rect.x}px`;
      this.dom.marquee.style.top = `${rect.y}px`;
      this.dom.marquee.style.width = `${rect.w}px`;
      this.dom.marquee.style.height = `${rect.h}px`;

      const now = performance.now();
      if (now - this._marqueeStamp > 70) {
        this._marqueeStamp = now;
        this._applyMarquee(active, rect);
      }
    }
  }

  _applyMarquee(active, rect) {
    const world = this.view.screenToWorld({ x: rect.x, y: rect.y });
    const worldRect = {
      x: world.x,
      y: world.y,
      w: rect.w / this.view.zoom,
      h: rect.h / this.view.zoom,
    };
    const hit = this.view.nodesIntersecting(worldRect, this.store.nodes);
    const ids = new Set(active.base);
    for (const node of hit) ids.add(node.id);
    this.store.setSelection([...ids]);
  }

  _onPointerUp(event) {
    const active = this._active;
    if (!active || event.pointerId !== active.pointerId) return;

    if (active.type === "drag") {
      this.store.commitTransaction();
      this.nodes.setDragging(active.ids, false);
      this._renderGuides([]);
      this.dom.stage.classList.remove("is-panning");
      this.scheduleRender();
    } else if (active.type === "resize") {
      this.store.commitTransaction();
      this.nodes.element(active.nodeId)?.classList.remove("is-resizing");
      this.dom.sizeHint.hidden = true;
      this.dom.stage.classList.remove("is-panning");
      this.scheduleRender();
    } else if (active.type === "pan") {
      this._commitCamera();
      this.dom.stage.classList.remove("is-panning");
    } else if (active.type === "marquee") {
      if (active.sx !== undefined) {
        const local = this.view.localPoint(event.clientX, event.clientY);
        const rect = {
          x: Math.min(active.sx, local.x),
          y: Math.min(active.sy, local.y),
          w: Math.abs(local.x - active.sx),
          h: Math.abs(local.y - active.sy),
        };
        this._applyMarquee(active, rect);
      }
      this.dom.marquee.hidden = true;
      this.dom.stage.classList.remove("is-marquee");
      this.scheduleRender();
    }

    this._active = null;
  }

  _snapPosition(node, nx, ny, exclude) {
    const threshold = 6 / this.view.zoom;
    const others = this.store.nodes.filter((item) => !exclude.has(item.id));
    if (!others.length) return { x: nx, y: ny, guides: [] };

    const xAnchors = [];
    const yAnchors = [];
    for (const other of others) {
      xAnchors.push(other.x, other.x + other.w / 2, other.x + other.w);
      yAnchors.push(other.y, other.y + other.h / 2, other.y + other.h);
    }

    const movingX = [nx, nx + node.w / 2, nx + node.w];
    const movingY = [ny, ny + node.h / 2, ny + node.h];
    let bestX = null;
    let bestY = null;
    for (const moving of movingX) {
      for (const anchor of xAnchors) {
        const diff = anchor - moving;
        if (Math.abs(diff) <= threshold && (!bestX || Math.abs(diff) < Math.abs(bestX.diff))) {
          bestX = { diff, anchor };
        }
      }
    }
    for (const moving of movingY) {
      for (const anchor of yAnchors) {
        const diff = anchor - moving;
        if (Math.abs(diff) <= threshold && (!bestY || Math.abs(diff) < Math.abs(bestY.diff))) {
          bestY = { diff, anchor };
        }
      }
    }

    const guides = [];
    if (bestX) {
      nx += bestX.diff;
      guides.push({ axis: "x", at: bestX.anchor });
    }
    if (bestY) {
      ny += bestY.diff;
      guides.push({ axis: "y", at: bestY.anchor });
    }
    return { x: nx, y: ny, guides };
  }

  _renderGuides(guides) {
    const root = this.dom.guides;
    root.innerHTML = "";
    for (const guide of guides) {
      const el = document.createElement("div");
      el.className = `guide ${guide.axis === "x" ? "guide-x" : "guide-y"}`;
      if (guide.axis === "x") {
        el.style.left = `${(guide.at - this.view.camera.x) * this.view.zoom}px`;
      } else {
        el.style.top = `${(guide.at - this.view.camera.y) * this.view.zoom}px`;
      }
      root.appendChild(el);
    }
  }

  _scheduleMinimap() {
    if (!this.store.state.ui.minimap) return;
    const now = performance.now();
    if (now - (this._miniStamp || 0) < 120) return;
    this._miniStamp = now;
    this.ui.updateMinimap(true);
  }

  _cancelInteraction() {
    const active = this._active;
    if (!active) return;
    if (active.type === "drag") {
      for (const [id, origin] of active.origins) {
        this.store.updateNodeGeometry(id, origin, { history: false });
      }
      this.store.cancelTransaction();
      this.nodes.setDragging(active.ids, false);
      this.nodes.updateGeometry(active.ids);
      this._renderGuides([]);
    } else if (active.type === "resize") {
      this.store.updateNodeGeometry(active.nodeId, active.original, {
        history: false,
      });
      this.store.cancelTransaction();
      this.nodes.updateGeometry([active.nodeId]);
      this.nodes.element(active.nodeId)?.classList.remove("is-resizing");
      this.dom.sizeHint.hidden = true;
    } else if (active.type === "marquee") {
      this.dom.marquee.hidden = true;
      this.store.setSelection(active.base);
    } else if (active.type === "pan") {
      this.view.setCamera(active.camera);
    }
    this.dom.stage.classList.remove("is-panning", "is-marquee");
    this._active = null;
    this.scheduleRender();
  }

  /* ── 滚轮 / 双击 / 键盘 ─────────────────────────────────────────────── */

  _onWheel(event) {
    if (this.ui.hasOverlay()) return;
    event.preventDefault();
    const local = this.view.localPoint(event.clientX, event.clientY);

    if (event.ctrlKey || event.metaKey) {
      const factor = Math.exp(-event.deltaY * 0.0015);
      if (this.view.zoomAt(factor, local)) {
        this._commitCamera();
      }
      return;
    }

    this.view.panBy(-event.deltaX, -event.deltaY);
    this._commitCamera();
  }

  _onDoubleClick(event) {
    if (this.ui.hasOverlay()) return;
    const nodeEl = event.target.closest("[data-node-id]");
    if (nodeEl) {
      const node = this.store.node(nodeEl.dataset.nodeId);
      if (!node) return;
      if (node.type === "image" || node.type === "video") {
        this.ui.openViewer(node.id);
      } else {
        this.store.setSelection([node.id]);
        requestAnimationFrame(() => this.ui.focusInspectorInput());
      }
      return;
    }
    if (event.target.closest("[data-handle]")) return;
    // 只读分享：空白处双击"新建便签"是写操作，必须与按钮/快捷键同口径拒绝。
    // 之前这里直接调 _createTextNode 绕过了 action guard（探针实测节点 323→324），
    // 画面上多出一张未保存的卡片，用户会以为"只读"没生效。
    if (this.readOnly) {
      this._denyReadOnly();
      return;
    }
    const local = this.view.localPoint(event.clientX, event.clientY);
    const world = this.view.screenToWorld(local);
    const node = this._createTextNode({
      x: world.x - 110,
      y: world.y - 66,
      w: 220,
      h: 132,
      text: "双击空白处新建便签；选中后右侧可编辑正文。",
    });
    if (node) requestAnimationFrame(() => this.ui.focusInspectorInput());
  }

  _onKeyDown(event) {
    if (event.defaultPrevented) return;
    const target = event.target;
    const typing =
      target instanceof HTMLElement &&
      (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) ||
        target.isContentEditable);
    const meta = event.ctrlKey || event.metaKey;

    // 只读分享：写类快捷键在入口拦掉（按钮已隐藏，但快捷键不经过按钮）。
    if (this.readOnly && !typing) {
      const blocked =
        (meta && ["s", "z", "d"].includes(event.key.toLowerCase())) ||
        event.key === "Delete" ||
        event.key === "Backspace" ||
        event.key.startsWith("Arrow");
      if (blocked) {
        event.preventDefault();
        this._denyReadOnly();
        return;
      }
    }

    if (meta && event.key.toLowerCase() === "k") {
      event.preventDefault();
      this.ui.flushEdit();
      this.ui.openPalette();
      return;
    }
    if (this.ui.hasOverlay()) {
      if (event.key === "Escape") {
        event.preventDefault();
        this.ui.closeTop();
      }
      return;
    }
    // 保存 / 适配属于"全局动作"，即使光标在输入框里也要生效，
    // 否则 Ctrl+S 会被浏览器当成"保存网页"。
    if (meta && event.key.toLowerCase() === "s") {
      event.preventDefault();
      this.ui.flushEdit();
      this.saveNow({ manual: true });
      return;
    }
    if (meta && event.key === "0") {
      event.preventDefault();
      this.ui.flushEdit();
      this.fit();
      return;
    }
    if (meta && event.key === "/") {
      event.preventDefault();
      this.ui.openShortcuts();
      return;
    }

    if (event.key === "Escape") {
      if (this._active) {
        event.preventDefault();
        this._cancelInteraction();
        return;
      }
      if (typing) {
        target.blur();
        return;
      }
      this.store.clearSelection();
      return;
    }

    if (typing) return;
    this.ui.flushEdit();

    if (event.key === "?") {
      event.preventDefault();
      this.ui.openShortcuts();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      this.home();
      return;
    }
    if (meta && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) this.store.redo();
      else this.store.undo();
      return;
    }
    if (meta && event.key.toLowerCase() === "d") {
      event.preventDefault();
      this.duplicateSelection();
      return;
    }
    if (meta && event.key.toLowerCase() === "a") {
      event.preventDefault();
      this.store.setSelection(this.store.nodes.map((node) => node.id));
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      this.deleteSelection();
      return;
    }
    if (event.key === " ") {
      if (target === document.body || target === this.dom.stage) {
        event.preventDefault();
        this._spaceDown = true;
        this.dom.stage.classList.add("is-space-pan");
      }
      return;
    }
    if (event.key.startsWith("Arrow")) {
      const ids = this.store.selectedIds;
      if (!ids.length) return;
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      const dx = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
      const dy = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
      this.store.beginTransaction("微移卡片");
      for (const id of ids) {
        const node = this.store.node(id);
        if (!node) continue;
        this.store.updateNodeGeometry(
          id,
          { x: node.x + dx, y: node.y + dy },
          { history: false },
        );
      }
      this.store.commitTransaction();
      this.nodes.updateGeometry(ids);
      this.scheduleRender();
    }
  }
}

const app = new CanvasApp();

// 兜底可见性：任何未捕获异常 / Promise 拒绝至少会变成 toast，而不是只在
// DevTools 里留一行。ResizeObserver 的良性循环告警不算错误。
window.addEventListener("error", (event) => {
  const message = String(event.message || "");
  if (/ResizeObserver loop/i.test(message)) return;
  app.ui?.toast(`运行错误：${message || "未知错误"}`, {
    type: "error",
    duration: 7000,
  });
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason?.message || String(event.reason || "");
  if (/ResizeObserver loop/i.test(reason)) return;
  app.ui?.toast(`未处理的异步错误：${reason || "未知错误"}`, {
    type: "error",
    duration: 7000,
  });
});

app.start().catch((error) => {
  console.error("[canvas] 启动失败", error);
  const boot = document.getElementById("boot");
  if (boot) boot.remove();
  const ui = app?.ui;
  if (ui) {
    ui.toast(`启动失败：${error.message}`, { type: "error", duration: 8000 });
  }
});

// 调试入口：控制台可以直接看当前 store / view（不参与业务逻辑）
globalThis.__museCanvas = app;
