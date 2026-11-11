r"""上游兼容层的纯函数集合：把「看起来一样、语义其实不同」的约定钉成事实来源。

为什么要单独一层，而不是继续散落在 app.py / jobs.py 里：

  * 同一个状态值在三家上游有四种写法。new-api 的 OpenAI video 响应示例写的是
    `succeeded`（QuantumNous/new-api:relaykit/dto/openai_video.go 的注释与
    dto/video.go:27 的示例），而它自己定义的常量只有 `completed`
    （relaykit/dto/openai_video.go:8-14）。muse2api 读侧把
    `succeeded / success / done` 都归一成 `completed`
    （czg86389-hub/muse2api:app.py:1499-1500），midjourney-proxy 又是另一套
    枚举（TaskStatus.java:10-26）。这些差异属于「上游事实」，不属于任何一条
    业务状态机；写进 jobs.py 会让「我们自己的状态」被上游的嘴瓢绑架。
  * 上游进度不是百分比，是里程碑。muse2api 生图是 0/5/…/100
    （app.py:1281/1288/1296），生视频是 10/15/…/100
    （app.py:1462/1468/1475），生成中只报 20/25/40 三个观察值
    （engine.py:1255-1258）。把这串数字当连续百分比插值，就会在 41 处
    报一个上游从没承诺过的进度。
  * 续期有且只有三种策略（defer / probe_unknown / renew_now），窗口 12 小时
    （muse2api:app/jobs_keepalive.py:186-235）。每 10 分钟把全池号续一遍会让
    代理账单和 403 风控一起涨，而「到期前 12 小时才续」才是上游生产写法。
  * 媒体交付必须带 key。上游 `/v1/media/{name}` 无鉴权是**跨仓复现的公开缺陷**
    （czg86389-hub/muse2api:app.py:1822-1829、bynne2602/muse2api:app.py:1538-1545、
    bitgacon/muse2api:app.py:1538-1545 三份实现一字不差），而 lichao199208 分支
    才补了 media_auth_required（app/routers/v1.py:596-644）。本仓 media.py 已修；
    这里把「为什么不能直连」做成一个会抛错的纯函数，防止后来的人把它改回去。

本模块**只做判断，不做 I/O**：不联网、不读盘、不看时钟以外的环境。
所有时间都可由调用方注入 `now`，测试因此不需要冻结真实时钟。

验证等级（本模块统一用这个刻度，避免「看起来像实测」）：

  * `V1`：上游源码逐行直读，行号可复核，**未执行**；
  * `V2`：本仓已有同型实现且被离线测试覆盖，结论与本模块一致；
  * `V3`：据 V1 推导出的边界（例如「窗口刚好等于 12h 算不算到期」），
    上游没有明写，按最保守方向定，并在此处显式标注。

对应关系：`app.py:678-683` 的 `_normalize_state`、`jobs.py:139-142`
`public_status` 与本模块 `normalize_state()` 是同一条规则的三种写法；
接线时以本模块为准（本次不改已存在文件，见 docs/HARVEST.md 第 3 节）。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

# ---------------------------------------------------------------- 状态归一

# 对外可见的状态值：与 jobs.JobStatus 的六个值保持一致（jobs.py:72-80）。
QUEUED = "queued"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

PUBLIC_STATES: frozenset[str] = frozenset(
    {QUEUED, PROCESSING, COMPLETED, FAILED, CANCELLED}
)

TERMINAL_STATES: frozenset[str] = frozenset({COMPLETED, FAILED, CANCELLED})
LIVE_STATES: frozenset[str] = frozenset({QUEUED, PROCESSING})

# 别名表。只收「上游源码里真实出现过」的写法，不收凭感觉的猜测：
#   succeeded/success/done → completed   czg app.py:1499-1500（读侧归一）
#   in_progress/running    → processing  relaykit/dto/openai_video.go:11；
#                                          lichao app/routers/generate.py:167-168
#                                          （running 与 processing 同列在飞状态）
#   pending/submitted/not_start → queued midjourney TaskStatus.java:10-18；
#                                          work/museapi-video/db.js:90（queue_items
#                                          默认 status=pending；映射到 queued 是我们的
#                                          决定，故整张别名的等级按 V3 看）
#   failure                → failed      midjourney TaskStatus.java:22
#   canceled/aborted       → cancelled   work/museapi-video/db.js:33/35（美式拼写）
STATE_ALIASES: dict[str, str] = {
    "succeeded": COMPLETED,
    "success": COMPLETED,
    "done": COMPLETED,
    "in_progress": PROCESSING,
    "running": PROCESSING,
    "pending": QUEUED,
    "submitted": QUEUED,
    "not_start": QUEUED,
    "failure": FAILED,
    "canceled": CANCELLED,
    "aborted": CANCELLED,
}

_SEPARATORS = re.compile(r"[\s\-]+")


def _text(raw: object) -> str:
    """把任意输入压成用于查表的形态；不认识的类型一律当空串。"""
    if raw is None or isinstance(raw, (dict, list, tuple, set, bytes)):
        return ""
    return str(raw)


def normalize_state(raw: object) -> str:
    """读侧归一：把上游各种嘴瓢写法收成六个对外状态之一。

    与 `jobs.public_status` 的差异（有意为之，不是重复实现）：
      * `public_status` 只认六个值原样透传（jobs.py:139-142），因为库里
        存的是我们自己的 worker 写的值；
      * 本函数还要吃**上游**返回的字符串，所以必须认识 `succeeded` 这一批
        别名（czg app.py:1499-1500 的上游同款规则）。
      * 认不出的值返回 `unknown`，绝不猜成 `processing`——猜错会让运维看到
        一个永远在动的任务（jobs.py:22-25 同一条理由）。

    验证等级：V1（别名来源全部可指到上游行）；V2（六个规范值与
    `jobs.public_status` 行为一致，tests 里对拍）。
    """
    lowered = _text(raw).strip().lower()
    lowered = _SEPARATORS.sub("_", lowered)
    if lowered in PUBLIC_STATES:
        return lowered
    return STATE_ALIASES.get(lowered, UNKNOWN)


# ---------------------------------------------------------------- 进度

# 上游实际会出现的进度里程碑（不是连续百分比）。逐个给证据：
#   0    queued 初值              czg app.py:1308
#   5    image 开工               czg app.py:1288
#   10   video 受理/排队          czg app.py:1462、1488
#   15   video 开工 + processing 下限  czg app.py:1468、1505
#   20   生成中（未见 Stop）      czg engine.py:1255-1258
#   25   生成中（Stop 生命周期走完）czg engine.py:1256
#   40   生成中（agent 数增加）    czg engine.py:1257-1258
#   100  完成                     czg app.py:1296、1475
PROGRESS_MILESTONES: tuple[int, ...] = (0, 5, 10, 15, 20, 25, 40, 100)

# processing 的读侧下限：上游自己也不信 0（czg app.py:1501-1505 用 max(...,15)）。
PROCESSING_FLOOR = 15

# 生成中三个观察值，engine.py:1255-1258 的 milestone 计算原样搬来。
MILESTONE_GENERATING = 20
MILESTONE_STOP_LIFECYCLE = 25
MILESTONE_AGENT_GREW = 40


def normalize_progress(raw: object) -> int | None:
    """把上游/worker 给的进度解析成 0..100 的整数；解析不了返回 None。

    为什么不用 `int(raw)`：上游把进度在两种类型间来回转——new-api 收到的是
    `"100%"` 字符串，它的 `SetProgressStr` 先 `TrimSuffix("%")` 再 `Atoi`，
    并且**把错误丢给 `_`**（relaykit/dto/openai_video.go:33-36），于是
    `"40.5"` 这种脏值会静默变成 0，进度条直接倒退。本函数遇到脏值返回
    None，让调用方「保留旧值」而不是「用 0 覆盖」。
    越界（<0 或 >100）同样返回 None：宁可不动，也不上报一个假数字
    （quota.py:15 对百分比用的就是这条规则）。

    验证等级：V1（上游两种类型转换都可复核）；V3（小数按脏值拒绝，
    上游 Atoi 对小数静默清零，这里选择显式拒绝）。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = _text(raw).strip()
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            value = float(text)
        except ValueError:
            return None
    if not math.isfinite(value):
        return None
    if not 0 <= value <= 100:
        return None
    if not value.is_integer():
        # 上游进度只有整数里程碑；40.5 不可能是它发的。
        return None
    return int(value)


