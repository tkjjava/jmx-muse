/**
 * nodes.js — 节点视图层（只负责 DOM 呈现，不处理手势）
 *
 * 为什么内容与布局分开缓存：
 *   拖动/缩放只改 translate + width/height，每帧重建 innerHTML 会让 <img>
 *   重新 decode、骨架动画重置。这里给每个节点算两个签名（layout/content），
 *   布局变化只写样式，内容变化才重建卡片；生成中的耗时文字再单独原地刷新。
 *
 * 为什么只渲染视口内节点：
 *   演示空间有 300+ 节点，全部挂 DOM 会让每次 render 都跑几百个命中样式计算。
 *   视口外（留 300px 余量）的节点直接移出 DOM；选中的节点始终保留，避免
 *   拖到屏幕外后手柄/删除按钮消失。图片本身仍在 Doc 里，不丢数据。
 */

const ICON = {
  x: '<path d="m6 6 12 12"/><path d="M18 6 6 18"/>',
  image:
    '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="m4 15 4.5-4.5 3.5 3.5 3-3 5 5"/><circle cx="9" cy="9" r="1.2"/>',
  play: '<path d="M8 5.5v13l10-6.5-10-6.5Z"/>',
  sparkles:
    '<path d="m12 3 1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4L12 3Z"/><path d="M18.5 16.5 19 18l1.5.5-1.5.5-.5 1.5-.5-1.5-1.5-.5 1.5-.5.5-1.5Z"/>',
  warning:
    '<path d="M12 4.5 21 20H3L12 4.5Z"/><path d="M12 10v4"/><path d="M12 17.5h.01"/>',
  refresh:
    '<path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M19 11A7.5 7.5 0 0 0 6.2 7.2L4 9.5"/><path d="M5 13a7.5 7.5 0 0 0 12.8 3.8L20 14.5"/>',
  text: '<path d="M5 6h14"/><path d="M5 11h9"/><path d="M5 16h12"/><path d="M5 20h6"/>',
  video: '<rect x="3.5" y="5.5" width="12" height="13" rx="2.5"/><path d="m16 10 4.5-2.5v9L16 14"/>',
};

function icon(name, cls = "ico") {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICON[name] || ""}</svg>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

const STATUS_LABEL = {
  idle: "待生成",
  queued: "排队中",
  generating: "生成中",
  // 后端任务有六态，`cancelled` 是**终态但非失败**（客户端断开/主动取消）。
  // 早期前端把它折叠成 failed，用户无法区分"生成出错"和"任务被取消"。
  cancelled: "已取消",
  failed: "失败",
  done: "已完成",
};

function clipPrompt(text, length = 120) {
  const clean = String(text || "").trim().replace(/\s+/g, " ");
  return clean.length > length ? `${clean.slice(0, length)}…` : clean;
}

function elapsedOf(node) {
  const started = Number(node.data?.createdAt) || 0;
  if (!started) return 0;
  return Math.max(0, (Date.now() - started) / 1000);
}

