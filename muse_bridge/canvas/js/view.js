/**
 * view.js — 相机、坐标换算、视口裁剪、命中测试、缩略图
 *
 * 坐标约定（契约 §6，前端所有模块都只能通过这里换算）：
 *   screen = (world - camera) * zoom
 *   world  = screen / zoom + camera
 * 屏幕点以画布容器左上角为原点。
 *
 * 为什么世界层用 translate + scale：
 *   translate(-camera.x*zoom, -camera.y*zoom) scale(zoom) 一次作用在父层，
 *   节点自身只写 translate3d(world.x, world.y, 0)，缩放/平移都不会触发
 *   子元素布局；拖动节点也只需改一个 transform，避免每帧 reflow。
 *
 * 为什么网格按 2x/0.5x 阶梯：
 *   缩到 0.05 时 28 世界单位只有 1.4 屏幕像素，点会糊成实心灰；放到 8 时
 *   点距 224px，又几乎看不到网格。把世界步长按 2 的幂调整，屏幕点距始终
 *   落在 16–64px，点径再随 zoom 微调，视觉密度才稳定。
 */

import { LIMITS, clamp, isFiniteNumber } from "./store.js";

function modulo(value, step) {
  if (!Number.isFinite(value) || !Number.isFinite(step) || step <= 0) return 0;
  return ((value % step) + step) % step;
}