def advance_progress(current: object, incoming: object) -> int:
    """进度只增不减：回退会让客户端以为任务被重新排队了（jobs.py:460-465）。

    任一值解析不了时保留另一个；两个都解析不了返回 0。
    上限天然是 100（normalize_progress 已拒绝越界值）。

    验证等级：V2（本仓 jobs.py:460-465 已用 max(int(job.progress), int(progress))；
    V1 上游读侧 completed→100、processing→max(15) 也保证不会回退）。
    """
    old = normalize_progress(current)
    new = normalize_progress(incoming)
    if old is None:
        return new if new is not None else 0
    if new is None:
        return old
    return max(old, new)


def progress_for_state(raw_state: object, reported: object = None) -> int:
    """读侧补正：按状态把上游没给全的进度补齐，但只补到上游承诺过的值。

    规则直接对应 czg app.py:1501-1505：
      * completed → 100；
      * processing → max(上报值, 15)；
      * 其余（queued / failed / cancelled / unknown）→ 原样透传上报值。
    为什么 failed 不补成 100：czg 只在 completed 分支补 100；
    失败任务显示 100% 会让客户端以为成品已经在路上。

    验证等级：V1（上游行号直读）。
    """
    state = normalize_state(raw_state)
    value = normalize_progress(reported)
    value = 0 if value is None else value
    if state == COMPLETED:
        return 100
    if state == PROCESSING:
        return max(value, PROCESSING_FLOOR)
    return value


