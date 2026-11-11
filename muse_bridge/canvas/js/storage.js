/**
 * storage.js — 离线 localStorage 适配 + 演示空间生成器
 *
 * 为什么需要离线模式：
 *   bridge 的 /canvas/api 还在并行开发。前端先跑通"没有后端也能完整演示"，
 *   这样 Lead 打开一个纯静态服务就能看视觉、交互与生成状态流转；等后端挂载好，
 *   api.js 的健康探测会自动切到 bridge，前端无需改代码。
 *
 * 为什么演示图是程序化 SVG dataURL：
 *   零外部依赖约束下不能下载占位图，也不能引图床。这里用确定性 PRNG 生成
 *   渐变/光晕/山脊/颗粒的抽象画面，同一个 seed 永远得到同一张图，离线生成
 *   也复用它，既省请求又不会污染 localStorage（每张 < 1.2 KB）。
 *
 * localStorage 结构（key 固定为 muse-canvas:spaces:v1）：
 *   { version, currentId, spaces: [{ id, name, created_at, updated_at,
 *     node_count, thumbnail, source, doc }] }
 *   和 bridge 的 SpaceMeta 字段保持一致，切换数据源时上层不用分叉。
 */

import {
  createEmptyDoc,
  newNodeId,
  newSpaceId,
  normalizeDoc,
  LIMITS,
} from "./store.js";

export const STORAGE_KEY = "muse-canvas:spaces:v1";

const DEMO_NAME = "灵感实验室";

/* ── 确定性随机 & 程序化画面 ───────────────────────────────────────────── */

/** mulberry32：小而稳的 32 位 PRNG，保证重启后演示空间的画面不变 */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function random() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pickHue(seed, hue) {
  if (typeof hue === "number" && Number.isFinite(hue)) {
    return ((Math.round(hue) % 360) + 360) % 360;
  }
  const rnd = mulberry32(seed * 2654435761);
  return Math.floor(rnd() * 360);
}

function fitArtSize(w, h) {
  const max = 720;
  if (w >= h) {
    return { width: max, height: Math.max(240, Math.round((max * h) / w)) };
  }
  return { width: Math.max(240, Math.round((max * w) / h)), height: max };
}

/**
 * 程序化画面：渐变天空 + 柔光 + 山脊 + 弧线 + 颗粒。
 * detailed=true 用于真实生成结果（少量节点），demo 用 false 控制体积。
 */
