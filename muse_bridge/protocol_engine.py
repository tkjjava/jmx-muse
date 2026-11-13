"""纯协议侧生成 / 媒体 / 提示词引擎（不启动浏览器，不碰 DOM）。

语义来源（逐处标注，未标注处即照搬）：
  sources/lichao199208__muse2api/app/muse_engine/protocol_engine.py
    :101-112  file_ref 阈值判定
    :114-127  媒体路径归一 + ``workspace/user/`` 排除（上传件不得当成品）
    :130-175  presentation/resources 递归提取
    :178-192  资源类型判定（mime 优先，退到扩展名）
    :336-447  chat/stream 提交与事件解析（本模块的 _consume）
    :484-500  成品取回 + 晚到宽限 12 秒
  sources/lichao199208__muse2api/app/muse_engine/prompts.py:102-239  提示词措辞
  sources/lichao199208__muse2api/app/muse_engine/protocol_client.py:545-584
    fs/write→fs/stat→fs/rename→file_ref 的上传链路（本模块经 client.fs_write 复用）
  sources/czg86389-hub__muse2api/engine.py:1277  额度关键词表

对上游的两处刻意偏离（理由写在代码里）：
  1. 上游把 w/h/mime/duration/seed 在 app 层全部丢掉，这里用 GenerationResult 保留。
  2. 上游把 message_done 的非 completed 状态统一抛 MuseGenerationError（会被换号重试），
     这里拆成 ProtocolModelRefusal / ProtocolQuotaExhausted，并用 ``retryable()`` 谓词
     钉死「拒绝与额度不足绝不换号」。

客户端契约（**现以 `muse_bridge/protocol_client.py` 的真实签名为准**）：
   request("POST"|"GET", path, body, timeout) -> ProtocolResponse(status/headers/body/json())
   read_stream() -> AsyncIterator[dict]（订阅已由 subscribe 建好时无参）
   fs_write(data, mime, filename) -> file_ref dict
   fs_read(path) -> bytes
   media_candidates(node_id) -> list[dict]      # 可选补充来源，与本地提取同样过滤

与 `protocol_generation.py` 的分工（别把两者当同一条链的两半）：
  * `protocol_generation.ProtocolGeneration` 是**照真实客户端签名写的可接线编排器**
    （提交 → 收事件 → 取件，未证实端点/直链取件显式抛），离线测试钉死；
  * 本模块是**语义参考层**：提示词措辞、媒体提取、错误分类（拒绝/额度不换号）
    与上游逐行对照，供离线对拍与接线时抄措辞用。两者各有一份 `GenerationResult`
    与几个同名工具函数，属于已知重复；接线时**以 protocol_generation 为准**，
    本模块待其语义被验证后合并或删除（见 docs/PROTOCOL.md §8.9）。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import posixpath
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from muse_bridge import env, refs, session_http
from muse_bridge.history import latest_assistant_text

if TYPE_CHECKING:  # 落盘前不得运行时 import，否则整包 import 会炸
    from muse_bridge.protocol_client import HatchProtocolClient

__all__ = [
    "ProtocolEngine",
    "ProtocolGenerationError",
    "ProtocolMediaTypeError",
    "ProtocolModelRefusal",
    "ProtocolQuotaExhausted",
    "GenerationResult",
    "build_image_prompt",
    "build_video_prompt",
    "retryable",
]

# ---------------------------------------------------------------- 订阅流参数

def accepts_kwarg(fn: object, name: str) -> bool:
    """判断这个可调用对象接不接受某个关键字参数（含 `**kwargs` 形态）。

    为什么需要：`self.client` 是鸭子契约，真实现是 `HatchProtocolClient`，
    离线测试用的是签名更窄的假件。多传一个它们不认识的 kwarg 会直接
    `TypeError`，把"打开了 idle 超时"变成"所有生成都失败"。所以按签名探测，
    而不是靠 `try/except TypeError` 猜。
    """
    try:
        params = inspect.signature(fn).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _close_stream(stream: AsyncIterator[Any]) -> None:
    """Close owned generators while accepting plain AsyncIterator clients."""
    close = getattr(stream, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


def stream_idle_timeout() -> float | None:
    """从 env 取 idle 预算；0/负数是"未启用"，返回 None。"""
    value = float(env.PROTOCOL_STREAM_IDLE_TIMEOUT or 0)
    return value if value > 0 else None


# ---------------------------------------------------------------- 异常与重试谓词


class ProtocolGenerationError(RuntimeError):
    """协议侧生成失败（可换号重试的那一类）。"""


class ProtocolModelRefusal(ProtocolGenerationError):
    """模型只回文本、明确拒绝生成。**不得**换号重试。"""


class ProtocolQuotaExhausted(ProtocolGenerationError):
    """额度/积分不足。**不得**换号重试（同池账号共享配额判定）。"""


class ProtocolMediaTypeError(ProtocolGenerationError):
    """期望模态与实际拿到的不符（例：期望视频只拿到封面静帧）。"""


class ProtocolInterrupted(ProtocolGenerationError):
    """上游明确回报 `message_done.status="interrupted"`（取消/中止）。

    这不是模型拒绝，也不是账号故障：换号重试只会把同一个被中止的结果再要
    一遍，还可能重复消耗额度。调用方应把已产出的部分当部分结果处理，并以
    明确的中断错误收尾，而不是伪装成完成或走浏览器回退。
    """


#: 拒绝、额度不足、上游中断都是终态；其余（超时、断连、模态不符）允许换号重试。
NON_RETRYABLE: frozenset[type[BaseException]] = frozenset(
    {ProtocolModelRefusal, ProtocolQuotaExhausted, ProtocolInterrupted}
)

QUOTA_TEXT_RE = re.compile(
    r"额度不足|积分不足|out of credits|达到上限|token limit", re.I
)
FILE_REF_DEFAULT_BYTES = 4 * 1024 * 1024
FILE_REF_VIDEO_FALLBACK_BYTES = 1 * 1024 * 1024
UPLOAD_PREFIX = "workspace/user/"
SANDBOX_PREFIX = "sandbox://"
#: 参考素材里“远端路径”的形态：这些交给客户端自己解析，本模块**不下载、不改写**
#: （纯协议侧不替调用方抓网；抓网的 SSRF 边界见 refs.fetch_public）。
PATH_REFERENCE_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "workspace/",
    SANDBOX_PREFIX,
    "/",
)
#: `upload_refs` 的有界上限。engine 挂在账号级驱动上，同一账号一天可能生成几十次，
#: 而每条内联参考素材都会留下一条上传路径；只增不清会随进程寿命无界增长。
#: 按“最近 8 轮满额参考素材”保留（每轮 ≤ `refs.MAX_REFERENCE_FILES` 条），FIFO 淘汰。
UPLOAD_REFS_MAX: int = refs.MAX_REFERENCE_FILES * 8
LATE_MEDIA_GRACE_SECONDS = 12.0  # 照上游 protocol_engine.py:498
MILESTONES: tuple[int, ...] = (25, 40, 50, 75, 100)
DURATION_WHITELIST: tuple[int, ...] = (5, 6, 8, 10)
SIZE_WHITELIST: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4")


def retryable(exc: BaseException) -> bool:
    """调用方（app/pool）据此决定是否换号重试；终态错误返回 False。

    `NON_RETRYABLE` 是 frozenset（可读性），但 `isinstance` 的第二个参数只接受
    type 或 type 元组：直接传 frozenset 会对**任何**异常抛 TypeError，把
    "这条失败该不该换号" 变成又一个崩溃点。这里显式转成元组。
    """
    return not isinstance(exc, tuple(NON_RETRYABLE))


# ---------------------------------------------------------------- 提示词构建

_KIND_LABELS = {
    "image": ("张", "图片"),
    "video": ("段", "视频"),
    "audio": ("段", "音频"),
    "document": ("份", "文档"),
}
_KIND_TOKENS = {"image": "图", "video": "视频", "audio": "音频", "document": "文档"}
_REFERENCE_TOKEN = re.compile(r"\[(?:图|视频|音频|文档)\s*\d+\]|第\s*\d+\s*(?:张|段|个)")


def _describe_kinds(kinds: Sequence[str]) -> str:
    out = []
    for kind in ("image", "video", "audio", "document"):
        count = kinds.count(kind)
        if count:
            out.append(f"{count} {_KIND_LABELS[kind][0]}{_KIND_LABELS[kind][1]}")
    return "、".join(out)


def _reference_legend(kinds: Sequence[str]) -> str:
    seen: list[str] = []
    parts = []
    for index, kind in enumerate(kinds, 1):
        seen.append(kind)
        parts.append(
            f"[{_KIND_TOKENS[kind]}{seen.count(kind)}] 是第 {index} 个附件"
            f"（{_KIND_LABELS[kind][1]}）"
        )
    return "附件对照：" + "，".join(parts)


def _check_size(size: str, caller: str) -> str:
    value = str(size or "").strip()
    if value not in SIZE_WHITELIST:
        raise ProtocolGenerationError(
            f"{caller}: size={value!r} 不在允许画幅 {list(SIZE_WHITELIST)} 内"
        )
    return value


def _check_duration(duration: int, caller: str) -> int:
    try:
        value = int(duration)
    except (TypeError, ValueError):
        raise ProtocolGenerationError(
            f"{caller}: duration={duration!r} 不是整数，允许时长 {list(DURATION_WHITELIST)} 秒"
        ) from None
    if value not in DURATION_WHITELIST:
        raise ProtocolGenerationError(
            f"{caller}: duration={value} 不在允许时长 {list(DURATION_WHITELIST)} 秒内"
        )
    return value


def _aspect_clause(aspect: str) -> str:
    if aspect == "9:16":
        return "【画面构图与比例要求】：严格 9:16 竖屏满屏画幅（9:16 vertical portrait aspect ratio）"
    if aspect == "16:9":
        return "【画面构图与比例要求】：16:9 宽屏横屏画幅（16:9 widescreen landscape aspect ratio）"
    if aspect == "1:1":
        return "【画面构图与比例要求】：1:1 正方形画幅（1:1 square aspect ratio）"
    return f"【画面构图与比例要求】：{aspect} 画面比例"


def _image_reference_clause(kinds: Sequence[str], prompt: str) -> str:
    if any(kind != "image" for kind in kinds):
        return (
            f"基于我本次上传附带的参考素材（{_describe_kinds(kinds)}；"
            f"{_reference_legend(kinds)}；只使用本次上传的这些附件，"
            "各附件的用途以下面的描述为准）进行生图/编辑，输出一张图片"
        )
    if len(kinds) > 1 and _REFERENCE_TOKEN.search(prompt):
        return f"基于我本次上传附带的参考图片（{_reference_legend(kinds)}）进行生图/编辑"
    if kinds:
        return "基于我本次上传附带的参考图片进行生图/编辑"
    return (
        "全新文生图创作（当前未提供任何参考图，请勿查找历史相册或向用户索要原图，"
        "直接根据文字描述从零绘制生成一张全新图片）"
    )


def build_image_prompt(
    prompt: str,
    *,
    size: str = "1:1",
    style: str | None = None,
    extra: str | None = None,
    kinds: Sequence[str] | None = None,
    ref_count: int = 0,
) -> str:
    """结构化参数 → 自然语言提示词（措辞照 prompts.py:113-158）。"""
    aspect = _check_size(size, "build_image_prompt")
    text = (prompt or "").strip()
    table = list(kinds if kinds is not None else ["image"] * max(0, int(ref_count)))
    if not text and not table:
        raise ProtocolGenerationError("build_image_prompt: prompt 不能为空")
    parts = [f"{_image_reference_clause(table, text)}：{text}"]
    parts.append(_aspect_clause(aspect))
    if style:
        parts.append(f"风格要求：{style}")
    if extra:
        parts.append(str(extra))
    return "，".join(parts)


def _video_reference_clause(kinds: Sequence[str], prompt: str) -> tuple[str, str]:
    """返回 (引用段, 生成词)；生成词只在「全是图片」时才是 图生视频（prompts.py:199）。"""
    count = len(kinds)
    if any(kind != "image" for kind in kinds):
        return (
            f"基于我本次上传的参考素材（{_describe_kinds(kinds)}）生成视频"
            f"（{_reference_legend(kinds)}；只使用本次上传的这些附件，严禁使用历史素材；"
            "各附件的用途以下面的描述为准）",
            "视频",
        )
    if count > 1 and _REFERENCE_TOKEN.search(prompt):
        return (
            f"基于我本次上传的 {count} 张参考图片生成视频（{_reference_legend(kinds)}；"
            "各图片的用途以下面的描述为准，未说明时以第一张作为主要起始画面；"
            "严禁使用历史图片或其他图像）",
            "图生视频",
        )
    if count > 1:
        return (
            f"基于我本次上传的 {count} 张参考图片生成视频（以第一张作为主要起始画面，"
            "其余图片作为主体、场景与风格参考，严禁使用历史图片或其他图像）",
            "图生视频",
        )
    if count == 1:
        return (
            "基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，"
            "必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）",
            "图生视频",
        )
    return (
        "全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）",
        "视频",
    )


def build_video_prompt(
    prompt: str,
    *,
    duration: int = 5,
    size: str = "16:9",
    extra: str | None = None,
    kinds: Sequence[str] | None = None,
    ref_count: int = 0,
) -> str:
    """时长/画幅白名单校验 + 自然语言提示词（prompts.py:161-239）。"""
    seconds = _check_duration(duration, "build_video_prompt")
    aspect = _check_size(size, "build_video_prompt")
    text = (prompt or "").strip()
    table = list(kinds if kinds is not None else ["image"] * max(0, int(ref_count)))
    if not text and not table:
        raise ProtocolGenerationError("build_video_prompt: prompt 不能为空")
    clause, gen_word = _video_reference_clause(table, text)
    has_ref = bool(table)
    if aspect == "9:16":
        if has_ref:
            head = (
                f"{clause}：生成一个严格9:16竖屏手机满屏的动态{gen_word}"
                f"（9:16 vertical portrait video，720x1280，时长严格为 {seconds} 秒）"
                f"：{text}"
            )
        else:
            head = (
                f"{clause}：生成一个严格9:16竖屏手机满屏视频"
                f"（时长严格为 {seconds} 秒）：{text}"
            )
    elif aspect == "16:9":
        if has_ref:
            head = (
                f"{clause}：生成一个16:9宽屏横屏{gen_word}（时长严格为 {seconds} 秒）：{text}"
            )
        else:
            head = (
                f"全新文生视频创作（纯文本全新生成）：生成一个16:9横屏宽屏视频"
                f"（时长严格为 {seconds} 秒）：{text}"
            )
    else:
        if has_ref:
            head = f"{clause}：生成动态{gen_word}（时长严格为 {seconds} 秒）：{text}"
        else:
            head = (
                f"全新文生视频创作（纯文本全新生成）：生成一个视频"
                f"（时长严格为 {seconds} 秒）：{text}"
            )
    parts = [head]
    if f"时长严格为 {seconds} 秒" not in head:
        parts.append(f"时长严格为 {seconds} 秒")
    if extra:
        parts.append(str(extra))
    return "，".join(parts)


# ---------------------------------------------------------------- 媒体提取


@dataclass(frozen=True)
class MediaResource:
    """presentation/resources 里一条成品候选（已归一，已过滤上传件）。"""

    path: str
    mime: str = ""
    kind: str = "image"
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    seed: int | str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass
class GenerationResult:
    """生成结果 + 上游丢失的全部元数据。

    偏离上游的理由：上游 ``app.py`` 拿到 engine 返回的 w/h/mime 后全部丢弃，只留
    bytes。这里既保留元数据，又用 ``__iter__`` 兼容 lead 要求的
    ``data, mime = await engine.generate_image(...)`` 二元组解包。
    """

    data: bytes
    mime: str
    kind: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    seed: int | str | None = None
    source_prompt: str = ""
    path: str = ""

    def __iter__(self):  # -> tuple[bytes, str] 兼容层
        yield self.data
        yield self.mime

    def to_dict(self) -> dict[str, Any]:
        return {
            "mime": self.mime,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "duration_ms": self.duration_ms,
            "seed": self.seed,
            "bytes": len(self.data),
            "source_prompt": self.source_prompt,
            "path": self.path,
        }


def _as_int(value: Any) -> int | None:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _decode_meta(value: Any) -> dict[str, Any] | None:
    """上游 :133-141：metadata 常是一段 JSON 字符串。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def normalize_media_path(path: Any) -> tuple[str, str] | None:
    """sandbox://workspace/... → (uri, remote)；上传件目录返回 None（上游 :114-127）。"""
    if not isinstance(path, str) or not path:
        return None
    if path.startswith(SANDBOX_PREFIX):
        remote, uri = path[len(SANDBOX_PREFIX):], path
    elif path.startswith("workspace/"):
        remote, uri = path, SANDBOX_PREFIX + path
    else:
        return None
    if remote.startswith(UPLOAD_PREFIX):
        return None
    return uri, remote


