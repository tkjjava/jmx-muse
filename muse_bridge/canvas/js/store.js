/**
 * store.js — 单一状态源 + 命令式撤销/重做 + 订阅
 *
 * 为什么把 Doc、选中集、空间元数据、保存状态放在一个 store 里：
 *   画布层、检查器、缩略图、导出必须读到同一份事实。任何一处各自缓存一份
 *   nodes，撤销后就会出现"画布没了但检查器还在"的鬼影。
 *
 * 历史为什么用「受影响的节点快照」而不是整份 Doc：
 *   演示空间有 300+ 节点，整份快照 × 100 步会吃掉十几 MB，还会拖慢每次提交；
 *   一次拖动/缩放实际只碰 1–6 个节点，记录 before/after 的节点级差异即可。
 *   拖动属于连续操作，由 app 在 pointerdown 时 beginTransaction、pointerup
 *   时 commitTransaction，因此整段拖动只产生一条历史，不会每帧写一条。
 *
 * 为什么 id 由前端生成、服务端只读：
 *   契约 §3.1 规定空间 id 服务端生成，节点 id（^n_[a-z0-9]{10}$）没有生成方
 *   约定；前端先生成才能离线落盘，再同步到 bridge。
 */

export const DOC_VERSION = 1;

export const LIMITS = Object.freeze({
  maxNodes: 2000,
  minSize: 80,
  minZoom: 0.05,
  maxZoom: 8,
  maxDocBytes: 4 * 1024 * 1024,
  history: 100,
});

export const NODE_TYPES = Object.freeze(["image", "video", "prompt", "text"]);

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";

function randomChars(count) {
  const chars = [];
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
    const buf = new Uint32Array(count);
    cryptoObj.getRandomValues(buf);
    for (let i = 0; i < count; i += 1) {
      chars.push(ID_ALPHABET[buf[i] % ID_ALPHABET.length]);
    }
  } else {
    for (let i = 0; i < count; i += 1) {
      chars.push(ID_ALPHABET[Math.floor(Math.random() * ID_ALPHABET.length)]);
    }
  }
  return chars.join("");
}

/** 节点 id：契约 §4 的 ^n_[a-z0-9]{10}$ */
export function newNodeId() {
  return `n_${randomChars(10)}`;
}

/** 本地空间 id：与 bridge 的 sp_ab12cd34 同形，离线时也保持一致 */
export function newSpaceId() {
  return `sp_${randomChars(8)}`;
}

export function newEdgeId() {
  return `e_${randomChars(10)}`;
}

