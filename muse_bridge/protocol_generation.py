r"""纯协议生成（不靠浏览器出图/出视频）—— Noise 通道里「生成」这条 RPC 的封装层。

本文件只做一件事：把已经破出来的生成链路照原样编排起来，并且**把没破出来的部分
显式标成 UNVERIFIED、调用即抛错**。理由很实际：协议层最怕的就是"看起来能跑、
其实发往一个不存在的端点"，那种代码会以 404/reset 的形式在很远的地方炸，
排查成本比现在多写几个 raise 高得多。

结论与验证等级（逐条证据，全部来自本地已落地的参考实现，未连真实 muse.ai）
--------------------------------------------------------------------------
[corroborated] 生成入口就是 `POST /chat/stream`，没有独立的生成 RPC。
    * lichao `protocol_engine.py:331` 组 body、`:336` 发出：
      `body = {"items": items, "node_id": node_id, "capabilities": {}}`
      → `await client.request("POST", "/chat/stream", body, 60)`
    * 本仓 `protocol_client.py:986-1002 _stream_body` + `:1036` 同一形态。
    * 反向证据：把 lichao 那套（protocol_client.py / protocol_engine.py / service.py /
      tasks.py / prompts.py / engine.py）里所有"以斜杠开头的字面量路径"全量 grep 出来，
      只有 `/api/ping`、`/chat/subscribe`、`/chat/stream`、`/fs/read|write|stat|rename|delete`、
      `/thread/new`、`/v1/media/`。**没有** `/gen/image`、`/gen/video`、`/image/generate`、
      `/video/generate`、`/api/hatch/generate`。
    * 图像还是视频，不是靠端点区分，而是靠**提示词文本 + 事件流里 resources 的 kind**
      （`resource_kind()`，protocol_client.py:228-238）。
[corroborated] `/chat/send` 不是生成入口：它只接受 `kind:"action"`，且是 agent 动作调用
    （消息不进聊天、也拿不到回答），人类输入走 `/chat/stream`
    —— protocol_client.py:970-984 的偏离说明（引 api-reference.md:97-98）。
[observed] 载荷里参考素材有两种形态，阈值写在 `should_use_file_ref()`：
    图片永远 inline；视频/其它按 10 MiB（有 `video_file_ref_ingest` 能力）或
    60 MiB（无该能力）转成 `{"type":"file_ref", path: "workspace/user/files/..."}`
    —— lichao `protocol_engine.py:101-111`、`:313-329`。
[corroborated] 成品**不是** HTTP body，也不是 CDN 直链，而是 VM(sandbox) 内文件：
    事件 `delta.presentation` 的 presentation/resources 信封里取
    `path|uri|image_path|video_path` → 只认 `sandbox://` 与 `workspace/` →
    `POST /fs/read {path, offset, len=1MiB}` 分页到 `eof`，每页 `data_base64` 并校验
    服务端声明的 `len` —— protocol_client.py:211-225（含"必须排除 workspace/user/
    用户上传区，否则成品会退化成回声一张输入图"的踩坑注释）、`:314-333`、`:1139-1168`；
    lichao `protocol_client.py:517-543`；完成标记 `download_via="protocol.noise.fs.read"`
    （lichao `protocol_engine.py:491`）。
[corroborated] 异步形态：`/chat/stream` 的响应只是**同步 ack**（message_id/session_id），
    真正的事件走 `/chat/subscribe` 那条 `end_body=False`、永不结束的流
    （protocol_client.py:944-968、lichao protocol_engine.py:336-349）。
    事件名集合：`delta.text_append` / `delta.presentation` / `delta.message_done` /
    `delta.tool_done` / `delta.placeholder` / `delta.message_removed` / `pool_error`，
    并按 `node_id`（device_id）做归属过滤 —— lichao protocol_engine.py:398-451。
    完成后的宽限窗口是 **12 秒**：done 之后 12s 仍没有对应 kind 的成品就放弃
    （lichao protocol_engine.py:480、:497-498）。
[hypothesis] 时长/比例/模型/seed 没有协议字段（seed 因此不设入口，见
    `gen_params.HYPOTHESIS_FIELDS`）。观测到的 body 只有
    `items / node_id / capabilities / metadata` 四个键，所以本模块把它们编进
    text item（与浏览器路径的做法一致：engine.py 的 build_video_prompt 也是写自然语言）。
    服务端是否另藏结构化参数，**未证实**；`capabilities` 是 dict 形态（lichao 传 `{}`），
    很可能就是协商位，但没证据。
[unverified] 配额扣减时点（发送时 vs 完成时）：语料里零证据。本模块不做任何扣减假设，
    只提供 `quota_observations()` 把"哪一步收到了 429/额度类错误"如实记下来。
[unverified] `https://muse.ai/files/<...>` 这类 CDN 直链：本仓库代码里没有任何引用，
    只有第三方 README 的说法，因此 `RetrievalMode.CDN` 走 raise，不假装能跑。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import posixpath
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .gen_params import PARAM_SPEC as _PARAM_SPEC
from .protocol_client import (
    ProtocolError,
    collect_media_candidates,
    normalize_media_path,
    resource_kind,
)

log = logging.getLogger("muse_bridge.protocol_generation")

# ---------------------------------------------------------------- 端点表

GENERATION_PATH = "/chat/stream"
SUBSCRIBE_PATH = "/chat/subscribe"
FS_READ_PATH = "/fs/read"
FS_WRITE_PATH = "/fs/write"
PING_PATH = "/api/ping"

#: 本轮点名要排查的候选生成端点 → 判定。**全部是排除结论**，不是可用路径。
#: 任何一条被调用都会抛 UnverifiedEndpointError，而不是悄悄发出去。
UNVERIFIED_ENDPOINTS: dict[str, str] = {
    "/gen/image": "全量路径字面量 grep 无命中（lichao 六文件），且 lichao 生成只走 /chat/stream",
    "/gen/video": "同上",
    "/image/generate": "同上",
    "/video/generate": "同上",
    "/api/hatch/generate": "muse.ai 域名下的 /api/hatch/* 只有 token/notary/lease-vm/vm-wake，无 generate",
    "/chat/send": "只接受 kind:'action' 的 agent 动作调用；人类输入与生成都走 /chat/stream",
}

# 事件名（证据：lichao protocol_engine.py:411-446）
EVENT_TEXT_DELTA = "delta.text_append"
EVENT_PRESENTATION = "delta.presentation"
EVENT_MESSAGE_DONE = "delta.message_done"
EVENT_TOOL_DONE = "delta.tool_done"
EVENT_PLACEHOLDER = "delta.placeholder"
EVENT_MESSAGE_REMOVED = "delta.message_removed"
EVENT_POOL_ERROR = "pool_error"

#: 出现即认为"这一轮已经交代完"的事件（presentation/message_done/tool_done/placeholder）
SETTLE_EVENTS = frozenset(
    {EVENT_PRESENTATION, EVENT_MESSAGE_DONE, EVENT_TOOL_DONE, EVENT_PLACEHOLDER}
)

EXPECT_IMAGE = "image"
EXPECT_VIDEO = "video"
EXPECTS = (EXPECT_IMAGE, EXPECT_VIDEO)

#: 桥自己的白名单（不是协议字段，只影响编进提示词的文本）。
#: 取值域直接取 `gen_params.PARAM_SPEC`——那是全仓唯一出处，避免 app.py 的
#: fal 入口与这里各写一套。服务端真正接受哪些值**未证实**（它不解析结构化参数）。
VIDEO_DURATIONS = tuple(_PARAM_SPEC["duration"].values)
ASPECT_RATIOS = tuple(_PARAM_SPEC["aspect_ratio"].values)

#: 参考素材转 file_ref 的阈值（lichao protocol_engine.py:97-98 原样搬）
FILE_REF_DEFAULT_BYTES = 10 * 1024 * 1024
FILE_REF_VIDEO_FALLBACK_BYTES = 60 * 1024 * 1024
VIDEO_FILE_REF_CAPABILITY = "video_file_ref_ingest"

#: ack 60s / done 后 12s 宽限 / fs.read 每页 1MiB（全部来自参考实现观测值）
ACK_TIMEOUT_SECONDS = 60.0
DONE_GRACE_SECONDS = 12.0
FS_READ_CHUNK = 1024 * 1024

MAX_GENERATION_ITEMS = 10  # 与 refs 上限一致：10 个素材 + 1 段文本


class GenerationError(ProtocolError):
    """生成流程失败（基类沿用 ProtocolError，上层已有的 except 不必改）。"""


class UnverifiedEndpointError(GenerationError):
    """有人在用一个**没有证据存在**的端点。带原因抛出，别让它变成一次 404。"""


class UnverifiedCapabilityError(GenerationError):
    """用到了本模块尚未证实的能力（例如 CDN 直链取件）。"""


# ---------------------------------------------------------------- 参数


@dataclass(frozen=True)
class Reference:
    """一份已判定合格的参考素材（通常来自 muse_bridge.refs.Reference）。"""

    data: bytes
    mime: str
    filename: str = ""

    @classmethod
    def coerce(cls, value: Any) -> Reference:
        """接受 Reference / (data, mime) / bytes / 带 .data 的任意对象。"""
        if isinstance(value, Reference):
            return value
        data = getattr(value, "data", None)
        if isinstance(data, (bytes, bytearray)):
            return cls(
                bytes(data),
                str(getattr(value, "mime", "") or ""),
                str(getattr(value, "filename", "") or ""),
            )
        if isinstance(value, (bytes, bytearray)):
            return cls(bytes(value), "", "")
        if isinstance(value, Mapping):
            raw = value.get("data") or value.get("bytes") or b""
            return cls(
                bytes(raw),
                str(value.get("mime") or value.get("mime_type") or ""),
                str(value.get("filename") or ""),
            )
        raise GenerationError(f"无法识别的参考素材类型：{type(value).__name__}")


@dataclass(frozen=True)
class GenerationRequest:
    """一次生成请求。`duration/aspect_ratio/model` 是提示词参数，不是协议字段。

    `seed` 特意**不**提供：它是 `gen_params.HYPOTHESIS_FIELDS` 里的单源字段
    （muse 侧零解析证据），本模块不给出"能编进提示词"的入口，免得调用方以为
    传了就有用。`model` 只作为文本说明，不参与线路格式。
    """

    prompt: str
    expect: str = EXPECT_IMAGE
    duration: int | None = None
    aspect_ratio: str | None = None
    model: str | None = None
    references: tuple[Reference, ...] = ()
    marker: str = ""

    def __post_init__(self) -> None:
        if not str(self.prompt or "").strip():
            raise GenerationError("提示词不能为空")
        if self.expect not in EXPECTS:
            raise GenerationError(
                f"expect 只支持 {'/'.join(EXPECTS)}，收到 {self.expect!r}；"
                "生成端点不区分图/视频，kind 由这里决定成品挑选规则"
            )
        if self.duration is not None and self.duration not in VIDEO_DURATIONS:
            raise GenerationError(
                f"duration 只允许 {list(VIDEO_DURATIONS)}（桥侧白名单，服务端真值未证实）"
            )
        if self.aspect_ratio is not None and self.aspect_ratio not in ASPECT_RATIOS:
            raise GenerationError(
                f"aspect_ratio 只允许 {list(ASPECT_RATIOS)}（同上，未证实服务端接受集）"
            )
        if len(self.references) > MAX_GENERATION_ITEMS:
            raise GenerationError(
                f"参考素材最多 {MAX_GENERATION_ITEMS} 个（收到 {len(self.references)}）"
            )

    def render_prompt(self) -> str:
        """把参数编进自然语言。

        为什么不用结构化字段：观测到的 /chat/stream body 只有
        items/node_id/capabilities/metadata 四个键（lichao protocol_engine.py:331），
        没有任何时长/比例/种子字段；浏览器路径同样是写进提示词文本。
        """
        parts = [self.prompt.strip()]
        if self.expect == EXPECT_VIDEO:
            if self.aspect_ratio:
                parts.append(f"画面比例严格 {self.aspect_ratio}")
            if self.duration:
                parts.append(f"时长严格为 {self.duration} 秒")
            parts.append("输出一段视频文件")
        else:
            if self.aspect_ratio:
                parts.append(f"画面比例严格 {self.aspect_ratio}")
            parts.append("输出一张图片文件")
        if self.model:
            parts.append(f"使用模型 {self.model}")
        if self.references:
            parts.append(f"只使用本次随消息附带的 {len(self.references)} 个参考素材，禁止使用历史素材")
        return "，".join(parts)

    def body(self, node_id: str, capabilities: Sequence[str] | Mapping[str, Any] | None = None) -> dict:
        """组 /chat/stream 的 body。

        `capabilities` 在观测里是 `{}`（lichao）而不是列表，这里两种都收：
        传序列时按字典形态的空值处理，避免我们凭猜测发明字段语义。
        """
        if capabilities and not isinstance(capabilities, Mapping):
            payload: dict[str, Any] = {}
        else:
            payload = dict(capabilities or {})
        body: dict[str, Any] = {
            "items": build_items(self.render_prompt(), self.references),
            "node_id": node_id,
            "capabilities": payload,
        }
        if self.marker:
            body["metadata"] = {"client_marker": self.marker}
        return body


def build_items(text: str, references: Iterable[Reference] = ()) -> list[dict]:
    """items 数组：素材在前、文本在**末位**（lichao protocol_engine.py:329 的顺序）。

    顺序是语义的一部分：观测里文本永远是最后一项，把提示词放到前面会改变
    agent 对"哪条是指令、哪些是素材"的判断。
    """
    items: list[dict] = []
    for index, ref in enumerate(references, start=1):
        mime = (ref.mime or "").split(";", 1)[0].strip().lower()
        filename = ref.filename or f"reference-{index}{mimetypes.guess_extension(mime) or '.bin'}"
        items.append(
            {
                "type": "image" if mime.startswith("image/") else "file",
                "mime_type": mime or "application/octet-stream",
                "data_base64": base64.b64encode(ref.data).decode("ascii"),
                "filename": filename,
            }
        )
    items.append({"type": "text", "text": text})
    return items


def should_use_file_ref(size: int, mime: str, capabilities: Iterable[str] | None) -> bool:
    """大素材改走 file_ref（先 /fs/write 再引用路径）。阈值见模块头 [observed]。"""
    caps = {str(item) for item in (capabilities or [])}
    mime_l = (mime or "").lower()
    if mime_l.startswith("image/"):
        return False
    if mime_l.startswith("video/"):
        if VIDEO_FILE_REF_CAPABILITY in caps:
            return size >= FILE_REF_DEFAULT_BYTES
        return size >= FILE_REF_VIDEO_FALLBACK_BYTES
    return size >= FILE_REF_DEFAULT_BYTES


def inline_item_to_file_ref(item: dict, path: str, mime: str) -> dict:
    """把已 inline 的 item 换成 file_ref 形态（path 由 /fs/write 返回）。"""
    return {
        "type": "file_ref",
        "mime_type": mime or item.get("mime_type") or "application/octet-stream",
        "path": path,
        "filename": item.get("filename") or posixpath.basename(path),
    }


# ---------------------------------------------------------------- 结果


class RetrievalMode:
    FS_READ = "protocol.noise.fs.read"
    CDN = "cdn-direct-url"  # UNVERIFIED：见模块头，用即抛


@dataclass
class GenerationResult:
    """一次成功生成的完整交代。`via` 用来区分取件路径，便于事后归因。"""

    data: bytes
    kind: str
    path: str
    mime: str
    width: int | None
    height: int | None
    byte_len: int | None
    text: str
    via: str = RetrievalMode.FS_READ
    node_id: str = ""
    marker: str = ""
    elapsed_ms: float = 0.0
    events_seen: dict[str, int] = field(default_factory=dict)
    resource: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class QuotaObservation:
    """配额线索：只记录"哪一步返回了什么"，不下结论（本项 unverified）。"""

    stage: str
    status: int | None
    detail: str
    at: float


# ---------------------------------------------------------------- 主类


class ProtocolGeneration:
    """鸭子契约对齐 protocol_client：`request` / `read_stream` / `fs_read` / `fs_write`。

    刻意**不**自己管连接与 bootstrap：那都是 HatchProtocolClient 的职责
    （三阶段 bootstrap 已在 protocol_client.py:477 起实现）。这里只负责
    "生成这件事在该信什么协议"，因此可以完全离线测试。
    """

    def __init__(
        self,
        client: Any,
        *,
        ack_timeout: float = ACK_TIMEOUT_SECONDS,
        done_grace: float = DONE_GRACE_SECONDS,
        timeout: float = 300.0,
        poll_interval: float = 0.5,
    ) -> None:
        for name in ("request", "read_stream", "fs_read", "fs_write"):
            if not hasattr(client, name):
                raise GenerationError(f"client 缺少 {name}()，无法驱动纯协议生成")
        self.client = client
        self.ack_timeout = ack_timeout
        self.done_grace = done_grace
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.quota_observations: list[QuotaObservation] = []

    # -- 未证实路径的统一闸门：宁可现在抛，也不要变成远处的一次 404
    def probe_endpoint(self, path: str, *, verb: str = "POST") -> None:
        reason = UNVERIFIED_ENDPOINTS.get(path)
        if reason is not None:
            raise UnverifiedEndpointError(
                f"{verb} {path} 未破出（{reason}）。已证实的生成入口只有 "
                f"POST {GENERATION_PATH}"
            )
        raise UnverifiedEndpointError(
            f"{verb} {path} 不在已证实端点表里；已证实集合：{self.known_endpoints()}"
        )

    @staticmethod
    def known_endpoints() -> tuple[str, ...]:
        return (
            GENERATION_PATH,
            SUBSCRIBE_PATH,
            FS_READ_PATH,
            FS_WRITE_PATH,
            PING_PATH,
        )

    # -- 生成主流程
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """提交 → 收事件 → 拿到 sandbox 资源 → /fs/read 取字节。"""
        started = time.perf_counter()
        node_id = self._node_for_task()
        capabilities = list(getattr(self.client, "capabilities", []) or [])

        items = build_items(request.render_prompt(), request.references)
        body = {
            "items": items,
            "node_id": node_id,
            "capabilities": {},
        }
        if request.marker:
            body["metadata"] = {"client_marker": request.marker}

        ack = await self.client.request("POST", GENERATION_PATH, body, self.ack_timeout)
        self._record("submit", ack)
        status = getattr(ack, "status", None)
        if status != 200:
            raise GenerationError(
                f"{GENERATION_PATH} HTTP {status}：提交阶段就失败，未产生任务"
            )
        payload = self._ack_payload(ack)
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        log.info(
            "chat.stream 已提交 node_id=%s marker=%s message_id=%s",
            node_id,
            request.marker or "-",
            payload.get("message_id"),
        )

        text_parts: list[str] = []
        candidates: list[dict] = []
        seen_counts: dict[str, int] = {}
        presentation_ready = False
        done_at: float | None = None
        deadline = time.perf_counter() + self.timeout
        # `_event_source` 是异步生成器函数：调用返回 async_generator，不能再 await。
        events = self._event_source(node_id, request, capabilities)

        async for event in events:
            if not isinstance(event, Mapping):
                continue
            name = str(event.get("name") or event.get("event") or event.get("type") or "")
            seen_counts[name] = seen_counts.get(name, 0) + 1
            if name == EVENT_POOL_ERROR:
                raise GenerationError(f"上游 pool_error：{event.get('payload') or event}")
            if not self._belongs_to(event, node_id):
                continue
            inner = event.get("payload") or {}
            if name == EVENT_TEXT_DELTA:
                delta = str((inner or {}).get("text") or "")
                if delta:
                    text_parts.append(delta)
                continue
            if name in SETTLE_EVENTS:
                found = collect_media_candidates(inner or {})
                for resource in found:
                    if not any(item.get("path") == resource.get("path") for item in candidates):
                        candidates.append(resource)
                if name == EVENT_PRESENTATION and len(found) > 0:
                    presentation_ready = True
                if name == EVENT_MESSAGE_DONE:
                    done_at = time.perf_counter()
            if name == EVENT_MESSAGE_REMOVED and not self._belongs_to(event, node_id):
                continue

            picked = self._pick(candidates, request.expect)
            if picked is not None and (presentation_ready or done_at is not None):
                data = await self._download(picked)
                return GenerationResult(
                    data=data,
                    kind=request.expect,
                    path=str(picked.get("path") or ""),
                    mime=str(picked.get("mime") or picked.get("mime_type") or ""),
                    width=self._maybe_int(picked.get("width")),
                    height=self._maybe_int(picked.get("height")),
                    byte_len=self._maybe_int(
                        picked.get("byte_len") or picked.get("size_bytes")
                    ),
                    text="".join(text_parts),
                    node_id=node_id,
                    marker=request.marker,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                    events_seen=seen_counts,
                    resource=dict(picked),
                )
            if (
                done_at is not None
                and time.perf_counter() - done_at >= self.done_grace
            ):
                raise GenerationError(
                    f"生成已完成但 12 秒内没等到 {request.expect} 成品："
                    f"候选 {len(candidates)} 个、事件 {seen_counts}；"
                    "（若上游只回了文本，通常是提示词没让模型产出文件）"
                )
            if time.perf_counter() > deadline:
                raise GenerationError(
                    f"纯协议生成超时（{self.timeout:.0f}s），事件统计 {seen_counts}"
                )
        raise GenerationError(
            f"事件流结束仍未拿到 {request.expect} 成品，事件统计 {seen_counts}"
        )

    # -- 内部
    def _node_for_task(self) -> str:
        factory = getattr(self.client, "new_task_node", None)
        if callable(factory):
            return str(factory())
        return str(getattr(self.client, "node_id", "") or uuid.uuid4().hex)

    async def _event_source(
        self, node_id: str, request: GenerationRequest, capabilities: list[str]
    ) -> AsyncIterator[dict]:
        """订阅流。read_stream() 的无参形态 = 纯读已建立的订阅。"""
        stream = self.client.read_stream()
        if not hasattr(stream, "__anext__"):
            raise GenerationError("client.read_stream() 必须返回异步迭代器")
        try:
            async for event in stream:
                yield event
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                await aclose()

    @staticmethod
    def _belongs_to(event: Mapping[str, Any], node_id: str) -> bool:
        """按 node_id/device_id 归属过滤；上游没带归属字段时**放行**。

        lichao 的判据是 `device_id == node_id`（protocol_engine.py:410），但同一处
        对 presentation 事件在 `ours` 为假时仍然放行（:439）。照抄这个宽容度，
        否则服务端不带 device_id 的事件会被我们全部丢掉，表现为"永远等不到成品"。
        """
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            return True
        device = payload.get("device_id") or payload.get("node_id")
        if not device:
            return True
        return str(device) == str(node_id)

    @staticmethod
    def _pick(candidates: list[dict], expect: str) -> dict | None:
        """取**最后一个** kind 匹配的资源：多轮补发时后者才是成品。

        另外必须过 normalize_media_path —— 它会把 `workspace/user/`（用户上传区）
        判成 None，从而挡掉"把输入图当输出"的回声坑（protocol_client.py:216-224）。
        """
        for resource in reversed(candidates):
            if normalize_media_path(resource.get("path")) is None:
                continue
            if resource_kind(resource) == expect:
                return resource
        return None

    async def _download(self, resource: Mapping[str, Any]) -> bytes:
        path = str(resource.get("path") or "")
        if not path:
            raise GenerationError("成品资源没有 path 字段，无法取件")
        if path.startswith("http://") or path.startswith("https://"):
            raise UnverifiedCapabilityError(
                f"成品给的是直链 {path[:60]}…，但 CDN 取件路径未证实"
                "（本仓代码零引用，只有第三方 README 的说法）。已证实的取件方式是 "
                f"POST {FS_READ_PATH} 分页 base64。"
            )
        data = await self.client.fs_read(path)
        if isinstance(data, dict):  # 容错：假 client 可能直接返回包装
            data = data.get("data") or b""
        if not data:
            raise GenerationError(f"fs.read 取到 0 字节：{path}")
        return bytes(data)

    def _record(self, stage: str, response: Any) -> None:
        """只记录事实，不推断配额扣减时点（本项 unverified）。"""
        status = getattr(response, "status", None)
        detail = ""
        if status in (402, 403, 429):
            detail = str(getattr(response, "body", "") or "")[:200]
        self.quota_observations.append(
            QuotaObservation(stage=stage, status=status, detail=detail, at=time.time())
        )

    @staticmethod
    def _ack_payload(ack: Any) -> dict:
        reader = getattr(ack, "json", None)
        if callable(reader):
            try:
                value = reader()
            except Exception:  # ack 不是 JSON 也不能让整次生成失败：它只带 id
                return {}
            return value if isinstance(value, dict) else {}
        return ack if isinstance(ack, dict) else {}

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