export function makeArtwork({
  seed = 1,
  width = 640,
  height = 640,
  hue: hueOverride = null,
  detailed = false,
} = {}) {
  const rnd = mulberry32((seed * 9301 + 49297) >>> 0);
  const hue = pickHue(seed, hueOverride);
  const hue2 = (hue + 32 + Math.floor(rnd() * 70)) % 360;
  const hue3 = (hue + 185 + Math.floor(rnd() * 90)) % 360;

  const w = Math.max(2, Math.round(width));
  const h = Math.max(2, Math.round(height));
  const sunX = Math.round(w * (0.18 + rnd() * 0.62));
  const sunY = Math.round(h * (0.14 + rnd() * 0.48));
  const sunR = Math.max(6, Math.round(Math.min(w, h) * (0.05 + rnd() * 0.11)));
  const blur = Math.max(8, Math.round(Math.min(w, h) * 0.09));

  const ridgeA = Math.round(h * (0.58 + rnd() * 0.14));
  const ridgeB = Math.round(h * (0.68 + rnd() * 0.16));
  const tilt = rnd() > 0.5 ? 1 : -1;

  const grain = detailed
    ? `<filter id="grain" x="0" y="0" width="100%" height="100%">
         <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="${Math.floor(rnd() * 99)}" stitchTiles="stitch" result="n"/>
         <feColorMatrix in="n" type="saturate" values="0"/>
         <feComponentTransfer><feFuncA type="linear" slope="0.16"/></feComponentTransfer>
       </filter>`
    : "";
  const grainRect = detailed
    ? `<rect width="${w}" height="${h}" fill="#808080" filter="url(#grain)" opacity="0.5"/>`
    : "";

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid slice">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="${0.35 + rnd() * 0.65}" y2="1">
    <stop offset="0" stop-color="hsl(${hue}, 44%, 9%)"/>
    <stop offset="0.55" stop-color="hsl(${hue2}, 46%, 17%)"/>
    <stop offset="1" stop-color="hsl(${hue3}, 52%, 28%)"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.5" r="0.5">
    <stop offset="0" stop-color="hsl(${hue2}, 88%, 64%)" stop-opacity="0.55"/>
    <stop offset="1" stop-color="hsl(${hue2}, 88%, 64%)" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="vig" cx="0.5" cy="0.42" r="0.76">
    <stop offset="0.52" stop-color="#000" stop-opacity="0"/>
    <stop offset="1" stop-color="#000" stop-opacity="0.56"/>
  </radialGradient>
  <filter id="soft" x="-60%" y="-60%" width="220%" height="220%">
    <feGaussianBlur stdDeviation="${blur}"/>
  </filter>
  ${grain}
</defs>
<rect width="${w}" height="${h}" fill="url(#bg)"/>
<ellipse cx="${Math.round(w * rnd())}" cy="${Math.round(h * (0.2 + rnd() * 0.5))}" rx="${Math.round(w * 0.42)}" ry="${Math.round(h * 0.3)}" fill="hsl(${hue3}, 78%, 58%)" opacity="0.38" filter="url(#soft)"/>
<ellipse cx="${sunX}" cy="${sunY}" rx="${sunR * 2.4}" ry="${sunR * 2.4}" fill="url(#glow)" filter="url(#soft)"/>
<circle cx="${sunX}" cy="${sunY}" r="${sunR}" fill="hsl(${hue2}, 92%, 74%)" opacity="0.72"/>
<path d="M0 ${ridgeA} C ${Math.round(w * 0.2)} ${ridgeA - Math.round(h * 0.1)} ${Math.round(w * 0.42)} ${ridgeA + Math.round(h * 0.08)} ${Math.round(w * 0.62)} ${ridgeA - Math.round(h * 0.04)} S ${Math.round(w * 0.88)} ${ridgeA + Math.round(h * 0.12)} ${w} ${ridgeA - Math.round(h * 0.02)} L ${w} ${h} L0 ${h} Z" fill="hsl(${hue3}, 42%, 9%)" opacity="0.78"/>
<path d="M0 ${ridgeB} C ${Math.round(w * 0.26)} ${ridgeB + Math.round(h * 0.1)} ${Math.round(w * 0.58)} ${ridgeB - Math.round(h * 0.12)} ${w} ${ridgeB + Math.round(h * 0.06)} L ${w} ${h} L0 ${h} Z" fill="hsl(${hue}, 38%, 6%)" opacity="0.82"/>
<g fill="none" stroke="hsl(${hue2}, 80%, 82%)" stroke-opacity="0.16" stroke-width="1.2">
  <path d="M${-Math.round(w * 0.1)} ${Math.round(h * 0.7)} Q ${Math.round(w * 0.5)} ${Math.round(h * 0.3 + rnd() * h * 0.1)} ${Math.round(w * 1.1)} ${Math.round(h * (0.66 + rnd() * 0.16))}"/>
  <path d="M${-Math.round(w * 0.2)} ${Math.round(h * (0.82 + rnd() * 0.1))} Q ${Math.round(w * 0.45)} ${Math.round(h * 0.5)} ${Math.round(w * 1.15)} ${Math.round(h * (0.78 + rnd() * 0.14))}"/>
  <path d="M${sunX - tilt * Math.round(w * 0.22)} ${sunY + Math.round(h * 0.03)} Q ${sunX} ${sunY - Math.round(h * 0.12)} ${sunX + tilt * Math.round(w * 0.22)} ${sunY + Math.round(h * 0.02)}"/>
</g>
${grainRect}
<rect width="${w}" height="${h}" fill="url(#vig)"/>
<rect x="0.5" y="0.5" width="${w - 1}" height="${h - 1}" fill="none" stroke="hsl(${hue2}, 70%, 85%)" stroke-opacity="0.08" rx="2"/>
</svg>`;

  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/* ── localStorage 读写 ─────────────────────────────────────────────────── */

function getStorage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function metaOf(space) {
  return {
    id: space.id,
    name: space.name,
    created_at: space.created_at,
    updated_at: space.updated_at,
    node_count: space.doc ? space.doc.nodes.length : space.node_count || 0,
    thumbnail: space.thumbnail ?? null,
    source: space.source || "local",
  };
}

/** 读取失败不抛异常：返回带 error 的空工作区，由 app toast 提示 */
export function loadWorkspace() {
  const storage = getStorage();
  if (!storage) {
    return { version: 1, spaces: [], currentId: null, error: "本机 localStorage 不可用" };
  }
  let raw;
  try {
    raw = storage.getItem(STORAGE_KEY);
  } catch (error) {
    return { version: 1, spaces: [], currentId: null, error: `读取本地存储失败：${error.message}` };
  }
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw);
    if (!parsed || !Array.isArray(parsed.spaces)) {
      return { version: 1, spaces: [], currentId: null, error: "本地画布数据格式不正确" };
    }
    const spaces = [];
    for (const item of parsed.spaces) {
      if (!item || typeof item.id !== "string") continue;
      const doc = normalizeDoc(item.doc);
      spaces.push({
        id: item.id,
        name: typeof item.name === "string" && item.name.trim() ? item.name : "未命名画布",
        created_at: Number.isFinite(item.created_at) ? item.created_at : nowSeconds(),
        updated_at: Number.isFinite(item.updated_at) ? item.updated_at : nowSeconds(),
        thumbnail: item.thumbnail ?? null,
        source: typeof item.source === "string" ? item.source : "local",
        doc,
      });
    }
    const currentId = spaces.some((space) => space.id === parsed.currentId)
      ? parsed.currentId
      : spaces[0]?.id || null;
    return { version: 1, spaces, currentId };
  } catch (error) {
    return { version: 1, spaces: [], currentId: null, error: `本地画布数据损坏：${error.message}` };
  }
}

/** 配额/隐私模式失败会抛异常，调用方必须 catch 并 toast */
export function saveWorkspace(workspace) {
  const storage = getStorage();
  if (!storage) throw new Error("本机 localStorage 不可用，无法保存");
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(workspace));
  } catch (error) {
    const quota = error && (error.name === "QuotaExceededError" || error.code === 22);
    throw new Error(
      quota
        ? "本地存储已满，请删除部分空间或图片后再保存"
        : `写入本地存储失败：${error.message}`,
    );
  }
}

export function clearWorkspace() {
  const storage = getStorage();
  try {
    storage?.removeItem(STORAGE_KEY);
  } catch {
    /* 忽略：清空失败不影响页面继续运行 */
  }
}

/* ── 离线后端（接口刻意对齐 bridge 的 spaces CRUD） ───────────────────── */

export class LocalBackend {
  constructor() {
    const loaded = loadWorkspace();
    this.loadError = loaded?.error || null;
    this.ws = loaded || { version: 1, spaces: [], currentId: null };
    if (!this.ws.spaces.length) this.ws.currentId = null;
    else if (!this.ws.spaces.some((space) => space.id === this.ws.currentId)) {
      this.ws.currentId = this.ws.spaces[0].id;
    }
  }

  lists() {
    return this.ws.spaces.map(metaOf);
  }

  get currentId() {
    return this.ws.currentId;
  }

  get(id) {
    return this.ws.spaces.find((space) => space.id === id) || null;
  }

  setCurrent(id) {
    if (!this.get(id)) return false;
    this.ws.currentId = id;
    return true;
  }

  create(name, doc = createEmptyDoc()) {
    const timestamp = nowSeconds();
    const record = {
      id: newSpaceId(),
      name: name && name.trim() ? name.trim() : "未命名画布",
      created_at: timestamp,
      updated_at: timestamp,
      thumbnail: null,
      source: "local",
      doc: normalizeDoc(doc),
    };
    this.ws.spaces.push(record);
    this.ws.currentId = record.id;
    this.persist();
    return record;
  }

  save(id, { name, doc } = {}) {
    const record = this.get(id);
    if (!record) throw new Error("空间不存在，无法保存");
    if (typeof name === "string" && name.trim()) record.name = name.trim();
    if (doc) {
      const normalized = normalizeDoc(doc);
      const stats = normalized.nodes.length;
      if (stats > LIMITS.maxNodes) throw new Error(`节点数超过 ${LIMITS.maxNodes}`);
      record.doc = normalized;
    }
    record.updated_at = Math.max(nowSeconds(), record.updated_at + 1);
    this.persist();
    return metaOf(record);
  }

  remove(id) {
    const index = this.ws.spaces.findIndex((space) => space.id === id);
    if (index < 0) return false;
    this.ws.spaces.splice(index, 1);
    if (this.ws.currentId === id) this.ws.currentId = this.ws.spaces[0]?.id || null;
    this.persist();
    return true;
  }

  persist() {
    saveWorkspace(this.ws);
  }

  meta(id) {
    const record = this.get(id);
    return record ? metaOf(record) : null;
  }

  setSpaces(spaces) {
    this.ws.spaces = spaces;
  }
}

/* ── 演示空间 ─────────────────────────────────────────────────────────── */

const PROMPTS = [
  "黄昏时分，悬浮在云海之上的未来城市，电影感光线",
  "一头半透明的鲸鱼穿过极光，背景是深蓝色星野",
  "雨夜霓虹街道，积水倒影，35mm 胶片颗粒",
  "极简主义建筑，巨大混凝土拱门，晨雾弥漫",
  "一只机械蝴蝶停在书页上，铜金色细节，微距",
  "深海观测站，生物荧光，冷色调，漂浮感",
  "沙漠中的镜面方尖碑，落日，长曝光",
  "白噪音般的雾中森林，微弱月光，低饱和",
  "复古科幻控制台，CRT 屏幕，暗房氛围",
  "漂浮的花园群岛，瀑布落入云层，柔光",
  "和纸质感，水墨扩散，朱红印章，留白",
  "封闭式图书馆，旋转楼梯，暖光，尘埃",
  "冰层下的蓝色洞穴，光线折射，静谧",
  "赛博神庙，霓虹符文，雨幕，广角",
  "温柔的超现实卧室，漂浮家具，晨光",
  "沙丘上的黑色巨石，极简构图，长阴影",
];

const NOTES = [
  "方向：低饱和 / 高对比。先把明度拉开，再谈颜色。",
  "试试把三张图放在一起，让它们的颜色自己说话。",
  "这一版像旧胶片，下一版换成更冷的光。",
  "记得留白——空白也是构图的一部分。",
  "素材来源：今天傍晚的散步。云的形状很好。",
  "把提示词拆成「主体 / 光线 / 材质 / 情绪」四段。",
  "这个角度再来一张竖构图，适合做封面。",
  "先收集 20 张参考，再决定最终方向。",
];

function demoArt(w, h, seed, hue) {
  const size = fitArtSize(w, h);
  return {
    url: makeArtwork({ seed, hue, width: size.width, height: size.height, detailed: false }),
    width: size.width,
    height: size.height,
  };
}

/**
 * 生成 300+ 节点的演示 Doc。
 * 近景 22 张卡片按"情绪板"手工编排在初始视口内（camera 默认 -200,-120），
 * 远景再用四个区域铺 300+ 张，既保证首屏有内容，也能验证视口裁剪性能。
 */
export function buildDemoDoc({ count = 323 } = {}) {
  const random = mulberry32(20261111);
  const nodes = [];
  const now = Date.now();
  const stats = { image: 0, video: 0, prompt: 0, text: 0 };
  let artSeed = 11;

  const add = (type, x, y, w, h, data) => {
    const node = {
      id: newNodeId(),
      type,
      x,
      y,
      w,
      h,
      z: nodes.length + 1,
      data: {
        status: "done",
        createdAt: now - (nodes.length + 1) * 45000,
        updatedAt: now - (nodes.length + 1) * 45000,
        ...data,
      },
    };
    nodes.push(node);
    stats[type] += 1;
    return node;
  };

  const addImage = (x, y, w, h, prompt, hue, extra = {}) => {
    artSeed += 1;
    const art = demoArt(w, h, artSeed, hue);
    return add("image", x, y, w, h, {
      prompt,
      url: art.url,
      width: art.width,
      height: art.height,
      mime: "image/svg+xml",
      model: "muse-image",
      params: { size: "1:1" },
      ...extra,
    });
  };

  const addVideo = (x, y, w, h, prompt, hue) => {
    artSeed += 1;
    const art = demoArt(w, h, artSeed, hue);
    return add("video", x, y, w, h, {
      prompt,
      thumbUrl: art.url,
      width: art.width,
      height: art.height,
      mime: "video/mp4",
      model: "muse-video",
      params: { size: "16:9", duration: 6 },
    });
  };

  const addPrompt = (x, y, w, h, prompt, extra = {}) =>
    add("prompt", x, y, w, h, {
      prompt,
      status: "idle",
      model: "muse-image",
      params: { size: "1:1" },
      ...extra,
    });

  const addText = (x, y, w, h, text) =>
    add("text", x, y, w, h, { text, status: "idle" });

  /* 近景：手工编排的情绪板（初始视口 200..1640 / 120..1020） */
  addImage(280, 160, 230, 190, PROMPTS[0], 212);
  addImage(280, 368, 230, 150, PROMPTS[1], 196);
  addText(280, 536, 230, 132, NOTES[1]);
  addImage(280, 686, 230, 170, PROMPTS[2], 24);
  addPrompt(280, 874, 230, 168, PROMPTS[3]);

  addPrompt(540, 160, 250, 150, PROMPTS[4]);
  addImage(540, 328, 250, 210, PROMPTS[5], 268);
  addImage(540, 556, 250, 160, PROMPTS[6], 172);
  addVideo(540, 734, 250, 150, PROMPTS[8], 300);
  addImage(540, 902, 250, 180, PROMPTS[9], 330);

  addImage(820, 160, 250, 180, PROMPTS[10], 202);
  addImage(820, 358, 250, 250, PROMPTS[11], 244);
  addImage(820, 626, 250, 170, PROMPTS[12], 46);
  addPrompt(820, 814, 250, 150, PROMPTS[13]);

  add(
    "image",
    1100,
    854,
    230,
    140,
    {
      prompt: "镜面方尖碑，超广角，等待重试…",
      status: "failed",
      error: "演示失败状态：bridge 暂时没有返回图片地址",
      model: "muse-image",
      params: { size: "3:4" },
      createdAt: now - 90000,
    },
  );
  addImage(1100, 160, 230, 280, PROMPTS[14], 214);
  addImage(1100, 458, 230, 170, PROMPTS[15], 162);
  addImage(1100, 646, 230, 190, PROMPTS[0], 282);

  addImage(1360, 300, 220, 170, PROMPTS[5], 188);
  addPrompt(1360, 500, 200, 160, PROMPTS[7]);
  addText(1360, 700, 200, 120, NOTES[3]);

  /* 远景：四个大区，填充到 count，验证无限画布与视口裁剪 */
  const zones = [
    { x: 2100, y: -320, w: 2200, h: 1950, count: 121 },
    { x: -1760, y: 1360, w: 1950, h: 1850, count: 90 },
    { x: 4520, y: 180, w: 1950, h: 2250, count: 62 },
    { x: 1680, y: -2180, w: 1650, h: 1280, count: 28 },
  ];

  for (const zone of zones) {
    let cursorX = zone.x;
    let cursorY = zone.y;
    let columnWidth = 0;
    for (let i = 0; i < zone.count && nodes.length < count; i += 1) {
      const kindRoll = random();
      const w = Math.round(150 + random() * 150);
      const h = Math.round(105 + random() * 165);
      if (columnWidth === 0) columnWidth = Math.round(200 + random() * 120);
      if (cursorY + h > zone.y + zone.h) {
        cursorX += columnWidth + 22;
        cursorY = zone.y + Math.round(random() * 40);
        columnWidth = 0;
      }
      const x = cursorX + Math.round(random() * 16);
      const y = cursorY + Math.round(random() * 14);
      const prompt = PROMPTS[Math.floor(random() * PROMPTS.length)];

      if (kindRoll < 0.62) {
        const hue = Math.floor(random() * 360);
        addImage(x, y, w, h, prompt, hue);
      } else if (kindRoll < 0.74) {
        addPrompt(x, y, w, h, prompt);
      } else if (kindRoll < 0.9) {
        addText(x, y, w, h, NOTES[Math.floor(random() * NOTES.length)]);
      } else {
        addVideo(x, y, w, Math.round(h * 0.82), prompt, Math.floor(random() * 360));
      }
      cursorY += h + 20 + Math.round(random() * 26);
    }
  }

  // 四舍五入到 count 时允许多一两张，前端按实际数量显示
  const doc = createEmptyDoc();
  doc.nodes = nodes;
  doc.meta.gridSize = 28;
  return doc;
}

export function makeDemoSpace() {
  const timestamp = nowSeconds();
  const doc = buildDemoDoc();
  return {
    id: newSpaceId(),
    name: DEMO_NAME,
    created_at: timestamp,
    updated_at: timestamp,
    thumbnail: null,
    source: "local",
    doc,
  };
}

export { DEMO_NAME, metaOf, nowSeconds };