def resource_kind(resource: dict[str, Any]) -> str:
    """mime 优先，退到扩展名（上游 :178-192）。"""
    mime = str(resource.get("mime") or resource.get("mime_type") or "").lower()
    kind = refs.reference_kind(mime)
    if mime and kind in ("video", "image"):
        return kind
    path = str(
        resource.get("path")
        or resource.get("uri")
        or resource.get("image_path")
        or resource.get("video_path")
        or ""
    )
    suffix = posixpath.splitext(path.split("?", 1)[0])[1].lower()
    return "video" if suffix in refs.VIDEO_EXTS else "image"


def collect_media(value: Any, output: list[MediaResource]) -> None:
    """递归提取成品候选；上传件与重复路径一律不进表（上游 :130-175）。"""
    if isinstance(value, dict):
        meta = _decode_meta(value.get("metadata"))
        merged = {**value, "metadata": meta} if meta else value
        path = (
            merged.get("path")
            or merged.get("uri")
            or merged.get("image_path")
            or merged.get("video_path")
        )
        normalized = normalize_media_path(path)
        if normalized is None and isinstance(meta, dict):
            normalized = normalize_media_path(meta.get("path") or meta.get("uri"))
        if normalized is not None:
            uri, _remote = normalized
            source = {**merged, "path": uri}
            if isinstance(meta, dict):
                for key in ("mime", "mime_type", "width", "height", "duration_ms", "seed"):
                    if source.get(key) is None and meta.get(key) is not None:
                        source[key] = meta[key]
            mime = str(source.get("mime") or source.get("mime_type") or "")
            if not mime:
                mime = str(meta.get("mime_type") or "") if isinstance(meta, dict) else ""
            item = MediaResource(
                path=uri,
                mime=mime,
                kind=resource_kind(source),
                width=_as_int(source.get("width")),
                height=_as_int(source.get("height")),
                duration_ms=_as_int(
                    source.get("duration_ms")
                    or source.get("durationMilliseconds")
                    or source.get("duration")
                ),
                seed=source.get("seed") if isinstance(source.get("seed"), (int, str)) else None,
                raw=source,
            )
            if not any(known.path == uri for known in output):
                output.append(item)
        for key, child in merged.items():
            if key not in ("request", "data_base64", "metadata"):
                collect_media(child, output)
    elif isinstance(value, list):
        for child in value:
            collect_media(child, output)


