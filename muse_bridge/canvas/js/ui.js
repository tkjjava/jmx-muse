/**
 * ui.js — 工具栏 / 检查器 / 提示词条 / 菜单 / Toast / 查看器 / 快捷键 / 命令面板
 *
 * 为什么 UI 不直接改 Doc：
 *   这里只做"呈现 + 把用户意图转成 actions"，所有写入走 app.js → store.js。
 *   这样工具栏、检查器、命令面板不会各自成为第二套状态机，撤销也只会有一条路径。
 *
 * 为什么图标是内联 SVG 字符串而不是 <use href="assets/icons.svg#...">：
 *   零外部请求、file:// 可用，也不受 sprite 加载时序影响。assets/icons.svg
 *   保留同一组手写 symbol，供 bridge 其它页面复用。
 *
 * 为什么检查器重建要跳过"正在输入"：
 *   每次按键都重建 textarea 会丢光标。editing 时只原地更新状态/耗时，
 *   失焦后再完整重建；重建前记录 focus id 与 selectionStart，能恢复光标。
 */

import { docStats } from "./store.js";
import { formatElapsed } from "./nodes.js";

const ICONS = {
  layers:
    '<path d="M12 3 3.5 7.5 12 12l8.5-4.5L12 3Z"/><path d="m3.5 12 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>',
  undo: '<path d="m9 14-4-4 4-4"/><path d="M5 10h8a5 5 0 0 1 0 10h-2"/>',
  redo: '<path d="m15 14 4-4-4-4"/><path d="M19 10h-8a5 5 0 0 0 0 10h2"/>',
  download:
    '<path d="M12 4v10"/><path d="m8 11 4 4 4-4"/><path d="M5 17v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2"/>',
  settings:
    '<path d="M4 7h7"/><path d="M15 7h5"/><circle cx="13" cy="7" r="2"/><path d="M4 17h5"/><path d="M13 17h7"/><circle cx="11" cy="17" r="2"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  chevron: '<path d="m8 10 4 4 4-4"/>',
  fit: '<path d="M9 4H5a1 1 0 0 0-1 1v4"/><path d="M15 4h4a1 1 0 0 1 1 1v4"/><path d="M20 15v4a1 1 0 0 1-1 1h-4"/><path d="M9 20H5a1 1 0 0 1-1-1v-4"/>',
  map: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M3.5 14.5 9 10l4.5 4 3-2.5 4 3.5"/><circle cx="9.5" cy="8.5" r="1"/>',
  x: '<path d="m6 6 12 12"/><path d="M18 6 6 18"/>',
  image:
    '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m4 15 4.5-4.5 3.5 3.5 3-3 5 5"/><circle cx="9" cy="9" r="1.2"/>',
  sparkles:
    '<path d="m12 3 1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4L12 3Z"/><path d="M18.5 16.5 19 18l1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5.5-1.5Z"/>',
  play: '<path d="M8 5.5v13l10-6.5-10-6.5Z"/>',
  video:
    '<rect x="3.5" y="5.5" width="12" height="13" rx="2.5"/><path d="m16 10 4.5-2.5v9L16 14"/>',
  text: '<path d="M5 6h14"/><path d="M5 11h9"/><path d="M5 16h12"/><path d="M5 20h6"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2.5"/>',
  trash:
    '<path d="M5 7h14"/><path d="M9 7V5h6v2"/><path d="M7 7v11a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V7"/><path d="M10.5 11v5"/><path d="M13.5 11v5"/>',
  refresh:
    '<path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M19 11A7.5 7.5 0 0 0 6.2 7.2L4 9.5"/><path d="M5 13a7.5 7.5 0 0 0 12.8 3.8L20 14.5"/>',
  command:
    '<path d="M9 9h6v6H9z"/><path d="M9 9V6a2 2 0 1 0-2 2h2Z"/><path d="M15 9V6a2 2 0 1 1 2 2h-2Z"/><path d="M9 15v3a2 2 0 1 1-2-2h2Z"/><path d="M15 15v3a2 2 0 1 0 2-2h-2Z"/>',
  keyboard:
    '<rect x="3.5" y="6.5" width="17" height="11" rx="2.5"/><path d="M7 10h.01M10 10h.01M13 10h.01M16 10h.01M7.5 13.5h9"/>',
  grid: '<rect x="4" y="4" width="6" height="6" rx="1.4"/><rect x="14" y="4" width="6" height="6" rx="1.4"/><rect x="4" y="14" width="6" height="6" rx="1.4"/><rect x="14" y="14" width="6" height="6" rx="1.4"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  warning: '<path d="M12 4.5 21 20H3L12 4.5Z"/><path d="M12 10v4"/><path d="M12 17.5h.01"/>',
  frame: '<path d="M7 3v18M17 3v18M3 7h18M3 17h18"/>',
  arrowLeft: '<path d="M15 5 8 12l7 7"/>',
  external:
    '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  save: '<path d="M5 5h11l3 3v11a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V5Z"/><path d="M8.5 5v5h7V5"/><path d="M8.5 20v-5h7v5"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v6"/><path d="M12 7.5h.01"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>',
  pin: '<path d="M12 21s6-5.5 6-11a6 6 0 0 0-12 0c0 5.5 6 11 6 11Z"/><circle cx="12" cy="10" r="2"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  eye: '<path d="M3.5 12S7 5.5 12 5.5 20.5 12 20.5 12 17 18.5 12 18.5 3.5 12 3.5 12Z"/><circle cx="12" cy="12" r="3"/>',
  shield: '<path d="M12 3.5 5 6v5.5c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-2.5Z"/>',
};

function iconSvg(name, cls = "ico") {
  const body = ICONS[name] || "";
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
}