def milestone_for(*, stop_seen: bool = False, agent_grew: bool = False) -> int:
    """把页面观察量折成上游会报的生成中里程碑（20 / 25 / 40）。

    原式（czg engine.py:1255-1258）：

        milestone = 25 if seen_stop else 20
        if cur_cnt > base_agent_cnt: milestone = 40

    所以 agent 数增长优先级最高；两者都成立时报 40 而不是 50——上游
    没有 50 这个值，自己发明一个就是「比上游更精确」的假精确。

    验证等级：V1。
    """
    if agent_grew:
        return MILESTONE_AGENT_GREW
    if stop_seen:
        return MILESTONE_STOP_LIFECYCLE
    return MILESTONE_GENERATING


# ---------------------------------------------------------------- 会话续期

# 续期窗口：只在上游 Cookie 到期前 12 小时才动（muse2api jobs_keepalive.py:186-217
# `expiring_ids(within_sec=12*3600)`，scheduled_loop 每 10 分钟才查一次 :220-235）。
RENEW_WINDOW_SECONDS = 12 * 3600

# 三种策略（全部大写，避免和 HTTP 状态混写）。
RENEW_NOW = "renew_now"  # 到期已知且进入 12h 窗口（或已过期）
RENEW_PROBE_UNKNOWN = "probe_unknown"  # 到期未知，只能靠上次续期时间戳兜底探测
RENEW_DEFER = "defer"  # 还早，什么都不做

# 续期的 HTTP 阶段（上游 renew_session_http 的真实顺序）。
RENEW_STAGE_SESSION = "renew"  # GET /api/session?vm_state=live
RENEW_STAGE_LEASE = "lease"  # POST /api/hatch/lease-vm（未分配工作区时）
RENEW_STAGE_WAKE = "wake"  # POST /api/hatch/vm/wake（已分配且非 DISABLED 时）