def should_use_file_ref(size: int, mime: str, capabilities: Iterable[Any] | None = None) -> bool:
    """上游 :101-112：图片走内联，视频/其它超阈值走 file_ref。"""
    caps = {str(item) for item in (capabilities or [])}
    low = (mime or "").lower()
    if low.startswith("image/"):
        return False
    if low.startswith("video/"):
        threshold = (
            FILE_REF_DEFAULT_BYTES
            if "video_file_ref_ingest" in caps
            else FILE_REF_VIDEO_FALLBACK_BYTES
        )
        return size >= threshold
    return size >= FILE_REF_DEFAULT_BYTES


def decode_reference(value: str) -> tuple[bytes, str]:
    """data URL / 裸 base64 / sandbox 路径 → (bytes, mime)。"""
    text = str(value or "")
    if text.startswith("data:"):
        header, _, payload = text[5:].partition(";base64,")
        data = base64.b64decode(payload, validate=False)
        mime = header.split(";", 1)[0].strip().lower()
        sniffed = refs.sniff_mime(data)
        return data, sniffed or mime or "application/octet-stream"
    if text.startswith(PATH_REFERENCE_PREFIXES):
        return b"", "application/octet-stream"
    try:
        data = base64.b64decode(text, validate=True)
    except Exception:
        raise ProtocolGenerationError(f"参考素材不是有效的 data URL / base64：{text[:48]!r}") from None
    sniffed = refs.sniff_mime(data)
    if sniffed is None:
        raise ProtocolGenerationError("参考素材内容无法识别为已知媒体类型")
    return data, sniffed