export function clamp(value, min, max) {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

export function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function cloneNode(node) {
  if (node == null) return null;
  if (typeof structuredClone === "function") {
    try {
      return structuredClone(node);
    } catch {
      /* 含不可克隆值（理论上 Doc 不会有）时退回 JSON */
    }
  }
  return JSON.parse(JSON.stringify(node));
}

function nodeSignature(node) {
  return JSON.stringify(node);
}

/** 初始 Doc —— 契约 §6：camera(-200,-120,1)，gridSize 28 */
export function createEmptyDoc() {
  return {
    version: DOC_VERSION,
    camera: { x: -200, y: -120, zoom: 1 },
    nodes: [],
    edges: [],
    meta: { theme: "dark", background: "dots", gridSize: 28 },
  };
}

function normalizeNode(raw) {
  if (!raw || typeof raw !== "object") return null;
  const node = { ...raw };
  node.id =
    typeof raw.id === "string" && /^n_[a-z0-9]{10}$/.test(raw.id)
      ? raw.id
      : newNodeId();
  node.type = NODE_TYPES.includes(raw.type) ? raw.type : "image";
  node.x = isFiniteNumber(raw.x) ? raw.x : 0;
  node.y = isFiniteNumber(raw.y) ? raw.y : 0;
  node.w = isFiniteNumber(raw.w) ? Math.max(LIMITS.minSize, raw.w) : 280;
  node.h = isFiniteNumber(raw.h) ? Math.max(LIMITS.minSize, raw.h) : 200;
  node.z = isFiniteNumber(raw.z) ? Math.round(raw.z) : 0;
  node.data =
    raw.data && typeof raw.data === "object" && !Array.isArray(raw.data)
      ? { ...raw.data }
      : {};
  return node;
}

/**
 * 宽容归一化：把外部 Doc（导入/旧版本/localStorage）修成当前版本。
 * 未知字段一律保留（契约 §4 "未知字段保留并透传"），方便未来扩展。
 */
export function normalizeDoc(raw) {
  if (!raw || typeof raw !== "object") return createEmptyDoc();
  const doc = { ...raw, version: DOC_VERSION };

  const camera = raw.camera && typeof raw.camera === "object" ? raw.camera : {};
  doc.camera = {
    x: isFiniteNumber(camera.x) ? camera.x : -200,
    y: isFiniteNumber(camera.y) ? camera.y : -120,
    zoom: clamp(
      isFiniteNumber(camera.zoom) ? camera.zoom : 1,
      LIMITS.minZoom,
      LIMITS.maxZoom,
    ),
  };

  const nodes = [];
  const seen = new Set();
  if (Array.isArray(raw.nodes)) {
    for (const item of raw.nodes.slice(0, LIMITS.maxNodes)) {
      const node = normalizeNode(item);
      if (!node) continue;
      if (seen.has(node.id)) node.id = newNodeId();
      seen.add(node.id);
      nodes.push(node);
    }
  }
  doc.nodes = nodes;

  const validIds = new Set(nodes.map((node) => node.id));
  doc.edges = Array.isArray(raw.edges)
    ? raw.edges
        .filter(
          (edge) =>
            edge &&
            typeof edge.from === "string" &&
            typeof edge.to === "string" &&
            validIds.has(edge.from) &&
            validIds.has(edge.to),
        )
        .map((edge) => ({
          id:
            typeof edge.id === "string" && edge.id
              ? edge.id
              : newEdgeId(),
          from: edge.from,
          to: edge.to,
        }))
    : [];

  const meta = raw.meta && typeof raw.meta === "object" ? raw.meta : {};
  doc.meta = {
    ...meta,
    theme: "dark",
    background: "dots",
    gridSize: clamp(
      isFiniteNumber(meta.gridSize) ? meta.gridSize : 28,
      8,
      160,
    ),
  };

  return doc;
}

/**
 * 前端自查（bridge 保存前也会校验）。返回 errors 而不是抛异常，
 * 这样保存流程可以 toast 具体原因而不是静默失败。
 */
export function validateDoc(doc) {
  const errors = [];
  if (!doc || typeof doc !== "object") {
    return { ok: false, errors: ["Doc 不是对象"] };
  }
  if (doc.version !== DOC_VERSION) errors.push(`version 必须为 ${DOC_VERSION}`);
  if (!Array.isArray(doc.nodes)) errors.push("nodes 必须是数组");
  if (!Array.isArray(doc.edges)) errors.push("edges 必须是数组");

  const nodes = Array.isArray(doc.nodes) ? doc.nodes : [];
  if (nodes.length > LIMITS.maxNodes) {
    errors.push(`节点数 ${nodes.length} 超过上限 ${LIMITS.maxNodes}`);
  }

  const ids = new Set();
  for (const node of nodes) {
    if (!node || typeof node !== "object") {
      errors.push("存在非对象节点");
      continue;
    }
    if (!/^n_[a-z0-9]{10}$/.test(node.id || "")) errors.push(`非法节点 id：${node.id}`);
    if (ids.has(node.id)) errors.push(`节点 id 重复：${node.id}`);
    ids.add(node.id);
    if (!NODE_TYPES.includes(node.type)) errors.push(`非法节点类型：${node.type}`);
    for (const key of ["x", "y", "w", "h"]) {
      if (!isFiniteNumber(node[key])) errors.push(`节点 ${node.id} 的 ${key} 非有限数`);
    }
    if (isFiniteNumber(node.w) && node.w <= 0) errors.push(`节点 ${node.id} 宽必须 > 0`);
    if (isFiniteNumber(node.h) && node.h <= 0) errors.push(`节点 ${node.id} 高必须 > 0`);
  }

  for (const edge of Array.isArray(doc.edges) ? doc.edges : []) {
    if (!ids.has(edge.from) || !ids.has(edge.to)) {
      errors.push(`边 ${edge.id || "?"} 指向不存在的节点`);
    }
  }

  const camera = doc.camera || {};
  if (!isFiniteNumber(camera.x) || !isFiniteNumber(camera.y)) errors.push("camera.x/y 必须是有限数");
  if (!isFiniteNumber(camera.zoom) || camera.zoom < LIMITS.minZoom || camera.zoom > LIMITS.maxZoom) {
    errors.push(`camera.zoom 必须在 ${LIMITS.minZoom}–${LIMITS.maxZoom}`);
  }

  try {
    const bytes = new TextEncoder().encode(JSON.stringify(doc)).length;
    if (bytes > LIMITS.maxDocBytes) {
      errors.push(`Doc 大小 ${(bytes / 1024 / 1024).toFixed(2)} MB 超过 4 MB`);
    }
  } catch {
    errors.push("Doc 无法序列化");
  }

  return { ok: errors.length === 0, errors: errors.slice(0, 8) };
}

export function docStats(doc) {
  const stats = {
    total: 0,
    image: 0,
    video: 0,
    prompt: 0,
    text: 0,
    done: 0,
    generating: 0,
    queued: 0,
    cancelled: 0,
    failed: 0,
    idle: 0,
  };
  for (const node of doc.nodes || []) {
    stats.total += 1;
    if (node.type in stats) stats[node.type] += 1;
    const status = node.data && node.data.status ? node.data.status : "idle";
    if (status in stats) stats[status] += 1;
  }
  return stats;
}

export class CanvasStore {
  constructor(doc = createEmptyDoc()) {
    const normalized = normalizeDoc(doc);
    this.state = {
      doc: normalized,
      selection: new Set(),
      mode: "offline",
      spaces: [],
      currentSpaceId: "",
      saveState: "saved",
      saveMessage: "",
      ready: false,
      ui: {
        grid: true,
        snap: true,
        minimap: true,
        reduceMotion: false,
      },
    };
    this._listeners = new Set();
    this._index = new Map(normalized.nodes.map((node) => [node.id, node]));
    this._sorted = null;
    this._undo = [];
    this._redo = [];
    this._trx = null;
  }

  /* ── 订阅 ───────────────────────────────────────────────────────────── */

  subscribe(fn) {
    this._listeners.add(fn);
    return () => this._listeners.delete(fn);
  }

  _emit(type, detail) {
    if (type === "change") this._sorted = null;
    for (const fn of this._listeners) {
      try {
        fn({ type, detail, store: this });
      } catch (error) {
        // 订阅者出错不能拖垮状态机；留给控制台排查。
        console.error("[canvas store] subscriber failed", error);
      }
    }
  }

  // 供 app 在纯 UI 事件（生成耗时刷新）时驱动重绘，不产生 Doc 变更
  tick() {
    this._emit("tick");
  }

  /* ── 读取 ───────────────────────────────────────────────────────────── */

  get doc() {
    return this.state.doc;
  }

  get nodes() {
    return this.state.doc.nodes;
  }

  get camera() {
    return this.state.doc.camera;
  }

  node(id) {
    return this._index.get(id) || null;
  }

  /** 按 z 升序缓存：命中测试从后往前取，避免每次排序 300 个节点 */
  get sortedNodes() {
    if (!this._sorted) {
      this._sorted = [...this.state.doc.nodes].sort((a, b) => a.z - b.z);
    }
    return this._sorted;
  }

  get selectedIds() {
    return [...this.state.selection];
  }

  selectedNodes() {
    return this.state.doc.nodes.filter((node) => this.state.selection.has(node.id));
  }

  isSelected(id) {
    return this.state.selection.has(id);
  }

  /* ── 选中集 ─────────────────────────────────────────────────────────── */

  setSelection(ids, { append = false, silent = false } = {}) {
    const valid = new Set();
    for (const id of ids || []) {
      if (this._index.has(id)) valid.add(id);
    }
    const next = append ? new Set([...this.state.selection, ...valid]) : valid;
    const changed =
      next.size !== this.state.selection.size ||
      [...next].some((id) => !this.state.selection.has(id));
    if (!changed) return;
    this.state.selection = next;
    if (!silent) this._emit("selection");
  }

  toggleSelection(id) {
    if (!this._index.has(id)) return;
    const next = new Set(this.state.selection);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.state.selection = next;
    this._emit("selection");
  }

  clearSelection() {
    if (this.state.selection.size === 0) return;
    this.state.selection = new Set();
    this._emit("selection");
  }

  _pruneSelection() {
    const next = new Set(
      [...this.state.selection].filter((id) => this._index.has(id)),
    );
    if (next.size !== this.state.selection.size) {
      this.state.selection = next;
      this._emit("selection");
    }
  }

  /* ── 历史事务 ───────────────────────────────────────────────────────── */

  get inTransaction() {
    return this._trx !== null;
  }

  beginTransaction(label = "编辑") {
    if (this._trx) {
      this._trx.depth += 1;
      return;
    }
    this._trx = { label, before: new Map(), depth: 1 };
  }

  _touch(id) {
    if (!this._trx || this._trx.before.has(id)) return;
    const node = this._index.get(id);
    this._trx.before.set(id, node ? cloneNode(node) : null);
  }

  /**
   * 结束事务并压入命令栈。没有实际变化（before/after 等价）时不产生历史，
   * 避免"点一下节点"也写一条 undo。
   */
  commitTransaction() {
    if (!this._trx) return false;
    this._trx.depth -= 1;
    if (this._trx.depth > 0) return false;

    const trx = this._trx;
    this._trx = null;
    const before = [];
    const after = [];
    for (const [id, oldNode] of trx.before) {
      const newNode = this._index.get(id) || null;
      const oldSig = nodeSignature(oldNode);
      const newSig = nodeSignature(newNode);
      if (oldSig === newSig) continue;
      before.push({ id, node: oldNode });
      after.push({ id, node: cloneNode(newNode) });
    }
    if (!before.length) {
      this._emit("history");
      return false;
    }

    this._undo.push({ label: trx.label, before, after, time: Date.now() });
    if (this._undo.length > LIMITS.history) this._undo.shift();
    this._redo.length = 0;
    this._emit("history");
    return true;
  }

  cancelTransaction() {
    if (!this._trx) return;
    this._trx = null;
    this._emit("history");
  }

  transact(label, fn) {
    this.beginTransaction(label);
    try {
      return fn();
    } finally {
      this.commitTransaction();
    }
  }

  /** 无事务时自动包一层历史，保证所有节点操作都可撤销 */
  _autoHistory(label, fn) {
    if (this._trx) return fn();
    this.beginTransaction(label);
    try {
      return fn();
    } finally {
      this.commitTransaction();
    }
  }

  get canUndo() {
    return this._undo.length > 0;
  }

  get canRedo() {
    return this._redo.length > 0;
  }

  _applyEntries(entries) {
    for (const { id, node } of entries) {
      const old = this._index.get(id);
      if (node == null) {
        if (old) {
          this._index.delete(id);
          const index = this.state.doc.nodes.indexOf(old);
          if (index >= 0) this.state.doc.nodes.splice(index, 1);
        }
        continue;
      }
      const restored = cloneNode(node);
      if (old) {
        const index = this.state.doc.nodes.indexOf(old);
        if (index >= 0) this.state.doc.nodes[index] = restored;
        else this.state.doc.nodes.push(restored);
      } else {
        this.state.doc.nodes.push(restored);
      }
      this._index.set(id, restored);
    }
    this._pruneSelection();
  }

  undo() {
    const command = this._undo.pop();
    if (!command) return false;
    this._applyEntries(command.before);
    this._redo.push(command);
    this._emit("history");
    this._emit("change", { reason: "undo" });
    return true;
  }

  redo() {
    const command = this._redo.pop();
    if (!command) return false;
    this._applyEntries(command.after);
    this._undo.push(command);
    this._emit("history");
    this._emit("change", { reason: "redo" });
    return true;
  }

  /* ── Doc / 空间 ─────────────────────────────────────────────────────── */

  setDoc(doc, { resetHistory = true } = {}) {
    const normalized = normalizeDoc(doc);
    this.state.doc = normalized;
    this._index = new Map(normalized.nodes.map((node) => [node.id, node]));
    this._sorted = null;
    this.state.selection = new Set();
    if (resetHistory) {
      this._undo = [];
      this._redo = [];
      this._trx = null;
    }
    this._emit("doc");
    this._emit("selection");
    this._emit("history");
  }

  setCamera(camera, { silent = false } = {}) {
    const current = this.state.doc.camera;
    const next = {
      x: isFiniteNumber(camera.x) ? camera.x : current.x,
      y: isFiniteNumber(camera.y) ? camera.y : current.y,
      zoom: clamp(
        isFiniteNumber(camera.zoom) ? camera.zoom : current.zoom,
        LIMITS.minZoom,
        LIMITS.maxZoom,
      ),
    };
    if (next.x === current.x && next.y === current.y && next.zoom === current.zoom) {
      return;
    }
    this.state.doc.camera = next;
    if (!silent) this._emit("camera");
  }

  setMeta(patch) {
    this.state.doc.meta = { ...this.state.doc.meta, ...patch };
    this._emit("change", { reason: "meta" });
  }

  setWorkspace({ spaces, currentSpaceId, mode } = {}) {
    if (Array.isArray(spaces)) this.state.spaces = spaces;
    if (typeof currentSpaceId === "string") this.state.currentSpaceId = currentSpaceId;
    if (mode) this.state.mode = mode;
    this._emit("workspace");
  }

  setMode(mode) {
    if (mode === this.state.mode) return;
    this.state.mode = mode;
    this._emit("workspace");
  }

  setSaveState(state, message = "") {
    if (this.state.saveState === state && this.state.saveMessage === message) return;
    this.state.saveState = state;
    this.state.saveMessage = message;
    this._emit("save");
  }

  setReady(ready = true) {
    this.state.ready = ready;
    this._emit("ready");
  }

  setUI(patch) {
    this.state.ui = { ...this.state.ui, ...patch };
    this._emit("ui");
  }

  /* ── 节点操作 ───────────────────────────────────────────────────────── */

  addNode(node, { select = true, label = "新增节点" } = {}) {
    if (this.state.doc.nodes.length >= LIMITS.maxNodes) {
      throw new Error(`节点数量已达上限（${LIMITS.maxNodes}）`);
    }
    const normalized = normalizeNode(node);
    if (!normalized) throw new Error("节点数据不合法");
    this._autoHistory(label, () => {
      this._touch(normalized.id);
      this.state.doc.nodes.push(normalized);
      this._index.set(normalized.id, normalized);
    });
    if (select) this.setSelection([normalized.id]);
    this._emit("change", { reason: "add", ids: [normalized.id] });
    return normalized;
  }

  removeNodes(ids, { label = "删除节点" } = {}) {
    const valid = [...ids].filter((id) => this._index.has(id));
    if (!valid.length) return [];
    this._autoHistory(label, () => {
      for (const id of valid) {
        this._touch(id);
        const node = this._index.get(id);
        const index = this.state.doc.nodes.indexOf(node);
        if (index >= 0) this.state.doc.nodes.splice(index, 1);
        this._index.delete(id);
      }
      // 同时清掉引用这些节点的边，避免 Doc 出现悬空引用
      const keep = new Set([...this._index.keys()]);
      this.state.doc.edges = this.state.doc.edges.filter(
        (edge) => keep.has(edge.from) && keep.has(edge.to),
      );
      this.state.selection = new Set(
        [...this.state.selection].filter((id) => !valid.includes(id)),
      );
    });
    this._emit("selection");
    this._emit("change", { reason: "remove", ids: valid });
    return valid;
  }

  /** 复制选中节点：偏移 24 世界像素，按当前最大 z 递增，复制体成为新选中集 */
  duplicateNodes(ids, { offset = 24, label = "复制节点" } = {}) {
    const source = [...ids]
      .map((id) => this._index.get(id))
      .filter(Boolean);
    if (!source.length) return [];
    const maxZ = Math.max(0, ...this.state.doc.nodes.map((node) => node.z));
    const copies = source.map((node, index) => ({
      ...cloneNode(node),
      id: newNodeId(),
      x: node.x + offset,
      y: node.y + offset,
      z: maxZ + 1 + index,
    }));
    this._autoHistory(label, () => {
      for (const node of copies) {
        this._touch(node.id);
        this.state.doc.nodes.push(node);
        this._index.set(node.id, node);
      }
    });
    this.setSelection(copies.map((node) => node.id));
    this._emit("change", { reason: "duplicate", ids: copies.map((node) => node.id) });
    return copies.map((node) => node.id);
  }

  updateNode(id, patch, { label = "修改节点", history = true } = {}) {
    const node = this._index.get(id);
    if (!node) return false;
    const apply = () => {
      this._touch(id);
      Object.assign(node, patch);
    };
    if (history) this._autoHistory(label, apply);
    else apply();
    this._emit("change", { reason: "update", ids: [id] });
    return true;
  }

  updateNodeData(id, patch, { label = "修改节点属性", history = true } = {}) {
    const node = this._index.get(id);
    if (!node) return false;
    const apply = () => {
      this._touch(id);
      node.data = { ...node.data, ...patch };
    };
    if (history) this._autoHistory(label, apply);
    else apply();
    this._emit("change", { reason: "update", ids: [id] });
    return true;
  }

  updateNodeGeometry(id, patch, { history = true, label = "调整节点" } = {}) {
    const node = this._index.get(id);
    if (!node) return false;
    const geometry = {};
    for (const key of ["x", "y", "w", "h"]) {
      if (isFiniteNumber(patch[key])) geometry[key] = patch[key];
    }
    if (geometry.w != null) geometry.w = Math.max(LIMITS.minSize, geometry.w);
    if (geometry.h != null) geometry.h = Math.max(LIMITS.minSize, geometry.h);
    if (!Object.keys(geometry).length) return false;
    const apply = () => {
      this._touch(id);
      Object.assign(node, geometry);
    };
    if (history) this._autoHistory(label, apply);
    else apply();
    this._emit("change", { reason: "geometry", ids: [id] });
    return true;
  }

  /** 拖动专用：只改位置、不自动提交历史（调用方必须已 beginTransaction） */
  setNodePosition(id, x, y) {
    const node = this._index.get(id);
    if (!node) return false;
    this._touch(id);
    node.x = x;
    node.y = y;
    this._emit("change", { reason: "geometry", ids: [id] });
    return true;
  }

  /**
   * 选中置顶。只改 z、不写历史（避免单击节点就产生 undo）；
   * 如果在外层拖动事务里调用，z 会作为那次拖动快照的一部分被记录。
   */
  bringToFront(ids) {
    const valid = [...ids].filter((id) => this._index.has(id));
    if (!valid.length) return;
    let maxZ = 0;
    for (const node of this.state.doc.nodes) {
      if (!valid.includes(node.id)) maxZ = Math.max(maxZ, node.z);
    }
    valid.forEach((id, index) => {
      const node = this._index.get(id);
      this._touch(id);
      node.z = maxZ + 1 + index;
    });
    this._emit("change", { reason: "z", ids: valid });
  }

  maxZ() {
    return Math.max(0, ...this.state.doc.nodes.map((node) => node.z));
  }
}
