/**
 * export.js — 视口 PNG 导出（离屏 canvas 重绘）
 *
 * 为什么不用 html2canvas / foreignObject：
 *   零依赖 + 不要构建。直接按同一套坐标公式在 2D canvas 上重画：
 *   背景 → 点阵 → 视口内节点（按 z 排序）。好处是导出结果不受浏览器
 *   截图限制，也不依赖 DOM 的 CSS 实现细节。
 *
 * 为什么先缓存 Image 并 decode()：
 *   导出是同步绘制流程，如果边画边等图片，会导出半张图；跨域图片还会直接
 *   抛错。先把所有图片并行加载并 decode()，失败的图只记一笔 skipped，
 *   卡片仍画出几何与提示词（降级而不是整张导出失败）。
 */

const BG = "#0b0d12";
const CARD_BG = "#12151c";
const FONT =
  '-apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';
const FONT_MONO = 'ui-monospace, Consolas, "Courier New", monospace';

function roundRect(ctx, x, y, w, h, r) {
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

function wrapLines(ctx, text, maxWidth, maxLines) {
  const chars = Array.from(String(text || ""));
  const lines = [];
  let current = "";
  for (const char of chars) {
    if (char === "\n") {
      lines.push(current);
      current = "";
      if (lines.length >= maxLines) break;
      continue;
    }
    const test = current + char;
    if (ctx.measureText(test).width > maxWidth && current) {
      lines.push(current);
      current = char;
      if (lines.length >= maxLines) break;
    } else {
      current = test;
    }
  }
  if (lines.length < maxLines && current) lines.push(current);
  if (lines.length >= maxLines) {
    const last = lines[maxLines - 1] || "";
    let clipped = last;
    while (clipped && ctx.measureText(`${clipped}…`).width > maxWidth) {
      clipped = clipped.slice(0, -1);
    }
    lines[maxLines - 1] = `${clipped}…`;
  }
  return lines;
}

function drawParagraph(ctx, text, x, y, maxWidth, maxLines, { size, color, lineHeight }) {
  ctx.font = `${size}px ${FONT}`;
  ctx.fillStyle = color;
  const lines = wrapLines(ctx, text, maxWidth, maxLines);
  lines.forEach((line, index) => {
    ctx.fillText(line, x, y + index * lineHeight);
  });
}

function drawCover(ctx, img, x, y, w, h, radius) {
  ctx.save();
  roundRect(ctx, x, y, w, h, radius);
  ctx.clip();
  const scale = Math.max(w / img.width, h / img.height);
  const sw = w / scale;
  const sh = h / scale;
  const sx = (img.width - sw) / 2;
  const sy = (img.height - sh) / 2;
  ctx.drawImage(img, sx, sy, sw, sh, x, y, w, h);
  ctx.restore();
}

function drawImageFallback(ctx, x, y, w, h, radius, label) {
  const gradient = ctx.createLinearGradient(x, y, x + w, y + h);
  gradient.addColorStop(0, "#171b25");
  gradient.addColorStop(1, "#0f1219");
  ctx.fillStyle = gradient;
  roundRect(ctx, x, y, w, h, radius);
  ctx.fill();
  ctx.fillStyle = "rgba(154, 163, 184, 0.65)";
  ctx.font = `10px ${FONT}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x + w / 2, y + h / 2);
  ctx.textAlign = "start";
  ctx.textBaseline = "alphabetic";
}

function urlKey(node) {
  return node.data?.url || node.data?.thumbUrl || "";
}

function isCrossOrigin(url) {
  if (!url || url.startsWith("data:") || url.startsWith("blob:")) return false;
  try {
    return new URL(url, window.location.href).origin !== window.location.origin;
  } catch {
    return false;
  }
}

/** 单张图片：load + decode，4s 超时；失败返回 null 交给几何降级 */
function loadImage(url, timeout = 4000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), timeout);

    const img = new Image();
    if (isCrossOrigin(url)) img.crossOrigin = "anonymous";
    img.decoding = "async";
    img.onload = async () => {
      try {
        if (typeof img.decode === "function") await img.decode();
      } catch {
        /* decode 失败但 naturalWidth 有值仍可 drawImage */
      }
      finish(img.naturalWidth > 0 ? img : null);
    };
    img.onerror = () => finish(null);
    try {
      img.src = url;
    } catch {
      finish(null);
    }
  });
}

async function loadAllImages(nodes) {
  const cache = new Map();
  const urls = new Set();
  for (const node of nodes) {
    if (node.type !== "image" && node.type !== "video") continue;
    const url = urlKey(node);
    if (url) urls.add(url);
  }
  const jobs = [...urls].map(async (url) => {
    const img = await loadImage(url);
    cache.set(url, img);
  });
  await Promise.all(jobs);
  return cache;
}

function drawGrid(ctx, view, scale) {
  const metrics = view.gridMetrics();
  const cssW = view.width;
  const cssH = view.height;
  ctx.save();
  ctx.fillStyle = "rgba(255, 255, 255, 0.13)";
  for (let x = metrics.offsetX; x <= cssW; x += metrics.spacing) {
    for (let y = metrics.offsetY; y <= cssH; y += metrics.spacing) {
      ctx.beginPath();
      ctx.arc(x * scale, y * scale, metrics.dot * scale, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.fillStyle = "rgba(122, 148, 255, 0.16)";
  for (let x = metrics.coarseX; x <= cssW; x += metrics.coarse) {
    for (let y = metrics.coarseY; y <= cssH; y += metrics.coarse) {
      ctx.beginPath();
      ctx.arc(
        x * scale,
        y * scale,
        Math.min(metrics.dot, 1.55) * scale,
        0,
        Math.PI * 2,
      );
      ctx.fill();
    }
  }
  ctx.restore();
}

function drawNode(ctx, node, cache, zoom) {
  const { x, y, w, h } = node;
  const radius = 12;
  const stroke = 1 / Math.max(0.4, zoom);
  const status = node.data?.status || "idle";

  // 卡片底
  ctx.fillStyle = node.type === "text" ? "#17150f" : CARD_BG;
  roundRect(ctx, x, y, w, h, radius);
  ctx.fill();

  if (node.type === "prompt") {
    const gradient = ctx.createLinearGradient(x, y, x + w, y + h);
    gradient.addColorStop(0, "#182036");
    gradient.addColorStop(0.55, "#121628");
    gradient.addColorStop(1, "#10131a");
    ctx.fillStyle = gradient;
    roundRect(ctx, x, y, w, h, radius);
    ctx.fill();
    ctx.fillStyle = "rgba(199, 210, 255, 0.22)";
    ctx.font = `${Math.round(w * 0.16)}px Georgia, serif`;
    ctx.fillText("“", x + w * 0.07, y + h * 0.26);
  }

  if (node.type === "text") {
    const gradient = ctx.createLinearGradient(x, y, x + w, y + h);
    gradient.addColorStop(0, "#1b1812");
    gradient.addColorStop(1, "#12141a");
    ctx.fillStyle = gradient;
    roundRect(ctx, x, y, w, h, radius);
    ctx.fill();
  }

  const pad = 16;
  if (node.type === "image" || node.type === "video") {
    const img = cache.get(urlKey(node));
    if (img) {
      drawCover(ctx, img, x, y, w, h, radius);
      // 底部轻微压暗，给提示词留出可读区
      const shade = ctx.createLinearGradient(x, y + h * 0.55, x, y + h);
      shade.addColorStop(0, "rgba(5,7,11,0)");
      shade.addColorStop(1, "rgba(5,7,11,0.58)");
      ctx.fillStyle = shade;
      roundRect(ctx, x, y, w, h, radius);
      ctx.fill();
      drawParagraph(ctx, node.data?.prompt || "", x + pad, y + h - 26, w - pad * 2, 2, {
        size: 11,
        color: "rgba(231, 234, 243, 0.86)",
        lineHeight: 14,
      });
    } else {
      drawImageFallback(
        ctx,
        x,
        y,
        w,
        h,
        radius,
        status === "failed" ? "生成失败" : "图片不可用（导出跳过）",
      );
    }

    if (node.type === "video") {
      ctx.fillStyle = "rgba(8, 10, 15, 0.6)";
      ctx.beginPath();
      ctx.arc(x + w - 26, y + h - 26, 14, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.moveTo(x + w - 29, y + h - 32);
      ctx.lineTo(x + w - 29, y + h - 20);
      ctx.lineTo(x + w - 18, y + h - 26);
      ctx.closePath();
      ctx.fill();
    }
  }

  if (node.type === "prompt") {
    drawParagraph(
      ctx,
      node.data?.prompt || "还没有提示词",
      x + pad,
      y + h * 0.34,
      w - pad * 2,
      4,
      { size: 12, color: "#e5e9f6", lineHeight: 18 },
    );
    ctx.fillStyle = "rgba(154, 163, 184, 0.75)";
    ctx.font = `10px ${FONT_MONO}`;
    ctx.fillText(node.data?.model || "muse-image", x + pad, y + h - 14);
  }

  if (node.type === "text") {
    drawParagraph(ctx, node.data?.text || node.data?.prompt || "", x + pad, y + pad + 12, w - pad * 2, 5, {
      size: 12,
      color: "#f0e8d8",
      lineHeight: 18,
    });
    ctx.fillStyle = "rgba(255, 176, 32, 0.55)";
    ctx.font = `9px ${FONT_MONO}`;
    ctx.fillText("NOTE", x + pad, y + h - 12);
  }

  // 生成中 / 排队 / 失败：导出也保留状态，截图能说明当时发生了什么
  if (status === "failed") {
    ctx.fillStyle = "rgba(46, 12, 18, 0.72)";
    roundRect(ctx, x, y, w, h, radius);
    ctx.fill();
    ctx.fillStyle = "#ff9aa8";
    ctx.beginPath();
    ctx.arc(x + w - 22, y + 22, 4, 0, Math.PI * 2);
    ctx.fill();
    drawParagraph(ctx, node.data?.error || "生成失败", x + pad, y + h / 2, w - pad * 2, 2, {
      size: 11,
      color: "#f1d9dd",
      lineHeight: 15,
    });
  } else if (status === "queued" || status === "generating") {
    const shade = ctx.createLinearGradient(x, y, x + w, y + h);
    shade.addColorStop(0, "rgba(110, 140, 255, 0.08)");
    shade.addColorStop(0.5, "rgba(139, 92, 246, 0.20)");
    shade.addColorStop(1, "rgba(110, 140, 255, 0.08)");
    ctx.fillStyle = shade;
    roundRect(ctx, x, y, w, h, radius);
    ctx.fill();
    ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
    ctx.font = `10px ${FONT}`;
    ctx.fillText(status === "queued" ? "排队中…" : "生成中…", x + pad, y + h - 14);
  }

  // 外描边最后画，避免被图片盖住
  ctx.strokeStyle =
    status === "failed" ? "rgba(255, 92, 114, 0.42)" : "rgba(255, 255, 255, 0.11)";
  ctx.lineWidth = Math.max(1, stroke);
  roundRect(ctx, x, y, w, h, radius);
  ctx.stroke();
}

function paintBackground(ctx, canvasW, canvasH) {
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, canvasW, canvasH);

  const glowA = ctx.createRadialGradient(
    canvasW * 0.18,
    -canvasH * 0.08,
    0,
    canvasW * 0.18,
    -canvasH * 0.08,
    Math.max(canvasW, canvasH) * 0.8,
  );
  glowA.addColorStop(0, "rgba(110, 140, 255, 0.10)");
  glowA.addColorStop(1, "rgba(110, 140, 255, 0)");
  ctx.fillStyle = glowA;
  ctx.fillRect(0, 0, canvasW, canvasH);

  const glowB = ctx.createRadialGradient(
    canvasW * 0.88,
    canvasH * 1.08,
    0,
    canvasW * 0.88,
    canvasH * 1.08,
    Math.max(canvasW, canvasH) * 0.75,
  );
  glowB.addColorStop(0, "rgba(139, 92, 246, 0.10)");
  glowB.addColorStop(1, "rgba(139, 92, 246, 0)");
  ctx.fillStyle = glowB;
  ctx.fillRect(0, 0, canvasW, canvasH);
}

export function exportFileName(spaceName) {
  const date = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
  const safe = String(spaceName || "canvas")
    .replace(/[\\/:*?"<>|\s]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
  return `muse-canvas-${safe || "canvas"}-${stamp}.png`;
}

/**
 * 导出当前视口。返回 { blob, exported, skipped, width, height }。
 * 所有图片失败也会成功导出几何，skipped 用来给 toast 提示。
 */
export async function exportViewportPNG({ view, doc, pixelRatio = 2 }) {
  const cssW = view.width;
  const cssH = view.height;
  const scale = Math.min(pixelRatio, 4096 / Math.max(cssW, cssH));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(cssW * scale));
  canvas.height = Math.max(1, Math.round(cssH * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("浏览器不支持 Canvas 2D，无法导出");

  paintBackground(ctx, canvas.width, canvas.height);
  drawGrid(ctx, view, scale);

  const worldRect = view.worldRect();
  const visible = doc.nodes
    .filter(
      (node) =>
        node.x < worldRect.x + worldRect.w &&
        node.x + node.w > worldRect.x &&
        node.y < worldRect.y + worldRect.h &&
        node.y + node.h > worldRect.y,
    )
    .sort((a, b) => a.z - b.z);

  let skipped = 0;
  const cache = await loadAllImages(visible);
  for (const node of visible) {
    if (
      (node.type === "image" || node.type === "video") &&
      urlKey(node) &&
      !cache.get(urlKey(node))
    ) {
      skipped += 1;
    }
  }

  const zoom = view.camera.zoom;
  ctx.save();
  ctx.setTransform(
    scale * zoom,
    0,
    0,
    scale * zoom,
    -view.camera.x * zoom * scale,
    -view.camera.y * zoom * scale,
  );
  // 只画视口内的部分，避免导出画布外的节点造成超大绘制面积
  ctx.beginPath();
  ctx.rect(worldRect.x, worldRect.y, worldRect.w, worldRect.h);
  ctx.clip();
  for (const node of visible) drawNode(ctx, node, cache, zoom);
  ctx.restore();

  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((value) => {
      if (value) resolve(value);
      else reject(new Error("浏览器未能生成 PNG 数据"));
    }, "image/png");
  });

  return {
    blob,
    exported: visible.length,
    skipped,
    width: canvas.width,
    height: canvas.height,
  };
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export function downloadUrl(url, filename) {
  if (!url) return false;
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "muse-canvas-media";
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  return true;
}