def _is_path_reference(value: str) -> bool:
    """是不是“已知路径”形态（http(s)/workspace/sandbox/绝对路径）。"""
    return value.startswith(PATH_REFERENCE_PREFIXES)


def _normalize_inline_references(values: Sequence[str]) -> list[refs.Reference]:
    """内联素材整批过 `refs.normalize_references`：数量 / 单件 / 总量 / 类型 / 时长。

    与浏览器路径（`driver.py:660`）共用同一套判定，**必须在任何 fs_write 之前**
    跑完：超限素材上传成功也不会被 muse 接受（页面表现为白等几分钟），提前拒绝
    才能把失败点留在本地、给出可读原因。

    `refs.ReferenceError` 翻译成 `ProtocolGenerationError`：前者是素材层异常，
    调用方（pool/app）只认协议层的异常族，不替它多记一个基类。
    """
    try:
        return refs.normalize_references(list(values))
    except refs.ReferenceError as exc:
        raise ProtocolGenerationError(f"参考素材校验未通过：{exc}") from None


# ---------------------------------------------------------------- 引擎


class ProtocolEngine:
    """协议侧聊天 / 生图 / 生视频。事件形状照 protocol_engine.py:411-447。"""

    def __init__(
        self,
        client: HatchProtocolClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        grace_seconds: float = LATE_MEDIA_GRACE_SECONDS,
        history_recovery: bool = True,
    ) -> None:
        self.client = client
        # 断线重连后的“历史对账恢复”：默认开启。活网验证（2026-11-14）：
        # 断线回合以普通状态收尾时，已提交历史里的完整正文与已收正文是严格
        # 前缀关系（169→456 字、2 段→456 字两次验证）。历史取不到或前缀不符时
        # 仍回退到“连续性无法确认”错误，不会伪造结果。可用 False 关闭。
        self.history_recovery = bool(history_recovery)
        # 时钟注入点：测试里同时冻结 monotonic 与 wall clock，零墙钟成本。
        self._monotonic = monotonic
        self._time = wall_clock
        self.grace_seconds = float(grace_seconds)
        #: 本引擎上传过的参考素材路径（URI 形态，服务于“上传件不得当成品”的过滤）。
        #: 有界：见 `_remember_upload_ref`（FIFO 淘汰，上限 `UPLOAD_REFS_MAX`）。
        self.upload_refs: set[str] = set()
        self._upload_ref_order: list[str] = []

    # ---- 客户端适配（集中处理签名差异，见模块 docstring 的契约说明）----

    def _stream_extras(self) -> dict[str, float]:
        """给 `read_stream` 的可选关键字（当前只有 idle 预算）。

        默认返回空 dict = 不改变任何既有行为；只有 env 打开了 idle 且客户端
        真的接受这个关键字时才带上，免得窄签名的假件/旧实现直接 TypeError。
        """
        idle = stream_idle_timeout()
        if idle is None:
            return {}
        if not accepts_kwarg(getattr(self.client, "read_stream", None), "idle_timeout"):
            return {}
        return {"idle_timeout": idle}

    async def _invoke(self, name: str, /, *args, **kwargs) -> Any:
        fn = getattr(self.client, name)
        try:
            result = fn(*args, **kwargs)
        except TypeError as exc:  # 位置/关键字形状不一致时再试一次
            if "argument" not in str(exc).lower():
                raise
            lowered = {str(key).lower(): value for key, value in kwargs.items()}
            result = fn(*args, **lowered)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _response(raw: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
        status = getattr(raw, "status", None)
        if status is None:
            status = raw.get("status", 200) if isinstance(raw, dict) else 200
        headers = dict(getattr(raw, "headers", None) or {})
        if isinstance(raw, dict) and not headers:
            headers = dict(raw.get("headers") or {})
        if hasattr(raw, "json"):
            body = raw.json()
        elif isinstance(raw, dict):
            body = raw.get("body", raw.get("result", raw))
        else:
            body = {}
        if isinstance(body, (bytes, bytearray)):
            body = json.loads(body or b"{}")
        if not isinstance(body, dict):
            body = {"value": body}
        return int(status), body, {str(k).lower(): v for k, v in headers.items()}

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        # timeout 必须按关键字传：真实客户端的签名是
        # `request(method, path, body=None, *, timeout=...)`（timeout 是
        # keyword-only）。按位置传第 4 个参数在真机客户端上是 TypeError，
        # 离线 stub 用 `**kwargs` 才没暴露——2026-11-11 接进 bridge 前抓到。
        raw = await self._invoke("request", method, path, body, timeout=timeout)
        return self._response(raw)

    def _node_id(self) -> str:
        maker = getattr(self.client, "new_task_node", None)
        if callable(maker):
            return str(maker())
        return uuid.uuid4().hex

    # ---- 事件流 ----

    async def stream_chat(
        self, prompt: str, *, timeout: float = 300.0
    ) -> AsyncIterator[dict[str, Any]]:
        """{"type":"delta"|"media"|"done"|"refused"} 事件流。"""
        stream = self._run(prompt, expect=None, timeout=timeout)
        try:
            async for event in stream:
                yield event
        finally:
            await _close_stream(stream)

    async def collect_chat(
        self, prompt: str, *, timeout: float = 300.0
    ) -> tuple[str, list[dict[str, Any]]]:
        parts: list[str] = []
        media: list[dict[str, Any]] = []
        done_text: str | None = None
        async for event in self.stream_chat(prompt, timeout=timeout):
            if event["type"] == "delta":
                parts.append(str(event.get("text") or ""))
            elif event["type"] == "media":
                media.append(event["ref"])
            elif event["type"] == "done":
                # `done` may carry the authoritative full text (history recovery
                # replaces a truncated post-reconnect answer); fall back to the
                # accumulated deltas when it is absent.
                done_text = str(event.get("text") or "")
        return (done_text if done_text is not None else "".join(parts)), media

    async def generate_image(
        self,
        prompt: str,
        *,
        references: list[str] | None = None,
        timeout: float = 600.0,
        on_progress: Callable[[int], None] | None = None,
    ) -> GenerationResult:
        return await self._generate(
            prompt, expect="image", references=references,
            timeout=timeout, on_progress=on_progress,
        )

    async def generate_video(
        self,
        prompt: str,
        *,
        references: list[str] | None = None,
        timeout: float = 600.0,
        on_progress: Callable[[int], None] | None = None,
    ) -> GenerationResult:
        return await self._generate(
            prompt, expect="video", references=references,
            timeout=timeout, on_progress=on_progress,
        )

    async def quota(self) -> dict[str, Any]:
        """高频可查的会话侧额度（无 UI）。

        上游没有额度端点：UI 路径（czg engine.py:888-908）要点开设置面板抓文本，
        纯协议不可行（`docs/PROTOCOL_QUOTA.md`：418 内层白名单无 usage/quota 命名，
        5 条候选路径真机全 404）。能读的只有 `/api/session` 的 vm_state 与
        hatch_vml 到期时间。**estimated_expiry_at 是推断**：上游只在 cookie 里带
        hatch_vml 过期时间，拿不到就返回 None，不编造。

        **走哪一层（2026-11-11 真机更正）**：`/api/session` **不在 Noise 内层路由表**
        —— 内层 `_request("GET", "/api/session")` 在真机上只会拿到
        `404 route not found`。客户端因此把外层 HTTP 读法暴露成
        `HatchProtocolClient.session_quota()`（内部走 `session_http.renew_session`，
        带 hatch_sess cookie）；此处优先用它，下面的内层 `_request` 分支只留给
        离线 Fake 客户端/旧客户端，保持 duck 契约不变。
        """
        snapshot = getattr(self.client, "session_quota", None)
        if callable(snapshot):
            info = await snapshot()
            expiry = _as_int(info.get("estimated_expiry_at"))
            return {
                "renewable": bool(info.get("renewable")),
                "vm_state": session_http.normalize_vm_state(info.get("vm_state")) or "",
                "estimated_expiry_at": expiry,
            }
        status, body, headers = await self._request("GET", "/api/session", None, 20)
        if status == 401:
            raise ProtocolGenerationError("会话已失效（/api/session HTTP 401）")  # 会话死了该换号/重登，不是模型拒绝
        if status >= 400:
            raise ProtocolGenerationError(f"/api/session HTTP {status}")
        payload = body.get("result") if isinstance(body.get("result"), dict) else body
        refreshed = str(headers.get("x-hatch-session-refreshed") or "").strip() == "1"
        assigned = str(payload.get("status") or "") == "assigned"
        vm_state = session_http.normalize_vm_state(payload.get("vm_state")) or ""
        expiry = _as_int(payload.get("expires_at") or payload.get("estimated_expiry_at"))
        if expiry is None and (refreshed or assigned) and vm_state == session_http.RUNNING:
            expiry = int(self._time()) + session_http.VML_TTL_SECONDS
        return {
            "renewable": bool(refreshed or assigned),
            "vm_state": vm_state,
            "estimated_expiry_at": expiry,
        }

    # ---- 内部 ----

    def _remember_upload_ref(self, path: str) -> None:
        """记下一条本引擎上传过的路径，**有界**（FIFO 淘汰最旧）。

        为什么必须有界：同一个 engine 被账号级驱动长期持有，一次生成最多留下
        `refs.MAX_REFERENCE_FILES` 条上传路径；只增不清会随进程寿命无界增长。
        为什么不是“每次生成前 clear”：`upload_refs` 的用途是把事件流里**回显的
        上传件**排除在成品候选之外（否则生成会退化成“把输入图当输出”），跨轮
        保留最近几轮比只保留当前轮更安全。所以保留最近 `UPLOAD_REFS_MAX`
        （默认 8 轮满额），超出按 FIFO 淘汰。

        证据等级：当前轮的“回显上传件不得当成品”有真机反证（见
        tests/test_protocol_staging.py 的同名用例）；“跨轮保留 / 淘汰窗口”
        是按内存有界的推断，未在真机上验证过 8 轮之后的回显行为。
        """
        if path in self.upload_refs:
            return
        self._upload_ref_order.append(path)
        self.upload_refs.add(path)
        while len(self._upload_ref_order) > UPLOAD_REFS_MAX:
            self.upload_refs.discard(self._upload_ref_order.pop(0))

    async def _stage_references(self, references: list[str] | None) -> list[dict[str, Any]]:
        """参考素材 → `/chat/stream` 的 items：**先整批校验，再逐个上传**。

        校验在任何 fs_write 之前完成，且与浏览器路径（`driver.py:660`）共用
        `refs.normalize_references`：数量（10）/ 单件（40 MiB）/ 总量（100 MiB）/
        类型白名单 / 视频时长（120 秒）对同一批素材必须给出同一结论。

        两类形态刻意分开处理：
          * 远端路径（http(s)/workspace/sandbox/绝对路径）不在这里下载——纯协议
            侧没有抓网授权——原样作为 `path_ref` 交给客户端。这意味着它们的单件/
            总量大小要到客户端/服务端解析时才知道，本层只有“数量”生效。
          * 内联素材（Data URL / 裸 base64）走 refs 全套判定；**空载荷 Data URL
            必须在这里被拒绝**，不能因为 `decode_reference` 返回空字节就落进
            `path_ref` 分支（那会把一个空文件当成假路径发出去）。

        裸 base64 的可读错误措辞（“不是有效的 data URL / base64”“无法识别为已知
        媒体类型”）由 `decode_reference` 保留；限额与类型白名单交给 refs 判定。
        """
        values = list(references or [])
        # 数量是**输入框整体**的限制（远端路径引用同样占位），先按 refs 的上限拦。
        if len(values) > refs.MAX_REFERENCE_FILES:
            raise ProtocolGenerationError(
                f"参考素材最多 {refs.MAX_REFERENCE_FILES} 个（收到 {len(values)} 个）"
            )

        inline_texts: list[str] = []
        for index, value in enumerate(values):
            text = "" if value is None else str(value)
            if not text.strip():
                raise ProtocolGenerationError(f"第 {index + 1} 个参考素材为空")
            if _is_path_reference(text):
                continue
            inline_texts.append(text)

        for text in inline_texts:
            if not text.startswith("data:"):
                decode_reference(text.strip())  # 只借它的报错措辞，数据由 refs 判定
        normalized = _normalize_inline_references(inline_texts)

        staged: list[dict[str, Any]] = []
        cursor = 0
        for index, value in enumerate(values):
            text = str(value)
            if _is_path_reference(text):
                _data, mime = decode_reference(text)
                staged.append({"type": "path_ref", "mime_type": mime, "path": text})
                continue
            reference = normalized[cursor]
            cursor += 1
            name = f"reference_{index}_{uuid.uuid4().hex[:8]}{reference.ext}"
            ref = await self._invoke("fs_write", reference.data, reference.mime, name)
            if not isinstance(ref, dict) or not ref.get("path"):
                raise ProtocolGenerationError(f"fs.write 未返回 file_ref（第 {index} 个参考素材）")
            # `/chat/stream` 的 `items[].path` 必须是 workspace 相对路径（真机证据：
            # 2026-11-11 带 `sandbox://` 前缀时服务端 500 "failed to resolve
            # file_ref path sandbox:/workspace/…"；hatch-web 07hpppij0ssd9.js@67023
            # 序列化的就是 `uploadPath`，无 URI 前缀）。fs_write 的返回值保留 URI
            # 形态给 fs_read/事件对比用，这里只在发请求前去掉前缀。
            self._remember_upload_ref(str(ref["path"]))
            remote = str(ref["path"])
            if remote.startswith(SANDBOX_PREFIX):
                remote = remote[len(SANDBOX_PREFIX) :]
            staged.append(
                {
                    "type": "file_ref",
                    "mime_type": ref.get("mime_type") or reference.mime,
                    "path": remote,
                    "filename": ref.get("filename") or name,
                }
            )
        return staged

    async def _fetch_bytes(self, item: MediaResource) -> bytes:
        reader = getattr(self.client, "fs_read", None)
        if callable(reader):
            data = await self._invoke("fs_read", item.path)
        else:  # 兼容上游 client.download_bytes(resource)
            data = await self._invoke("download_bytes", item.raw)
        if not isinstance(data, (bytes, bytearray)):
            raise ProtocolGenerationError(f"fs.read 返回的不是字节：{type(data).__name__}")
        return bytes(data)

    def _extra_candidates(self, node_id: str, out: list[MediaResource]) -> None:
        """客户端若提供 media_candidates，作为补充来源，走同一套排除规则。"""
        getter = getattr(self.client, "media_candidates", None)
        if not callable(getter):
            return
        try:
            listing = getter(node_id)
        except TypeError:
            listing = getter()
        if inspect.isawaitable(listing):
            return  # 异步补充来源不在事件循环里同步等待，交给 _run 之前预取
        for item in listing or []:
            collect_media(item, out)

    async def _prefetch_media(self, node_id: str, out: list[MediaResource]) -> None:
        getter = getattr(self.client, "media_candidates", None)
        if not callable(getter):
            return
        try:
            listing = getter(node_id)
        except TypeError:
            listing = getter()
        if inspect.isawaitable(listing):
            listing = await listing
        for item in listing or []:
            collect_media(item, out)

    async def _generate(
        self,
        prompt: str,
        *,
        expect: str,
        references: list[str] | None,
        timeout: float,
        on_progress: Callable[[int], None] | None,
    ) -> GenerationResult:
        # This coroutine never yields to a consumer: its full lifetime can safely
        # own cancellation, including staging, prefetch and the final file fetch.
        try:
            async with asyncio.timeout(timeout) as budget:
                return await self._generate_with_budget(
                    prompt, expect=expect, references=references,
                    timeout=timeout, on_progress=on_progress,
                )
        except TimeoutError as exc:
            if not budget.expired():
                # A transport/staging/fetch operation can time out before the
                # overall generation budget. Do not misreport that as exhaustion.
                raise ProtocolGenerationError("纯协议上游操作超时，未收到完整结果") from exc
            raise ProtocolGenerationError("纯协议生成超时，未收到完整结果") from exc

    async def _generate_with_budget(
        self,
        prompt: str,
        *,
        expect: str,
        references: list[str] | None,
        timeout: float,
        on_progress: Callable[[int], None] | None,
    ) -> GenerationResult:
        started = self._monotonic()
        if expect not in ("image", "video"):
            raise ProtocolGenerationError(f"不支持的生成类型：{expect}")
        if not (prompt or "").strip():
            raise ProtocolGenerationError("生成提示词为空")
        staged = await self._stage_references(references)
        candidates: list[MediaResource] = []
        text_parts: list[str] = []
        seen: set[Any] = set()
        done_at: float | None = None
        grace_expired = False
        fired: set[int] = set()
        node_id = self._node_id()

        def progress(milestone: int) -> None:
            if milestone in fired or milestone not in MILESTONES:
                return
            fired.add(milestone)
            if on_progress is not None:
                on_progress(milestone)

        def matching(kind: str) -> list[MediaResource]:
            return [
                item
                for item in candidates
                if item.kind == kind and item.path not in self.upload_refs
            ]

        stream = self.client.read_stream(  # type: ignore[attr-defined]
            prompt, staged, node_id=node_id, timeout=timeout, **self._stream_extras()
        )
        try:
            await self._prefetch_media(node_id, candidates)
            iterator = aiter(stream)
            while True:
                now = self._monotonic()
                remaining = timeout - (now - started)
                grace_remaining = (self.grace_seconds - (now - done_at)
                                   if done_at is not None else None)
                if remaining <= 0:
                    raise ProtocolGenerationError("纯协议生成超时，未收到完整结果")
                if grace_remaining is not None and grace_remaining <= 0:
                    grace_expired = True
                    break
                wait_budget = (min(remaining, grace_remaining)
                               if grace_remaining is not None else remaining)
                try:
                    async with asyncio.timeout(wait_budget) as wait_timeout:
                        raw = await anext(iterator)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    if (wait_timeout.expired() and grace_remaining is not None
                            and grace_remaining <= remaining):
                        grace_expired = True
                        break
                    raise
                if self._monotonic() - started > timeout:
                    raise ProtocolGenerationError("纯协议生成超时，未收到完整结果")
                if not isinstance(raw, dict):
                    continue
                if raw.get("type") == "pool_error":
                    raise ProtocolGenerationError("Noise 连接已断开，请重试")
                sequence = raw.get("seq")
                if sequence is not None:
                    if sequence in seen:
                        continue
                    seen.add(sequence)
                name = raw.get("event") or raw.get("type")
                payload = raw.get("payload")
                if name == "res" or not isinstance(payload, dict):
                    continue
                device = (payload.get("chat_context") or {}).get("device_id") \
                    if isinstance(payload.get("chat_context"), dict) else None
                ours = device is None or str(device) == node_id

                if name == "delta.message_done" and ours:
                    status = str(payload.get("status") or "").strip().lower()
                    if status == "interrupted":
                        # Explicit upstream abort: not a refusal, not success, and
                        # media carried by this event must not masquerade as output.
                        raise ProtocolInterrupted(
                            "Muse 中止了这次生成（message_done.status=interrupted）"
                        )

                if name == "delta.text_append" and ours:
                    progress(MILESTONES[0])
                    delta = str(payload.get("text") or "")
                    if delta:
                        text_parts.append(delta)
                        progress(40)
                        if QUOTA_TEXT_RE.search(delta):
                            raise ProtocolQuotaExhausted(f"账号额度不足：{delta[:120]}")
                if name in (
                    "delta.presentation",
                    "delta.message_done",
                    "delta.tool_done",
                    "delta.placeholder",
                ) and ours:
                    before = len(candidates)
                    collect_media(payload, candidates)
                    if len(candidates) > before:
                        progress(50)
                if name == "delta.message_done" and ours:
                    status = payload.get("status")
                    transcript = payload.get("transcript")
                    text = self._transcript_text(transcript)
                    if status not in (None, "completed"):
                        reason = text or str(status)
                        if QUOTA_TEXT_RE.search(reason):
                            raise ProtocolQuotaExhausted(f"账号额度不足：{reason[:120]}")
                        raise ProtocolModelRefusal(f"模型未生成媒体，仅返回文本: {reason[:240]}")
                    if not text_parts and text:
                        text_parts.append(text)
                    if done_at is None:
                        done_at = self._monotonic()
                        progress(75)
                if name == "delta.message_removed" and ours:
                    raise ProtocolModelRefusal("Muse 在完成前移除了这次生成")

                if done_at is not None:
                    # Completed media is final; otherwise wait only the smaller
                    # of the remaining absolute budget and the late-media grace.
                    if matching(expect):
                        break
                    if self._monotonic() - done_at >= self.grace_seconds:
                        break
        finally:
            await _close_stream(stream)

        wanted = matching(expect)
        if wanted:
            item = wanted[-1]
            data = await self._fetch_bytes(item)
            progress(100)
            mime = item.mime or refs.sniff_mime(data) or "application/octet-stream"
            return GenerationResult(
                data=data,
                mime=mime,
                kind=item.kind,
                width=item.width,
                height=item.height,
                duration_ms=item.duration_ms or _duration_ms(data, mime),
                seed=item.seed,
                source_prompt=prompt,
                path=item.path,
            )
        if done_at is not None and (
            grace_expired or self._monotonic() - done_at >= self.grace_seconds
        ):
            # 上游 :498-499 只抛通用错误；这里区分「只拿到封面/别的模态」与「什么都没拿到」，
            # 让调用方能分辨要不要换号（模态不符可换，纯无成品也可换，但语义要写清）。
            strays = [item for item in candidates if item.path not in self.upload_refs]
            if strays:
                raise ProtocolMediaTypeError(
                    f"生成完成但只拿到 {strays[-1].kind}，期望 {expect}"
                    f"（封面静帧不得冒充成品）"
                )
            raise ProtocolGenerationError(f"生成完成但未收到 {expect} 成品")
        raise ProtocolGenerationError(f"未收到 {expect} 成品")

    async def _recover_resumed_chat(self, partial: str) -> str | None:
        """Try to replace a truncated post-reconnect answer with committed history.

        Live probes showed events produced during a disconnect are not replayed,
        so the only trustworthy recovery source is the committed history page.
        The guard is deliberately strict: the committed assistant text must be a
        *strict superset* that starts with everything already streamed to the
        caller. Anything else returns None, and the caller keeps the incomplete
        error.

        Note: history event seq and the live subscription stream seq are
        **different sequences** (live 2026-11-14: committed newest 683 while the
        live cursor was 734 for the same answer), so cursor comparison must not
        be used as a freshness check. The prefix identity check is the guard.
        """
        if not self.history_recovery:
            return None
        if not partial or getattr(self.client, "chat_history", None) is None:
            return None
        try:
            page = await self._invoke("chat_history", limit=20, timeout=30.0)
        except Exception:  # noqa: BLE001 - recovery must never mask the real error
            return None
        full = latest_assistant_text(page)
        if not full or not full.startswith(partial) or len(full) <= len(partial):
            return None
        return full

    async def _run(
        self, prompt: str, *, expect: str | None, timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        """chat 路径：只出文本与媒体引用，不做模态裁决。"""
        del expect  # chat 不挑模态
        candidates: list[MediaResource] = []
        text_parts: list[str] = []
        seen: set[Any] = set()
        done = False
        refused: str | None = None
        started = self._monotonic()
        node_id = self._node_id()
        stream = self.client.read_stream(  # type: ignore[attr-defined]
            prompt, [], node_id=node_id, timeout=timeout, **self._stream_extras()
        )
        try:
            iterator = aiter(stream)
            while True:
                remaining = timeout - (self._monotonic() - started)
                if remaining <= 0:
                    raise ProtocolGenerationError("纯协议生成超时，未收到完整结果")
                try:
                    # Never leave a timeout active across yield: cancellation
                    # belongs only to our upstream wait, not the consumer's work.
                    async with asyncio.timeout(remaining) as wait_budget:
                        raw = await anext(iterator)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    if not wait_budget.expired():
                        raise ProtocolGenerationError("纯协议上游操作超时，未收到完整结果") from exc
                    raise ProtocolGenerationError("纯协议生成超时，未收到完整结果") from exc
                if self._monotonic() - started > timeout:
                    raise ProtocolGenerationError("纯协议生成超时，未收到完整结果")
                if not isinstance(raw, dict):
                    continue
                if raw.get("type") == "pool_error":
                    raise ProtocolGenerationError("Noise 连接已断开，请重试")
                sequence = raw.get("seq")
                if sequence is not None:
                    if sequence in seen:
                        continue
                    seen.add(sequence)
                name = raw.get("event") or raw.get("type")
                payload = raw.get("payload")
                if name == "res" or not isinstance(payload, dict):
                    continue
                device = (payload.get("chat_context") or {}).get("device_id") \
                    if isinstance(payload.get("chat_context"), dict) else None
                ours = device is None or str(device) == node_id

                if name == "delta.message_done" and ours:
                    status = str(payload.get("status") or "").strip().lower()
                    if status == "interrupted":
                        # Explicit upstream abort: not a refusal, not success, and
                        # media carried by this event must not masquerade as output.
                        raise ProtocolInterrupted(
                            "Muse 中止了这次对话（message_done.status=interrupted）"
                        )

                if name == "delta.text_append" and ours:
                    delta = str(payload.get("text") or "")
                    if delta:
                        text_parts.append(delta)
                        if QUOTA_TEXT_RE.search(delta):
                            raise ProtocolQuotaExhausted(f"账号额度不足：{delta[:120]}")
                        yield {"type": "delta", "text": delta}
                if name in (
                    "delta.presentation",
                    "delta.message_done",
                    "delta.tool_done",
                    "delta.placeholder",
                ) and ours:
                    # Media is subject to the same turn-ownership check as text.
                    before = len(candidates)
                    collect_media(payload, candidates)
                    for item in candidates[before:]:
                        yield {"type": "media", "ref": item.to_event()}
                if name == "delta.message_done" and ours:
                    transcript = payload.get("transcript")
                    text = self._transcript_text(transcript)
                    if status and status != "completed":
                        refused = text or "".join(text_parts) or status
                    elif not text_parts and text:
                        text_parts.append(text)
                        yield {"type": "delta", "text": text}
                    done = True
                    break
                if name == "delta.message_removed" and ours:
                    raise ProtocolModelRefusal("Muse 在完成前移除了这次生成")
        finally:
            await _close_stream(stream)
        if not done:
            elapsed = self._monotonic() - started
            # A finite stream can end through disconnect/idle timeout just as it
            # can through agent.status. Only the client's recognized terminal
            # signal confirms the latter; partial text is never proof of success.
            terminal = getattr(self.client, "turn_terminal_reason", None)
            resumed = getattr(self.client, "_turn_resumed", False) is True
            if getattr(self.client, "_turn_complete", False) is True and elapsed < timeout:
                if resumed and terminal != "delta.message_done":
                    # Live probes show events emitted while the socket was down are
                    # not replayed, and seq is not contiguous even without a drop.
                    # A generic status terminal can therefore close a truncated
                    # answer; only an authoritative message_done is trusted.
                    # Optional recovery: if committed history contains a strict
                    # superset of what we streamed, return the complete text.
                    recovered = await self._recover_resumed_chat("".join(text_parts))
                    if recovered is None:
                        raise ProtocolGenerationError(
                            "断线重连后未收到权威完成事件，正文连续性无法确认"
                            f"（已收 {len(text_parts)} 段，用时 {elapsed:.1f}s/{timeout}s）"
                        )
                    text_parts[:] = [recovered]
                done = True
            else:
                raise ProtocolGenerationError(
                    "纯协议对话未收到完整结果"
                    f"（流已结束：正文 {len(text_parts)} 段，用时 {elapsed:.1f}s/{timeout}s）"
                )
        if refused is not None:
            if QUOTA_TEXT_RE.search(refused):
                raise ProtocolQuotaExhausted(f"账号额度不足：{refused[:120]}")
            yield {"type": "refused", "text": refused}
            raise ProtocolModelRefusal(f"模型未生成媒体，仅返回文本: {refused[:240]}")
        yield {"type": "done", "text": "".join(text_parts)}

    @staticmethod
    def _transcript_text(transcript: Any) -> str:
        """上游 :455-462：message_done 可能不带 delta，正文在 transcript 里。"""
        if not isinstance(transcript, dict):
            return ""
        out: list[str] = []
        for message in transcript.get("messages") or []:
            if not isinstance(message, dict):
                continue
            for part in message.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    out.append(str(part.get("text") or ""))
        return "".join(out)


def _duration_ms(data: bytes, mime: str) -> int | None:
    """资源没给 duration_ms 时，从容器里读（czg engine.py:126-207 的做法）。"""
    try:
        seconds = refs.video_duration(data, mime)
    except Exception:
        return None
    return int(round(seconds * 1000)) if seconds else None


def _media_to_event(self: MediaResource) -> dict[str, Any]:
    return {
        "path": self.path,
        "mime": self.mime,
        "kind": self.kind,
        "width": self.width,
        "height": self.height,
        "duration_ms": self.duration_ms,
        "seed": self.seed,
    }


MediaResource.to_event = _media_to_event  # type: ignore[attr-defined]
