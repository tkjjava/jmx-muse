r"""不开浏览器的 Hatch 协议客户端：Noise XX 之上跑 HTTP-over-WebSocket。

分工边界（改代码前先读）：

    * 会话与 VM 唤醒 **不在这里实现**。`session_http.renew_session()` /
      `session_http.wake_vm()` 已经把 401/403/429 的分类、不跟随重定向、
      "wake 只是受理"这些做对了，重复实现一遍是最容易腐化的地方。
      本文件只把它们的结果一对一翻译成协议层异常类型。
    * 加密原语来自 `noise.py`，报文结构来自 `proto.py`。这里只**使用**它们。
    * WS 后端优先 `websockets`，没装则退到 `ws_fallback.py`（同样的四件套）。

对 `protocol_engine.py` 的鸭子契约（该文件第 22-28 行写明的形状）与本模块
签名的对应关系，见下面每个方法上的"兼容形态"注释。两套调用姿势都要能跑，
否则引擎一落地就要回头改客户端 —— 而客户端签名是本轮规格钉死的。

四条硬语义，测试逐条钉住：

  1. **wake 的 HTTP 200 只是受理**。必须再读一次 /api/session，归一化
     vm_state == RUNNING 才算唤醒成功（session_http.py 模块头第 1 条）。
     所以 `wake_ok` 定义为"确实发过 wake 且被确认"：VM 本来就 RUNNING 时它是
     False —— 没唤醒过就别声称唤醒成功。
  2. **delegation. 前缀的 notary_token 直接失败，不回退浏览器**。那是机密 VM
     的背书令牌，需要在 msg3 里回签名过的 RV challenge；我们不复刻那一套，
     也绝不在失败后偷偷改用 CDP。静默降级会把"协议不支持"误读成"账号坏了"，
     并把两条完全不同的失败路径合成一条无法诊断的。
  3. **fs 的结果不是普通 HTTP body**。`/fs/read` 每次最多 1MiB，必须循环到 eof，
     并且每页都用服务端声明的 `len` 校验实收字节数。只取第一页会拿到截断文件，
     而截断文件比失败更难诊断（渲染出一张坏图，没人知道为什么）。
  4. **WS 目标只用常量 LB host**。2026-11-11 真机第一次把纯协议链路打通（走本机
     mihomo 代理 127.0.0.1:18000 到 Meta 的 LB，直连被墙）：能握手的是
     `wss://hatch.metaaivm.com/v1/noise?...`；`/api/session` 下发的
     `wss://<vm_id>.metaaivm.com/` 这个 per-VM 子域在同一代理下 TLS 直接失败
     （`ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]`）。所以常量是首选，
     endpoint_url 只作为记录（它仍是 `/api/hatch/token` 的 `vmAddress` 参数），
     由它派生 host 的旧结论已被真机推翻，那条路径降级为默认关闭的备选。

**断线重连 + 订阅 resume（0.4.x，离线实现 + 2026-11-11 真机断线验收）**——
证据等级不同，别混着读（实验记录：work/canvas_re/resume-live2/）：

  * `read_stream` 检测到连接死掉（接收循环退出 / ws 句柄为空）时，用**同一个
    client** 走一遍 `connect()`（`/api/session` 续签 → `/api/hatch/token` →
    Noise XX），再按已消费的 seq 重新 `subscribe`，然后从新流继续消费。
  * **真机断线实验（2026-11-11 11:2x，图像生成回合）**：在 `task.status` 之后、
    成品 `delta.presentation` 之前把 WS 掐断；重连 1 次后，带
    `after_stream_seq=322` / `after_chat_event_seq=0` 的 subscribe 返回 200（未触发
    降级重订），随后继续收到 tail 事件与最终的 `delta.presentation`。⇒
    "重连之后服务端确实接着推"**已实测**，不再是推断。
  * resume 字段名 `after_stream_seq` / `after_chat_event_seq` 是
    **前端静态锚点**（见 `RESUME_EVIDENCE`，锚在打包 chunk 里；不是抓包）。
  * "严格大于该 seq"的边界语义仍是**推断**：前端把已消费的最大 seq 传回去，
    等于说"这一条别再发了"；社区实现文档同语义。真机只证明了"带字段被接受、
    尾部继续到"，没有把 seq=322 本身是否重发单独钉住，所以客户端**不把去重押在
    服务端**：重放里 `seq <= 游标` 的事件本地丢弃，两种边界下都不会重复正文。
  * 服务端是否接受 resume 字段**未证实**：真机订阅实测过的 body 是 `{}`。
    所以带字段的订阅如果被拒（非 200），降级成普通 `{}` 订阅 + 本地 seq 去重
    （`_resume_subscription` 里的两段式降级），而不是把重连整条打死。

作者：**君莫笑** · **永恒之兰**
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import posixpath
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx

from .noise import MAX_PLAINTEXT_FRAME, NoiseXX, Reassembler, msg1_payload
from .proto import (
    ApplicationRequest,
    ApplicationResponse,
    BodyChunk,
    Header,
    NoiseTransportFrame,
    Reset,
    ServiceFrame,
    ServiceRequest,
    ServiceResponse,
    ServiceType,
)
from .route_limits import limits_for
from .session_http import (
    RUNNING,
    EgressBlocked,
    RateLimited,
    RenewResult,
    SessionError,
    SessionExpired,
    SessionUnconfirmed,
    build_headers,
    renew_session,
    wake_vm,
)

logger = logging.getLogger("muse-bridge.protocol_client")

# Noise WS 目标常量：**首选目标**，不是兜底。
#
# 2026-11-11 真机（本机 mihomo 代理 127.0.0.1:18000 → Meta 的 LB；直连被墙）实测：
# `wss://hatch.metaaivm.com/v1/noise?...` 的 TLS + WS 升级返回 101，随后 Noise XX
# 握手成功，connect() 全程约 3.3s。这是纯协议链路第一次被打通的那条 URL。
#
# 被真机推翻的旧结论：`/api/session` 下发的 endpoint_url 是
# `wss://<vm_id>.metaaivm.com/`（每台 VM 一个子域），旧实现把它当成 "唯一可信的
# WS host 来源"、把本常量降级成回退。同一代理下用那个子域做 WS 目标时 TLS 直接失败：
#     ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure
# 所以现在的优先级反过来了：常量 host/path 是默认路径，endpoint 派生只保留为
# `ws_url_from_endpoint(use_endpoint_host=True)` 这个**默认关闭**的备选（未来 LB
# 切换时用一次开关就能验证，不必重写 URL 构造）。endpoint_url 本身仍然记录在
# `client.endpoint_url` 上 —— 它还是 `/api/hatch/token` 的 `vmAddress` 参数，
# 只是不再作 WS host。
NOISE_WS_HOST = "hatch.metaaivm.com"
NOISE_WS_PATH = "/v1/noise"
# 浏览器建 WS 时恒定带的 `app_version`（docs/NOISE_BUNDLE_NOTES.md 里
# `3w55hkryr8wwj.js@33403` 的 URL 拼装顺序里有它）。真机缺它也能连，但既然是
# 客户端恒发值，补上比省一个 query 参数更稳。
HATCH_APP_VERSION = "1"
DISPLAY_NAME = "muse-bridge"
CLIENT_VERSION = "0.0.0"
PLATFORM = "web"
TIMEZONE = "Asia/Shanghai"

# 观测到的能力清单。**注意：它不再上线路上**。
# 真机 2026-11-11：把这份清单按字符串数组塞进 /client/register-capabilities 的
# `capabilities` 字段 → 400（`invalid type: string "chat_cancel", expected a map`）。
# 所以 register 现在只发 client_id，/chat/stream 发 `capabilities: []`。
# 常量本身保留：`protocol_generation.py` / `protocol_engine.py` 会读
# `client.capabilities` 并自行决定发什么（改掉这两份的输入等于悄悄改它们的 body）。
DEFAULT_CAPABILITIES: tuple[str, ...] = (
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
)

# ---------------------------------------------------------------- resume 证据等级
# 订阅 resume 的三个事实，各自知道到什么程度 —— 写在这里是为了让"注释里的
# 证据等级"不随重构丢失（tests/test_protocol_resume.py 会钉住它不许被删）。
#
# 静态锚点原文（D:\deep seek\work\muse-bundles\24thn7in_976_.js，打包 chunk，
# 单文件 210,970 B；模块 459544 的事件处理段）：
#
#     function Z(e){return Number.isFinite(e)?Math.max(0,Math.trunc(e)):0}
#     e.s(["M",0,function(e,t){if("number"!=typeof t||!Number.isFinite(t))return e;
#           let n=Math.trunc(t);return n<=0?e:Math.max(e,n)},
#          "T",0,function(e,t,n){if("draft"===e.kind)return{capabilities:en()};
#           let r=function(e,t=e){return{after_stream_seq:Z(e),
#             after_chat_event_seq:Z(t),capabilities:en()}}(t,n);
#           return"thread"===e.kind?{...r,session_id:e.sessionId}:r},
#          ...
#          "q",0,function(e){return e?.reduce((e,t)=>Math.max(e,t.seq),0)??0}],459544)
#
# 调用侧（同一 chunk 的 chat provider）：
#     let[r]=await Promise.allSettled([t4("chat.subscribe",(0,W.T)(ra,a_.current,aS.current),...
# 其中 `a_.current` 由每条事件的 `a0(t.seq??e.seq)` 更新，更新函数就是上面的 "M"
# （单调取 max）。也就是说：**客户端把"已应用事件的最大 seq"当 after_stream_seq
# 传回**。传回已消费值 ⇒ 期望服务端不再重发该条 ⇒ 推断语义为"严格大于"。
RESUME_EVIDENCE: dict[str, str] = {
    "after_stream_seq": (
        "前端静态锚点（强，非真机抓包）：chunk 24thn7in_976_.js 构建 subscribe "
        "body 时传 after_stream_seq=已应用事件 seq 的单调最大值（Math.max）。"
        "推断语义=从该 seq 之后（严格大于）回放；包含/排除边界真机未验证。"
    ),
    "after_chat_event_seq": (
        "同一锚点里的第二个游标，取自事件的 chat_event_seq（同样是单调 max）。"
        "但 2026-11-11 真机事件样本里没有 chat_event_seq 这个键 —— 它存在与否"
        "未证实，所以客户端只在见过它之后才把它当游标（默认 0）。"
    ),
    "server_accepts_resume_fields": (
        "真机实测接受（2026-11-11 11:2x）：中途掐断 WS 后重连，subscribe body 带 "
        "`after_stream_seq=322` / `after_chat_event_seq=0` 返回 200，且没有触发"
        "降级重订（降级会再发一次空 `{}` 订阅，实测没有）。证据："
        "work/canvas_re/resume-live2/resume-live2.json。"
    ),
    "server_replays_after_disconnect": (
        "真机实测（2026-11-11 11:2x）：同一实验里断开发生在 `task.status` 之后、"
        "成品 `delta.presentation` 之前；重连后仍收到后续 "
        "model_lock.updated/agent.status/task.status/approvals.snapshot 以及最终的 "
        "`delta.presentation`（成品是在重连之后才生成完的）。即“重连后服务端继续"
        "推送尾部”成立。**未单独钉**：游标严格大于/包含边界（seq=322 本身会不会"
        "再发一次）——本地 `seq <= 游标` 去重兜底，但边界语义仍属推断。"
    ),
}

# 重连策略默认值。3 次 × 指数退避（0.5/1/2s，封顶 4s）在真机冷启动（握手约 3s）
# 下是可接受的等待；超过就抛错而不是无限重试 —— 对一条已经不可用的连接，
# 无限重试只会把上层 deadline 烧完，然后给出一个比"连不上"更难诊断的超时。
DEFAULT_MAX_RECONNECTS = 3
DEFAULT_RECONNECT_BACKOFF = 0.5
DEFAULT_RECONNECT_BACKOFF_MAX = 4.0

# VM 重启窗口的重试策略：`/api/session` 可能 200 + status=unavailable +
# vm_resolution_issue.kind=retryable（muse-cli docs/PROTOCOL.md 的 live 记录）。
# 重试 2 次（1s/2s）足以跨过大多数冷启动/重调度窗口；再久就交给上层失败，
# 不能把派单 deadline 全烧在这一个号上。只在 retryable=真 时生效，
# 其它非 assigned 仍是一次失败（不把"号坏了"伪装成"正在重启"）。
SESSION_UNAVAILABLE_RETRIES = 2
SESSION_UNAVAILABLE_DELAYS = (1.0, 2.0)


def _resume_cursor(value: object) -> int:
    """把 resume 游标归一成前端 `Z(e)` 的形态：非有限数 → 0，否则 max(0, 截断)。

    为什么照抄前端的夹取而不是"原样传"：`Z` 是那条静态锚点里唯一处理边界的
    代码，传 `None`/负数/小数上去等于自己发明一种服务端没见过的输入。这里也
    不接受 bool（Python 的 `True` 是 int，但 JSON 的 true 不是 seq）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        number = int(value)
    except (OverflowError, ValueError):
        # int(inf)/int(nan) 会抛：前端 `Number.isFinite` 把它们归 0，这里对齐。
        return 0
    return max(0, number)