function roundRectPath(ctx, x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}

 export class CanvasView {
  constructor({ stage, world, grid }) {
    this.stage = stage;
    this.world = world;
    this.grid = grid;
    this.camera = { x: -200, y: -120, zoom: 1 };
    this.gridSize = 28;
    this.baseGridSize = 28;
    this.width = 0;
    this.height = 0;
    this.rect = { left: 0, top: 0, width: 0, height: 0 };
    this.onResize = null;
    this._mini = null;
    this.measure();
    if (typeof ResizeObserver === "function") {
      this._observer = new ResizeObserver(() => this.measure(true));
      this._observer.observe(stage);
    }
    window.addEventListener("resize", () => this.measure(true));
  }

  measure(notify = false) {
    const rect = this.stage.getBoundingClientRect();
    this.rect = rect;
    this.width = Math.max(1, rect.width);
    this.height = Math.max(1, rect.height);
    this.apply();
    if (notify && typeof this.onResize === "function") this.onResize(this.width, this.height);
  }

  setGridSize(size) {
    if (!isFiniteNumber(size)) return;
    this.baseGridSize = size;
    this.apply();
  }

  get zoom() {
    return this.camera.zoom;
  }

  getCamera() {
    return { ...this.camera };
  }

  setCamera(camera) {
    if (!camera) return;
    this.camera = {
      x: isFiniteNumber(camera.x) ? camera.x : this.camera.x,
      y: isFiniteNumber(camera.y) ? camera.y : this.camera.y,
      zoom: clamp(
        isFiniteNumber(camera.zoom) ? camera.zoom : this.camera.zoom,
        LIMITS.minZoom,
        LIMITS.maxZoom,
      ),
    };
    this.apply();
  }

  /** 世界层变换 + 网格重绘的唯一入口 */
  apply() {
    const { x, y, zoom } = this.camera;
    this.world.style.transform = `translate3d(${-x * zoom}px, ${-y * zoom}px, 0) scale(${zoom})`;
    this.world.style.setProperty("--zoom", String(zoom));
    this.world.style.setProperty("--inv", String(1 / zoom));
    this.grid.style.setProperty("--zoom", String(zoom));
    this._applyGrid();
  }

  _applyGrid() {
    const zoom = this.camera.zoom;
    const x = this.camera.x;
    const y = this.camera.y;
    let step = this.baseGridSize;
    while (step * zoom < 16) step *= 2;
    while (step * zoom > 64) step /= 2;

    const spacing = step * zoom;
    const coarse = spacing * 4;
    const offsetX = modulo(-x * zoom, spacing);
    const offsetY = modulo(-y * zoom, spacing);
    const coarseX = modulo(-x * zoom, coarse);
    const coarseY = modulo(-y * zoom, coarse);

    // 点径随缩放微微变化：远景更实一点，近景更精致
    const dot = clamp(1.05, 1.15 * Math.pow(zoom, 0.16), 1.9);
    const dotStrong = clamp(1.0, dot * 0.78, 1.55);

    this.grid.style.setProperty("--dot", `${dot.toFixed(2)}px`);
    this.grid.style.setProperty("--dot-strong", `${dotStrong.toFixed(2)}px`);
    this.grid.style.backgroundSize = `${spacing}px ${spacing}px, ${coarse}px ${coarse}px`;
    this.grid.style.backgroundPosition = `${offsetX.toFixed(2)}px ${offsetY.toFixed(2)}px, ${coarseX.toFixed(2)}px ${coarseY.toFixed(2)}px`;
  }

  gridMetrics() {
    const zoom = this.camera.zoom;
    let step = this.baseGridSize;
    while (step * zoom < 16) step *= 2;
    while (step * zoom > 64) step /= 2;
    const spacing = step * zoom;
    return {
      spacing,
      coarse: spacing * 4,
      offsetX: modulo(-this.camera.x * zoom, spacing),
      offsetY: modulo(-this.camera.y * zoom, spacing),
      coarseX: modulo(-this.camera.x * zoom, spacing * 4),
      coarseY: modulo(-this.camera.y * zoom, spacing * 4),
      dot: clamp(1.05, 1.15 * Math.pow(zoom, 0.16), 1.9),
    };
  }

  /* ── 坐标换算 ───────────────────────────────────────────────────────── */

  localPoint(clientX, clientY) {
    const rect = this.stage.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  screenToWorld(point) {
    const zoom = this.camera.zoom;
    return {
      x: point.x / zoom + this.camera.x,
      y: point.y / zoom + this.camera.y,
    };
  }

  worldToScreen(point) {
    const zoom = this.camera.zoom;
    return {
      x: (point.x - this.camera.x) * zoom,
      y: (point.y - this.camera.y) * zoom,
    };
  }

  screenRect(node) {
    const p = this.worldToScreen({ x: node.x, y: node.y });
    const zoom = this.camera.zoom;
    return { x: p.x, y: p.y, w: node.w * zoom, h: node.h * zoom };
  }

  worldRect() {
    const zoom = this.camera.zoom;
    return {
      x: this.camera.x,
      y: this.camera.y,
      w: this.width / zoom,
      h: this.height / zoom,
    };
  }

  visibleWorldRect(marginPx = 260) {
    const zoom = this.camera.zoom;
    return {
      x: this.camera.x - marginPx / zoom,
      y: this.camera.y - marginPx / zoom,
      w: (this.width + marginPx * 2) / zoom,
      h: (this.height + marginPx * 2) / zoom,
    };
  }

  static intersects(a, b) {
    return (
      a.x < b.x + b.w &&
      a.x + a.w > b.x &&
      a.y < b.y + b.h &&
      a.y + a.h > b.y
    );
  }

  isNodeVisible(node, margin = 220) {
    const rect = this.screenRect(node);
    return (
      rect.x + rect.w >= -margin &&
      rect.x <= this.width + margin &&
      rect.y + rect.h >= -margin &&
      rect.y <= this.height + margin
    );
  }

  nodesInWorldRect(rect, nodes = []) {
    const result = [];
    for (const node of nodes) {
      if (CanvasView.intersects(node, rect)) result.push(node);
    }
    return result;
  }

  /* ── 相机操作 ───────────────────────────────────────────────────────── */

  panBy(dxScreen, dyScreen) {
    const zoom = this.camera.zoom;
    this.camera.x -= dxScreen / zoom;
    this.camera.y -= dyScreen / zoom;
    this.apply();
  }

  /** 光标锚定缩放：保证光标下的世界点不动（契约 §6 公式） */
  zoomAt(factor, screenPoint) {
    if (!Number.isFinite(factor) || factor === 1) return false;
    const oldZoom = this.camera.zoom;
    const newZoom = clamp(oldZoom * factor, LIMITS.minZoom, LIMITS.maxZoom);
    if (newZoom === oldZoom) return false;
    const world = this.screenToWorld(screenPoint);
    this.camera.zoom = newZoom;
    this.camera.x = world.x - screenPoint.x / newZoom;
    this.camera.y = world.y - screenPoint.y / newZoom;
    this.apply();
    return true;
  }

  setZoom(zoom, screenPoint = null) {
    const next = clamp(Number(zoom) || 1, LIMITS.minZoom, LIMITS.maxZoom);
    if (next === this.camera.zoom) return false;
    const anchor = screenPoint || { x: this.width / 2, y: this.height / 2 };
    const world = this.screenToWorld(anchor);
    this.camera.zoom = next;
    this.camera.x = world.x - anchor.x / next;
    this.camera.y = world.y - anchor.y / next;
    this.apply();
    return true;
  }

  fitNodes(nodes, { padding = 110, maxZoom = 2 } = {}) {
    if (!nodes || !nodes.length) {
      this.reset();
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      minX = Math.min(minX, node.x);
      minY = Math.min(minY, node.y);
      maxX = Math.max(maxX, node.x + node.w);
      maxY = Math.max(maxY, node.y + node.h);
    }
    const bw = Math.max(1, maxX - minX);
    const bh = Math.max(1, maxY - minY);
    const zoom = clamp(
      Math.min(
        (this.width - padding * 2) / bw,
        (this.height - padding * 2) / bh,
        maxZoom,
      ),
      LIMITS.minZoom,
      LIMITS.maxZoom,
    );
    this.camera = {
      x: (minX + maxX) / 2 - this.width / (2 * zoom),
      y: (minY + maxY) / 2 - this.height / (2 * zoom),
      zoom,
    };
    this.apply();
  }

  reset() {
    this.camera = { x: -200, y: -120, zoom: 1 };
    this.apply();
  }

  centerOnWorld(point) {
    this.camera.x = point.x - this.width / (2 * this.camera.zoom);
    this.camera.y = point.y - this.height / (2 * this.camera.zoom);
    this.apply();
  }

  /** 新建卡片若落在视口外，就平移让它完整露出（不改 zoom，避免镜头突然跳） */
  ensureVisible(rect, margin = 70) {
    const zoom = this.camera.zoom;
    const screen = {
      x: (rect.x - this.camera.x) * zoom,
      y: (rect.y - this.camera.y) * zoom,
      w: rect.w * zoom,
      h: rect.h * zoom,
    };
    let dx = 0;
    let dy = 0;
    if (screen.w <= this.width - margin * 2) {
      if (screen.x < margin) dx = screen.x - margin;
      else if (screen.x + screen.w > this.width - margin) {
        dx = screen.x + screen.w - (this.width - margin);
      }
    }
    if (screen.h <= this.height - margin * 2) {
      if (screen.y < margin) dy = screen.y - margin;
      else if (screen.y + screen.h > this.height - margin) {
        dy = screen.y + screen.h - (this.height - margin);
      }
    }
    if (dx || dy) this.panBy(dx, dy);
  }

  /* ── 命中测试 ───────────────────────────────────────────────────────── */

  /** 从最高 z 往下找：视觉上压在上面的节点先被命中 */
  hitTest(worldPoint, nodes) {
    const list = nodes || [];
    for (let i = list.length - 1; i >= 0; i -= 1) {
      const node = list[i];
      if (
        worldPoint.x >= node.x &&
        worldPoint.x <= node.x + node.w &&
        worldPoint.y >= node.y &&
        worldPoint.y <= node.y + node.h
      ) {
        return node;
      }
    }
    return null;
  }

  nodesIntersecting(rect, nodes) {
    const list = nodes || [];
    const result = [];
    for (const node of list) {
      if (CanvasView.intersects(node, rect)) result.push(node);
    }
    return result;
  }

  /* ── 缩略图 ─────────────────────────────────────────────────────────── */

  /**
   * 缩略图用节点包围盒 + 当前视口并集，保证无论节点铺多远，视口小方块
   * 都能和内容出现在同一张图里；点击缩略图即可跳转。
   */
  drawMinimap(canvas, nodes, selection) {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const cw = canvas.width;
    const ch = canvas.height;
    ctx.clearRect(0, 0, cw, ch);

    const view = this.worldRect();
    let minX = view.x;
    let minY = view.y;
    let maxX = view.x + view.w;
    let maxY = view.y + view.h;
    if (nodes && nodes.length) {
      for (const node of nodes) {
        minX = Math.min(minX, node.x);
        minY = Math.min(minY, node.y);
        maxX = Math.max(maxX, node.x + node.w);
        maxY = Math.max(maxY, node.y + node.h);
      }
    }
    const padding = 10;
    const bw = Math.max(1, maxX - minX);
    const bh = Math.max(1, maxY - minY);
    const scale = Math.min((cw - padding * 2) / bw, (ch - padding * 2) / bh);
    const ox = (cw - bw * scale) / 2 - minX * scale;
    const oy = (ch - bh * scale) / 2 - minY * scale;
    this._mini = { scale, ox, oy };

    const colors = {
      image: "#6e8cff",
      prompt: "#8b5cf6",
      text: "#ffb020",
      video: "#3ddc97",
    };

    for (const node of nodes || []) {
      const rx = node.x * scale + ox;
      const ry = node.y * scale + oy;
      const rw = Math.max(1.5, node.w * scale);
      const rh = Math.max(1.5, node.h * scale);
      const selected = selection && selection.has(node.id);
      ctx.globalAlpha = selected ? 0.95 : 0.5;
      ctx.fillStyle = colors[node.type] || "#9aa3b8";
      roundRectPath(ctx, rx, ry, rw, rh, Math.min(3, rw / 2, rh / 2));
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    const vx = view.x * scale + ox;
    const vy = view.y * scale + oy;
    const vw = Math.max(2, view.w * scale);
    const vh = Math.max(2, view.h * scale);
    ctx.fillStyle = "rgba(110, 140, 255, 0.16)";
    ctx.strokeStyle = "rgba(158, 175, 255, 0.95)";
    ctx.lineWidth = 1.5;
    roundRectPath(ctx, vx, vy, vw, vh, 2);
    ctx.fill();
    ctx.stroke();
  }

  minimapToWorld(canvas, clientX, clientY) {
    if (!this._mini) return { x: this.camera.x, y: this.camera.y };
    const rect = canvas.getBoundingClientRect();
    const px = ((clientX - rect.left) * canvas.width) / Math.max(1, rect.width);
    const py = ((clientY - rect.top) * canvas.height) / Math.max(1, rect.height);
    return {
      x: (px - this._mini.ox) / this._mini.scale,
      y: (py - this._mini.oy) / this._mini.scale,
    };
  }
}