function hydrateIcons(root = document) {
  for (const el of root.querySelectorAll("[data-icon]")) {
    const name = el.getAttribute("data-icon");
    if (!name) continue;
    el.innerHTML = iconSvg(name);
  }
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 分享创建时间的人类可读形态；解析失败时原样显示，不抛错。 */
function formatShareTime(value) {
  if (!value) return "未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const TYPE_LABEL = {
  image: "图片卡片",
  video: "视频卡片",
  prompt: "提示词卡片",
  text: "文本便签",
};

const STATUS_LABEL = {
  idle: "待生成",
  queued: "排队中",
  generating: "生成中",
  cancelled: "已取消",
  failed: "生成失败",
  done: "已完成",
};

const SAVE_TEXT = {
  saved: "已保存",
  saving: "保存中…",
  dirty: "未保存",
  error: "保存失败",
};

function statusElapsed(node) {
  const started = Number(node.data?.createdAt) || 0;
  if (!started) return 0;
  return Math.max(0, (Date.now() - started) / 1000);
}

function statusText(node) {
  const status = node.data?.status || "idle";
  if (status === "generating") {
    return `生成中 · ${formatElapsed(statusElapsed(node))}`;
  }
  return STATUS_LABEL[status] || status;
}

function typeLabel(node) {
  return TYPE_LABEL[node.type] || node.type;
}

function ratioOf(node) {
  const configured = node.data?.params?.size;
  if (typeof configured === "string" && configured) return configured;
  const w = node.data?.width || node.w;
  const h = node.data?.height || node.h;
  if (!w || !h) return "—";
  const gcd = (a, b) => (b ? gcd(b, a % b) : a);
  const g = gcd(Math.round(w), Math.round(h));
  return `${Math.round(w / g)}:${Math.round(h / g)}`;
}

function menuItemHTML(item, index) {
  const cls = [
    "menu-item",
    item.active ? "is-active" : "",
    item.danger ? "is-danger" : "",
    item.disabled ? "is-disabled" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return `<button class="${cls}" type="button" role="menuitem" data-menu-index="${index}"
            ${item.disabled ? "disabled" : ""}>
    ${item.icon ? iconSvg(item.icon) : ""}
    <span class="menu-text">${esc(item.label)}</span>
    ${item.meta ? `<span class="menu-meta">${esc(item.meta)}</span>` : ""}
    ${item.active ? iconSvg("check") : ""}
  </button>`;
}

function kbd(value) {
  const parts = String(value)
    .split("+")
    .map((part) => `<kbd>${esc(part)}</kbd>`)
    .join("");
  return `<span class="shortcut-keys">${parts}</span>`;
}

function shortcutRow(keys, label) {
  return `<div class="shortcut-row"><span>${esc(label)}</span>${kbd(keys)}</div>`;
}

export class CanvasUI {
  constructor({ store, view, actions }) {
    this.store = store;
    this.view = view;
    this.actions = actions;

    this.model = "muse-image";
    this.size = "1:1";
    this.modeNote = "";
    this._editing = false;
    this._editTrx = false;
    this._inspectorSig = "";
    this._inspectorNodeId = null;
    this._inspectorSelKey = "";
    this._renaming = false;
    this._popover = null;
    this._overlayStack = [];
    this._confirm = null;
    this._viewer = { zoom: 1, x: 0, y: 0, pan: null, nodeId: null };
    this._lastMini = 0;
    this._bootHidden = false;
    this._genCount = 0;
    // 只读分享模式：由 app 在 `?share=` 启动路径里置位。这里只负责 UI 侧
    // （隐藏编辑入口、显示徽标），写操作由 app 的 action 守卫拦住。
    this.readOnly = false;

    this.refs = {
      stage: document.getElementById("stage"),
      toolbar: document.getElementById("toolbar"),
      spaceName: document.getElementById("space-name"),
      spaceNameText: document.getElementById("space-name-text"),
      spaceMenuBtn: document.getElementById("space-menu-btn"),
      saveBtn: document.getElementById("btn-save"),
      saveText: document.getElementById("save-text"),
      saveDot: document.querySelector("#btn-save .save-dot"),
      zoomLabel: document.getElementById("zoom-label"),
      zoomMenuBtn: document.getElementById("zoom-menu-btn"),
      zoomOut: document.getElementById("zoom-out"),
      zoomIn: document.getElementById("zoom-in"),
      undo: document.getElementById("undo"),
      redo: document.getElementById("redo"),
      shareBtn: document.getElementById("share-btn"),
      sharePanel: document.getElementById("share-panel"),
      shareClose: document.getElementById("share-close"),
      shareBody: document.getElementById("share-body"),
      shareStatus: document.getElementById("share-status"),
      exportBtn: document.getElementById("export-btn"),
      settingsBtn: document.getElementById("settings-btn"),
      museBtn: document.getElementById("muse-import-btn"),
      musePanel: document.getElementById("muse-panel"),
      museClose: document.getElementById("muse-close"),
      museTabSpaces: document.getElementById("muse-tab-spaces"),
      museTabIdeas: document.getElementById("muse-tab-ideas"),
      museBody: document.getElementById("muse-body"),
      museStatus: document.getElementById("muse-status"),
      readonlyBadge: document.getElementById("readonly-badge"),
      inspector: document.getElementById("inspector"),
      inspectorBody: document.getElementById("inspector-body"),
      promptbar: document.getElementById("promptbar"),
      promptInput: document.getElementById("prompt-input"),
      generateBtn: document.getElementById("generate-btn"),
      modelChip: document.getElementById("model-chip"),
      modelChipText: document.getElementById("model-chip-text"),
      sizeChip: document.getElementById("size-chip"),
      sizeChipText: document.getElementById("size-chip-text"),
      batchChip: document.getElementById("batch-chip"),
      toasts: document.getElementById("toasts"),
      minimap: document.getElementById("minimap"),
      minimapCanvas: document.getElementById("minimap-canvas"),
      minimapToggle: document.getElementById("minimap-toggle"),
      fitBtn: document.getElementById("fit-btn"),
      viewer: document.getElementById("viewer"),
      viewerStage: document.getElementById("viewer-stage"),
      viewerMedia: document.getElementById("viewer-media"),
      viewerTitle: document.getElementById("viewer-title"),
      viewerMeta: document.getElementById("viewer-meta"),
      viewerZoomLabel: document.getElementById("viewer-zoom-label"),
      viewerZoomOut: document.getElementById("viewer-zoom-out"),
      viewerZoomReset: document.getElementById("viewer-zoom-reset"),
      viewerZoomIn: document.getElementById("viewer-zoom-in"),
      viewerDownload: document.getElementById("viewer-download"),
      viewerClose: document.getElementById("viewer-close"),
      shortcuts: document.getElementById("shortcuts"),
      palette: document.getElementById("palette"),
      confirmLayer: document.getElementById("confirm"),
      popoverLayer: document.getElementById("popover-layer"),
      emptyState: document.getElementById("empty-state"),
      emptyGenerate: document.getElementById("empty-generate"),
      emptyNote: document.getElementById("empty-note"),
      boot: document.getElementById("boot"),
    };

    hydrateIcons(document);
    this._bind();
    this.applyUIState();
    this.renderToolbar();
    this.renderInspector(true);
  }

  /* ── 绑定 ───────────────────────────────────────────────────────────── */

  _bind() {
    const r = this.refs;

    r.spaceMenuBtn.addEventListener("click", () => this.openSpaceMenu(r.spaceMenuBtn));
    r.spaceName.addEventListener("click", () => {
      clearTimeout(this._spaceClickTimer);
      this._spaceClickTimer = setTimeout(() => this.openSpaceMenu(r.spaceName), 190);
    });
    r.spaceName.addEventListener("dblclick", (event) => {
      event.preventDefault();
      clearTimeout(this._spaceClickTimer);
      this.closePopover();
      this.startRenameSpace();
    });

    r.saveBtn.addEventListener("click", () => this.actions.saveNow());
    r.shareBtn.addEventListener("click", () => this.actions.shareSpace());
    r.shareClose?.addEventListener("click", () => this.closeSharePanel());
    r.sharePanel
      ?.querySelector("[data-share-close]")
      ?.addEventListener("click", () => this.closeSharePanel());
    r.undo.addEventListener("click", () => this.actions.undo());
    r.redo.addEventListener("click", () => this.actions.redo());
    r.exportBtn.addEventListener("click", () => this.actions.exportPNG());
    r.settingsBtn.addEventListener("click", () => this.openSettings(r.settingsBtn));
    r.museBtn?.addEventListener("click", () => this.openMusePanel());
    r.zoomOut.addEventListener("click", () => this.actions.zoomBy(0.8));
    r.zoomIn.addEventListener("click", () => this.actions.zoomBy(1.25));
    r.zoomMenuBtn.addEventListener("click", () => this.openZoomMenu(r.zoomMenuBtn));

    r.modelChip.addEventListener("click", () => this.openModelMenu(r.modelChip));
    r.sizeChip.addEventListener("click", () => this.openSizeMenu(r.sizeChip));

    r.promptInput.addEventListener("input", () => this._resizePrompt());
    r.promptInput.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.submitPrompt();
      }
    });
    r.generateBtn.addEventListener("click", () => this.submitPrompt());
    r.emptyGenerate.addEventListener("click", () => {
      if (!this.refs.promptInput.value.trim()) {
        this.refs.promptInput.value = "一座悬浮在云海上的未来城市，黄昏，电影感光线";
        this._resizePrompt();
      }
      this.submitPrompt();
    });
    r.emptyNote.addEventListener("click", () => this.actions.newNoteAtCenter());

    r.minimapToggle.addEventListener("click", () => {
      const visible = !this.store.state.ui.minimap;
      this.store.setUI({ minimap: visible });
      if (visible) this.updateMinimap(true);
    });
    r.fitBtn.addEventListener("click", () => this.actions.fit());
    r.minimapCanvas.addEventListener("click", (event) => {
      const world = this.view.minimapToWorld(
        r.minimapCanvas,
        event.clientX,
        event.clientY,
      );
      this.actions.centerOnWorld(world);
    });

    this._bindInspector();
    this._bindViewer();

    document.addEventListener(
      "pointerdown",
      (event) => {
        if (!this._popover) return;
        if (
          this._popover.menu.contains(event.target) ||
          this._popover.anchor.contains(event.target)
        ) {
          return;
        }
        this.closePopover();
      },
      true,
    );

    document.addEventListener(
      "keydown",
      (event) => {
        if (event.key !== "Escape") return;
        if (!this._popover) return;
        // 菜单打开时 Esc 只关菜单，不继续冒泡去清空选择
        event.preventDefault();
        event.stopPropagation();
        this.closePopover();
      },
      true,
    );

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      if (!this._overlayStack.length) return;
      this._trapTab(event);
    });
  }

  _bindInspector() {
    const body = this.refs.inspectorBody;

    body.addEventListener("focusin", (event) => {
      const input = event.target.closest("[data-insp-input]");
      if (!input) return;
      this._editing = true;
      if (!this._editTrx && !this.store.inTransaction) {
        this.store.beginTransaction("修改内容");
        this._editTrx = true;
      }
    });

    body.addEventListener("focusout", (event) => {
      if (!event.target.closest("[data-insp-input]")) return;
      setTimeout(() => {
        if (this.refs.inspector.contains(document.activeElement)) return;
        this._editing = false;
        this._commitEdit();
        this.renderInspector(true);
      }, 0);
    });

    body.addEventListener("input", (event) => {
      const input = event.target.closest("[data-insp-input]");
      if (!input || !this._inspectorNodeId) return;
      const kind = input.dataset.inspInput;
      // 程序化 focus / 浏览器自动填充不一定派发 focusin，所以输入事件本身
      // 也要兜底开启事务；否则失焦前的修改会退化成逐字符历史。
      if (!this._editTrx && !this.store.inTransaction) {
        this.store.beginTransaction("修改内容");
        this._editTrx = true;
      }
      this._editing = true;
      if (kind === "prompt") {
        this.store.updateNodeData(
          this._inspectorNodeId,
          { prompt: input.value, updatedAt: Date.now() },
          { history: false },
        );
      } else if (kind === "text") {
        this.store.updateNodeData(
          this._inspectorNodeId,
          { text: input.value, updatedAt: Date.now() },
          { history: false },
        );
      }
    });

    body.addEventListener("click", (event) => {
      const actionEl = event.target.closest("[data-insp-action]");
      if (!actionEl) return;
      // 先结束输入事务，避免"编辑中直接点删除"把未提交的 textarea 变更
      // 留在打开的事务里，导致撤销栈错位。
      if (this._editTrx) {
        this._editTrx = false;
        this._editing = false;
        this.store.commitTransaction();
      }
      const action = actionEl.dataset.inspAction;
      const id = this._inspectorNodeId;

      if (action === "shortcuts") return this.openShortcuts();
      if (action === "demo") return this.actions.loadDemo();
      if (action === "new-space") return this.actions.newSpace();
      if (action === "save") return this.actions.saveNow();
      if (action === "viewer" && id) return this.openViewer(id);
      if (action === "regenerate" && id) {
        this._commitEdit();
        return this.actions.regenerate(id);
      }
      if (action === "retry" && id) {
        this._commitEdit();
        return this.actions.regenerate(id);
      }
      if (action === "delete" && id) return this.actions.deleteNodes([id]);
      if (action === "duplicate" && id) return this.actions.duplicateNodes([id]);
      if (action === "download" && id) return this.actions.downloadNode(id);
      if (action === "delete-selection") return this.actions.deleteSelection();
      if (action === "duplicate-selection") return this.actions.duplicateSelection();
      if (action === "export") return this.actions.exportPNG();
    });
  }

  _bindViewer() {
    const r = this.refs;
    r.viewerClose.addEventListener("click", () => this.closeTop());
    r.viewerZoomOut.addEventListener("click", () => this._viewerZoom(0.8));
    r.viewerZoomIn.addEventListener("click", () => this._viewerZoom(1.25));
    r.viewerZoomReset.addEventListener("click", () => {
      this._viewer.zoom = 1;
      this._viewer.x = 0;
      this._viewer.y = 0;
      this._applyViewerTransform();
    });
    r.viewerDownload.addEventListener("click", () => {
      const id = this._viewer.nodeId;
      if (id) this.actions.downloadNode(id);
    });
    r.viewerStage.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const factor = Math.exp(-event.deltaY * 0.0016);
        this._viewerZoom(factor);
      },
      { passive: false },
    );
    r.viewerStage.addEventListener("dblclick", (event) => {
      if (event.target.closest("video")) return;
      this._viewer.zoom = 1;
      this._viewer.x = 0;
      this._viewer.y = 0;
      this._applyViewerTransform();
    });
    r.viewerStage.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      if (event.target.closest("video")) return;
      this._viewer.pan = {
        id: event.pointerId,
        sx: event.clientX,
        sy: event.clientY,
        ox: this._viewer.x,
        oy: this._viewer.y,
      };
      r.viewerStage.classList.add("is-panning");
    });
    window.addEventListener("pointermove", (event) => {
      const pan = this._viewer.pan;
      if (!pan || event.pointerId !== pan.id) return;
      this._viewer.x = pan.ox + (event.clientX - pan.sx);
      this._viewer.y = pan.oy + (event.clientY - pan.sy);
      this._applyViewerTransform();
    });
    const endPan = (event) => {
      if (!this._viewer.pan || event.pointerId !== this._viewer.pan.id) return;
      this._viewer.pan = null;
      r.viewerStage.classList.remove("is-panning");
    };
    window.addEventListener("pointerup", endPan);
    window.addEventListener("pointercancel", endPan);
  }

  _viewerZoom(factor) {
    const next = Math.min(6, Math.max(0.2, this._viewer.zoom * factor));
    if (next === this._viewer.zoom) return;
    this._viewer.zoom = next;
    this._applyViewerTransform();
  }

  _applyViewerTransform() {
    const { zoom, x, y } = this._viewer;
    this.refs.viewerMedia.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${zoom})`;
    this.refs.viewerZoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  }

  _resizePrompt() {
    const input = this.refs.promptInput;
    input.style.height = "auto";
    input.style.height = `${Math.min(96, Math.max(24, input.scrollHeight))}px`;
  }

  submitPrompt() {
    const prompt = this.refs.promptInput.value.trim();
    this.actions.generate({
      prompt,
      model: this.model,
      size: this.size,
    });
  }

  focusPrompt() {
    this.refs.promptInput.focus();
  }

  /** 双击便签 / 新建文本后把焦点送进检查器，直接开始打字 */
  focusInspectorInput() {
    const input = this.refs.inspectorBody.querySelector("[data-insp-input]");
    if (input) {
      input.focus();
      if ("select" in input) input.select();
    }
  }

  setPromptValue(value) {
    this.refs.promptInput.value = value;
    this._resizePrompt();
  }

  /* ── store 事件 ─────────────────────────────────────────────────────── */

  onStoreEvent(event) {
    const { type } = event;
    if (type === "selection") {
      this.renderInspector(true);
      this.renderPromptBar();
      this.renderToolbar();
      return;
    }
    if (type === "doc") {
      this.renderInspector(true);
      this.renderPromptBar();
      this.renderToolbar();
      this.applyUIState();
      return;
    }
    if (type === "change" || type === "history") {
      this.renderInspector(false);
      this.renderToolbar();
      this.renderPromptBar();
      if (this.store.state.ui.minimap) this.updateMinimap();
      return;
    }
    if (type === "camera") {
      this.renderToolbar();
      this.updateMinimap(true);
      return;
    }
    if (type === "save") {
      this.renderToolbar();
      return;
    }
    if (type === "workspace") {
      this.renderToolbar();
      // 自动保存后 app 会刷新空间列表（node_count/updated_at）。编辑中强制
      // 重建检查器会丢掉 textarea 光标，所以只在非编辑态刷新统计。
      if (!this._editing) this.renderInspector(true);
      return;
    }
    if (type === "ui") {
      this.applyUIState();
      return;
    }
    if (type === "tick") {
      this.updateLive();
      return;
    }
    if (type === "ready") {
      this.hideBoot();
    }
  }

  /* ── 工具栏 ─────────────────────────────────────────────────────────── */

  renderToolbar() {
    const s = this.store.state;
    if (!this._renaming) {
      const name =
        s.spaces.find((space) => space.id === s.currentSpaceId)?.name ||
        "未命名画布";
      if (this.refs.spaceNameText.textContent !== name) {
        this.refs.spaceNameText.textContent = name;
      }
      this.refs.spaceNameText.hidden = false;
    }

    const mode = s.mode === "bridge" ? "bridge" : "离线";
    this.refs.saveBtn.dataset.state = s.saveState;
    this.refs.saveBtn.setAttribute(
      "aria-label",
      `立即保存画布（${mode}模式，当前${SAVE_TEXT[s.saveState] || ""}）`,
    );
    this.refs.saveText.textContent = SAVE_TEXT[s.saveState] || "已保存";
    this.refs.saveDot.classList.toggle("is-saving", s.saveState === "saving");
    this.refs.saveDot.classList.toggle("is-dirty", s.saveState === "dirty");
    this.refs.saveDot.classList.toggle("is-error", s.saveState === "error");

    const percent = Math.round(s.doc.camera.zoom * 100);
    this.refs.zoomLabel.textContent = `${percent}%`;

    this.refs.undo.disabled = !this.store.canUndo;
    this.refs.redo.disabled = !this.store.canRedo;
    this.renderPromptBar();
  }

  renderPromptBar() {
    const count = this.store.state.selection.size;
    const chip = this.refs.batchChip;
    if (count > 1) {
      chip.hidden = false;
      chip.textContent = `批量 ×${count}`;
    } else {
      chip.hidden = true;
    }
    this.refs.modelChipText.textContent = this.model;
    this.refs.sizeChipText.textContent = this.size;
    this.refs.modelChip.setAttribute("aria-label", `选择模型，当前 ${this.model}`);
    this.refs.sizeChip.setAttribute("aria-label", `选择画面比例，当前 ${this.size}`);
  }

  setGenerating(count) {
    this._genCount = count;
    const active = count > 0;
    this.refs.generateBtn.classList.toggle("is-loading", active);
    this.refs.generateBtn.disabled = active;
    this.refs.generateBtn.setAttribute("aria-busy", active ? "true" : "false");
  }

  /* ── 检查器 ─────────────────────────────────────────────────────────── */

  _inspectorSignature(nodes) {
    return nodes
      .map((node) =>
        [
          node.id,
          node.type,
          node.w,
          node.h,
          node.data?.status,
          node.data?.url,
          node.data?.thumbUrl,
          node.data?.prompt,
          node.data?.text,
          node.data?.error,
          node.data?.model,
          node.data?.width,
          node.data?.height,
        ].join("~"),
      )
      .join("|");
  }

  renderInspector(force = false) {
    if (this._editing && !force) {
      this.updateLive();
      return;
    }
    const nodes = this.store.selectedNodes();
    const sig = this._inspectorSignature(nodes);
    if (!force && sig === this._inspectorSig) {
      this.updateLive();
      return;
    }
    this._inspectorSig = sig;
    const selKey = nodes.map((node) => node.id).join(",");
    const selectionChanged = selKey !== this._inspectorSelKey;
    this._inspectorSelKey = selKey;

    const active = document.activeElement;
    const restoreId =
      active && this.refs.inspector.contains(active) ? active.id : "";
    const caret = active && "selectionStart" in active ? active.selectionStart : null;

    let html = "";
    if (nodes.length === 0) {
      this._inspectorNodeId = null;
      html = this._buildEmptyInspector();
    } else if (nodes.length > 1) {
      this._inspectorNodeId = null;
      html = this._buildMultiInspector(nodes);
    } else {
      this._inspectorNodeId = nodes[0].id;
      html = this._buildSingleInspector(nodes[0]);
    }

    this.refs.inspectorBody.innerHTML = `<div class="insp-content">${html}</div>`;
    if (selectionChanged) this.refs.inspectorBody.scrollTop = 0;

    if (restoreId) {
      const el = document.getElementById(restoreId);
      if (el && this.refs.inspector.contains(el)) {
        el.focus();
        if (caret != null && "setSelectionRange" in el) {
          try {
            el.setSelectionRange(caret, caret);
          } catch {
            /* 部分 input 类型不支持 */
          }
        }
      }
    }
  }

  _buildEmptyInspector() {
    const stats = docStats(this.store.doc);
    const mode =
      this.store.state.mode === "bridge"
        ? "已连接 bridge · 文档保存在服务端"
        : "离线模式 · 文档保存在本机 localStorage";
    return `
      <div class="insp-head">
        <div>
          <div class="insp-kicker">SPACE</div>
          <h2 class="insp-title">空间概览</h2>
          <div class="insp-sub">${esc(mode)}</div>
        </div>
      </div>
      <div class="stats-grid">
        <div class="stat"><div class="stat-value">${stats.total}</div><div class="stat-label">节点</div></div>
        <div class="stat"><div class="stat-value">${stats.image}</div><div class="stat-label">图片</div></div>
        <div class="stat"><div class="stat-value">${stats.generating + stats.queued}</div><div class="stat-label">生成中</div></div>
        <div class="stat"><div class="stat-value">${stats.failed}</div><div class="stat-label">失败</div></div>
      </div>
      <div class="stats-note">
        <strong>${this.store.state.mode === "bridge" ? "bridge 模式" : "离线模式"}</strong>
        ${esc(this.modeNote ? ` · ${this.modeNote}` : "")}<br>
        拖动空白处框选，双击空白处新建便签；右键式的批量操作在命令面板。
      </div>
      <div class="insp-divider"></div>
      <div class="field-label">快捷键 <span class="field-hint">?</span></div>
      <div class="kbd-list">
        <div class="kbd-row"><span>平移画布</span>${kbd("空格+拖动")}</div>
        <div class="kbd-row"><span>缩放（光标锚点）</span>${kbd("Ctrl+滚轮")}</div>
        <div class="kbd-row"><span>适配全部</span>${kbd("Ctrl+0")}</div>
        <div class="kbd-row"><span>命令面板</span>${kbd("Ctrl+K")}</div>
        <div class="kbd-row"><span>撤销 / 重做</span>${kbd("Ctrl+Z")}</div>
      </div>
      <div class="action-row">
        <button class="btn btn-flex" type="button" data-insp-action="shortcuts">${iconSvg("keyboard")}全部快捷键</button>
        <button class="btn" type="button" data-insp-action="demo">${iconSvg("sparkles")}演示</button>
      </div>
    `;
  }

  _buildMultiInspector(nodes) {
    const labels = nodes.slice(0, 8).map((node) => {
      const text =
        node.data?.prompt || node.data?.text || TYPE_LABEL[node.type] || node.type;
      return `<span class="multi-item">
        <span class="swatch" style="background:${
          node.type === "text"
            ? "#ffb020"
            : node.type === "prompt"
              ? "#8b5cf6"
              : node.type === "video"
                ? "#3ddc97"
                : "#6e8cff"
        }"></span>
        <span class="label">${esc(text.slice(0, 28))}</span>
      </span>`;
    });
    return `
      <div class="insp-head">
        <div>
          <div class="insp-kicker">SELECTION</div>
          <h2 class="insp-title">已选中 ${nodes.length} 张卡片</h2>
          <div class="insp-sub">拖动任意一张会带着整组移动</div>
        </div>
      </div>
      <div class="multi-list">${labels.join("")}${nodes.length > 8 ? `<span class="multi-item">+${nodes.length - 8}</span>` : ""}</div>
      <div class="action-row">
        <button class="btn btn-flex" type="button" data-insp-action="duplicate-selection">${iconSvg("copy")}复制全部</button>
        <button class="btn btn-danger" type="button" data-insp-action="delete-selection">${iconSvg("trash")}删除</button>
      </div>
      <div class="insp-divider"></div>
      <div class="stats-note">批量生成会在每张选中卡片下方各放一张提示词卡，并共用一个提示词。</div>
    `;
  }

  _buildSingleInspector(node) {
    const status = node.data?.status || "idle";
    const isText = node.type === "text";
    const mediaUrl = node.data?.url || node.data?.thumbUrl || "";
    const isGenerating = status === "queued" || status === "generating";
    // cancelled 与 failed 都算"终态失败面"：检查器要给重试入口，文案由 statusText 区分。
    const failed = status === "failed" || status === "cancelled";

    let preview = "";
    if (mediaUrl && node.type !== "text") {
      preview = `<div class="insp-preview" data-insp-action="viewer" role="button" tabindex="0"
                    aria-label="打开媒体查看器">
          <img src="${esc(mediaUrl)}" alt="" decoding="async">
          <span class="preview-open">${iconSvg("eye")}查看</span>
        </div>`;
    } else {
      const hint = failed
        ? esc(node.data?.error || "生成失败")
        : isText
          ? "文本便签没有图片预览"
          : isGenerating
            ? "正在生成，稍后出现预览"
            : "还没有生成结果";
      preview = `<div class="insp-preview" style="cursor:default">
          <div class="preview-placeholder">${iconSvg(isText ? "text" : failed ? "warning" : "image")}
            <span>${hint}</span>
          </div>
        </div>`;
    }

    const editor = isText
      ? `<div class="insp-section">
          <div class="field-label">正文 <span class="field-hint">失焦后保存</span></div>
          <textarea class="insp-textarea" id="insp-text-${node.id}" data-insp-input="text"
            aria-label="便签正文" rows="4">${esc(node.data?.text || "")}</textarea>
        </div>`
      : `<div class="insp-section">
          <div class="field-label">提示词 <span class="field-hint">失焦后保存</span></div>
          <textarea class="insp-textarea" id="insp-prompt-${node.id}" data-insp-input="prompt"
            aria-label="提示词" rows="3">${esc(node.data?.prompt || "")}</textarea>
        </div>`;

    const statusLine = `
      <div class="status-inline">
        <span class="status-dot" data-status="${esc(status)}"></span>
        <span class="js-insp-status" data-node-id="${node.id}">${esc(statusText(node))}</span>
      </div>`;

    const progress =
      isGenerating
        ? `<div class="insp-progress"><span></span></div>`
        : failed
          ? `<div class="insp-error">${iconSvg("warning")}<span>${esc(node.data?.error || "生成失败，没有返回原因")}</span></div>`
          : "";

    const regenerate = isText
      ? ""
      : `<button class="btn btn-primary btn-flex" type="button"
            data-insp-action="${failed ? "retry" : "regenerate"}"
            ${isGenerating ? "disabled" : ""}>
          ${iconSvg("refresh")}${failed ? "重试生成" : isGenerating ? "生成中…" : "重新生成"}
        </button>`;

    const downloadBtn =
      mediaUrl
        ? `<button class="btn" type="button" data-insp-action="download" aria-label="下载媒体">
            ${iconSvg("download")}下载
          </button>`
        : "";

    const metaRows = [
      ["类型", typeLabel(node)],
      ["状态", STATUS_LABEL[status] || status],
      ["画布尺寸", `${Math.round(node.w)} × ${Math.round(node.h)}`],
      ["原图尺寸", node.data?.width ? `${node.data.width} × ${node.data.height}` : "—"],
      ["模型", node.data?.model || "muse-image"],
      ["比例", ratioOf(node)],
      node.data?.taskId ? ["任务", node.data.taskId] : null,
      ["节点", node.id],
    ].filter(Boolean);

    return `
      <div class="insp-head">
        <div>
          <div class="insp-kicker">${esc(node.type.toUpperCase())}</div>
          <h2 class="insp-title">${esc(typeLabel(node))}</h2>
          <div class="insp-sub">${statusLine}</div>
        </div>
        <div class="insp-head-actions">
          <button class="insp-icon-btn" type="button" data-insp-action="duplicate" aria-label="复制卡片">${iconSvg("copy")}</button>
          <button class="insp-icon-btn" type="button" data-insp-action="delete" aria-label="删除卡片">${iconSvg("trash")}</button>
        </div>
      </div>
      ${preview}
      ${progress}
      ${editor}
      <div class="action-row">
        ${regenerate}
        ${downloadBtn}
      </div>
      <div class="insp-meta">
        ${metaRows
          .map(
            ([key, value]) =>
              `<div class="meta-row"><span class="meta-key">${esc(key)}</span><span class="meta-val">${esc(value)}</span></div>`,
          )
          .join("")}
      </div>
    `;
  }

  /** tick / 生成中：只改耗时文字，不重建 DOM */
  updateLive() {
    if (this._inspectorNodeId) {
      const node = this.store.node(this._inspectorNodeId);
      if (node) {
        const target = this.refs.inspectorBody.querySelector(
          `.js-insp-status[data-node-id="${node.id}"]`,
        );
        if (target) {
          const text = statusText(node);
          if (target.textContent !== text) target.textContent = text;
        }
      }
    }
  }

  _commitEdit() {
    if (!this._editTrx) return;
    this._editTrx = false;
    this.store.commitTransaction();
  }

  /**
   * 外部（画布 pointerdown / 快捷键 / 手动保存）需要在改动文档结构前，
   * 把正在编辑的输入事务同步落盘到撤销栈，避免"编辑事务 + 拖拽事务"叠加。
   */
  flushEdit() {
    if (!this._editTrx) return false;
    this._editing = false;
    this._commitEdit();
    this.renderInspector(true);
    return true;
  }

  /* ── UI 偏好 ────────────────────────────────────────────────────────── */

  applyUIState() {
    const ui = this.store.state.ui;
    this.refs.stage.classList.toggle("no-grid", !ui.grid);
    this.refs.minimap.hidden = !ui.minimap;
    this.refs.minimapToggle.setAttribute(
      "aria-pressed",
      ui.minimap ? "true" : "false",
    );
    document.documentElement.classList.toggle("reduce-motion", Boolean(ui.reduceMotion));
    if (ui.minimap) this.updateMinimap(true);
    this.view.setGridSize(this.store.doc.meta.gridSize || 28);
  }

  /**
   * 进入/退出只读分享模式。
   *
   * 为什么 UI 侧只做"藏入口 + 徽标"而不是删按钮：app 的 action 守卫才是真正的
   * 拦截点（快捷键、命令面板、检查器回调都会绕过按钮显隐）。这里藏起来是为了
   * 让访客一眼看懂"这是只读画布"，不是安全边界。
   */
  setReadOnly(enabled, name = "") {
    this.readOnly = Boolean(enabled);
    document.body.classList.toggle("is-readonly", this.readOnly);
    this.refs.readonlyBadge.hidden = !this.readOnly;
    this.refs.shareBtn.setAttribute(
      "aria-label",
      this.readOnly ? "复制当前只读分享链接" : "复制只读分享链接",
    );
    if (this.readOnly && name) {
      this.refs.spaceNameText.textContent = name;
      this.refs.spaceNameText.hidden = false;
    }
    this.renderToolbar();
    this.applyUIState();
  }

  updateMinimap(force = false) {
    if (!this.store.state.ui.minimap) return;
    const now = performance.now();
    if (!force && now - this._lastMini < 160) return;
    this._lastMini = now;
    this.view.drawMinimap(
      this.refs.minimapCanvas,
      this.store.nodes,
      this.store.state.selection,
    );
  }

  setEmpty(visible) {
    this.refs.emptyState.hidden = !visible;
  }

  setModeInfo({ mode, note = "" } = {}) {
    if (mode) this.store.setMode(mode);
    this.modeNote = note;
    this.renderInspector(true);
    this.renderToolbar();
  }

  hideBoot() {
    if (this._bootHidden) return;
    this._bootHidden = true;
    const boot = this.refs.boot;
    if (!boot) return;
    boot.classList.add("is-out");
    setTimeout(() => boot.remove(), 480);
  }

  /* ── Toast ──────────────────────────────────────────────────────────── */

  toast(message, { type = "info", actionLabel = "", onAction = null, duration = 0 } = {}) {
    const icons = {
      info: "info",
      success: "check",
      warn: "warning",
      error: "warning",
    };
    const el = document.createElement("div");
    el.className = "toast";
    el.dataset.type = type;
    el.innerHTML = `
      ${iconSvg(icons[type] || "info", "toast-icon")}
      <div class="toast-body">
        <div class="toast-message">${esc(message)}</div>
        ${actionLabel ? `<div class="toast-actions"><button class="toast-action" type="button">${esc(actionLabel)}</button></div>` : ""}
      </div>
      <button class="toast-close" type="button" aria-label="关闭提示">${iconSvg("x")}</button>
    `;

    const remove = () => {
      if (!el.isConnected) return;
      el.classList.add("is-out");
      setTimeout(() => el.remove(), 180);
    };

    el.querySelector(".toast-close").addEventListener("click", remove);
    if (actionLabel && onAction) {
      el.querySelector(".toast-action").addEventListener("click", () => {
        onAction();
        remove();
      });
    }

    this.refs.toasts.appendChild(el);
    while (this.refs.toasts.children.length > 4) {
      this.refs.toasts.firstElementChild?.remove();
    }
    const life = duration || (type === "error" ? 6200 : 3800);
    setTimeout(remove, life);
    return { close: remove };
  }

  /* ── 浮层菜单 ───────────────────────────────────────────────────────── */

  closePopover() {
    if (!this._popover) return;
    this._popover.anchor.setAttribute("aria-expanded", "false");
    this._popover.menu.remove();
    this._popover = null;
  }

  openPopover(anchor, html, { align = "left", width = 0 } = {}) {
    this.closePopover();
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.setAttribute("role", "menu");
    menu.innerHTML = html;
    this.refs.popoverLayer.appendChild(menu);
    if (width) menu.style.width = `${width}px`;

    const anchorRect = anchor.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    let left = anchorRect.left;
    if (align === "center") left = anchorRect.left + anchorRect.width / 2 - menuRect.width / 2;
    if (align === "right") left = anchorRect.right - menuRect.width;
    left = Math.min(Math.max(8, left), window.innerWidth - menuRect.width - 8);
    let top = anchorRect.bottom + 8;
    if (top + menuRect.height > window.innerHeight - 8) {
      top = Math.max(8, anchorRect.top - menuRect.height - 8);
    }
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    anchor.setAttribute("aria-expanded", "true");
    this._popover = { anchor, menu };
    return menu;
  }

  _wireMenu(menu, items) {
    menu.addEventListener("click", (event) => {
      const item = event.target.closest(".menu-item");
      if (!item || item.disabled) return;
      const index = Number(item.dataset.menuIndex);
      const definition = items[index];
      if (!definition || definition.disabled) return;
      this.closePopover();
      definition.action?.();
    });
  }

  openZoomMenu(anchor) {
    const zoom = this.store.doc.camera.zoom;
    const values = [25, 50, 100, 200];
    const items = values.map((value) => ({
      label: `${value}%`,
      icon: Math.abs(zoom * 100 - value) < 0.5 ? "check" : "",
      active: Math.abs(zoom * 100 - value) < 0.5,
      action: () => this.actions.setZoom(value / 100),
    }));
    items.push({
      label: "适配全部节点",
      meta: "Ctrl+0",
      icon: "fit",
      action: () => this.actions.fit(),
    });
    const html = `<div class="menu-label">缩放</div>${items
      .map((item, index) => menuItemHTML(item, index))
      .join("")}<div class="menu-sep"></div>${menuItemHTML(
      { label: "复位到原点", meta: "Home", icon: "pin" },
      items.length,
    )}`;
    items.push({ label: "复位到原点", icon: "pin", action: () => this.actions.home() });
    const menu = this.openPopover(anchor, html, { align: "center" });
    this._wireMenu(menu, items);
  }

  openModelMenu(anchor) {
    const items = [
      {
        label: "muse-image",
        meta: "当前",
        icon: "image",
        active: true,
        action: () => {},
      },
      {
        label: "muse-video",
        meta: "暂未接入",
        icon: "video",
        disabled: true,
        action: () => {},
      },
    ];
    const html = `<div class="menu-label">模型</div>${items
      .map((item, index) => menuItemHTML(item, index))
      .join("")}<div class="menu-sep"></div>
      <div class="menu-row"><span>由 bridge 调度</span><span class="menu-meta">muse</span></div>`;
    const menu = this.openPopover(anchor, html, { align: "left" });
    this._wireMenu(menu, items);
  }

  openSizeMenu(anchor) {
    const sizes = [
      ["1:1", "方形"],
      ["16:9", "横屏"],
      ["9:16", "竖屏"],
      ["4:3", "横向"],
      ["3:4", "竖向"],
    ];
    const items = sizes.map(([value, meta]) => ({
      label: value,
      meta,
      icon: value === this.size ? "check" : "",
      active: value === this.size,
      action: () => {
        this.size = value;
        this.renderPromptBar();
      },
    }));
    const html = `<div class="menu-label">画面比例</div>${items
      .map((item, index) => menuItemHTML(item, index))
      .join("")}`;
    const menu = this.openPopover(anchor, html, { align: "left" });
    this._wireMenu(menu, items);
  }

  openSpaceMenu(anchor) {
    const state = this.store.state;
    const spaces = state.spaces || [];
    const spaceItems = spaces.map((space) => ({
      label: space.name || "未命名画布",
      meta: `${space.node_count ?? "?"} 节点`,
      icon: space.id === state.currentSpaceId ? "check" : "",
      active: space.id === state.currentSpaceId,
      action: () => this.actions.switchSpace(space.id),
    }));
    const newItem = {
      label: "新建空间",
      meta: "空白",
      icon: "plus",
      action: () => this.actions.newSpace(),
    };
    const deleteItem = state.currentSpaceId
      ? {
          label: `删除「${spaces.find((s) => s.id === state.currentSpaceId)?.name || "当前空间"}」`,
          icon: "trash",
          danger: true,
          action: () => this.actions.deleteSpace(state.currentSpaceId),
        }
      : null;
    const items = [...spaceItems, newItem, ...(deleteItem ? [deleteItem] : [])];

    const modeLabel = state.mode === "bridge" ? "bridge · 服务端" : "离线 · 本机";
    const html = `
      <div class="menu-label">空间 · ${esc(modeLabel)}</div>
      ${spaces.length ? `<div class="menu-list-scroll">${spaceItems
        .map((item, index) => menuItemHTML(item, index))
        .join("")}</div>` : ""}
      ${spaces.length ? '<div class="menu-sep"></div>' : ""}
      ${menuItemHTML(newItem, spaceItems.length)}
      ${deleteItem ? menuItemHTML(deleteItem, spaceItems.length + 1) : ""}
    `;
    const menu = this.openPopover(anchor, html, { align: "left", width: 248 });
    this._wireMenu(menu, items);
  }

  openSettings(anchor) {
    const ui = this.store.state.ui;
    const mode = this.store.state.mode === "bridge" ? "bridge" : "离线";
    const note = this.modeNote ? esc(this.modeNote) : "";
    const settings = [
      ["grid", "点阵网格", ui.grid],
      ["snap", "吸附对齐", ui.snap],
      ["minimap", "缩略图", ui.minimap],
      ["reduceMotion", "减少动效", ui.reduceMotion],
    ];

    const html = `
      <div class="menu-label">视图</div>
      ${settings
        .map(
          ([key, label, value]) => `<div class="menu-row">
            <span>${label}</span>
            <button class="switch" type="button" role="switch" aria-checked="${value}" data-setting="${key}" aria-label="${label}"></button>
          </div>`,
        )
        .join("")}
      <div class="menu-sep"></div>
      <div class="menu-label">数据</div>
      <div class="menu-row"><span>存储</span><span class="menu-meta">${mode}${note ? ` · ${note}` : ""}</span></div>
      ${menuItemHTML({ label: "立即保存", icon: "save" }, "save")}
      <div class="menu-sep"></div>
      <div class="menu-label">操作</div>
      ${menuItemHTML({ label: "导出 PNG", icon: "download" }, "export")}
      ${menuItemHTML({ label: "加载演示空间", icon: "sparkles" }, "demo")}
      ${menuItemHTML({ label: "新建空间", icon: "plus" }, "new-space")}
      ${menuItemHTML({ label: "清空当前画布", icon: "trash", danger: true }, "clear")}
      <div class="menu-sep"></div>
      ${menuItemHTML({ label: "快捷键", meta: "?", icon: "keyboard" }, "shortcuts")}
      ${menuItemHTML({ label: "重试连接 bridge", icon: "refresh" }, "retry")}
    `;

    const menu = this.openPopover(anchor, html, { align: "right", width: 260 });

    menu.addEventListener("click", (event) => {
      const switchEl = event.target.closest(".switch");
      if (switchEl) {
        const key = switchEl.dataset.setting;
        const next = switchEl.getAttribute("aria-checked") !== "true";
        switchEl.setAttribute("aria-checked", next ? "true" : "false");
        this.store.setUI({ [key]: next });
        return;
      }
      const item = event.target.closest(".menu-item");
      if (!item) return;
      const action = item.dataset.menuIndex;
      if (action === "save") this.actions.saveNow();
      else if (action === "export") this.actions.exportPNG();
      else if (action === "demo") this.actions.loadDemo();
      else if (action === "new-space") this.actions.newSpace();
      else if (action === "clear") this.actions.clearCanvas();
      else if (action === "shortcuts") this.openShortcuts();
      else if (action === "retry") this.actions.retryBridge();
      this.closePopover();
    });
  }

  startRenameSpace() {
    if (this._renaming) return;
    this._renaming = true;
    const current = this.refs.spaceNameText.textContent || "未命名画布";
    const button = this.refs.spaceName;
    const input = document.createElement("input");
    input.className = "space-rename";
    input.value = current;
    input.setAttribute("aria-label", "重命名空间");
    button.hidden = true;
    button.parentNode.insertBefore(input, button);
    input.focus();
    input.select();

    let finished = false;
    const finish = (commit) => {
      if (finished) return;
      finished = true;
      this._renaming = false;
      const value = input.value.trim();
      input.remove();
      button.hidden = false;
      if (commit && value && value !== current) {
        this.actions.renameSpace(value);
      } else {
        this.refs.spaceNameText.textContent = current;
      }
      this.renderToolbar();
    };

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
  }

  /* ── 查看器 ─────────────────────────────────────────────────────────── */

  /* ── 从 muse 导入（只读适配 P3）────────────────────────────────────── */

  /**
   * 打开「从 muse 导入」面板。
   *
   * 为什么走浮层栈而不是 toast/跳页：这是"挑一张卡片放进画布"的短流程，用户
   * 需要边看预览边决定；也复用既有的 Esc 关闭与焦点陷阱（`_pushOverlay`）。
   * 为什么面板自己发请求而不是由 app 预取：面板是懒加载的（只在用户点开时
   * 才打 muse 协议），切 tab 时按需拉数据；失败只在本面板里显示并可重试，
   * 不打断画布。
   */
  openMusePanel() {
    if (this.readOnly) {
      this.toast("只读分享里不能导入新卡片", { type: "warn" });
      return;
    }
    const panel = this.refs.musePanel;
    if (!panel) return;
    if (!this._museEntry) {
      this._museEntry = {
        type: "muse",
        layer: panel,
        onClose: () => {
          this._museLoaded = null;
        },
        restoreFocus: this.refs.museBtn,
      };
      const close = () => this.closeMusePanel();
      this.refs.museClose?.addEventListener("click", close);
      panel
        .querySelector("[data-muse-close]")
        ?.addEventListener("click", close);
      panel.querySelectorAll("[data-muse-tab]").forEach((button) => {
        button.addEventListener("click", () =>
          this._setMuseTab(button.dataset.museTab),
        );
      });
    }
    this._pushOverlay(this._museEntry);
    if (this._museTab !== "ideas") this._setMuseTab("spaces");
  }

  closeMusePanel() {
    const entry = this._overlayStack.find((item) => item.type === "muse");
    if (!entry) {
      this.refs.musePanel.hidden = true;
      return;
    }
    // 从栈顶往回弹到 muse 那一层（正常情况下它就是栈顶）。
    while (this._overlayStack.length && this._overlayStack.at(-1) !== entry) {
      this.closeTop();
    }
    this.closeTop();
  }

  /* ── 分享画布：链接管理（复制 / 轮换 / 撤销） ─────────────────────── */

  openSharePanel() {
    const panel = this.refs.sharePanel;
    if (!panel) return;
    if (!this._shareEntry) {
      this._shareEntry = {
        type: "share",
        layer: panel,
        restoreFocus: this.refs.shareBtn,
      };
      this.refs.shareBody?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-share-action]");
        if (!button) return;
        const action = button.dataset.shareAction;
        if (action === "copy") this.actions.copyShare();
        else if (action === "rotate") this.actions.rotateShare();
        else if (action === "revoke") this.actions.revokeShare();
        else if (action === "create") this.actions.createShare();
        else if (action === "retry") this.actions.refreshShare();
      });
    }
    this._pushOverlay(this._shareEntry);
    // 浮层打开后焦点必须进层：否则 Tab 焦点还留在被遮住的工具栏上，
    // 键盘用户要按一次 Tab 才"发现"面板。
    this.refs.shareClose?.focus?.();
  }

  closeSharePanel() {
    const entry = this._overlayStack.find((item) => item.type === "share");
    if (!entry) {
      this.refs.sharePanel.hidden = true;
      return;
    }
    while (this._overlayStack.length && this._overlayStack.at(-1) !== entry) {
      this.closeTop();
    }
    this.closeTop();
  }

  setShareStatus(text, tone = "") {
    if (!this.refs.shareStatus) return;
    this.refs.shareStatus.textContent = text;
    this.refs.shareStatus.dataset.tone = tone;
  }

  /** 面板当前渲染的分享对象；`null` = 尚未分享。供 app 的复制/撤销动作取用。 */
  currentShare() {
    return this._shareView || null;
  }

  /**
   * 渲染分享面板。
   *
   * 三种状态必须分开渲染，不允许"把错误吞掉只显示空状态"：
   *   * `loading`：正在读取；
   *   * `error`：读取失败 + 重试按钮（bridge 不可达时用户需要知道原因）；
   *   * 正常：未分享（引导创建）/ 已分享（URL + 创建时间 + 三个动作）。
   */
  renderSharePanel(share, { loading = false, error = "" } = {}) {
    const body = this.refs.shareBody;
    if (!body) return;
    this._shareView = share || null;
    if (loading) {
      body.innerHTML = `<div class="share-empty">正在读取分享状态…</div>`;
      return;
    }
    if (error) {
      body.innerHTML = `
        <div class="share-empty">
          <strong>读取分享状态失败</strong>
          ${esc(error)}
          <div class="share-actions">
            <button class="btn" type="button" data-share-action="retry">重试</button>
          </div>
        </div>`;
      return;
    }
    if (!share) {
      body.innerHTML = `
        <div class="share-empty">
          <strong>还没有分享链接</strong>
          生成一条只读链接后，任何人凭链接都能查看这张画布（可缩放、可导出）。
          <div class="share-actions">
            <button class="btn btn-primary" type="button" data-share-action="create">生成分享链接</button>
          </div>
        </div>`;
      return;
    }
    const absolute = `${window.location.origin}${share.url || ""}`;
    const created = formatShareTime(share.created_at);
    body.innerHTML = `
      <div class="share-url">
        <input id="share-url-input" type="text" readonly value="${esc(absolute)}"
               aria-label="分享链接" spellcheck="false" />
      </div>
      <div class="share-meta">
        <span>状态：<b>有效</b></span>
        <span>创建于：<b>${esc(created)}</b></span>
        <span>权限：<b>只读</b></span>
      </div>
      <div class="share-actions">
        <button class="btn btn-primary" type="button" data-share-action="copy">复制链接</button>
        <button class="btn" type="button" data-share-action="rotate">重新生成</button>
        <button class="btn btn-danger" type="button" data-share-action="revoke">撤销分享</button>
      </div>
      <p class="share-hint">重新生成 = 换一条新 token，旧链接立即失效；撤销 = 删除这条分享，旧链接立即打不开。两者都不可恢复。</p>`;
  }

  _setMuseTab(tab) {
    this._museTab = tab === "ideas" ? "ideas" : "spaces";
    for (const [name, button] of [
      ["spaces", this.refs.museTabSpaces],
      ["ideas", this.refs.museTabIdeas],
    ]) {
      const active = name === this._museTab;
      button?.classList.toggle("is-active", active);
      button?.setAttribute("aria-selected", active ? "true" : "false");
    }
    this._loadMuseTab(this._museTab);
  }

  _museStatus(text, tone = "") {
    if (!this.refs.museStatus) return;
    this.refs.museStatus.textContent = text;
    this.refs.museStatus.dataset.tone = tone;
  }

  async _loadMuseTab(tab) {
    const body = this.refs.museBody;
    if (!body) return;
    const token = (this._museToken = (this._museToken || 0) + 1);
    body.innerHTML = `<div class="muse-loading">正在读取 muse…</div>`;
    this._museStatus(tab === "ideas" ? "读取 Explore 灵感卡…" : "读取 muse Spaces…");
    try {
      if (tab === "ideas") {
        const sections = await this.actions.listMuseIdeaCards();
        if (token !== this._museToken) return;
        this._renderMuseIdeas(sections);
        this._museStatus(`已读取 ${sections.length} 个分区`);
      } else {
        const spaces = await this.actions.listMuseSpaces();
        if (token !== this._museToken) return;
        this._renderMuseSpaces(spaces);
        this._museStatus(
          spaces.length ? `共 ${spaces.length} 个 Space` : "该账号还没有 Space",
        );
      }
    } catch (error) {
      if (token !== this._museToken) return;
      const message = error?.message || "读取失败";
      body.innerHTML = `<div class="muse-error"><p>读取失败：${esc(message)}</p>
        <button class="btn" type="button" data-muse-retry>重试</button></div>`;
      body
        .querySelector("[data-muse-retry]")
        ?.addEventListener("click", () => this._loadMuseTab(tab));
      this._museStatus("读取失败", "error");
    }
  }

  _renderMuseSpaces(spaces) {
    const body = this.refs.museBody;
    if (!spaces.length) {
      body.innerHTML = `<div class="muse-empty">
        <p>这个 muse 账号还没有 Space。</p>
        <p class="muse-empty-hint">先在 muse.ai 里建一个 Space，再回来点「Spaces」刷新；
        也可以直接去「灵感卡」看看 Explore 里的卡片。</p>
      </div>`;
      return;
    }
    const rows = spaces
      .map((space, index) => {
        const preview = space.preview_image || {};
        const image = preview.thumbnail_url || space.image_url || "";
        const updated = space.updated_at_ms
          ? new Date(Number(space.updated_at_ms)).toLocaleString()
          : "";
        const status =
          space.construction_status && space.construction_status !== "ready"
            ? esc(space.construction_status)
            : "";
        return `<article class="muse-space" data-index="${index}">
          <div class="muse-space-thumb">${
            image
              ? `<img src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
              : `<span data-icon="image" aria-hidden="true"></span>`
          }</div>
          <div class="muse-space-info">
            <h4>${esc(space.name)}</h4>
            <p>${esc(space.slug)}${updated ? ` · ${esc(updated)}` : ""}${
              status ? ` · ${status}` : ""
            }</p>
          </div>
          <button class="btn primary" type="button" data-muse-import="${index}">
            导入卡片
          </button>
        </article>`;
      })
      .join("");
    body.innerHTML = `<div class="muse-spaces">${rows}</div>`;
    hydrateIcons(body);
    body.querySelectorAll("[data-muse-import]").forEach((button) => {
      button.addEventListener("click", () => {
        const space = spaces[Number(button.dataset.museImport)];
        if (!space) return;
        button.disabled = true;
        button.textContent = "导入中…";
        Promise.resolve(this.actions.importMuseSpace(space.slug, space))
          .then((result) => {
            button.disabled = false;
            button.textContent = "已导入";
            this._museStatus(
              result?.node ? `已导入「${space.name}」为画布卡片` : "已导入",
              "ok",
            );
          })
          .catch((error) => {
            button.disabled = false;
            button.textContent = "导入卡片";
            this._museStatus(`导入失败：${error?.message || "未知错误"}`, "error");
          });
      });
    });
  }

  _renderMuseIdeas(sections) {
    const body = this.refs.museBody;
    const usable = sections.filter((section) => (section.cards || []).length);
    if (!usable.length) {
      body.innerHTML = `<div class="muse-empty"><p>灵感卡暂时没有可用条目。</p></div>`;
      return;
    }
    body.innerHTML = usable
      .map(
        (section) => `<section class="muse-idea-section">
          <h4>${esc(section.title || section.id)}</h4>
          <div class="muse-idea-grid">${(section.cards || [])
            .slice(0, 12)
            .map(
              (card, index) => `<button class="muse-idea" type="button"
                data-muse-idea="${esc(section.id)}:${index}"
                title="${esc(card.summary || card.title || "")}">
                <span class="muse-idea-icon">${
                  card.iconUrl
                    ? `<img src="${esc(card.iconUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
                    : ``
                }</span>
                <span class="muse-idea-title">${esc(card.title || "")}</span>
                <span class="muse-idea-summary">${esc(card.summary || "")}</span>
              </button>`,
            )
            .join("")}</div>
        </section>`,
      )
      .join("");
    body.querySelectorAll("[data-muse-idea]").forEach((button) => {
      button.addEventListener("click", () => {
        const [sectionId, index] = String(button.dataset.museIdea).split(":");
        const section = usable.find((item) => item.id === sectionId);
        const card = section?.cards?.[Number(index)];
        if (!card) return;
        this.actions.addIdeaCard(card);
        this._museStatus(`已把「${card.title}」加成便签`, "ok");
      });
    });
  }

  openViewer(nodeId) {
    const node = this.store.node(nodeId);
    if (!node) return;
    const mediaUrl = node.data?.url || node.data?.thumbUrl || "";
    if (!mediaUrl) {
      this.toast("这张卡片还没有可查看的媒体", { type: "warn" });
      return;
    }

    if (this._overlayStack.some((entry) => entry.type === "viewer")) this.closeTop();

    const isVideo = node.type === "video";
    const playable = isVideo && node.data?.url;
    this._viewer = { zoom: 1, x: 0, y: 0, pan: null, nodeId };
    this.refs.viewerMedia.innerHTML = playable
      ? `<video src="${esc(node.data.url)}" controls loop playsinline preload="metadata"${
          node.data?.thumbUrl ? ` poster="${esc(node.data.thumbUrl)}"` : ""
        }></video>`
      : `<img src="${esc(mediaUrl)}" alt="" draggable="false">`;
    this.refs.viewerTitle.textContent =
      (node.data?.prompt || "").slice(0, 80) || typeLabel(node);
    this.refs.viewerMeta.textContent = `${typeLabel(node)} · ${
      node.data?.width ? `${node.data.width} × ${node.data.height}` : "尺寸未知"
    } · ${node.data?.model || "muse-image"}`;
    this._applyViewerTransform();

    const previous = document.activeElement;
    this.refs.viewer.hidden = false;
    const entry = {
      layer: this.refs.viewer,
      type: "viewer",
      restoreFocus: previous,
      onClose: () => {
        const video = this.refs.viewerMedia.querySelector("video");
        if (video) {
          video.pause();
          video.removeAttribute("src");
          video.load();
        }
        this.refs.viewerMedia.innerHTML = "";
        this._viewer = { zoom: 1, x: 0, y: 0, pan: null, nodeId: null };
      },
    };
    this._pushOverlay(entry);
    this.refs.viewerClose.focus();
  }

  /* ── 快捷键面板 / 命令面板 / 确认框 ─────────────────────────────────── */

  openShortcuts() {
    const html = `
      <div class="sheet">
        <div class="sheet-head">
          <div>
            <div class="sheet-title">快捷键</div>
            <div class="sheet-sub">画布、选择、生成与面板</div>
          </div>
          <button class="sheet-close" type="button" data-sheet-close aria-label="关闭">${iconSvg("x")}</button>
        </div>
        <div class="sheet-body">
          <div class="shortcut-groups">
            <div class="shortcut-group-title">画布</div>
            ${shortcutRow("空格+拖动", "平移画布")}
            ${shortcutRow("中键+拖动", "平移画布")}
            ${shortcutRow("Ctrl+滚轮", "以光标为锚点缩放")}
            ${shortcutRow("Ctrl+0", "适配全部节点")}
            ${shortcutRow("Home", "复位到原点")}
            ${shortcutRow("双击空白", "新建便签")}
            <div class="shortcut-group-title">编辑</div>
            ${shortcutRow("Ctrl+Z", "撤销")}
            ${shortcutRow("Ctrl+Shift+Z", "重做")}
            ${shortcutRow("Ctrl+D", "复制选中卡片")}
            ${shortcutRow("Delete", "删除选中卡片")}
            ${shortcutRow("Shift+点击", "加选 / 减选")}
            ${shortcutRow("拖拽空白", "框选多张")}
            ${shortcutRow("Shift+拖动边角", "等比缩放卡片")}
            <div class="shortcut-group-title">生成</div>
            ${shortcutRow("Enter", "用当前提示词生成")}
            ${shortcutRow("Shift+Enter", "输入框内换行")}
            ${shortcutRow("Ctrl+S", "立即保存画布")}
            ${shortcutRow("双击卡片", "打开图片 / 视频查看器")}
            ${shortcutRow("Esc", "取消选择 / 关闭浮层")}
            <div class="shortcut-group-title">面板</div>
            ${shortcutRow("Ctrl+K", "命令面板")}
            ${shortcutRow("?", "打开这张快捷键表")}
            ${shortcutRow("Ctrl+/", "打开这张快捷键表")}
            ${shortcutRow("Ctrl+滚轮", "查看器内缩放")}
          </div>
        </div>
      </div>
    `;
    const layer = this.refs.shortcuts;
    layer.innerHTML = html;
    layer.hidden = false;
    const entry = {
      layer,
      type: "shortcuts",
      restoreFocus: document.activeElement,
      cleanup: () => {
        layer.innerHTML = "";
        layer.removeEventListener("pointerdown", onBackdrop, true);
      },
    };
    const onBackdrop = (event) => {
      if (event.target === layer) this.closeTop();
    };
    layer.addEventListener("pointerdown", onBackdrop, true);
    this._pushOverlay(entry);
    layer.querySelector(".sheet-close").focus();
  }

  _commands() {
    return [
      {
        id: "fit",
        label: "适配全部节点",
        kbd: "Ctrl+0",
        icon: "fit",
        run: () => this.actions.fit(),
      },
      {
        id: "home",
        label: "复位视图到原点",
        kbd: "Home",
        icon: "pin",
        run: () => this.actions.home(),
      },
      {
        id: "note",
        label: "新建便签卡片",
        kbd: "双击空白",
        icon: "text",
        run: () => this.actions.newNoteAtCenter(),
      },
      {
        id: "export",
        label: "导出当前视口 PNG",
        kbd: "",
        icon: "download",
        run: () => this.actions.exportPNG(),
      },
      {
        id: "save",
        label: "立即保存画布",
        kbd: "",
        icon: "save",
        run: () => this.actions.saveNow(),
      },
      {
        id: "new-space",
        label: "新建空白空间",
        kbd: "",
        icon: "plus",
        run: () => this.actions.newSpace(),
      },
      {
        id: "demo",
        label: "加载演示空间",
        kbd: "",
        icon: "sparkles",
        run: () => this.actions.loadDemo(),
      },
      {
        id: "undo",
        label: "撤销上一步",
        kbd: "Ctrl+Z",
        icon: "undo",
        run: () => this.actions.undo(),
      },
      {
        id: "redo",
        label: "重做",
        kbd: "Ctrl+Shift+Z",
        icon: "redo",
        run: () => this.actions.redo(),
      },
      {
        id: "duplicate",
        label: "复制选中的卡片",
        kbd: "Ctrl+D",
        icon: "copy",
        run: () => this.actions.duplicateSelection(),
      },
      {
        id: "delete",
        label: "删除选中的卡片",
        kbd: "Delete",
        icon: "trash",
        run: () => this.actions.deleteSelection(),
      },
      {
        id: "grid",
        label: "切换点阵网格",
        kbd: "",
        icon: "grid",
        run: () => this.store.setUI({ grid: !this.store.state.ui.grid }),
      },
      {
        id: "minimap",
        label: "切换缩略图",
        kbd: "",
        icon: "map",
        run: () => this.store.setUI({ minimap: !this.store.state.ui.minimap }),
      },
      {
        id: "shortcuts",
        label: "查看全部快捷键",
        kbd: "?",
        icon: "keyboard",
        run: () => this.openShortcuts(),
      },
      {
        id: "clear",
        label: "清空当前画布",
        kbd: "",
        icon: "trash",
        danger: true,
        run: () => this.actions.clearCanvas(),
      },
    ];
  }

  openPalette() {
    if (this._overlayStack.some((entry) => entry.type === "palette")) return;
    const layer = this.refs.palette;
    layer.innerHTML = `
      <div class="palette">
        <div class="palette-input-row">
          ${iconSvg("search")}
          <input class="palette-input" id="palette-input" type="text"
                 placeholder="搜索命令…" autocomplete="off" spellcheck="false"
                 aria-label="搜索命令">
          <kbd>Esc</kbd>
        </div>
        <div class="palette-list" id="palette-list" role="listbox" aria-label="命令列表"></div>
        <div class="palette-foot"><span>↑↓ 选择 · Enter 执行</span><span>Ctrl+K</span></div>
      </div>
    `;
    layer.hidden = false;

    const input = layer.querySelector("#palette-input");
    const list = layer.querySelector("#palette-list");
    const commands = this._commands();
    let filtered = commands;
    let selected = 0;

    const renderList = () => {
      if (!filtered.length) {
        list.innerHTML = `<div class="palette-empty">没有匹配的命令</div>`;
        return;
      }
      list.innerHTML = filtered
        .map(
          (command, index) => `
        <button class="palette-item ${index === selected ? "is-selected" : ""}" type="button"
                role="option" aria-selected="${index === selected}" data-command-index="${index}">
          ${iconSvg(command.icon)}
          <span class="label">${esc(command.label)}</span>
          ${command.danger ? `<span class="menu-meta" style="color:var(--danger)">危险</span>` : ""}
          ${command.kbd ? `<kbd>${esc(command.kbd)}</kbd>` : ""}
        </button>`,
        )
        .join("");
      const selectedEl = list.querySelector(".is-selected");
      selectedEl?.scrollIntoView({ block: "nearest" });
    };

    const runSelected = () => {
      const command = filtered[selected];
      if (!command) return;
      this.closeTop();
      command.run();
    };

    const onInput = () => {
      const query = input.value.trim().toLowerCase();
      filtered = query
        ? commands.filter((command) => command.label.toLowerCase().includes(query))
        : commands;
      selected = 0;
      renderList();
    };

    const onKey = (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        selected = Math.min(filtered.length - 1, selected + 1);
        renderList();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        selected = Math.max(0, selected - 1);
        renderList();
      } else if (event.key === "Enter") {
        event.preventDefault();
        runSelected();
      } else if (event.key === "Escape") {
        event.preventDefault();
        this.closeTop();
      }
    };

    input.addEventListener("input", onInput);
    input.addEventListener("keydown", onKey);
    list.addEventListener("click", (event) => {
      const item = event.target.closest("[data-command-index]");
      if (!item) return;
      const index = Number(item.dataset.commandIndex);
      const command = filtered[index];
      if (!command) return;
      this.closeTop();
      command.run();
    });

    renderList();

    const entry = {
      layer,
      type: "palette",
      restoreFocus: document.activeElement,
      cleanup: () => {
        layer.innerHTML = "";
        layer.removeEventListener("pointerdown", onBackdrop, true);
      },
    };
    const onBackdrop = (event) => {
      if (event.target === layer) this.closeTop();
    };
    layer.addEventListener("pointerdown", onBackdrop, true);
    this._pushOverlay(entry);
    input.focus();
  }

  confirm({
    title = "确认操作",
    message = "",
    confirmLabel = "确定",
    cancelLabel = "取消",
    danger = true,
  } = {}) {
    if (this._confirm) {
      this._confirm.settle(false);
      this.closeTop();
    }
    return new Promise((resolve) => {
      const layer = this.refs.confirmLayer;
      layer.innerHTML = `
        <div class="confirm-sheet">
          <div class="confirm-title">${esc(title)}</div>
          <div class="confirm-text">${esc(message)}</div>
          <div class="confirm-actions">
            <button class="btn" type="button" data-confirm="cancel">${esc(cancelLabel)}</button>
            <button class="btn ${danger ? "btn-danger" : "btn-primary"}" type="button" data-confirm="ok">${esc(confirmLabel)}</button>
          </div>
        </div>
      `;
      layer.hidden = false;

      let settled = false;
      // 只负责结算 Promise，不递归关闭浮层。Esc / 点击遮罩由 closeTop 走 onClose。
      const settle = (value) => {
        if (settled) return;
        settled = true;
        this._confirm = null;
        layer.removeEventListener("click", onClick);
        resolve(value);
      };
      const closeWith = (value) => {
        settle(value);
        this.closeTop();
      };
      const onClick = (event) => {
        const button = event.target.closest("[data-confirm]");
        if (button) {
          closeWith(button.dataset.confirm === "ok");
          return;
        }
        if (event.target === layer) closeWith(false);
      };
      this._confirm = { settle };
      layer.addEventListener("click", onClick);

      const entry = {
        layer,
        type: "confirm",
        restoreFocus: document.activeElement,
        onClose: () => settle(false),
        cleanup: () => {
          layer.removeEventListener("click", onClick);
          layer.innerHTML = "";
        },
      };
      this._pushOverlay(entry);
      const cancel = layer.querySelector('[data-confirm="cancel"]');
      cancel?.focus();
    });
  }

  /* ── 浮层栈 / 焦点 ─────────────────────────────────────────────────── */

  _pushOverlay(entry) {
    this._overlayStack.push(entry);
    entry.layer.hidden = false;
  }

  hasOverlay() {
    return this._overlayStack.length > 0;
  }

  closeTop() {
    const entry = this._overlayStack.pop();
    if (!entry) return false;
    entry.layer.hidden = true;
    entry.onClose?.();
    entry.cleanup?.();
    if (this._confirm && entry.type === "confirm") {
      // onClose 已 resolve，这里只清空引用
      this._confirm = null;
    }
    const restore = entry.restoreFocus;
    if (restore && restore.isConnected && typeof restore.focus === "function") {
      restore.focus();
    }
    return true;
  }

  _trapTab(event) {
    const top = this._overlayStack[this._overlayStack.length - 1];
    if (!top) return;
    const nodes = [
      ...top.layer.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select, [tabindex]:not([tabindex="-1"])',
      ),
    ].filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !top.layer.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !top.layer.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }
}

export { iconSvg, hydrateIcons, kbd, TYPE_LABEL, STATUS_LABEL };