def _seq_of(value: object) -> int | None:
    """事件里的 seq/chat_event_seq → 正整数或 None（用于单调游标，不夹取 0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = int(value)
    except (OverflowError, ValueError):
        return None
    return number if number > 0 else None



# 订阅流 NDJSON 的事件名（2026-11-11 真机实测序列）：
#   res → model_lock.updated → agent.status(online) → task.status →
#   approvals.snapshot → message.user → agent.status(working) →
#   agent.status(responding) → delta.message_start → delta.text_append →
#   agent.status(回到 online/idle)
# 助手正文只出现在 `delta.text_append.payload.text`；其余事件都是状态类，
# read_stream 不会把它们的任何字段当正文再吐一遍。
_TEXT_DELTA_EVENT = "delta.text_append"
_STATUS_EVENTS = frozenset(
    {
        "res",
        "model_lock.updated",
        "agent.status",
        "task.status",
        "approvals.snapshot",
        "message.user",
        "delta.message_start",
        "delta.message_done",
        "delta.message_removed",
        "delta.presentation",
        "delta.tool_done",
        "delta.placeholder",
        "pool_error",
    }
)

# cvm_eligibility 里"与机密 VM 无关"的那些状态；其余一律拒绝（模块头第 2 条）。
_NOT_CVM_STATES = frozenset(
    {"", "NOT_ELIGIBLE", "INELIGIBLE", "NONE", "DISABLED", "N/A"}
)

_FS_READ_CHUNK = 1024 * 1024
_FS_WRITE_CHUNK = 512 * 1024
_REFERENCE_DIR = "workspace/user/files"

# ---------------------------------------------------------------- WS 目标地址
# 2026-11-11 真机打通之后的结论，与这轮之前的写法**相反**：
#
#   * 能握手的是常量 LB 入口：`wss://hatch.metaaivm.com/v1/noise?...`
#     （TLS+WS 101 → Noise XX 成功 → connect() 约 3.3s）。
#   * `/api/session` 下发的 `endpoint_url = wss://<vm_id>.metaaivm.com/`（每台 VM
#     一个子域）在同一代理下 TLS 直接失败（SSLV3_ALERT_HANDSHAKE_FAILURE）。
#     所以旧实现"host 优先取 endpoint_url"是把不通的那条当成了唯一可信来源。
#
# 因此下面这个纯函数的默认行为是**忽略 endpoint_url、只用常量**；从 endpoint
# 派生 host 保留成 `use_endpoint_host=True` 这个默认关闭的备选（未来 LB 切换时
# 打开一次就能验证，不必重写 URL 构造）。`HatchProtocolClient._build_ws_url`
# 只负责把连接状态喂进来。纯 stdlib 解析（urllib.parse），不引入网络库 ——
# 这里只处理字符串，不该有 IO 语义。


def _ws_authority_and_path(endpoint_url: object) -> tuple[str, str] | None:
    """从 endpoint_url 抽出 WS 的 authority 与 path；不可用返回 None。

    只接受 ws/wss：`endpoint_url` 的语义是"这台 VM 的 WS 接入点"，http/https
    是 HTTP 基址而不是 WS 目标（旧测试夹具里就有一个 https 形态，正好落在常量
    路径上）。scheme 不区分大小写；userinfo 丢弃（凭据走查询串，不拼进 URL）。

    **本函数只服务备选路径**：默认路径根本不解析 endpoint（见
    `ws_url_from_endpoint` 的 `use_endpoint_host`）。真机没证明派生出来的 host
    能握手，所以它不能参与默认行为。
    """
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        return None
    try:
        parts = urlsplit(endpoint_url.strip())
        # .port 越界或非数字会在这里抛 ValueError，和 urlsplit 本身一起吞掉：
        # 坏 endpoint 只该走回退，不该让 connect() 多出一条崩溃路径。
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("ws", "wss"):
        return None
    host = parts.hostname or ""
    if not host:
        return None
    if ":" in host:  # IPv6 字面量必须加方括号才能拼回 URL
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    return authority, parts.path


def ws_url_from_endpoint(
    endpoint_url: object,
    *,
    vm_id: str,
    auth_token: str,
    app_id: str,
    request_id: str,
    notary: object = None,
    fallback_host: str = NOISE_WS_HOST,
    fallback_path: str = NOISE_WS_PATH,
    use_endpoint_host: bool = False,
) -> str:
    """拼 Noise WS 目标 URL。**默认用常量 LB host，endpoint 派生默认关闭。**

    * 默认路径（`use_endpoint_host=False`，产品代码走这条）：authority/path 恒取
      `fallback_host` + `fallback_path`，`endpoint_url` 完全不参与，传进来只是
      为了签名稳定。依据是真机 2026-11-11：`hatch.metaaivm.com/v1/noise` 的
      TLS+WS 101 与 Noise XX 都成功；而 endpoint 的 per-VM 子域
      `wss://<vm_id>.metaaivm.com/` 在同一代理下 TLS 直接
      `SSLV3_ALERT_HANDSHAKE_FAILURE`。旧实现"host 优先取 endpoint"因此是错的。
    * 备选路径（`use_endpoint_host=True`，**默认关闭**）：才去解析 endpoint_url
      的 authority/path —— scheme 强制 `wss`（`ws` 原样透传等于让 auth_token 走
      明文），userinfo 丢弃，path 为空或 `/` 时补 `fallback_path`，endpoint 自带
      path 原样保留。这条路径**一次真机握手都没有**（TLS 就不通），留着只为未来
      LB 切换时能开一个开关验证；默认打开它等于把已知不通的目标设为首选。
    * 拿不到可用 endpoint_url（缺失 / 空 / http(s) / 解析失败 / 无 host）时，
      备选路径回退 `fallback_host` + `fallback_path` —— 前置条件失败不该让
      connect() 多出一条崩溃路径。

    query 两条路径一致：`vm_id`、`auth_token`、`app_id`、`request_id` 恒在，
    `notary_token` 有值才带（空串会被对端当成"带了但无效"而拒握手）；endpoint
    自带的 query 不合并进来：目标 URL 的查询串是申请 token 时那一套。
    """
    authority, path = fallback_host, fallback_path
    if use_endpoint_host:
        parsed = _ws_authority_and_path(endpoint_url)
        if parsed is not None:
            authority, endpoint_path = parsed
            if endpoint_path and endpoint_path != "/":
                path = endpoint_path

    query: dict[str, str] = {
        "vm_id": vm_id,
        "auth_token": auth_token,
    }
    if notary:
        query["notary_token"] = str(notary)
    query["app_id"] = app_id
    # 2026-11-11 前端静态产物复核（docs/NOISE_BUNDLE_NOTES.md）：浏览器恒定
    # 发 `app_version=1`（`3w55hkryr8wwj.js@33403`），且参数顺序也如上。
    # 真机不发它也能连（服务端当前没强校验），但服务端一旦开始校验，
    # 缺它就会从"能连"变成"握手被拒"，成本极低，所以补上。
    query["app_version"] = HATCH_APP_VERSION
    query["request_id"] = request_id
    return f"wss://{authority}{path}?{urlencode(query)}"


# 契约面：BodyChunk / Reset / ApplicationResponse 不在本模块直接构造，
# 但它们是 ServiceFrame oneof 的分支，列在这里既钉住契约，也让 app 层
# 从同一个入口拿到全部异常类型与报文类型。
__all__ = [
    "ApplicationRequest",
    "ApplicationResponse",
    "BodyChunk",
    "DEFAULT_CAPABILITIES",
    "HatchProtocolClient",
    "Header",
    "ProtocolAuthError",
    "ProtocolEgressError",
    "ProtocolError",
    "ProtocolRateLimited",
    "ProtocolResponse",
    "Reset",
    "ServiceFrame",
    "ServiceRequest",
    "collect_media_candidates",
    "normalize_media_path",
    "resource_kind",
    "ws_url_from_endpoint",
]


class ProtocolError(RuntimeError):
    """协议层失败。app 层映射 502。"""

    status_code = 502


class ProtocolAuthError(ProtocolError):
    """401 一类：cookie 真失效。app 层映射 401 / SESSION_EXPIRED。"""

    status_code = 401


class ProtocolEgressError(ProtocolError):
    """403 一类：出口受限。**不得**据此判定 cookie 失效。"""

    status_code = 403


class ProtocolRateLimited(ProtocolError):
    """429：退避重试即可，账号本身没坏。"""

    status_code = 429


@dataclass
class ProtocolResponse:
    """一次 HTTP-over-Noise 往来的累积结果。

    `body` 用 bytearray：服务端会把同一 stream_id 的响应拆成 response 帧 +
    N 个 BodyChunk 陆续推来，反复 `bytes +=` 是 O(n^2)。
    """

    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)
    ended: bool = False
    reset: dict | None = None
    # 非 None 表示这条响应撞到上限：值为登记的字节数，body 已被截断在当前长度。
    limit_exceeded: int | None = None

    @property
    def text(self) -> str:
        return bytes(self.body).decode("utf-8", "replace")

    def json(self) -> object:
        return json.loads(bytes(self.body) or b"null")


def _map_session_error(exc: SessionError) -> ProtocolError:
    """session_http 的分类一对一映射到协议层分类。

    401/403/429/其它 的处置动作完全不同（重新登录 / 换出口 / 等一会儿 / 保留
    原判），所以这里绝不做"统一归并成通用错误"这种省事的事。
    """
    if isinstance(exc, SessionExpired):
        return ProtocolAuthError(str(exc))
    if isinstance(exc, EgressBlocked):
        return ProtocolEgressError(str(exc))
    if isinstance(exc, RateLimited):
        return ProtocolRateLimited(str(exc))
    if isinstance(exc, SessionUnconfirmed):
        # 规格里点名：非 assigned / 缺 endpoint_url 归到 "workspace not assigned"。
        return ProtocolError(f"workspace not assigned：{exc}")
    return ProtocolError(str(exc))


async def renew_with_retry(
    cookies: Mapping[str, str],
    *,
    site: str,
    client: httpx.AsyncClient,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> RenewResult:
    """`renew_session` 的窄重试：只对 VM 重启窗口（retryable）生效。

    为什么重试放在这里而不是 `session_http`：`/admin/.../refresh` 需要的是
    "一次事实"（unavailable 就如实返回 503），而纯协议连接需要的是
    "等到能握手的时刻"。把策略放在协议侧，HTTP 运维面不会被悄悄拖长。
    """
    wait = sleep or asyncio.sleep
    for attempt in range(SESSION_UNAVAILABLE_RETRIES + 1):
        try:
            return await renew_session(cookies, site=site, client=client)
        except SessionUnconfirmed as exc:
            if not exc.retryable or attempt >= SESSION_UNAVAILABLE_RETRIES:
                raise
            await wait(SESSION_UNAVAILABLE_DELAYS[attempt])
    raise AssertionError("unreachable")  # pragma: no cover - 循环必 return/raise


def _classify_status(code: int, *, what: str) -> None:
    """token 端点的状态码分类，规则与 session_http 保持一致。"""
    if code == 401:
        raise ProtocolAuthError(f"{what} 认证失败（HTTP 401）：cookie 已失效")
    if code == 403:
        raise ProtocolEgressError(
            f"{what} 被拒绝（HTTP 403）：多半是出口 IP／地区受限，"
            "不能据此判定 cookie 失效"
        )
    if code == 429:
        raise ProtocolRateLimited(f"{what} 被限流（HTTP 429）：稍后重试")
    if code != 200:
        raise ProtocolError(f"{what} 未成功（HTTP {code}）")


def _response_snippet(response: ProtocolResponse, *, limit: int = 240) -> str:
    """把服务端 body 压成一行片段，供异常消息带出去（排障可见）。

    为什么需要：只报 `HTTP 400` 等于零信息 —— 真机 register-capabilities 的 400
    才在 body 里写明 `invalid type: string "chat_cancel", expected a map`。
    压成一行 + 截断，是为了多行 JSON / HTML 错误页不会把日志和异常串撑爆；
    服务端错误 body 里没有本地凭据，可以安全拼进异常。
    """
    text = " ".join(response.text.split())
    if not text:
        return "（空 body）"
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------- 媒体路径
# 语义照抄 lichao protocol_engine.py:114-192：只认 sandbox:// 与 workspace/，
# 并显式把 workspace/user/（用户上传区）排除在"生成物"之外。


def normalize_media_path(path: object) -> tuple[str, str] | None:
    """把素材路径拆成 (sandbox URI, 远端相对路径)。不是生成物返回 None。"""
    if not isinstance(path, str) or not path:
        return None
    if path.startswith("sandbox://"):
        remote, uri = path[len("sandbox://") :], path
    elif path.startswith("workspace/"):
        remote, uri = path, "sandbox://" + path
    else:
        return None
    if remote.startswith("workspace/user/"):
        # 用户上传区里的东西是**输入**，不是本次生成的产物。把它当产物返回会让
        # "生成结果"退化成回声一张输入图 —— 上游踩过这个坑。
        return None
    return uri, remote


def resource_kind(resource: Mapping[str, object]) -> str:
    """mime 优先，取不到再退到扩展名。"""
    mime = str(resource.get("mime") or resource.get("mime_type") or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    path = str(
        resource.get("path")
        or resource.get("uri")
        or resource.get("image_path")
        or resource.get("video_path")
        or ""
    )
    suffix = posixpath.splitext(path)[1].lower()
    return "video" if suffix in (".mp4", ".webm", ".mov", ".3gp") else "image"


def _merge_json_string_meta(value: dict) -> dict:
    """metadata 有时是 JSON 字符串而不是对象（观测到），统一成对象形态。"""
    meta = value.get("metadata")
    if not isinstance(meta, str) or not meta.strip().startswith("{"):
        return value
    try:
        parsed = json.loads(meta)
    except (TypeError, ValueError):
        return value
    if not isinstance(parsed, dict):
        return value
    return {**value, "metadata": parsed}


def collect_media_candidates(value: object) -> list[dict]:
    """从 presentation/resources 信封里收集生成物引用。

    返回 [{"path": "sandbox://...", "kind": "image|video", ...原始字段}]，按
    sandbox URI 去重。返回归一化的 dict 而不是原始 payload，是为了让调用方不必
    再处理 metadata 到底是字符串还是对象这一层偶然复杂度。
    """
    output: list[dict] = []
    _collect(value, output)
    return output


def _collect(value: object, output: list[dict]) -> None:
    if isinstance(value, dict):
        value = _merge_json_string_meta(value)
        path = (
            value.get("path")
            or value.get("uri")
            or value.get("image_path")
            or value.get("video_path")
        )
        normalized = normalize_media_path(path)
        if normalized is None and isinstance(value.get("metadata"), dict):
            meta = value["metadata"]
            normalized = normalize_media_path(meta.get("path") or meta.get("uri"))
            if normalized is not None:
                value = {
                    **value,
                    "path": normalized[0],
                    "mime": value.get("mime") or meta.get("mime_type"),
                    "mime_type": value.get("mime_type") or meta.get("mime_type"),
                    "width": value.get("width") or meta.get("width"),
                    "height": value.get("height") or meta.get("height"),
                    "byte_len": (
                        value.get("byte_len")
                        or meta.get("byte_len")
                        or meta.get("size_bytes")
                    ),
                }
        if normalized is not None:
            uri = normalized[0]
            resource = dict(value)
            resource["path"] = uri
            if not any(item.get("path") == uri for item in output):
                resource.setdefault("kind", resource_kind(resource))
                output.append(resource)
        for key, child in value.items():
            if key not in ("request", "data_base64", "metadata"):
                _collect(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect(child, output)


def _decode_fs_chunk(result: Mapping[str, object]) -> bytes:
    """把 /fs/read 的一页解成字节，并按服务端声明的 len 校验。

    长度校验不是洁癖：截断文件会渲染成半张图或坏掉的 mp4，失败点离原因很远。
    """
    encoded = result.get("data_base64")
    if not isinstance(encoded, str):
        raise ProtocolError("fs.read 响应缺少 data_base64")
    try:
        chunk = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("fs.read 返回的 base64 无法解码") from exc
    declared = result.get("len", len(chunk))
    try:
        declared_int = int(declared)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("fs.read 的 len 不是整数") from exc
    if declared_int != len(chunk):
        raise ProtocolError(f"fs.read 长度不符：声明 {declared_int} 实收 {len(chunk)}")
    return chunk


def _safe_name(filename: str) -> str:
    """把用户给的文件名压成安全片段：只留 [A-Za-z0-9._-]，并防目录穿越。"""
    cleaned = posixpath.basename(str(filename or ""))
    cleaned = "".join(
        char if (char.isalnum() or char in "._-") else "_" for char in cleaned
    )
    return cleaned.strip("._") or "reference.bin"


class HatchProtocolClient:
    """一个账号一条 Noise 连接。发送路径由 asyncio.Lock 串行化。

    两个额外关键字参数（`http_client` / `ws_opener`）是规格之外的**加法**，
    只为离线测试留注入点：不注入就用真实 httpx / websockets(或 ws_fallback)。
    """

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        proxy: dict | None = None,
        site: str = "https://muse.ai",
        model: str = "hatch-web",
        user_agent: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        ws_opener: Callable[..., object] | None = None,
        max_reconnects: int = DEFAULT_MAX_RECONNECTS,
        reconnect_backoff: float = DEFAULT_RECONNECT_BACKOFF,
        reconnect_backoff_max: float = DEFAULT_RECONNECT_BACKOFF_MAX,
        reconnect_enabled: bool = True,
    ) -> None:
        # 会话 cookie：不写日志、不进异常消息。
        self.cookies = dict(cookies)
        self.proxy = proxy or None
        self.site = site.rstrip("/")
        self.model = model
        self.user_agent = user_agent
        # 能力清单保留下来给引擎读（`protocol_generation` 会读 `client.capabilities`）；
        # 本客户端自己**不再**把它发上线：register 只发 client_id，/chat/stream 发
        # `[]`（字符串 capabilities 在真机里是 400 的来源，见 DEFAULT_CAPABILITIES）。
        self.capabilities = list(DEFAULT_CAPABILITIES)
        self.timezone = TIMEZONE
        self.node_id = str(uuid.uuid4())
        self.session_id: str | None = None

        self.ws: object | None = None
        self.send_cipher: object | None = None
        self.recv_cipher: object | None = None
        self.reassembler = Reassembler()
        self.next_stream = 1
        self.responses: dict[int, ProtocolResponse] = {}
        self.waiters: dict[int, set[asyncio.Future]] = {}
        self.receiver_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._injected_http = http_client
        self._http: httpx.AsyncClient | None = http_client
        self._ws_opener = ws_opener
        self.closing = False

        self._vm_id: str | None = None
        self._vm_state: str | None = None
        # 最近一次 /api/session 的 RenewResult：session_quota() 与排障都读它，
        # 避免"额度接口另起一套 HTTP 逻辑"。
        self._last_session: RenewResult | None = None
        self._endpoint_url: str | None = None
        self._auth_token: str | None = None
        self._notary_token: str | None = None
        self._ws_url = ""
        self._wake_ok = False
        self._handshake_done = False
        # "这条 client 曾经握手成功过"与"现在是否连着"是两件事：前者用来区分
        # "从来就没连过"（该报'尚未连接'）和"连过但断了"（该走重连）。
        self._ever_connected = False
        self._sub_stream_id: int | None = None
        self._scan_offset = 0
        # 每个 stream 的响应体上限（只在 end_body=True 的一次性请求上登记）。
        # 在接收路径上就地生效：超过就停止累积并标记，而不是读完再判断。
        self._stream_limits: dict[int, int] = {}
        # 最近一次 read_stream 是否因为"连续无新字节"而收尾（排障用，不是契约）。
        self._idle_timeout_hit = False
        # True only for an observed, owned terminal event; plain EOF is not success.
        self._turn_complete = False

        # ---- 断线重连 / 订阅 resume（0.4.x，离线实现；真机未验收）----
        # 预算按"每个 read_stream 回合"重置（见 read_stream），连续抖动仍受上限
        # 约束：一次回合里最多 max_reconnects 次重连，之后抛错而不是无限重试。
        self.max_reconnects = max(0, int(max_reconnects))
        self.reconnect_backoff = max(0.0, float(reconnect_backoff))
        self.reconnect_backoff_max = max(0.0, float(reconnect_backoff_max))
        self.reconnect_enabled = bool(reconnect_enabled)
        self._reconnect_count = 0
        # 接收循环死掉的原因（排障用，并作为"连接已失效"的判据之一）。
        self._disconnect_reason: str | None = None
        # resume 游标：已消费/已跳过事件里的单调最大 seq 与 chat_event_seq。
        # 只增不减（前端锚点同样是 Math.max），否则服务端会重放更多历史。
        self._last_seen_seq = 0
        self._last_seen_chat_seq = 0

        # WS 目标默认走常量 LB host（真机唯一握手成功的形态）。置 True 才会尝试
        # /api/session 的 endpoint_url 派生的 per-VM 子域 —— 那条真机 TLS 不通
        # （SSLV3_ALERT_HANDSHAKE_FAILURE），只作为未来 LB 切换的备选开关。
        self.use_endpoint_ws_host = False
        self.heartbeat_interval = 30.0
        self.handshake_timeout = 30.0
        # 与上游一致：受理之后立即确认一次。真机冷启动常超过一个 RTT，
        # 需要更稳的部署把它调大；默认值不擅自放大，每次确认都是真实请求。
        self.wake_verify_tries = 1

    # ------------------------------------------------------------ 只读状态
    @property
    def ws_url(self) -> str:
        return self._ws_url

    @property
    def vm_id(self) -> str | None:
        return self._vm_id

    @property
    def auth_token(self) -> str | None:
        return self._auth_token

    @property
    def notary_token(self) -> str | None:
        return self._notary_token

    @property
    def vm_state(self) -> str | None:
        return self._vm_state

    @property
    def endpoint_url(self) -> str | None:
        """最近一次 /api/session 下发的接入点记录。**不作为 WS host**。

        为什么还留着：它是 `/api/hatch/token` 的 `vmAddress` 参数，排障时第一个
        要回答的问题是"我们连的是哪台 VM"，而 ws_url 的 host 现在是常量
        `hatch.metaaivm.com`，看不出 VM 子域原本长什么样。

        为什么不再派生 host：真机 2026-11-11 用 `wss://<vm_id>.metaaivm.com/`
        做 WS 目标时 TLS 直接 `SSLV3_ALERT_HANDSHAKE_FAILURE`；能握手的是常量
        LB 入口。要临时验证备选路径，把 `client.use_endpoint_ws_host` 置 True。
        """
        return self._endpoint_url

    @property
    def wake_ok(self) -> bool:
        """只在"真的发过 wake 且二次确认 RUNNING"时为真（模块头第 1 条）。"""
        return self._wake_ok

    @property
    def connected(self) -> bool:
        return bool(self._handshake_done and self.ws is not None and not self.closing)

    def new_task_node(self) -> str:
        """给本条任务换一个新的 node_id，不断连。

        为什么需要：事件按 node_id 归属，一条连接复用同一个 node_id 跑多次生成
        时，上一轮残留的 delta 会被当成这一轮的回答。
        """
        self.node_id = str(uuid.uuid4())
        return self.node_id

    # ------------------------------------------------------------ 连接
    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            kwargs: dict = {
                "follow_redirects": False,
                "timeout": 30.0,
                # **不要**读系统代理（Windows 注册表/WinHTTP 也会被 httpx 当
                # 环境代理读进来）。2026-11-11 真机踩过：本机 mihomo 节点失效时
                # 系统代理指向 127.0.0.1:18000，trust_env=True 的 httpx 会把
                # *直连可用* 的 /api/session 也塞进那条死代理，报
                # "ConnectError: SSL UNEXPECTED_EOF"，看起来像"服务器出口网络
                # 坏了"，实际是客户端自己偷用了代理。代理只允许显式传：
                # `proxy=` 参数是唯一来源，和 session_http._OwnClient 的
                # trust_env=False 口径一致。
                "trust_env": False,
                "headers": build_headers(self.cookies, self.site),
            }
            proxy_url = self._proxy_url()
            if proxy_url:
                kwargs["proxy"] = proxy_url
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    def _proxy_url(self) -> str | None:
        """把 {server, username, password} 变成 httpx 能吃的代理地址。

        这个串含凭据：只交给 httpx，**绝不**进异常消息或日志。
        """
        if not self.proxy:
            return None
        server = str(self.proxy.get("server") or "").strip()
        if not server:
            return None
        if "://" not in server:
            server = "http://" + server
        username = str(self.proxy.get("username") or "")
        password = str(self.proxy.get("password") or "")
        if username and "@" not in server:
            scheme, _, rest = server.partition("://")
            creds = f"{username}:{password}" if password else username
            server = f"{scheme}://{creds}@{rest}"
        return server

    async def _drop_connection(self) -> None:
        """幂等拆掉当前这条连接（接收循环、心跳、WS 句柄、握手状态）。

        与 `close()` 的分工：这里**不**动 `closing`、HTTP 客户端和 `responses`
        （重连时别的调用方可能还在等自己的 stream；清空它们会把"重连"变成
        "顺手丢掉所有在途请求"）。`connect()` 开头也调它 —— 于是 `connect()`
        自己就是"用同一个 client 重新握手"的入口（重连复用 token/cookie 续期
        路径，不另开一套）。
        """
        self._handshake_done = False
        self.send_cipher = self.recv_cipher = None
        self._sub_stream_id = None
        self._scan_offset = 0
        tasks = [
            task
            for task in (self.receiver_task, self.heartbeat_task)
            if task is not None
        ]
        self.receiver_task = self.heartbeat_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._teardown_ws()

    def _connection_lost(self) -> bool:
        """这条连接还能用吗？判据是"接收循环已死 / 句柄没了 / 没握手"。

        为什么不用"最近一次收发失败"当判据：发送失败可能只是那一条请求的问题，
        而接收循环退出是连接级的确定信号。`_disconnect_reason` 由 `_receiver`
        的异常分支写入；`closing` 期间一律当作"不是断线"（那是主动关闭）。
        """
        if self._disconnect_reason is not None:
            return True
        if self.ws is None or not self._handshake_done:
            return True
        task = self.receiver_task
        return task is None or task.done()

    async def connect(self) -> None:
        """三阶段 bootstrap；对**已连上的 client 再次调用 = 重连**（幂等）。

        重连语义：先 `_drop_connection()` 把旧接收循环/心跳/WS 摘干净，再走一遍
        `/api/session` 续签（token/cookie 续期仍是 session_http 那一套，不复制
        实现）→ `/api/hatch/token` → Noise XX，最后重建 `_receiver` 与心跳。
        不做这一步的话第二次握手会留下两个 `_receiver` 抢同一个 ws、两个心跳
        交替打 `/api/ping`，表现为随机丢帧。
        """
        if self.closing:
            # 关闭是终态：close() 之后不能再悄悄建一条新连接（否则调用方以为
            # 已释放，实际泄漏一条 WS + 两个任务）。
            raise ProtocolError("客户端已关闭，不能重新握手")
        await self._drop_connection()
        client = await self._ensure_http()
        try:
            session = await renew_with_retry(
                self.cookies, site=self.site, client=client
            )
        except SessionError as exc:
            raise _map_session_error(exc) from None

        # 未知 vm_state 一律按"还没起来"处理：None 不等于 RUNNING，放行它会让
        # connect 带着一条睡着的 VM 继续换 token/握手，最后表现为随机超时。
        if not session.vm_id or session.vm_state != RUNNING:
            try:
                result = await wake_vm(
                    session,
                    site=self.site,
                    client=client,
                    verify_tries=self.wake_verify_tries,
                )
            except SessionError as exc:
                raise _map_session_error(exc) from None
            self._wake_ok = bool(result.wake_ok)
            if result.session is not None:
                session = result.session
            if not self._wake_ok:
                # 受理 != 成功。宁可直接失败，也不要在一条起不来的 VM 上继续
                # 握手 —— 那会表现为"随机超时"，比明确报错难诊断一个量级。
                raise ProtocolError(f"VM 未确认 RUNNING：{result.reason}")

        self._vm_id = session.vm_id
        self._vm_state = session.vm_state
        # 本轮 /api/session 快照：`session_quota()` 在需要时复用它（不额外发请求）。
        self._last_session = session
        # endpoint_url 只做记录（并作为 /api/hatch/token 的 vmAddress 参数），**不是**
        # WS host 的来源：真机 2026-11-11 用它做 WS 目标时 TLS 直接失败，能握手的是
        # 常量 LB host。缺失时存 None 而不是空串，让"没下发"和"下发了一个空值"在
        # 排障时还能区分。
        endpoint = session.endpoint_url
        self._endpoint_url = str(endpoint) if endpoint else None
        if not session.assigned or not endpoint or not session.vm_id:
            # 判据不变：缺 endpoint_url 仍然不可连 —— 它是换 token 的前置参数，
            # 不是可省的装饰（换不到 token 就没有 auth_token 可握手）。
            raise ProtocolError(f"workspace not assigned：{session.reason}")
        if session.vm_state != RUNNING:
            # wake 之后仍不是 RUNNING：宁可现在就失败，也不要连上去等超时。
            raise ProtocolError(
                f"VM 状态不是 RUNNING（{session.vm_state or '空'}）：{session.reason}"
            )

        token = await self._fetch_token(session, endpoint, client)
        auth_token = token.get("token")
        if not auth_token:
            raise ProtocolAuthError("hatch/token 未返回 auth token")
        notary = token.get("notary_token")
        self._auth_token = str(auth_token)
        self._notary_token = str(notary) if notary else None

        vm_type = str(session.raw.get("vm_type") or "").lower()
        if isinstance(notary, str) and notary.startswith("delegation."):
            # 模块头第 2 条：不回退浏览器。
            raise ProtocolError(
                "notary_token 是 delegation. 前缀（机密 VM 背书令牌），"
                "本客户端只支持标准 hatch_vm；不回退 CDP／浏览器"
            )
        if "confidential" in vm_type:
            raise ProtocolError(
                f"不支持的 vm_type={session.raw.get('vm_type')!r}：机密 VM 需要 "
                "attestation/recovery 证明；不回退 CDP／浏览器"
            )
        cvm_state = self._cvm_state(session)
        if cvm_state not in _NOT_CVM_STATES:
            raise ProtocolError(
                f"cvm_eligibility.state={cvm_state!r} 非 NOT_ELIGIBLE 系：本客户端"
                "只支持标准 hatch_vm；不回退 CDP／浏览器"
            )

        self._ws_url = self._build_ws_url(str(auth_token), notary)
        await self._open_noise_ws(vm_type)
        self._handshake_done = True
        # 握手成功 = 这条连接是新的：旧的断线原因清零，否则 `_connection_lost()`
        # 会一直把它当断线（重连后永远修不好）。
        self._disconnect_reason = None
        self._ever_connected = True
        self.receiver_task = asyncio.create_task(self._receiver())
        if self.heartbeat_interval:
            self.heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _fetch_token(
        self, session: RenewResult, endpoint: str, client: httpx.AsyncClient
    ) -> dict:
        body = {"vmAddress": endpoint}
        vm_name = session.raw.get("vm_name") or session.raw.get("vmName")
        if vm_name:
            body["vmName"] = str(vm_name)
        url = f"{self.site}/api/hatch/token"
        headers = build_headers(session.cookies, self.site)
        headers["Content-Type"] = "application/json"
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            # 只留类型名：代理 URL 可能带凭据，异常串会进日志。
            raise ProtocolError(
                f"hatch/token 网络请求失败（{type(exc).__name__}）"
            ) from None
        _classify_status(response.status_code, what="/api/hatch/token")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProtocolError("hatch/token 返回 200 但不是 JSON") from exc
        if not isinstance(payload, dict):
            raise ProtocolError(
                f"hatch/token 的 JSON 不是对象（{type(payload).__name__}）"
            )
        return payload

    @staticmethod
    def _cvm_state(session: RenewResult) -> str:
        for key in ("cvm_eligibility", "cvmEligibility"):
            blob = session.raw.get(key)
            if isinstance(blob, dict):
                state = blob.get("state") or blob.get("status") or ""
                return str(state).strip().upper()
        return ""

    def _build_ws_url(self, auth_token: str, notary: object) -> str:
        """薄封装：把当前连接状态喂给纯函数 `ws_url_from_endpoint`。

        这里只做取值与 uuid4 生成，不做解析/回退判断 —— 那些都在纯函数里，
        测试可以完全不碰连接状态就钉住它们。

        `endpoint_url` 照样传进去（签名与记录不变），但纯函数默认
        `use_endpoint_host=False`，所以 host/path 恒取常量：
        `wss://hatch.metaaivm.com/v1/noise`。真机唯一握手成功的就是这个形态，
        endpoint 的 per-VM 子域 TLS 不通（见常量处注释）。
        """
        return ws_url_from_endpoint(
            self._endpoint_url,
            vm_id=self._vm_id or "",
            auth_token=auth_token,
            app_id=self.model,
            request_id=str(uuid.uuid4()),
            notary=notary,
            use_endpoint_host=self.use_endpoint_ws_host,
        )

    @staticmethod
    def _default_ws_opener() -> Callable:
        """优先 websockets；没装就用本包的 ws_fallback（同一个四件套接口）。"""
        try:
            import websockets
        except ModuleNotFoundError:
            from . import ws_fallback

            return ws_fallback.connect
        async def open_ws(url: str, **kwargs):
            # Our injectable opener contract uses user_agent (as does ws_fallback),
            # but websockets passes unknown kwargs to loop.create_connection.
            # Its actual HTTP header argument is user_agent_header.
            if "user_agent" in kwargs:
                kwargs["user_agent_header"] = kwargs.pop("user_agent")
            # websockets >=15 defaults proxy=True (system/environment discovery).
            # Match HTTP trust_env=False: only use an explicitly supplied proxy.
            kwargs.setdefault("proxy", None)
            return await websockets.connect(url, **kwargs)

        return open_ws

    async def _open_noise_ws(self, vm_type: str) -> None:
        opener = self._ws_opener or self._default_ws_opener()
        kwargs: dict = {
            "origin": self.site,
            "max_size": 4 * 1024 * 1024,
            "open_timeout": self.ws_open_timeout,
            "close_timeout": 3.0,
            "additional_headers": {
                "Accept-Language": "en-US,en-GB;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        }
        proxy_url = self._proxy_url()
        if proxy_url:
            kwargs["proxy"] = proxy_url
        if self.user_agent:
            kwargs["user_agent"] = self.user_agent
        try:
            self.ws = await opener(self._ws_url, **kwargs)
        except ProtocolError:
            raise
        except Exception as exc:
            # Only accept typed numeric metadata, never stringify a response or
            # exception: upgrade headers/URLs may contain authentication tokens.
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if type(status) is not int:
                status = getattr(exc, "status_code", None)
            detail = f" HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
            raise ProtocolError(
                f"Noise WebSocket 连接失败（{type(exc).__name__}{detail}）"
            ) from None
        try:
            await self._noise_handshake(vm_type)
        except BaseException:
            # asyncio.CancelledError is not an Exception. A deadline/caller cancel
            # during msg1/msg2/msg3 must retire this partial cryptographic session.
            self._handshake_done = False
            self.send_cipher = self.recv_cipher = None
            await self._teardown_ws()
            raise

    async def _noise_handshake(self, vm_type: str) -> None:
        noise = NoiseXX()
        # msg1 载荷 = protobuf field1 里的 32 字节 CSPRNG nonce（`0a 20` + 32B）。
        # served VM 期望非空载荷：全零或固定值会被当成重放直接拒，
        # 所以这一步必须用 CSPRNG，noise.py 的 msg1_payload 就是这个语义。
        try:
            payload = msg1_payload()
        except Exception:
            payload = b"\x0a\x20" + secrets.token_bytes(32)
        await self._ws_send(noise.msg1(payload))
        msg2 = await asyncio.wait_for(self._ws_recv(), self.handshake_timeout)
        if not isinstance(msg2, (bytes, bytearray)):
            raise ProtocolError("Noise 握手期间收到文本帧")
        challenge = noise.read_msg2(bytes(msg2))
        if challenge and "confidential" in vm_type:
            raise ProtocolError(
                "收到机密 VM 的 attestation challenge，未实现 recovery 证明；"
                "不回退 CDP／浏览器"
            )
        await self._ws_send(noise.msg3(b""))
        self.send_cipher, self.recv_cipher = noise.split()

    # ------------------------------------------------------------ 收发原语
    async def _ws_send(self, data: bytes) -> None:
        if self.ws is None:
            raise ProtocolError("尚未连接：先调用 connect()")
        await self.ws.send(data)  # type: ignore[attr-defined]

    async def _ws_recv(self) -> str | bytes:
        if self.ws is None:
            raise ProtocolError("尚未连接：先调用 connect()")
        return await self.ws.recv()  # type: ignore[attr-defined]

    def _notify(self, stream_id: int) -> None:
        for waiter in self.waiters.get(stream_id, set()):
            if not waiter.done():
                waiter.set_result(True)

    def _fail_waiters(self, exc: ProtocolError) -> None:
        for waiters in self.waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(exc)

    async def _wait_update(self, stream_id: int, timeout: float) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(stream_id, set()).add(waiter)
        try:
            await asyncio.wait_for(waiter, max(0.05, timeout))
        except TimeoutError:
            pass
        finally:
            waiters = self.waiters.get(stream_id)
            if waiters is not None:
                waiters.discard(waiter)
                if not waiters:
                    self.waiters.pop(stream_id, None)

    async def _wait_end(self, stream_id: int, timeout: float) -> ProtocolResponse:
        deadline = time.monotonic() + timeout
        while True:
            response = self.responses.get(stream_id)
            if response is None:
                raise ProtocolError(f"stream {stream_id} 的响应已被丢弃")
            if response.ended or response.reset is not None:
                return response
            if self._disconnect_reason is not None:
                raise ProtocolError(self._disconnect_reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(f"等待 stream {stream_id} 结束超时")
            await self._wait_update(stream_id, remaining)

    async def _wait_headers(self, stream_id: int, timeout: float) -> ProtocolResponse:
        deadline = time.monotonic() + timeout
        while True:
            response = self.responses.get(stream_id)
            if response is None:
                raise ProtocolError(f"stream {stream_id} 的响应已被丢弃")
            if response.status is not None or response.ended or response.reset:
                return response
            if self._disconnect_reason is not None:
                raise ProtocolError(self._disconnect_reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(f"等待 stream {stream_id} 响应头超时")
            await self._wait_update(stream_id, remaining)

    @staticmethod
    def _frame_kind(frame: ServiceFrame) -> str:
        """ServiceFrame oneof 命中了哪一支。

        proto.py 用 `.kind` 属性；这里保留对 protobuf 生成类（WhichOneof）的
        兼容，是为了不让本模块和某一个具体代码生成器绑死。
        """
        kind = getattr(frame, "kind", None)
        if isinstance(kind, str):
            return kind
        which = getattr(frame, "WhichOneof", None)
        if callable(which):
            return str(which("kind") or "")
        for name in ("request", "response", "body_chunk", "reset"):
            if getattr(frame, name, None) is not None:
                return name
        return ""

    def _append_body(
        self, stream_id: int, response: ProtocolResponse, data: bytes
    ) -> None:
        """把一段响应体并进 response，同时在**追加时**执法上限。

        超过上限的做法是"停止累积 + 标记 + 视为结束"，而不是抛异常：
        接收循环是整条连接的公共设施，抛异常会把一次超大响应升级成断线，
        连累同一个连接上别的 stream。判定交给 `request()`。
        """
        if not data:
            return
        limit = self._stream_limits.get(stream_id)
        if limit is not None and len(response.body) + len(data) > limit:
            response.limit_exceeded = limit
            response.ended = True
            return
        response.body.extend(data)

    async def _receiver(self) -> None:
        try:
            while not self.closing:
                message = await self._ws_recv()
                if isinstance(message, str):
                    raise ProtocolError("收到文本帧，协议通道只走二进制")
                plain = self.recv_cipher.dec(b"", bytes(message))  # type: ignore[union-attr]
                frame = NoiseTransportFrame.decode(plain)
                raw = self.reassembler.push(
                    frame.chunk_id, frame.chunk_index, frame.total_chunks, frame.payload
                )
                if raw is None:
                    continue
                inner = ServiceResponse.decode(raw).payload
                service_frame = ServiceFrame.decode(inner)
                stream_id = int(service_frame.stream_id)
                response = self.responses.get(stream_id)
                # HTTP-over-Noise responses must belong to a locally opened stream.
                # Late/unknown frames still advance the receive cipher, but must not
                # resurrect timed-out streams or allocate unbounded response buffers.
                if response is None or response.ended:
                    continue
                kind = self._frame_kind(service_frame)
                if kind == "response":
                    payload = service_frame.response
                    if payload is not None:
                        response.status = int(payload.status)
                        response.headers = [
                            (item.key, item.value) for item in payload.headers
                        ]
                        self._append_body(stream_id, response, bytes(payload.body))
                        response.ended = response.ended or bool(payload.end_body)
                elif kind == "body_chunk":
                    chunk = service_frame.body_chunk
                    if chunk is not None:
                        self._append_body(stream_id, response, bytes(chunk.data))
                        response.ended = response.ended or bool(chunk.end_body)
                elif kind == "reset":
                    reset = service_frame.reset
                    response.reset = {
                        "code": int(getattr(reset, "code", 0) or 0),
                        "reason": str(getattr(reset, "reason", "") or ""),
                    }
                    response.ended = True
                self._notify(stream_id)
            if not self.closing:
                self._disconnect_reason = "Noise 连接已关闭"
                self._fail_waiters(ProtocolError("Noise 连接已关闭"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closing:
                # 连接级故障：记下原因（read_stream 据此触发重连，排障也用这一句
                # 而不是"无响应"）。主动 close() 时不记 —— 那不是断线。
                self._disconnect_reason = f"Noise 接收失败（{type(exc).__name__}）"
                self._fail_waiters(ProtocolError(self._disconnect_reason))

    async def _heartbeat(self) -> None:
        """每 30s 一次 /api/ping。空闲的 Noise 连接会被服务端回收。"""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            with contextlib.suppress(Exception):
                await self.ping("active")

    # ------------------------------------------------------------ 请求发送
    def _build_headers(
        self, extra: Mapping[str, str] | None, has_body: bool
    ) -> tuple[Header, ...]:
        pairs = [
            ("x-request-id", str(uuid.uuid4())),
            ("x-app-id", self.model),
            ("Accept-Language", "en-US"),
            ("Accept-Encoding", "identity"),
        ]
        if has_body:
            pairs.append(("Content-Type", "application/json"))
        for name, value in (extra or {}).items():
            # 调用方同名覆盖：客户端库常要自己塞一个 request id 来做日志关联。
            pairs = [
                (key, val) for key, val in pairs if key.lower() != name.lower()
            ] + [(name, str(value))]
        return tuple(Header(key=key, value=value) for key, value in pairs)

    def _pack(self, message: ServiceRequest) -> list[bytes]:
        """序列化 + 按 MAX_PLAINTEXT_FRAME 分块 + 逐块加密成 WS 帧。"""
        raw = message.encode()
        chunks = [
            raw[index : index + MAX_PLAINTEXT_FRAME]
            for index in range(0, len(raw), MAX_PLAINTEXT_FRAME)
        ] or [b""]
        chunk_id = int.from_bytes(secrets.token_bytes(8), "big", signed=True)
        out: list[bytes] = []
        for index, chunk in enumerate(chunks):
            frame = NoiseTransportFrame(
                chunk_id=chunk_id,
                chunk_index=index,
                total_chunks=len(chunks),
                payload=chunk,
            )
            if self.send_cipher is None:
                # 未 connect 就发请求：在公共入口给出协议层错误，而不是在
                # `None.enc` 上抛 AttributeError（那会把"没连上"误报成内部 bug）。
                raise ProtocolError("尚未连接：必须先 await connect() 再发请求")
            out.append(self.send_cipher.enc(b"", frame.encode()))
        return out

    async def _send(
        self,
        verb: str,
        path: str,
        body: dict | bytes | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        end_body: bool = True,
        max_bytes: int | None = None,
    ) -> int:
        if isinstance(body, (bytes, bytearray)):
            raw_body = bytes(body)
        elif body is None:
            raw_body = b""
        else:
            raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        # 发送路径整体串行：stream_id 递增 + 帧写出必须成对，交错会把两个请求的
        # 帧混进同一条流，表现为对端回包错乱。
        async with self._send_lock:
            stream_id = self.next_stream
            self.next_stream += 1
            self.responses[stream_id] = ProtocolResponse()
            # 上限必须在帧写出**之前**登记：接收任务是独立协程，一旦放帧出去，
            # 第一批回包可能先于本行执行就被收下。
            if max_bytes is not None:
                self._stream_limits[stream_id] = max_bytes
            request = ApplicationRequest(
                verb=verb.upper(),
                path=path,
                headers=self._build_headers(headers, body is not None),
                body=raw_body,
                end_body=end_body,
            )
            frame = ServiceFrame(stream_id=stream_id, request=request)
            service = ServiceRequest(
                service=ServiceType.DAEMON, payload=frame.encode()
            )
            try:
                if self._disconnect_reason is not None:
                    raise ProtocolError("Noise 连接已失效，需重新握手")
                # _pack consumes cipher nonces before network writes. If a write
                # fails or is cancelled, delivery is ambiguous: never replay here.
                for encrypted in self._pack(service):
                    await self._ws_send(encrypted)
            except BaseException:
                self.responses.pop(stream_id, None)
                self._stream_limits.pop(stream_id, None)
                self._disconnect_reason = "Noise 发送中断，需重新握手"
                self._handshake_done = False
                self._fail_waiters(ProtocolError(self._disconnect_reason))
                raise
            return stream_id

    async def request(
        self,
        verb: str,
        path: str,
        body: dict | bytes | None = None,
        *,
        headers: dict | None = None,
        timeout: float | None = None,
        end_body: bool = True,
        max_bytes: int | None = None,
    ) -> ProtocolResponse:
        """跑一次 HTTP-over-Noise 并等它结束。

        兼容形态：`protocol_engine.py:24` 的鸭子契约是
        `request(method, path, body, timeout)`（四个位置参），所以 body 放在
        `*` 之前；规格里的关键字写法同样可用。

        `timeout` / `max_bytes` 未显式给出时按 `route_limits.limits_for()` 取逐路由
        预算：上游路由表给 `/api/dyi/export` 65 min、`/chat/subscribe` 30 s，
        固定 120 s 对前者是假失败、对后者是白等。订阅流（`end_body=False`）不登记
        响应体上限——它的 body 设计上持续增长。
        """
        limits = limits_for(verb, path)
        budget = limits.timeout if timeout is None else timeout
        cap = None if not end_body else (max_bytes or limits.max_bytes)
        stream_id = await self._send(
            verb, path, body, headers=headers, end_body=end_body, max_bytes=cap
        )
        try:
            response = await self._wait_end(stream_id, budget)
        except BaseException:
            self.responses.pop(stream_id, None)
            raise
        finally:
            if end_body:
                self._stream_limits.pop(stream_id, None)
        if not end_body:
            # 长连接订阅流不归本函数管：它的 body 会一直长大。
            return response
        self.responses.pop(stream_id, None)
        if response.limit_exceeded is not None:
            raise ProtocolError(
                f"{path} 响应超过上限（{response.limit_exceeded} 字节，"
                f"已收 {len(response.body)} 字节）：拒绝继续累积"
            )
        if response.reset is not None:
            raise ProtocolError(
                f"{path} 被重置（code={response.reset['code']} "
                f"reason={response.reset['reason']}）"
            )
        return response

    async def _request_json(
        self, verb: str, path: str, body: dict | None, timeout: float
    ) -> dict:
        response = await self.request(verb, path, body, timeout=timeout)
        if response.status != 200:
            # 只报 `HTTP 400` 等于零信息：真机 register-capabilities 的 400 body 才写着
            # `invalid type: string "chat_cancel", expected a map`。把服务端 body 压成
            # 一行片段拼进异常，排障时不必再抓一次包。
            raise ProtocolError(
                f"{path} HTTP {response.status}：{_response_snippet(response)}"
            )
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(
                f"{path} 的响应不是 JSON：{_response_snippet(response)}"
            ) from exc
        return payload if isinstance(payload, dict) else {"result": payload}

    # ------------------------------------------------------------ RPC 面
    async def cancel_chat(
        self, *, session_id: str | None = None, timeout: float = 30.0
    ) -> dict:
        """Explicit POST /chat/cancel; never implicitly cancel on disconnect.

        Static frontend evidence: 24thn7in_976_.js chat.cancel/a2 assembles
        {} for the main route, {session_id} for a thread. It does NOT send node_id.
        Route mapping in 3qzcufyl9d94_.js is noiseOnly POST /chat/cancel.
        A main-route cancellation may affect other clients sharing that session:
        callers must choose the scope explicitly, not infer it from a device ID.
        cancelled=False is a legitimate no-op, not evidence of a successful stop.
        """
        body: dict[str, str] = {}
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                raise ProtocolError("取消会话的 session_id 必须是非空字符串")
            body["session_id"] = session_id
        result = await self._request_json("POST", "/chat/cancel", body, timeout)
        if not isinstance(result.get("cancelled"), bool):
            raise ProtocolError("chat.cancel 响应缺少布尔 cancelled，无法确认取消结果")
        return result

    async def register_capabilities(self, caps: list[str] | None = None) -> dict:
        """POST /client/register-capabilities：body 只有 `client_id`。

        必须在 /chat/subscribe 之前发：没有它，服务端会把推流事件路由给
        "最后一次注册的 client_id"（往往就是浏览器那个），于是本协议客户端
        永远等不到事件。`client_id` 与后面 /chat/stream 的 `node_id`
        必须是同一个 UUID。

        为什么只发 client_id（2026-11-11 真机两组对照，这是唯一权威）：
          * 旧 body 把 capabilities 发成**字符串数组** → 400：
            `{"ok":false,"error":{"code":"invalid_params","message":"parse JSON:
            invalid type: string \\"chat_cancel\\", expected a map at line 1 column 145"}}`
            —— capabilities 的元素必须是 map/object，不是字符串。
          * 去掉 capabilities、只留 `{"client_id": "<node_id uuid>"}` → **200**：
            `{"accepted_data_sources":0,"accepted_device_commands":0,
            "accepted_hatch_app_commands":0,"capabilities":
            ["video_file_ref_ingest","voice_user_speech_status"],"rejected_ids":[]}`。
        所以这里发的是那次 200 的**逐字最小 body**。`caps` 参数保留只为兼容既有
        调用点，**不会**上线路 —— 把字符串 capability 发回去就是重演那个 400。

        另记一条实测：`GET /client/register-capabilities` → 403
        `{"error":{"code":"forbidden","message":"matched route missing from endpoint ACL"}}`，
        说明 ACL 只放行 POST。本方法只用 POST，这条只作排障背景。
        """
        result = await self._request_json(
            "POST", "/client/register-capabilities", {"client_id": self.node_id}, 30.0
        )
        if result.get("ok") is False:
            raise ProtocolError(
                f"/client/register-capabilities 未成功：{result.get('error') or result}"
            )
        return result

    async def register_node(self, node_id: str) -> dict:
        """POST /api/nodes/register。没有它，侧聊线程的事件不会投递给本连接。"""
        self.node_id = node_id
        return await self._request_json(
            "POST",
            "/api/nodes/register",
            {
                "node_id": node_id,
                "display_name": DISPLAY_NAME,
                "platform": PLATFORM,
                "commands_v2": {"ping": {"description": "Connection liveness check"}},
            },
            30.0,
        )

    async def subscribe(
        self,
        *,
        after_stream_seq: int | None = None,
        after_chat_event_seq: int | None = None,
    ) -> int:
        """打开 /chat/subscribe 长连接流，返回 stream_id。

        真机 2026-11-11 两条硬事实（改之前先看完）：
          * 请求体必须**完整发送**（`end_body=True`）+ 空 body `{}`。旧实现的
            `end_body=False` 让服务端**永远不回**响应头，现象就是
            `ProtocolError: 等待 stream N 响应头超时`；改成 `end_body=True` + `{}`
            后立刻 200（`content-type: application/x-ndjson`）。
          * 这条流**永不结束**：200 之后 body 一行一条 NDJSON 地持续增长，首行是
            `{"id":"http-subscribe-...","type":"res","status":"ok",
            "payload":{"subscribed":true,...}}`。
        所以这里只等响应头、把 stream_id 记下来、把增量行游标归零；真正的按行消费
        在 read_stream 里做（按换行切、逐行 json.loads）。

        resume（`after_stream_seq` / `after_chat_event_seq`，证据见
        `RESUME_EVIDENCE`）：不传时 body 保持**真机实测过**的 `{}`；传了才加
        这两个字段。字段名与"已消费 seq 的单调最大值"取自前端静态锚点，语义
        （严格大于）是推断，服务端是否接受这两字段未证实 —— 所以调用方
        （`_resume_subscription`）必须在被拒时降级成普通订阅，不能把重连押在它身上。
        没有顺手把前端的 `capabilities`（字符串数组）抄进来：真机 subscribe
        实测过的 body 只有 `{}`，多塞未验证的字段是在重演 register 那次 400 的风险。
        """
        body: dict = {}
        if after_stream_seq is not None:
            body["after_stream_seq"] = _resume_cursor(after_stream_seq)
            body["after_chat_event_seq"] = _resume_cursor(after_chat_event_seq)
        stream_id = await self._send("POST", "/chat/subscribe", body, end_body=True)
        try:
            headers = await self._wait_headers(stream_id, 30.0)
        except BaseException:
            self.responses.pop(stream_id, None)
            raise
        if headers.status != 200:
            detail = _response_snippet(headers)
            self.responses.pop(stream_id, None)
            raise ProtocolError(f"chat.subscribe HTTP {headers.status}：{detail}")
        self._sub_stream_id = stream_id
        self._scan_offset = 0
        return stream_id

    async def send_action(self, text: str) -> None:
        """POST /chat/stream 发一条人类消息，等它的同步 ack。

        真机 2026-11-11：body
        `{"message","node_id","capabilities":[],"timezone":"Asia/Shanghai"}` → 200，
        ack `{"channel":"main","is_thread":false,"message_id":"<uuid>",
        "reply_to_message_id":"<uuid>","session_id":"<uuid>"}`
        （实测 `message_id` 与 `reply_to_message_id` 相同）。`node_id` 必须等于
        register-capabilities 用的 client_id，否则事件会被路由到别的 client。

        偏离记录：规格写的是 `kind:"action"`，但观测到的 ACL 语义是 `/chat/send`
        只接受 kind=action 且它是 agent 动作调用（消息不会出现在聊天里、也拿不到
        人类回答），人类输入走 `/chat/stream`。旧实现还回填 `session_id` /
        `metadata` 进 body —— 那是从浏览器抓包推的，没有那一次真机的对照证据，
        已删除；`session_id` 仍从 ack 记下来供上层恢复会话。
        """
        await self._post_message(text, None, self.node_id)

    async def _post_message(
        self, text: str, references: list[dict] | None, node_id: str
    ) -> dict:
        """发 /chat/stream 并记下 ack 里的 session_id；返回完整 ack。"""
        payload = await self._request_json(
            "POST", "/chat/stream", self._stream_body(text, references, node_id), 60.0
        )
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            self.session_id = sid
        return payload

    def _stream_body(
        self, prompt: str, references: list[dict] | None, node_id: str
    ) -> dict:
        """`/chat/stream` 的请求体：形状取自 2026-11-11 真机 200 的那一次。

        实测四个字段就够：`message`、`node_id`、`capabilities: []`、`timezone`。
        为什么 capabilities 是空数组而不是 `self.capabilities`：字符串 capability
        在 register-capabilities 已被服务端按 map 解析而 400（`expected a map`），
        同一个推断错误不该在第二条请求里再犯一次；真机那次发的就是 `[]`。
        """
        body: dict = {
            "message": prompt,
            "node_id": node_id,
            "capabilities": [],
            "timezone": self.timezone,
        }
        if references:
            # 带附件时 body 形状取自 hatch-web 的**两处真实调用点**（静态逆向）：
            #   07hpppij0ssd9.js@67023  序列化：{type:"file_ref", mime_type, path, filename}
            #     其中 `path` 是 `uploadPath`（workspace 相对路径，**不带 sandbox://**）；
            #   24thn7in_976_.js@97321  发送：{items:[...file_ref], node_id, capabilities, …}
            #     文本项也走 items（`{type:"text", text}`），这一形态**不带 message**。
            # 2026-11-11 真机反证：把 fs_write 返回的 `sandbox://…` 塞进 file_ref.path，
            # 服务端回 500 "failed to resolve file_ref path sandbox:/workspace/…"（单斜杠、
            # 路径不存在）——说明它按 uploadPath 解析，不接受 URI 前缀。因此：
            #   * text-only 继续用 message（真机 200 已验）；
            #   * 带附件改成 items（text item + file_ref item），path 用裸 workspace 路径。
            body.pop("message", None)
            body["items"] = [{"type": "text", "text": prompt}, *references]
        return body

    async def _ensure_preamble(self, node_id: str) -> None:
        """register-capabilities 与 subscribe 必须就位（见方法内注释）。

        顺序不能换：先注册 client_id，再订阅；反过来的话服务端会把本回合的事件
        路由到"最后一次注册的 client"（往往是浏览器那个），本连接永远等不到事件。
        """
        if self._sub_stream_id is not None:
            return
        if node_id != self.node_id:
            await self.register_node(node_id)
        await self.register_capabilities(list(self.capabilities))
        await self.subscribe()

    async def _resume_subscription(self, *, resume_after: int) -> bool:
        """断线后：同 client 重新握手 → 重声明 client_id → 带 seq 游标重订阅。

        返回 True = 已恢复（调用方继续从新的订阅流消费）；False = 客户端正在
        关闭，调用方应直接收尾（关闭期间不重连，见 close()）。

        三层证据（`RESUME_EVIDENCE` 是机器可读的那份，这里写为什么这么做）：
          1. 重连本身的每个环节（session 续签 / token / Noise XX / subscribe）
             都**真机实测过**；"重连后服务端会继续推事件"**未证实**。
          2. resume 字段名与"已消费 seq 的单调最大值"来自**前端静态锚点**；
             "严格大于"是**推断**。所以带字段的订阅被拒时降级成普通 `{}`
             订阅（真机实测 200 的形态）+ 调用方按 seq 本地去重，不把整条
             重连打死。
          3. 重注册 `client_id` 的理由：事件按最后一次注册的 client_id 路由
             （§1.6 第 3 条）；重连后的新 Noise 会话要不要重新声明**未证实**，
             但这一步是幂等 POST、代价一个 RTT，漏掉它的失败模式（事件被路由
             到浏览器那个 client，本连接永远等不到）比多发一次更糟。
        """
        if self.closing:
            return False
        if not self.reconnect_enabled:
            raise ProtocolError(
                f"连接已断开且未启用重连：{self._disconnect_reason or '原因未知'}"
            )
        stale = self._sub_stream_id
        # This subscription belongs to the dead connection. Reclaim it even if
        # reconnect/auth ultimately fails; unrelated one-shot responses stay owned
        # by their request callers. Stream ids remain monotonic across reconnect.
        if stale is not None:
            self.responses.pop(stale, None)
        reason = self._disconnect_reason or "连接已失效"
        last_error = "未发起"
        while self._reconnect_count < self.max_reconnects:
            if self.closing:
                return False
            self._reconnect_count += 1
            # 指数退避，封顶；测试把 reconnect_backoff 设 0 即可零等待。
            delay = min(
                self.reconnect_backoff * (2 ** (self._reconnect_count - 1)),
                self.reconnect_backoff_max,
            )
            if delay > 0:
                await asyncio.sleep(delay)
            if self.closing:
                return False
            try:
                # connect() 自己负责摘旧连接 + 完整 bootstrap（复用 token/cookie
                # 续期路径，不是另写一套握手）。
                await self.connect()
                await self.register_capabilities(list(self.capabilities))
                try:
                    await self.subscribe(
                        after_stream_seq=resume_after,
                        after_chat_event_seq=self._last_seen_chat_seq,
                    )
                except ProtocolError as exc:
                    # 两段式降级：服务端不认 resume 字段时，退回真机实测过的
                    # `{}` 订阅。响应头 body 可能已同时带有旧事件和新尾部，
                    # 不能 seek 到末尾丢掉新事件；调用方始终按重连前固定的
                    # resume_floor 过滤，和回包到达时机无关。
                    last_error = f"resume 订阅被拒（{exc}）"
                    await self.subscribe()
                return True
            except ProtocolAuthError:
                # 401 / 续签失败是终态：重试只会把同一个 401 再打两遍，还会把
                # 调用方 deadline 烧掉。原样抛给上层（它本来就区分得了这一类）。
                raise
            except ProtocolError as exc:
                last_error = str(exc)
                await self._drop_connection()
        raise ProtocolError(
            f"断线重连失败（已尝试 {self._reconnect_count}/{self.max_reconnects} 次）："
            f"{reason}；最后一次：{last_error}"
        )

    async def read_stream(
        self,
        prompt: str | None = None,
        references: list[dict] | None = None,
        *,
        node_id: str | None = None,
        timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> AsyncIterator[dict]:
        """把订阅流里的 NDJSON 事件逐条变成 dict 吐出，**回合结束就返回**。

        两种调用姿势（都要支持）：
          * 规格形态 `read_stream()` —— 纯读已建立的订阅，不补前奏、不发消息；
            但**收尾条件与引擎形态相同**（见下），读到回合结束就返回。
          * 引擎形态 `read_stream(prompt, references, node_id=..., timeout=...)`
            （protocol_engine.py:25 的鸭子契约）—— 先补齐 register + subscribe，
            再发这条消息，然后吐事件。引擎自己不碰 /chat/*，前奏只能在这里做。

        事件字典的对外契约：保留 NDJSON 原始键（`event`/`payload`/`seq`/`ts_ms`/
        `type`）+ `name`，另加两个短键：
          * `kind == "delta"` 且带 `text`：助手正文增量（`delta.text_append.payload.text`）；
          * `kind == "status"`：状态类事件（`res`/`agent.status`/`task.status`/
            `message.user`/`delta.message_start` …），**不带 `text`**，正文不会重复；
          * 其它未知事件 `kind == "event"`。
        这样 `protocol_engine.py` / `protocol_generation.py` 的鸭子契约（按
        `event`/`name`/`payload` 读）不受影响，调用方也不必自己判断 event/type 先后。

        结束条件（多条件，单信号靠不住）见 `_turn_ended`：必须**已收到过正文**，
        且命中 `agent.status` 回到非 working/responding、或 `task.status` 回到
        completed、或旧的 `delta.message_done`。

        `timeout`（秒）：给了就是本回合的读截止。到点**停止迭代**（return，而不是
        抛超时异常）——"没等到"的裁决留给上层引擎，避免这里再抛一个与它重复的
        异常类型；也避免流静默时 `async for` 永远挂住。不给就一直等。

        `idle_timeout`（秒）：**连续这么久没有任何新字节**就停止迭代（return），
        与 `timeout`（总时长）是两把不同的尺子。默认 `None` = 不启用，行为与旧版
        完全一致。为什么要它：一条僵死的订阅会一直安静地等到总 deadline；总
        deadline 往往按最长任务给（导出类 65 min），中间卡死就是几十分钟空转。
        判定只认"body 有没有长新东西"，`_wait_update` 的 0.5 s 正常空转不算。

        断线（0.4.x，离线实现，真机未验收）：接收循环死掉 / ws 句柄没了时，本
        方法**原地**重连（`_resume_subscription`：同 client 重新握手 → 重注册 →
        带已消费 seq 重订阅）并从新流继续消费，公开契约（签名 / 事件形状 /
        `_turn_ended` 多条件收尾）一条不改。保证与不保证分开写：
          * 保证：已消费的事件不会二次吐（按 `seq <= resume_floor` 本地裁剪；
            无 seq 的事件只能信服务端，见 `RESUME_EVIDENCE`）；
          * 保证：断开前没消费到的尾部事件（收尾 status 等）会被重放送到，
            `_turn_ended` 因而仍能正常收尾；
          * 保证：重连有上限（每回合 `max_reconnects` 次，指数退避封顶），
            超限抛 `ProtocolError` 并带上"最后一次失败原因"，不无限重试；
          * 不保证：真机上服务端是否接受 resume 字段、是否真的补发断开期间的
            事件 —— 没验证过。带字段被拒会降级为普通订阅 + 本地去重。

        为什么自己按 offset 增量扫：subscribe 是单条长流，服务端把事件以换行分隔
        追加进同一个 stream_id 的 body，一次 recv 可能带来半行、一行或很多行；
        不等换行就吐事件会截断 JSON，每次 recv 当整行处理则会丢事件。
        """
        # 本回合的重连预算：每次 read_stream 重新给（也覆盖前奏阶段的修复），
        # 避免一次抖动把后续回合的额度提前烧掉；但同一回合内成功恢复也不重置 ——
        # 一直抖的连接最终会撞上限抛错，而不是无限循环。
        self._reconnect_count = 0
        self._idle_timeout_hit = False
        self._turn_complete = False
        # Keep the replay floor fixed at the last accepted cursor even when the
        # reconnect happens during preamble rather than inside the read loop.
        resume_floor = -1
        skip_ack = False
        if prompt is not None:
            target = node_id or self.node_id
            # 已存在的订阅才需要"只看本轮增量"：长连接跨回合，上一回合没消费完的
            # 尾巴（尤其上一轮 assistant 的 delta）会被重新吐出来当成本轮回答。
            # 刚建立的订阅本来就没有历史，游标就是 0。
            reused = self._sub_stream_id is not None
            existing = self._sub_stream_id
            if reused and existing is not None:
                # 先推进游标（含"跳过的尾巴"的 seq）：这两个值就是断线重连时要
                # 报给服务端的 `after_stream_seq`。
                self._seek_to_end(existing)
            if self._ever_connected and self._connection_lost():
                # 空闲期就断了（上一回合结束后服务端/代理把 WS 关了）：先把连接
                # 修好再发消息。这里**不**重发 /chat/stream —— 它没有幂等键，
                # 发送失败后盲目重发会把一次提问变成两条。
                resume_floor = self._last_seen_seq
                if not await self._resume_subscription(resume_after=resume_floor):
                    return
                skip_ack = True
            else:
                try:
                    await self._ensure_preamble(target)
                except ProtocolError:
                    # 前奏里撞上死连接：修连接后重放前奏（register/subscribe 幂等）。
                    # 从未连过的 client 不走这里 —— 那该报"尚未连接"，而不是
                    # 悄悄替调用方建一条连接。
                    if not (self._ever_connected and self._connection_lost()):
                        raise
                    resume_floor = self._last_seen_seq
                    if not await self._resume_subscription(resume_after=resume_floor):
                        return
                    skip_ack = True
            await self._post_message(prompt, references, target)

        if self._sub_stream_id is None:
            raise ProtocolError("先调用 subscribe()（或带 prompt 调用 read_stream）")
        poll = min(0.5, timeout) if timeout else 0.5
        deadline = time.monotonic() + timeout if timeout else None
        saw_text = False
        # idle 判定的基准：最后一次"body 真的长了"的时刻。轮询空转不刷新它。
        last_progress = time.monotonic()
        last_size = -1
        # Replay uses the fixed pre-reconnect floor, not the moving maximum:
        # out-of-order fresh events above the floor remain deliverable. The first
        # valid event may be a subscription ack, which is not a turn event.
        while not self.closing:
            if deadline is not None and time.monotonic() >= deadline:
                return
            if self._sub_stream_id is None:
                raise ProtocolError("订阅流已被丢弃")
            if self._connection_lost():
                resume_floor = self._last_seen_seq
                if not await self._resume_subscription(resume_after=resume_floor):
                    return
                skip_ack = True
                continue
            stream_id = self._sub_stream_id
            response = self.responses.get(stream_id)
            if response is None:
                raise ProtocolError("订阅流已被丢弃")
            if response.reset is not None:
                raise ProtocolError(
                    f"订阅流被重置（code={response.reset['code']} "
                    f"reason={response.reset['reason']}）"
                )
            raw = bytes(response.body)
            if len(raw) != last_size:
                # 有新字节（哪怕只是半行）就算"流还活着"。
                last_size = len(raw)
                last_progress = time.monotonic()
            cut = raw.rfind(b"\n", self._scan_offset)
            if cut >= 0:
                # Commit each complete line before yielding or discarding it.
                # A consumer can aclose() while paused at yield; batch-end-only
                # offsets would replay that already delivered event on the next read.
                for line in raw[self._scan_offset : cut + 1].split(b"\n")[:-1]:
                    self._scan_offset += len(line) + 1
                    event = self._parse_event(line)
                    if event is None:
                        continue
                    if skip_ack:
                        skip_ack = False
                        if str(event.get("name") or "") == "res":
                            continue
                    if resume_floor >= 0:
                        seq = _seq_of(event.get("seq"))
                        if seq is not None and seq <= resume_floor:
                            # 服务端把已消费的事件又发了一遍（或按包含游标重放）：
                            # 本地挡住 —— 正文/状态都不会重复。
                            continue
                    # Only accepted events advance resume cursors. An old replay
                    # can carry a different chat_event_seq and must not move the
                    # second cursor past data that this consumer never applied.
                    self._track_cursors(event)
                    payload = event.get("payload")
                    context = payload.get("chat_context") if isinstance(payload, Mapping) else None
                    device = context.get("device_id") if isinstance(context, Mapping) else None
                    ours = device is None or str(device) == (node_id or self.node_id)
                    if self._decorate_event(event) and ours:
                        saw_text = True
                    reason = self._turn_ended(event, saw_text) if ours else None
                    # Set before yielding: a consumer may stop at this event.
                    if reason is not None:
                        self._turn_complete = True
                    yield event
                    if reason is not None:
                        return
                self._scan_offset = cut + 1
                if self._scan_offset > 4 * 1024 * 1024:
                    # 长连接的 body 会无限增长：丢掉已消费的前缀，否则一个号跑一天
                    # 就能把内存吃光。
                    self._trim_response(stream_id, self._scan_offset)
                continue
            if response.ended:
                # 服务端把订阅流关了（response 帧 end_body / 连接结束）：已消费的
                # 前缀算完就结束，不要对着一条死流空转到 closing。
                return
            try:
                await self._wait_update(stream_id, poll)
            except ProtocolError:
                # 接收循环死掉时 _fail_waiters 会把异常塞给正在等的 future：
                # 这是"连接级断线"，交给下一轮的重连分支，不要把异常直接抛给
                # 调用方（那会让一次可恢复的抖动变成一次失败生成）。
                if not self._connection_lost():
                    raise
            if idle_timeout and time.monotonic() - last_progress >= idle_timeout:
                # 连续 idle_timeout 秒没有新字节：判定这条订阅僵死，结束本回合。
                # 用 return 而不是 raise —— 与 `timeout` 的语义保持一致（"没等到"
                # 由上层裁决），同时留一条可诊断的痕迹。
                self._idle_timeout_hit = True
                logger.warning(
                    "订阅流 idle 超时（%.1fs 无新字节，已收 %d 字节），结束本回合",
                    idle_timeout,
                    len(response.body),
                )
                return

    def _seek_to_end(self, stream_id: int) -> None:
        """把行游标挪到当前 body 末尾：新一轮只看本轮增量（见 read_stream 注释）。

        顺手把跳过区间里的事件 seq 记进 resume 游标：对服务端而言"跳过"也是
        已经消费（本回合不吐，但重连时不该再要求它补发），游标必须跟上。
        """
        response = self.responses.get(stream_id)
        if response is None:
            return
        raw = bytes(response.body)
        for line in raw[self._scan_offset :].splitlines():
            event = self._parse_event(line)
            if event is not None:
                self._track_cursors(event)
        self._scan_offset = len(response.body)

    def _track_cursors(self, event: Mapping) -> None:
        """把"已消费到哪一条"记成两个单调最大值（只增不减）。

        锚点：前端在每条事件处理里 `a0(t.seq??e.seq)` / `a1(e.chat_event_seq)`，
        更新函数是 Math.max（见 RESUME_EVIDENCE）；订阅时再把这两个 ref 当
        `after_stream_seq` / `after_chat_event_seq` 传回。这里照同一套算：
        父事件可能没有 chat_event_seq（真机样本就没有），那就只更新 seq。
        """
        seq = _seq_of(event.get("seq"))
        if seq is not None and seq > self._last_seen_seq:
            self._last_seen_seq = seq
        chat_seq = _seq_of(event.get("chat_event_seq"))
        if chat_seq is not None and chat_seq > self._last_seen_chat_seq:
            self._last_seen_chat_seq = chat_seq

    @staticmethod
    def _decorate_event(event: dict) -> str:
        """补 `kind`/`text` 两个对外短键，返回本条事件的正文增量（状态事件为空串）。

        只补键、不重写：`protocol_engine.py` 与 `protocol_generation.py` 都按
        `event`/`name`/`payload`/`seq` 读事件（鸭子契约），把原始键换掉会静默打断
        两份引擎。正文只从 `delta.text_append.payload.text` 取，状态类事件一律
        不带 `text`，所以同一段回答不会被吐两次。
        """
        name = str(event.get("name") or "")
        payload = event.get("payload")
        inner = payload if isinstance(payload, dict) else {}
        if name == _TEXT_DELTA_EVENT:
            text = str(inner.get("text") or "")
            event["kind"] = "delta"
            event["text"] = text
            return text
        event["kind"] = "status" if name in _STATUS_EVENTS else "event"
        return ""

    @staticmethod
    def _turn_ended(event: Mapping, saw_text: bool) -> str | None:
        """这一回合结束了吗？结束返回原因串（排障用），否则 None。

        多条件，因为真机那个序列里没有单一可靠信号：
          1. 必须**已经收到过正文**。实测 `agent.status(online)` 与
             `task.status(completed)` 会在 `message.user` / 正文**之前**出现，
             只看状态就会在开头立刻把流关掉、把回答丢掉。
          2. `agent.status` 的 `activity_code` 明确回到 online/idle
             —— 空值或未知状态不能作为正常完成的证据。
          3. `task.status` 的 `status` 回到 completed。
          4. `delta.message_done` —— 旧模型假设里唯一的结束信号，真机序列没出现；
             保留它是因为老路径与引擎契约还指望它（收到正文后仍按它收尾）。
        什么都不命中就继续读，直到 `timeout` 截止；"这一回合没正文"的兜底判定
        留给上层（本函数不猜）。
        """
        if not saw_text:
            return None
        name = str(event.get("name") or "")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return None
        if name == "agent.status":
            code = str(payload.get("activity_code") or "").strip().lower()
            if code in ("online", "idle"):
                return f"agent.status:{code}"
        elif name == "task.status":
            status = str(payload.get("status") or "").strip().lower()
            if status == "completed":
                return "task.status:completed"
        elif name == "delta.message_done":
            return "delta.message_done"
        return None

    def _trim_response(self, stream_id: int, consumed: int) -> None:
        response = self.responses.get(stream_id)
        if response is None or consumed <= 0:
            return
        del response.body[:consumed]
        self._scan_offset -= consumed

    @staticmethod
    def _parse_event(line: bytes) -> dict | None:
        """一行 NDJSON -> 事件 dict。坏行跳过，不杀整条流。

        保留原始键（event/payload/seq/ts_ms/type），因为下游按这些键读；
        额外补一个 `name`，让本模块的调用方不必关心 event 与 type 的先后。
        `kind`/`text` 由 `_decorate_event` 在 read_stream 里补，本函数只负责解析。
        """
        if not line.strip():
            return None
        try:
            payload = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        name = payload.get("event") or payload.get("type") or ""
        event = dict(payload)
        event["name"] = str(name)
        event.setdefault("payload", {})
        return event

    async def session_quota(self, timeout: float = 20.0) -> dict:
        """外层 HTTP `/api/session` 快照：额度/VM 状态**唯一**可读路径。

        为什么不是内层：2026-11-11 真机把 Noise 内层 `GET /api/session` 打出来是
        404 `{"code":"not_found","message":"route not found"}`（证据
        `docs/PROTOCOL_QUOTA.md` 候选表 + `work/canvas_re/protocol-route-dump/`）；
        418 内层方法白名单里也没有任何 usage/quota/billing 命名。真正能读
        `/api/session` 的是本模块 `connect()` 用的外层 `renew_session`（带
        `hatch_sess` cookie 的 HTTP）。`protocol_engine.quota()` 过去走内层
        `_request("GET", "/api/session")`，真机上必然 404 —— 本方法把它接回外层。

        返回结构与引擎 quota 兼容（`renewable`/`vm_state`/`estimated_expiry_at`）；
        `raw` 保留本轮响应快照，`expires` 里的 hatch_vml 到期时间单独提出来。
        """
        client = await self._ensure_http()
        try:
            session = await renew_session(
                self.cookies, site=self.site, client=client, timeout=timeout
            )
        except SessionError as exc:
            raise _map_session_error(exc) from None
        # /api/session 会 Set-Cookie 续期（hatch_vml 约 48h）：合并回客户端，
        # 否则下一次握手/请求还带旧 cookie，续签白做。
        self.cookies = dict(session.cookies)
        self._last_session = session
        expiry: int | None = None
        for name, value in session.expires.items():
            if "vml" in str(name).lower():
                try:
                    expiry = int(value)
                except (TypeError, ValueError):
                    expiry = None
                break
        return {
            "renewable": True,
            "vm_state": session.vm_state or "",
            "estimated_expiry_at": expiry,
            "vm_id": session.vm_id,
            "status": session.status,
            "http_status": session.http_status,
            "raw": dict(session.raw),
        }

    async def ping(self, presence: str = "active") -> dict:
        """POST /api/ping：心跳，也是"这条连接还活着"最便宜的证据。"""
        return await self._request_json(
            "POST", "/api/ping", {"presence": presence}, 15.0
        )

    def media_candidates(self, payload: object = None) -> list[dict]:
        """从事件 payload 里挑出生成物引用（只认 sandbox://workspace/...）。

        兼容形态：引擎会以 `media_candidates(node_id)` 调用（protocol_engine.py:28）。
        本客户端不反向列举服务端资源，所以传字符串/None 时返回空列表 —— 事件流
        里的提取由引擎自己做，这里不能假装有一个不存在的列举端点。
        """
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = json.loads(bytes(payload))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ProtocolError("media_candidates 的字节输入不是 JSON") from exc
        if not isinstance(payload, (dict, list)):
            return []
        return collect_media_candidates(payload)

    # ------------------------------------------------------------ 文件系统
    async def fs_stat(self, path: str) -> dict:
        return await self._request_json("POST", "/fs/stat", {"path": path}, 40.0)

    async def fs_rename(self, src: str, dst: str) -> dict:
        return await self._request_json(
            "POST",
            "/fs/rename",
            {
                "source": src,
                "destination": dst,
                "create_parent": True,
                "overwrite": True,
            },
            60.0,
        )

    async def fs_delete(self, path: str) -> dict:
        return await self._request_json(
            "POST", "/fs/delete", {"path": path, "recursive": False}, 30.0
        )

    async def fs_read(
        self, path: str, *, offset: int = 0, length: int = _FS_READ_CHUNK
    ) -> bytes:
        """从 sandbox 读整个文件：POST /fs/read 分页到 eof + 每页长度校验。

        模块头第 3 条：结果不是普通 HTTP body，而是一页一页 JSON 里的 base64。
        只取第一页会拿到截断文件，所以 eof 之前必须一直取；单页 0 字节却没置
        eof 是异常形态（否则就是死循环），直接报错而不是空转。
        """
        remote = self._require_workspace_path(path)
        output = bytearray()
        cursor = int(offset)
        chunk_size = max(1, int(length))
        while True:
            payload = await self._request_json(
                "POST",
                "/fs/read",
                {"path": remote, "offset": cursor, "len": chunk_size},
                60.0,
            )
            result = payload.get("result", payload)
            if not isinstance(result, dict):
                raise ProtocolError("fs.read 的 result 不是对象")
            chunk = _decode_fs_chunk(result)
            output.extend(chunk)
            if result.get("eof"):
                return bytes(output)
            if not chunk:
                raise ProtocolError("fs.read 返回 0 字节但未置 eof，已停止避免死循环")
            cursor += len(chunk)

    async def fs_write(  # noqa: C901 - 两种调用姿势都要兼容，分支比抽象更诚实
        self,
        path_or_data: str | bytes,
        data_or_mime: bytes | str | None = None,
        filename: str | None = None,
        *,
        chunk: int = _FS_WRITE_CHUNK,
        mime: str | None = None,
    ) -> dict:
        """写文件：分片落临时名 -> stat 校验字节数 -> rename 到位 -> 返回 file_ref。

        两种调用姿势：
          * 规格形态 `fs_write(path, data, chunk=...)`。
          * 引擎形态 `fs_write(data, mime, filename)`（protocol_engine.py:26，
            等于 lichao 的 upload_file_ref）：目标路径由文件名派生，落在
            workspace/user/files 下 —— 参考素材**必须**在那里才会被会话读到。
        任一步失败都先 fs/delete 清掉临时文件（fail closed）：留半个文件在
        workspace 里，下一次同名上传会读到脏内容，比直接失败更难诊断。
        """
        if isinstance(path_or_data, (bytes, bytearray)):
            data = bytes(path_or_data)
            resolved_mime = str(data_or_mime or "") or mime
            remote = self._reference_path(filename or "reference.bin", resolved_mime)
        else:
            data = bytes(data_or_mime or b"")
            resolved_mime = str(mime or "")
            remote = self._require_workspace_path(path_or_data, allow_user=True)

        size = len(data)
        step = max(1, int(chunk))
        staging = posixpath.join(
            posixpath.dirname(remote) or "workspace", f".upload-{uuid.uuid4()}.part"
        )
        try:
            if size == 0:
                self._check_ok(
                    await self._request_json(
                        "POST",
                        "/fs/write",
                        {
                            "path": staging,
                            "data_base64": "",
                            "overwrite": True,
                            "append": False,
                            "create_parent": True,
                        },
                        90.0,
                    ),
                    "fs.write",
                )
            else:
                for index in range(0, size, step):
                    block = data[index : index + step]
                    self._check_ok(
                        await self._request_json(
                            "POST",
                            "/fs/write",
                            {
                                "path": staging,
                                "data_base64": base64.b64encode(block).decode("ascii"),
                                "overwrite": index == 0,
                                "append": index != 0,
                                "create_parent": index == 0,
                            },
                            90.0,
                        ),
                        "fs.write",
                    )
            stat = await self.fs_stat(staging)
            info = stat.get("result", stat)
            if not isinstance(info, dict) or info.get("kind") != "file":
                raise ProtocolError("fs.stat 没把临时文件认成文件")
            declared = info.get("size")
            # 0 字节是合法上传：`info.get("size") or -1` 会把 0 变成 -1，
            # 于是空文件永远失败，且报错文案自相矛盾（"声明 0，stat 说 0"）。
            if declared is None or int(declared) != size:
                raise ProtocolError(
                    f"上传大小不符：声明 {size}，服务端 stat 说 {info.get('size')}"
                )
            await self.fs_rename(staging, remote)
        except Exception:
            with contextlib.suppress(Exception):
                await self.fs_delete(staging)
            raise
        guessed, _ = mimetypes.guess_type(remote)
        return {
            "type": "file_ref",
            "path": f"sandbox://{remote}",
            "mime_type": resolved_mime or guessed or "application/octet-stream",
            "filename": posixpath.basename(remote),
            "size": size,
        }

    @staticmethod
    def _reference_path(filename: str, mime: str) -> str:
        """引擎形态上传的落点：workspace/user/files/<安全名>。"""
        safe = _safe_name(filename)
        if "." not in safe:
            ext = mimetypes.guess_extension(mime or "") or ".bin"
            safe = f"{safe}{ext}"
        return f"{_REFERENCE_DIR}/{safe}"

    @staticmethod
    def _check_ok(payload: dict, what: str) -> None:
        if payload.get("ok") is False:
            raise ProtocolError(f"{what} 未成功：{payload.get('error') or '无错误详情'}")

    @staticmethod
    def _require_workspace_path(path: str, *, allow_user: bool = False) -> str:
        """路径只认 workspace/，默认再排除 workspace/user/（用户上传区）。

        fs_write 用 allow_user=True：那是写入侧的合法目标，
        而"生成物"的判定必须排除它（模块里 normalize_media_path 的同一条规则）。
        """
        if not isinstance(path, str) or not path:
            raise ProtocolError("路径为空")
        remote = path[len("sandbox://") :] if path.startswith("sandbox://") else path
        if not remote.startswith("workspace/"):
            raise ProtocolError(f"路径必须位于 workspace/ 之下：{path!r}")
        if not allow_user and remote.startswith("workspace/user/"):
            raise ProtocolError(f"路径指向用户上传区，不是生成物：{path!r}")
        return remote

    # ------------------------------------------------------------ 关闭
    async def _teardown_ws(self) -> None:
        ws, self.ws = self.ws, None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()  # type: ignore[attr-defined]

    async def close(self) -> None:
        """幂等；没连过也能调（规格第 10 条测试钉住）。

        关闭是**终态**：`closing=True` 之后 `connect()` 会拒绝重握手，`read_stream`
        检测到 `_connection_lost()` 也不会重连（`_resume_subscription` 直接返回
        False 让迭代器干净收尾）。这里把重连相关的状态一并清掉，避免"已经关掉
        的 client"还留着旧订阅 id / 断线原因，下一次误用时报出误导性的错误。
        """
        self.closing = True
        tasks = [t for t in (self.receiver_task, self.heartbeat_task) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.receiver_task = self.heartbeat_task = None
        await self._teardown_ws()
        if self._http is not None and self._http is not self._injected_http:
            await self._http.aclose()
        self._http = None
        self._handshake_done = False
        self.send_cipher = self.recv_cipher = None
        self.responses.clear()
        for waiters in self.waiters.values():
            waiters.clear()
        self.waiters.clear()
        # 重连/订阅状态：关闭后一律归零（见 docstring）。
        self._sub_stream_id = None
        self._scan_offset = 0
        self._disconnect_reason = None
        self._reconnect_count = 0
        self._last_seen_seq = 0
        self._last_seen_chat_seq = 0

    # 让 with/async with 形态也能用，避免调用方漏 close 时留下悬空任务。
    async def __aenter__(self) -> HatchProtocolClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