function formatElapsed(seconds) {
  if (seconds < 0.1) return "0.0s";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function statusPill(node, labelOverride) {
  const status = node.data?.status || "idle";
  const label =
    labelOverride !== undefined ? labelOverride : STATUS_LABEL[status] || "";
  if (!label) return "";
  return `<span class="status-pill" data-status="${status}"><span class="dot"></span><span class="js-gen-text">${escapeHtml(label)}</span></span>`;
}

function captionOf(node) {
  const prompt = node.data?.prompt || "";
  if (!prompt) return "";
  return `<div class="caption"><span class="caption-text">${escapeHtml(clipPrompt(prompt))}</span></div>`;
}

function mediaFallback() {
  return `<div class="media-fallback">${icon("image")}<span>图片不可用</span></div>`;
}

/**
 * 生成中的媒体：有旧图时保留旧图并在上面铺 shimmer，没有旧图就用骨架。
 * 这样"重新生成"不会把卡片瞬间变成一块空白。
 */
function buildPromptCard(node) {
  const prompt = node.data?.prompt || "还没有提示词";
  const model = node.data?.model || "muse-image";
  const status = node.data?.status || "idle";
  const pill = status !== "idle" ? statusPill(node) : "";
  return `
    <div class="prompt-card"></div>
    <div class="prompt-body">
      <div class="prompt-mark">“</div>
      <p class="prompt-text">${escapeHtml(prompt)}</p>
      <div class="prompt-foot">
        <span class="hint">${icon("sparkles")} 待生成</span>
        <span>${escapeHtml(model)}</span>
      </div>
    </div>
    ${pill}
  `;
}

function buildTextCard(node) {
  const text = node.data?.text || node.data?.prompt || "";
  return `
    <div class="text-card"></div>
    <div class="text-body">
      <p class="text-content">${escapeHtml(text)}</p>
      <div class="text-foot">note</div>
    </div>
  `;
}

function buildImageCard(node, selected) {
  const { status, url, prompt, error } = node.data || {};
  const hasImage = Boolean(url);

  // cancelled 与 failed 都终止生成，但文案与语义不同：前者是"任务被取消"，
  // 保留重试按钮（取消不是内容错误，重试通常有效）。
  if (status === "failed" || status === "cancelled") {
    const body =
      status === "cancelled"
        ? escapeHtml(error || "任务已取消，可重试")
        : escapeHtml(error || "生成失败，没有返回原因");
    return `
      <div class="media"><div class="skeleton"></div>${mediaFallback()}</div>
      <div class="fail-body">
        ${icon(status === "cancelled" ? "clock" : "warning", "fail-icon")}
        <p class="fail-msg">${body}</p>
        <button class="retry-btn" type="button" data-node-action="retry">${icon("refresh")}重试</button>
      </div>
      ${statusPill(node)}
    `;
  }

  if ((status === "queued" || status === "generating") && !hasImage) {
    return `
      <div class="media">
        <div class="skeleton"></div>
        ${mediaFallback()}
      </div>
      ${captionOf(node)}
      ${statusPill(node)}
      <div class="gen-overlay"></div>
      <div class="gen-progress"><span class="spinner"></span><span class="js-gen-text">${escapeHtml(STATUS_LABEL[status] || "生成中")}</span></div>
    `;
  }

  if (hasImage) {
    const generating = status === "queued" || status === "generating";
    return `
      <div class="media">
        <img class="media-img" alt="" decoding="async" draggable="false">
        ${mediaFallback()}
      </div>
      ${captionOf(node)}
      ${
        generating
          ? `<div class="gen-overlay"></div>
             <div class="gen-progress"><span class="spinner"></span><span class="js-gen-text">生成中</span></div>
             ${statusPill(node)}`
          : ""
      }
    `;
  }

  // 没有 URL 的 idle image（例如导入的占位节点）：展示提示词而不是黑块
  return `${buildPromptCard(node)}`;
}

function buildVideoCard(node) {
  const { status, url, thumbUrl, prompt, error } = node.data || {};
  const poster = thumbUrl || url || "";
  if (status === "failed" || status === "cancelled") {
    return `
      <div class="media"><div class="skeleton"></div>${mediaFallback()}</div>
      <div class="fail-body">
        ${icon(status === "cancelled" ? "clock" : "warning", "fail-icon")}
        <p class="fail-msg">${escapeHtml(
          status === "cancelled"
            ? error || "任务已取消，可重试"
            : error || "视频生成失败",
        )}</p>
        <button class="retry-btn" type="button" data-node-action="retry">${icon("refresh")}重试</button>
      </div>
    `;
  }
  return `
    <div class="media ${poster ? "" : "is-loading"}">
      ${poster ? `<img class="media-img" alt="" decoding="async" draggable="false">` : `<div class="skeleton"></div>`}
      ${mediaFallback()}
    </div>
    ${captionOf(node)}
    ${
      (status === "queued" || status === "generating")
        ? `<div class="gen-overlay"></div><div class="gen-progress"><span class="spinner"></span><span class="js-gen-text">${escapeHtml(STATUS_LABEL[status] || "生成中")}</span></div>`
        : `<span class="node-tag">${icon("video")}视频</span><span class="play-badge">${icon("play")}</span>`
    }
    ${statusPill(node, status === "done" ? "" : STATUS_LABEL[status] || "")}
  `;
}

export class NodeLayer {
  constructor({ world, view, store }) {
    this.world = world;
    this.view = view;
    this.store = store;
    this.cache = new Map();
    this.dragIds = new Set();
    this.animated = new Set();
    this.enterCount = 0;
  }

  clear() {
    for (const entry of this.cache.values()) entry.el.remove();
    this.cache.clear();
    this.dragIds.clear();
    this.enterCount = 0;
  }

  element(id) {
    return this.cache.get(id)?.el || null;
  }

  setDragging(ids, dragging) {
    for (const id of ids) {
      if (dragging) this.dragIds.add(id);
      else this.dragIds.delete(id);
      const entry = this.cache.get(id);
      if (entry) entry.el.classList.toggle("is-dragging", dragging);
    }
  }

  /** 拖动中只更新位置：不重排内容、不重建图片 */
  updateGeometry(ids) {
    for (const id of ids) {
      const node = this.store.node(id);
      const entry = this.cache.get(id);
      if (!node || !entry) continue;
      this._applyLayout(entry, node, this.store.isSelected(id));
    }
  }

  _createShell(node) {
    const el = document.createElement("div");
    el.className = "node";
    el.dataset.nodeId = node.id;
    el.innerHTML = `
      <div class="card"></div>
      <button class="node-del" type="button" tabindex="-1" data-node-action="delete" aria-label="删除卡片">${icon("x")}</button>
      <div class="handle" data-handle="nw" aria-hidden="true"></div>
      <div class="handle" data-handle="ne" aria-hidden="true"></div>
      <div class="handle" data-handle="sw" aria-hidden="true"></div>
      <div class="handle" data-handle="se" aria-hidden="true"></div>
    `;
    return el;
  }

  _applyLayout(entry, node, selected) {
    const el = entry.el;
    // 拖动每帧只改 transform；宽高与 z 值相等时不重复写 style，避免布局抖动。
    const width = `${node.w}px`;
    const height = `${node.h}px`;
    if (el.style.width !== width) el.style.width = width;
    if (el.style.height !== height) el.style.height = height;
    el.style.transform = `translate3d(${node.x}px, ${node.y}px, 0)`;
    const z = String(node.z);
    if (el.style.zIndex !== z) el.style.zIndex = z;
    el.style.setProperty("--nx", `${node.x}px`);
    el.style.setProperty("--ny", `${node.y}px`);
    el.classList.toggle("is-selected", selected);
    if (el.dataset.type !== node.type) el.dataset.type = node.type;
    const label = `${node.type} 卡片${
      node.data?.prompt ? `：${clipPrompt(node.data.prompt, 60)}` : ""
    }`;
    if (el.getAttribute("aria-label") !== label) el.setAttribute("aria-label", label);
  }

  _contentSignature(node) {
    const d = node.data || {};
    return [
      node.type,
      d.status,
      d.url,
      d.thumbUrl,
      d.prompt,
      d.text,
      d.error,
      d.model,
      d.width,
      d.height,
    ].join("\u0001");
  }

  _buildContent(entry, node) {
    const card = entry.el.querySelector(".card");
    if (!card) return;
    let html = "";
    if (node.type === "text") {
      html = buildTextCard(node);
      card.className = "card";
    } else if (node.type === "prompt") {
      html = buildPromptCard(node);
      card.className = "card";
    } else if (node.type === "video") {
      html = buildVideoCard(node);
      card.className = "card";
    } else {
      html = buildImageCard(node, entry.el.classList.contains("is-selected"));
      card.className = "card";
    }
    card.innerHTML = html;

    // <img> 需要在挂监听之后再赋 src，才能可靠地拿到 load/error
    const img = card.querySelector(".media-img");
    if (img) {
      const media = card.querySelector(".media");
      const skeleton = card.querySelector(".skeleton");
      const url = node.data?.url || node.data?.thumbUrl || "";
      const done = () => {
        img.classList.add("is-loaded");
        media?.classList.remove("is-loading");
        skeleton?.remove();
      };
      const failed = () => {
        media?.classList.add("has-error");
        skeleton?.remove();
      };
      img.addEventListener("load", done, { once: true });
      img.addEventListener("error", failed, { once: true });
      img.src = url;
      if (img.complete && img.naturalWidth > 0) done();
    }
  }

  _updateDynamic(entry, node) {
    const status = node.data?.status;
    if (status !== "queued" && status !== "generating") return;
    const label =
      status === "queued" ? "排队中" : `生成中 · ${formatElapsed(elapsedOf(node))}`;
    const targets = entry.el.querySelectorAll(".js-gen-text");
    for (const target of targets) target.textContent = label;
  }

  render() {
    const selected = this.store.state.selection;
    const rect = this.view.visibleWorldRect(300);
    const visible = [];
    for (const node of this.store.nodes) {
      if (
        selected.has(node.id) ||
        this.dragIds.has(node.id) ||
        CanvasViewIntersects(node, rect)
      ) {
        visible.push(node);
      }
    }

    const seen = new Set();
    for (const node of visible) {
      seen.add(node.id);
      let entry = this.cache.get(node.id);
      if (!entry) {
        const el = this._createShell(node);
        this.world.appendChild(el);
        entry = { el, layout: "", content: "" };
        this.cache.set(node.id, entry);
        this._animateIn(el, node);
      }

      const selectedNow = selected.has(node.id);
      const layout = `${node.x}|${node.y}|${node.w}|${node.h}|${node.z}|${selectedNow ? 1 : 0}`;
      if (entry.layout !== layout) {
        this._applyLayout(entry, node, selectedNow);
        entry.layout = layout;
      }

      const content = this._contentSignature(node);
      if (entry.content !== content) {
        this._buildContent(entry, node);
        entry.content = content;
      }
      this._updateDynamic(entry, node);
    }

    for (const [id, entry] of this.cache) {
      if (!seen.has(id)) {
        entry.el.remove();
        this.cache.delete(id);
      }
    }
  }

  _animateIn(el, node) {
    if (this.animated.has(node.id)) return;
    this.animated.add(node.id);
    el.classList.add("is-entering");
    const delay = Math.min(this.enterCount, 12) * 16;
    this.enterCount += 1;
    if (delay) el.style.animationDelay = `${delay}ms`;
    const cleanup = () => {
      el.classList.remove("is-entering");
      el.style.animationDelay = "";
    };
    el.addEventListener("animationend", cleanup, { once: true });
    // 减少动效 / 动画被禁用时 animationend 可能不触发，兜底清理
    setTimeout(cleanup, 520 + delay);
  }
}

/** 避免从 view.js 反向 import：这里只做世界矩形相交判断 */
function CanvasViewIntersects(node, rect) {
  return (
    node.x < rect.x + rect.w &&
    node.x + node.w > rect.x &&
    node.y < rect.y + rect.h &&
    node.y + node.h > rect.y
  );
}

export { STATUS_LABEL, clipPrompt, formatElapsed };