# 响应分类：401 / 403 / 407 / 429 各自代表完全不同的处置。
RENEW_CONTINUE = "continue"
RENEW_RELOGIN = "relogin"
RENEW_EGRESS_BLOCKED = "egress_blocked"
RENEW_PROXY_AUTH = "proxy_auth"
RENEW_BACKOFF = "backoff"
RENEW_RETRY = "retry"
RENEW_FAIL = "fail"


def _timestamp(value: object) -> int | None:
    """宽松解析时间戳：字符串/浮点都收，解析不了返回 None（=未知，不是 0）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(_text(value).strip())
        except ValueError:
            return None
    if not math.isfinite(number):
        return None
    return int(number)


def renew_policy(
    *,
    expires_at: object = None,
    last_renew_at: object = None,
    now: int | None = None,
    window_seconds: int = RENEW_WINDOW_SECONDS,
) -> str:
    """决定这个号现在该不该续期，返回三种策略之一。

    上游的两个判据（muse2api jobs_keepalive.py:186-217）：

      1. `expires_at` 已知 → `expires_at - now <= 12h` 才续；
      2. `expires_at` 未知（没记录到真实 Set-Cookie）→ 用 `last_keepalive`
         兜底：超过 12 小时没续过就查一次，避免漏掉；
      3. 两样都没有 → 探测（不能假设「还早」，那会让新导入的号永远不续）。

    边界：`expires_at - now == window` 返回 renew_now。选择「刚好到点就续」
    而不是「再多等一秒」，理由是晚一秒可能撞上上游 48h 硬过期，代价不对称。

    验证等级：V1（判据行号可复核）；V3（相等边界与「未知 + 无记录 → 探测」
    是推导出的保守方向，上游注释只说「避免漏掉」）。
    """
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
        raise TypeError("window_seconds 必须是整数")
    if window_seconds <= 0:
        raise ValueError("window_seconds 必须为正，否则等于每轮都续")
    current = int(now) if now is not None else int(time.time())

    expiry = _timestamp(expires_at)
    if expiry is not None:
        return RENEW_NOW if expiry - current <= window_seconds else RENEW_DEFER

    last = _timestamp(last_renew_at)
    if last is None:
        return RENEW_PROBE_UNKNOWN
    return RENEW_PROBE_UNKNOWN if current - last >= window_seconds else RENEW_DEFER


def renew_stages(*, assigned: bool = True, wake_vm: bool = True) -> tuple[str, ...]:
    """一次续期要打哪几个上游接口，顺序固定（上游同款）。

    顺序证据（muse2api app/muse_engine/engine.py:534-624）：
      * 先 renew：`GET /api/session?vm_state=live`（:556）；
      * 未分配（`status != "assigned"`）→ lease-vm 再 renew 一次
        （:608-650，注释「新号先租 standard 工作区，再续签一次」）；
      * 已分配且 wake_vm → wake，并**必须**再打一次 `/api/session`
        确认 vm_state==RUNNING（:666-687；wake 的 200 只是受理）。
      * 未分配时不 wake（:661-662 直接关掉 wake_vm）——没有 vm_id 可唤醒。

    验证等级：V1。
    """
    if not assigned:
        return (RENEW_STAGE_SESSION, RENEW_STAGE_LEASE, RENEW_STAGE_SESSION)
    if wake_vm:
        return (RENEW_STAGE_SESSION, RENEW_STAGE_WAKE)
    return (RENEW_STAGE_SESSION,)


def classify_renew_response(status_code: object) -> str:
    """把续期响应码翻译成处置动作，防止 403 被当成 cookie 失效。

    硬规则（都是上游踩过的坑，不是常识）：
      * 401 → relogin：cookie 真失效（muse2api engine.py:564-565；keepalive
        任务里 401 会自动密码重登，jobs_keepalive.py:170）。
      * 403 → egress_blocked：出口 IP/地区被拒，**不得**标记账号失效
        （engine.py:566-568 的原话；本仓 session_http.py:26-27 同一条）。
      * 407 → proxy_auth：账号代理认证失败，换 cookie 没用（engine.py:562-563）。
      * 429 → backoff：限流，冷却后重试（session_http.py:102-106）。
      * 5xx → retry；其余 4xx → fail（别再重试同一请求）。

    验证等级：V1（每条都有上游行号）。
    """
    if isinstance(status_code, bool):
        raise TypeError("status_code 不能是布尔值")
    try:
        code = int(str(status_code).strip()) if not isinstance(status_code, int) else status_code
    except (TypeError, ValueError):
        raise ValueError(f"status_code 不是可解析的整数：{status_code!r}") from None
    if code == 200:
        return RENEW_CONTINUE
    if code == 401:
        return RENEW_RELOGIN
    if code == 403:
        return RENEW_EGRESS_BLOCKED
    if code == 407:
        return RENEW_PROXY_AUTH
    if code == 429:
        return RENEW_BACKOFF
    if 500 <= code <= 599:
        return RENEW_RETRY
    return RENEW_FAIL


# ---------------------------------------------------------------- 媒体交付

# 上游直链的观测寿命：约 2 天（docs/PROTOCOL.md:176；本仓 media.py:59-61
# 的 MUSE_MEDIA_TTL 默认 172800 就是按这个值取齐）。
MEDIA_UPSTREAM_TTL_SECONDS = 2 * 86400

MEDIA_SERVE_BRIDGE = "bridge"
MEDIA_SERVE_UPSTREAM = "upstream"

# token 形状：32 位小写 hex（uuid4().hex），与 media.py:66 的 TOKEN_RE 同一事实源。
MEDIA_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


class MediaPolicyError(ValueError):
    """交付策略被拒绝。继承 ValueError：这是调用方参数错误，不是运行故障。"""


@dataclass(frozen=True)
class MediaPolicy:
    """一次媒体交付的决定。字段名就是给 app 层接线用的，别再自行解释。"""

    url: str
    ttl_seconds: int
    require_key: bool
    allow_range: bool
    serve_via: str
    reason: str


def is_media_token(token: object) -> bool:
    """token 必须是 32 位小写 hex。非字符串/带分隔符/带 `..` 一律不合法。

    为什么不用「长度够长就行」：文件名会拼进落盘路径与 URL；长度检查
    挡不住 `../../etc/passwd`，而 fullmatch 挡住（media.py:19-24 的同一条边界）。

    验证等级：V2（media.py:66、19-24 已有实现并被 test_media.py 覆盖）。
    """
    if not isinstance(token, str):
        return False
    return MEDIA_TOKEN_RE.fullmatch(token) is not None


def media_url_policy(
    token: object,
    *,
    public_base: str = "",
    upstream_direct: bool = False,
    auth_required: bool = True,
    ttl_seconds: int = MEDIA_UPSTREAM_TTL_SECONDS,
    upstream_expires_at: object = None,
    now: int | None = None,
) -> MediaPolicy:
    """决定成品怎么交付，并把「直连上游」这个已知坏选项直接拒掉。

    为什么必须拒（三份上游实现一字不差，说明是系统性缺陷不是笔误）：
      * czg86389-hub/muse2api:app.py:1822-1829 `get_media(name)` **没有**
        `Depends(auth)`，只做文件名黑名单就 `FileResponse`；
      * bynne2602/muse2api:app.py:1538-1545、bitgacon/muse2api:app.py:1538-1545
        是同一段代码的拷贝；
      * 对比：lichao199208/muse2api:app/routers/v1.py:596-644 才补上了
        `media_auth_required` + Bearer 校验。
    所以 `upstream_direct=True` 与 `auth_required=False` 都抛
    `MediaPolicyError`，**不提供**「关掉鉴权也能跑」的回退分支。

    其它两条规则：
      * `public_base` 非空时给绝对 URL（czg app.py:995-1003 的教训：相对路径
        会被客户端平台解析到自己域名，导致 404），但只接受 http(s)，
        否则 url 字段就成了注入点。
      * TTL 取「本地保留时长」与「上游直链剩余寿命」的较小值：直链两天过期
        （docs/PROTOCOL.md:176），本地留更久只是磁盘负债，留更短会让慢客户端
        拿不到已付款的成品。上游已过期时保留 1 秒下限——我们手里的本地副本
        仍然可交付，但绝不再把上游链接发出去。

    验证等级：V1（缺陷与修法都有上游行号）；V2（token 形状、TTL 与
    media.py:66/59-61 一致）；V3（TTL 下限取 1 是保守推导）。
    """
    if upstream_direct:
        raise MediaPolicyError(
            "上游 /v1/media/{name} 无鉴权（czg app.py:1822-1829），"
            "不得作为交付路径；成品必须落在本地并由带 key 的 /v1/media/{token} 交付"
        )
    if not auth_required:
        raise MediaPolicyError(
            "媒体交付必须要求 key：关掉鉴权等于把成品变成公开可枚举资源"
            "（同一缺陷见 bynne2602 app.py:1538-1545）"
        )
    if not is_media_token(token):
        raise MediaPolicyError(
            "media token 非法：只接受 32 位小写 hex（media.py:66），"
            f"收到 {token!r}"
        )
    base = (public_base or "").strip().rstrip("/")
    if base and not (base.startswith("http://") or base.startswith("https://")):
        raise MediaPolicyError(f"public_base 只接受 http(s) 绝对地址，收到 {public_base!r}")

    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise MediaPolicyError("ttl_seconds 必须是整数")
    if ttl_seconds <= 0:
        raise MediaPolicyError("ttl_seconds 必须为正：0 秒保留等于立刻 404")

    expiry = _timestamp(upstream_expires_at)
    if expiry is not None:
        current = int(now) if now is not None else int(time.time())
        remaining = expiry - current
        ttl_seconds = min(ttl_seconds, max(remaining, 1))

    url = f"{base}/v1/media/{token}" if base else f"/v1/media/{token}"
    return MediaPolicy(
        url=url,
        ttl_seconds=ttl_seconds,
        require_key=True,
        allow_range=True,
        serve_via=MEDIA_SERVE_BRIDGE,
        reason="本地落盘 + 带 key 的 /v1/media/{token}（含 Range）；不暴露上游直链",
    )


__all__ = [
    "CANCELLED",
    "COMPLETED",
    "FAILED",
    "LIVE_STATES",
    "MEDIA_SERVE_BRIDGE",
    "MEDIA_SERVE_UPSTREAM",
    "MEDIA_TOKEN_RE",
    "MEDIA_UPSTREAM_TTL_SECONDS",
    "MILESTONE_AGENT_GREW",
    "MILESTONE_GENERATING",
    "MILESTONE_STOP_LIFECYCLE",
    "MediaPolicy",
    "MediaPolicyError",
    "PROCESSING",
    "PROCESSING_FLOOR",
    "PROGRESS_MILESTONES",
    "PUBLIC_STATES",
    "QUEUED",
    "RENEW_BACKOFF",
    "RENEW_CONTINUE",
    "RENEW_DEFER",
    "RENEW_EGRESS_BLOCKED",
    "RENEW_FAIL",
    "RENEW_NOW",
    "RENEW_PROBE_UNKNOWN",
    "RENEW_PROXY_AUTH",
    "RENEW_RELOGIN",
    "RENEW_RETRY",
    "RENEW_STAGE_LEASE",
    "RENEW_STAGE_SESSION",
    "RENEW_STAGE_WAKE",
    "RENEW_WINDOW_SECONDS",
    "STATE_ALIASES",
    "TERMINAL_STATES",
    "UNKNOWN",
    "advance_progress",
    "classify_renew_response",
    "is_media_token",
    "media_url_policy",
    "milestone_for",
    "normalize_progress",
    "normalize_state",
    "progress_for_state",
    "renew_policy",
    "renew_stages",
]
