/**
 * api.js — bridge 客户端（/canvas/api 空间 CRUD + /v1/images/tasks 任务轮询）
 *
 * 为什么先探测再决定数据源：
 *   后端 P1 与前端 P2 并行开发。页面可能被 python -m http.server 托管（没有
 *   /canvas/api），也可能由 bridge 托管。启动时用 1.2s 超时 GET 一次 spaces：
 *   成功则整个会话走 bridge；失败则落到 storage.js 的 localStorage，且所有失败
 *   原因都 toast 出来，不静默。
 *
 * 为什么所有请求都带超时：
 *   无限画布是交互产品，网络挂起不能让保存按钮永远转圈。AbortController 保证
 *   最坏情况也能回到"保存失败"而不是僵死。
 *
 * 认证：bridge 的 require_key 读 Authorization: Bearer <key>。本地免 key 时
 *   后端直接放行；需要 key 的部署可以从 ?key= / ?api_key= 或 localStorage
 *   的 muse-bridge:key 取到，前端不把 key 写进 Doc/日志。
 */

const CANVAS_BASE = "/canvas/api";
const TASK_CREATE = "/v1/images/tasks";

export class ApiError extends Error {
  constructor(message, { status = 0, code = "", body = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

function readApiKey() {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("key") || params.get("api_key");
    if (fromUrl) return fromUrl;
  } catch {
    /* location 解析失败时回退 localStorage */
  }
  try {
    return globalThis.localStorage?.getItem("muse-bridge:key") || "";
  } catch {
    return "";
  }
}

function describeNetworkError(error) {
  if (error?.name === "AbortError") return "连接超时（bridge 未响应）";
  if (error instanceof ApiError) return error.message;
  if (error instanceof TypeError) return "无法连接 bridge（网络或跨域被拒绝）";
  return error?.message || "未知网络错误";
}

function parseBody(text, contentType) {
  if (!text) return null;
  if (contentType && contentType.includes("application/json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function errorMessageOf(status, body) {
  if (body && typeof body === "object") {
    const raw = body.error ?? body.detail ?? body.note;
    if (typeof raw === "string" && raw) {
      const note = typeof body.note === "string" && body.note !== raw ? `（${body.note}）` : "";
      return `${raw}${note}`;
    }
    if (raw && typeof raw === "object") {
      return raw.message || JSON.stringify(raw);
    }
    if (typeof body.note === "string" && body.note) return body.note;
  }
  if (typeof body === "string" && body.trim()) {
    const text = body.trim();
    // python -m http.server 的 404 是 HTML，直接塞进 toast 会把整页 DOCTYPE
    // 显示给用户。这里只保留状态码，说明该地址没有 /canvas/api。
    if (text.startsWith("<")) return `HTTP ${status}（该地址没有 /canvas/api）`;
    return text.slice(0, 240);
  }
  return `HTTP ${status}`;
}

/** bridge 的任务状态归一：上游实现状态名不完全一致，前端认六态（含 cancelled）。
 *
 * 为什么 `cancelled` 不再折叠成 `failed`：bridge 的公开状态机是
 * `queued|processing|completed|failed|cancelled|unknown`（jobs.py），
 * cancelled 是"客户端断开/主动取消"的终态，不是生成出错。折叠后用户看到
 * "生成失败"却查不到失败原因，也无法区分"重试可能成功"与"取消后重试"。
 */
export function normalizeTaskStatus(raw) {
  const value = String(raw || "").toLowerCase();
  if (["queued", "pending", "created", "waiting", "submitted", "enqueued"].includes(value)) {
    return "queued";
  }
  if (["running", "generating", "processing", "in_progress", "started"].includes(value)) {
    return "generating";
  }
  if (["succeeded", "success", "completed", "complete", "done", "finished"].includes(value)) {
    return "succeeded";
  }
  if (["cancelled", "canceled", "aborted"].includes(value)) {
    return "cancelled";
  }
  if (["failed", "error", "timeout", "expired"].includes(value)) {
    return "failed";
  }
  return value ? "unknown" : "queued";
}

function unwrapTask(payload) {
  if (!payload || typeof payload !== "object") return null;
  const data = Array.isArray(payload.data) ? payload.data[0] : null;
  const result = payload.result && typeof payload.result === "object" ? payload.result : null;
  const url = payload.url || result?.url || data?.url || payload.media_url || "";
  const width =
    payload.width || result?.width || data?.width || payload.media?.width || 0;
  const height =
    payload.height || result?.height || data?.height || payload.media?.height || 0;
  const errorValue = payload.error;
  const error =
    typeof errorValue === "string"
      ? errorValue
      : errorValue?.message || payload.note || payload.detail || "";
  const statusRaw = payload.status || payload.state || (payload.ok === false ? "failed" : "");
  return {
    id: payload.task_id || payload.id || payload.request_id || "",
    status: normalizeTaskStatus(statusRaw),
    rawStatus: statusRaw,
    url,
    width: Number(width) || 0,
    height: Number(height) || 0,
    error: typeof error === "string" ? error : "",
    progress: typeof payload.progress === "number" ? payload.progress : null,
    result,
    raw: payload,
  };
}

export class BridgeClient {
  constructor({ base = CANVAS_BASE, key = readApiKey() } = {}) {
    this.base = base;
    this.key = key;
  }

  _headers(extra = {}) {
    const headers = { Accept: "application/json", ...extra };
    if (this.key) headers.Authorization = `Bearer ${this.key}`;
    return headers;
  }

  async _request(path, { method = "GET", body = null, timeout = 8000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        method,
        headers: this._headers(body ? { "Content-Type": "application/json" } : {}),
        body: body ? JSON.stringify(body) : null,
        credentials: "same-origin",
        signal: controller.signal,
      });
      const text = await response.text();
      const parsed = parseBody(text, response.headers.get("content-type"));
      if (!response.ok) {
        throw new ApiError(errorMessageOf(response.status, parsed), {
          status: response.status,
          body: parsed,
        });
      }
      return parsed;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * 健康探测：GET /canvas/api/spaces。任何非 JSON/非 2xx 都视为不可用，
   * 交给 storage.js 离线模式，而不是让页面白屏。
   */
  async probe({ timeout = 1200 } = {}) {
    const started = performance.now();
    try {
      const data = await this._request(`${this.base}/spaces`, { timeout });
      const spaces = Array.isArray(data?.spaces) ? data.spaces : [];
      return {
        ok: true,
        spaces,
        latency: Math.round(performance.now() - started),
        reason: "",
      };
    } catch (error) {
      return {
        ok: false,
        spaces: [],
        latency: Math.round(performance.now() - started),
        reason: describeNetworkError(error),
      };
    }
  }

  async listSpaces() {
    const data = await this._request(`${this.base}/spaces`);
    return Array.isArray(data?.spaces) ? data.spaces : [];
  }

  /* ── muse 只读适配（P3）───────────────────────────────────────────── */

  /**
   * 列出当前 muse 账号的 Spaces（只读）。
   *
   * 500/503 的语义由后端给：没有可用账号/协议失败时返回
   * `{"error":"muse_unavailable","note":...}`，这里原样抛 ApiError，
   * 让 UI 显示一句人话而不是 404。
   */
  async listMuseSpaces({ includeConstructing = false, timeout = 25000 } = {}) {
    const query = includeConstructing ? "?include_constructing=true" : "";
    const data = await this._request(`${this.base}/muse/spaces${query}`, { timeout });
    return Array.isArray(data?.spaces) ? data.spaces : [];
  }

  /** 把 muse Space 转成画布卡片节点（后端纯函数，不写 muse、不写本地库）。 */
  async importMuseSpace(slug, { timeout = 45000 } = {}) {
    const data = await this._request(`${this.base}/muse/import`, {
      method: "POST",
      body: { slug: String(slug || "") },
      timeout,
    });
    if (!data?.node) throw new ApiError("bridge 未返回可导入的节点", { status: 502 });
    return data;
  }

  /** Explore 灵感卡列表（只读，含 7 个分区）。 */
  async listMuseIdeaCards({ timeout = 30000 } = {}) {
    const data = await this._request(`${this.base}/muse/idea-cards`, { timeout });
    return Array.isArray(data?.sections) ? data.sections : [];
  }

  async createSpace(name) {
    const data = await this._request(`${this.base}/spaces`, {
      method: "POST",
      body: { name: String(name || "未命名画布") },
    });
    if (!data?.space?.id) throw new ApiError("bridge 未返回空间 id", { status: 502 });
    return data.space;
  }

  async getSpace(id) {
    const data = await this._request(`${this.base}/spaces/${encodeURIComponent(id)}`);
    if (!data?.space) throw new ApiError("bridge 未返回空间数据", { status: 502 });
    return { space: data.space, doc: data.doc || null };
  }

  async saveSpace(id, { name, doc } = {}) {
    if (!doc) throw new ApiError("保存时缺少 doc");
    const data = await this._request(`${this.base}/spaces/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: name ? { name, doc } : { doc },
      timeout: 12000,
    });
    return data?.space || null;
  }

  async deleteSpace(id) {
    await this._request(`${this.base}/spaces/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    return true;
  }

  /**
   * 只读分享：读口**不带 key 也能取**（令牌本身就是能力串）。
   * `?share=` 页面启动时走这里，取回 `{space, doc, share, read_only}`。
   */
  async getSharedSpace(token) {
    const data = await this._request(
      `${this.base}/shares/${encodeURIComponent(token)}`,
      { timeout: 15000 },
    );
    if (!data?.doc) throw new ApiError("分享链接没有文档", { status: 404 });
    return data;
  }

  /** 创建（或轮换）分享链接；写入口，必须带 key。 */
  async createShare(id) {
    const data = await this._request(
      `${this.base}/spaces/${encodeURIComponent(id)}/share`,
      { method: "POST", timeout: 12000 },
    );
    if (!data?.share?.url) {
      throw new ApiError("bridge 未返回分享链接", { status: 502 });
    }
    return data.share;
  }

  /** 查询当前分享链接（无副作用）；没分享过返回 null。 */
  async getShare(id) {
    const data = await this._request(
      `${this.base}/spaces/${encodeURIComponent(id)}/share`,
      { timeout: 8000 },
    );
    return data?.share || null;
  }

  /**
   * 撤销一条分享：`DELETE /canvas/api/shares/<token>`（写入口，必须带 key）。
   *
   * 语义是"token 立即失效"（下一次 GET /shares/<token> 回 404），不是"暂时关闭"。
   * 返回 `ok` 布尔值：服务端对不存在的 token 也回 200 + `ok:false`（幂等），
   * 这样 UI 可以把"已经失效"和"撤销成功"都渲染成同一句话，而不是抛错吓人。
   */
  async revokeShare(token) {
    const data = await this._request(
      `${this.base}/shares/${encodeURIComponent(token)}`,
      { method: "DELETE", timeout: 10000 },
    );
    return data?.ok === true;
  }

  /**
   * 建图任务。契约 §3.2：POST /v1/images/tasks，body 与 generations 一致。
   * 返回的 payload 可能是任务形状（id/task_id）也可能直接带 status。
   */
  async createImageTask({ prompt, model = "muse-image", size = "1:1", n = 1 } = {}) {
    if (!prompt || !String(prompt).trim()) throw new ApiError("提示词为空");
    const payload = await this._request(TASK_CREATE, {
      method: "POST",
      body: {
        prompt: String(prompt),
        model,
        size,
        n,
        response_format: "url",
        async_mode: true,
      },
      timeout: 12000,
    });
    const task = unwrapTask(payload);
    if (!task?.id) throw new ApiError("bridge 没有返回 task_id", { status: 502, body: payload });
    return task;
  }

  async getTask(taskId) {
    const payload = await this._request(
      `${TASK_CREATE}/${encodeURIComponent(taskId)}`,
      { timeout: 8000 },
    );
    const task = unwrapTask(payload);
    if (!task) throw new ApiError("任务响应为空", { status: 502 });
    return task;
  }
}

/** 同源媒体 URL；`/v1/media/<token>` 改走画布同源代理。
 *
 * 为什么改：bridge 的 `/v1/media` 鉴权只认 `Authorization: Bearer`，而 `<img>` 与
 * 离屏 `drawImage` 都发不了自定义请求头。`/canvas/api/media/<token>` 是同一份字节的
 * 无头出口，既让图片能显示，也让导出 PNG 时画布不被跨源 taint。
 */
export function resolveMediaUrl(url) {
  if (!url || typeof url !== "string") return "";
  if (url.startsWith("data:") || url.startsWith("blob:")) return url;
  try {
    const parsed = new URL(url, window.location.origin);
    const proxy = parsed.pathname.match(/^\/v1\/media\/([^\s?#]+)$/);
    if (proxy) return `${window.location.origin}/canvas/api/media/${proxy[1]}`;
    return parsed.href;
  } catch {
    return url;
  }
}

export { CANVAS_BASE, describeNetworkError };
