r"""成品媒体落盘与鉴权下载的后半段：本地存储、token 校验、Range 读取、过期清理。

为什么必须落本地（而不是把上游直链交给客户端）：

  * muse 的成品是 `muse.ai/files/...` 直链，约 **2 天过期**（docs/PROTOCOL.md:131），
    而且挂在会话 cookie 的域里。把直链写进任务结果，等于让客户端在一个
    两天后必然 404、且需要浏览器身份才能取的路径上重试。
  * 上游那条媒体路由自己不带鉴权（re-surface 报告：muse2api app.py:1822 只按
    文件名取文件），所以"能取到"就等于"知道名字就能取"。
  * 结论：成品一律进 `MEDIA_DIR`，对外只暴露 **随机 token 路径**；鉴权由 app 层
    挂 `require_key`（app.py:31）。本模块只保证"给对 token 才能拿到字节"。

为什么视频判定要单独钉死（上游 U-MVD-02..05）：
视频任务在视频真正生成完之前，页面上先出现的往往是**封面图** `.jpg`。
上游实测的判定在 engine.py:1911-1913：扩展名不在视频集合里就不算 video，
而"期望视频却拿到图片扩展"必须直接拒收——拿封面当成品交付，客户端会以为
一条 5 秒视频生成成功了，实际什么都没有。这里把同一条规则做成纯函数，离线可测。

安全边界（与 refs.py 的 SSRF 判定分层，各管一段）：
  * token 只接受 **32 位小写 hex**（`uuid4().hex` 的形状）。非字符串、带分隔符、
    带 `..`、长度不对全判非法；非法 token 与不存在的文件一律同一处理，
    不提供"存在但被拒"这种可探测的差异。
  * 读取前把路径解析出来并确认仍在 `root` 内（纵深防御：即便 token 校验被
    后来的改动绕过，也读不到 root 之外）。
  * 类型判定认文件头，不认调用方声明的 mime（refs.py:443-446 同一条理由：
    声明是别人写的）。

对上游/规格的两处偏离，写在代码旁边而不是提交信息里：
  1. 上游 engine.py:1911 的视频扩展名集合是 `(".mp4", ".webm", ".mov")`，**漏了
     `.3gp`**；本模块用 `refs.VIDEO_EXTS`（含 `.3gp`），与 refs.py:140 的
     输入侧集合保持同一个事实来源。理由：muse 前端白名单接受 3GP 视频，
     输出侧不认它会把合法成品判成拒收。
  2. `Content-Type` 按约定恒为 `application/octet-stream`，真实 mime 通过
    `X-Muse-Media-Mime` 响应头与 `MediaRef.mime` 元数据交付。理由：既满足
    "octet-stream 直发 + 正确 mime"，又不给浏览器就地渲染的机会。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import observability
from .env import env_positive_int
from .refs import VIDEO_EXTS, normalize_mime, reference_kind, sniff_mime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 配置

# 落盘目录。默认放在 data/ 下（.gitignore:12 已忽略 data/，成品不会误提交）。
# 偏离说明：env.py 只有数值解析器（env_int/env_positive_int/env_port），
# 没有路径型解析器，所以这一项按 refs.py:79 的写法直接读 os.environ。
DEFAULT_MEDIA_DIR = os.environ.get("MUSE_MEDIA_DIR", "./data/media")

# 成品保留时长：默认 2 天，与上游直链的过期节奏一致（PROTOCOL.md:131）。
# 留更久只是把磁盘变成负债；留更短会让慢客户端拿不到已付款的成品。
MEDIA_TTL_SECONDS = env_positive_int("MUSE_MEDIA_TTL", "172800")

# 单个成品上限（10 秒视频可到几十 MiB）。超限直接拒写，不写半截文件。
MAX_MEDIA_BYTES = env_positive_int("MUSE_MAX_MEDIA_BYTES", "268435456")  # 256 MiB

# 后台清扫间隔（秒），默认 1 小时。`sweep()` 是幂等的：跑密一点只是多几次目录
# 遍历。真正的保留时长是 MEDIA_TTL_SECONDS，两者互不影响。
MEDIA_SWEEP_INTERVAL_SECONDS = env_positive_int("MUSE_MEDIA_SWEEP_INTERVAL", "3600")

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
PART_SUFFIX = ".part"
_SNIFF_HEAD = 32  # 文件头判定需要的字节数（RIFF/WEBP 的形态在 12 字节内）

# 已知扩展名 -> kind。图片集合来自上游 `_pick_ext`（engine.py:1923-1930），
# 视频集合与 refs.VIDEO_EXTS 一致（含 .3gp，见模块头偏离 1）。
EXT_KIND: dict[str, str] = {
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".3gp": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".heic": "image",
    ".heif": "image",
    ".avif": "image",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".ogg": "audio",
    ".opus": "audio",
    ".flac": "audio",
    ".aac": "audio",
}

# mime 关键词 -> 扩展名。顺序与匹配写法逐条对齐 engine.py:1923-1927
# （注意 "jpeg" 排在 "jpg" 前：上游就是这么排的，改成 dict 会丢顺序语义）。
MIME_EXT_ORDER: tuple[tuple[str, str], ...] = (
    ("mp4", ".mp4"),
    ("webm", ".webm"),
    ("png", ".png"),
    ("jpeg", ".jpg"),
    ("jpg", ".jpg"),
    ("webp", ".webp"),
    ("gif", ".gif"),
    # 上游 `_pick_ext` 没有这两条：3GP 与 QuickTime 会一路掉到 `.png` 兜底，
    # 于是合法视频被存成图片扩展名。补齐，与 refs.py 输入侧集合同一事实来源。
    ("3gp", ".3gp"),
    ("quicktime", ".mov"),
)

# URL 里扫扩展名的顺序，对齐 engine.py:1928-1930。
URL_EXT_ORDER: tuple[str, ...] = (".mp4", ".webm", ".png", ".jpg", ".webp")


# ---------------------------------------------------------------- 异常


class MediaError(RuntimeError):
    """媒体侧失败基类。子类带 status_code，app 层直接映射，不再写 isinstance 链。"""

    status_code = 500


class MediaTokenError(MediaError):
    """token 形状非法。按 404 处理：非法 token 与不存在的文件不给可区分信号。"""

    status_code = 404


class MediaNotFound(MediaError):
    """token 合法但文件不在（没落盘，或已被 sweep 清掉）。"""

    status_code = 404


class MediaKindError(MediaError):
    """成品类型与期望不符（封面冒充视频、期望图片却给了视频）。

    这是上游交付缺陷，不是调用方写错请求：调用方改不动它，也不该被静默成功骗过去。
    映射 502 而不是 400。
    """

    status_code = 502


class MediaTooLargeError(MediaError):
    """成品超过 MUSE_MAX_MEDIA_BYTES。落盘前判定，不写半截文件。"""

    status_code = 502


class MediaRangeError(MediaError):
    """Range 不可满足（起点越界 / 起点大于终点）。app 层据此回 416 + Content-Range。"""

    status_code = 416


# ---------------------------------------------------------------- 纯函数


def ext_from_mime(mime: str) -> str:
    """声明 mime -> 扩展名。找不到返回空串，由调用方继续按 URL / 期望形态决定。"""
    lowered = (normalize_mime(mime) or "").lower()
    for key, ext in MIME_EXT_ORDER:
        if key in lowered:
            return ext
    return ""


def ext_from_url(url: str) -> str:
    """URL 里扫已知扩展名。上游附件常是 `.../files/xxxx.mp4?sig=...` 这种形状。"""
    lowered = (url or "").lower()
    for ext in URL_EXT_ORDER:
        if ext in lowered:
            return ext
    return ""


def ext_from_expect(expect: str) -> str:
    """什么线索都没有时的兜底：期望视频给 .mp4，期望图片给 .png（engine.py:1931）。"""
    return ".mp4" if (expect or "").strip().lower() == "video" else ".png"


def ext_for(mime: str = "", url: str = "", expect: str = "") -> str:
    """三级判定，顺序与上游 `_pick_ext` 一致：声明 mime → URL 扩展名 → 期望形态。"""
    return ext_from_mime(mime) or ext_from_url(url) or ext_from_expect(expect)


def kind_of(value: str = "", mime: str = "") -> str:
    """kind 判定：入参可以是扩展名（`.mp4` / `mp4`）或文件名，也可以给 mime 兜底。

    优先按扩展名——"期望视频拿到 .jpg"这条缺陷只有看最终落盘形态才钉得住，
    而声明里的 mime 恰恰是上游最容易写错的那一环。
    """
    text = (value or "").strip().lower()
    if text:
        if not text.startswith("."):
            ext = os.path.splitext(text)[1].lower()
            if ext in EXT_KIND:
                return EXT_KIND[ext]
        dotted = text if text.startswith(".") else f".{text}"
        if dotted in EXT_KIND:
            return EXT_KIND[dotted]
    return reference_kind(mime)


def is_video_ext(ext: str) -> bool:
    return (ext or "").lower() in VIDEO_EXTS


#: OpenAI 的 `b64_json` 没有 mime 字段，客户端普遍按 PNG 解码。muse 的成品常是
#: WebP（真机证据：image 产出 `RIFF....WEBP`），直接塞 b64 会让严格客户端报
#: "not a PNG file"。这里只把 WebP 转成真 PNG；PNG 原样返回，其它格式不碰
#: （JPEG→PNG 只会把体积放大数倍，而 JPEG 客户端本来就能解）。
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_deliverable(data: bytes) -> bytes:
    """把 WebP 成品转成 PNG，供 `/v1/images/*` 的 `b64_json` 交付。

    依赖是**可选的**：Pillow 缺席时原样返回（不因为格式转换把图片接口打死），
    无法解码的字节同样原样返回，让调用方拿到真实内容而不是被吞掉的空值。
    转换失败不抛异常的理由同 `decide` 的拒收判定：这里只负责"更好交付"，
    内容是否合法已经由 `sniff_mime` / `decide` 在落盘前判过。
    """
    if not data or data.startswith(PNG_MAGIC):
        return data
    if sniff_mime(data[:_SNIFF_HEAD]) != "image/webp":
        return data
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 取决于运行环境是否装了 Pillow
        return data
    try:
        image = Image.open(io.BytesIO(data))
        if image.mode in ("RGBA", "LA"):
            converted = image
        elif image.mode == "P" and "transparency" in image.info:
            converted = image.convert("RGBA")
        else:
            converted = image.convert("RGB")
        buffer = io.BytesIO()
        converted.save(buffer, format="PNG", optimize=True)
        png = buffer.getvalue()
    except Exception:  # noqa: BLE001 - 可选增强，失败回退原始字节
        return data
    return png if png.startswith(PNG_MAGIC) else data


def decide(
    *,
    mime: str = "",
    url: str = "",
    expect: str = "",
    kind: str = "",
    head: bytes = b"",
) -> tuple[str, str, str]:
    """把"这份成品该用什么扩展名、什么 mime、什么 kind"一次定死。

    返回 (ext, final_mime, final_kind)，并在这里做全部拒收判定，
    这样 `save`（内存）与 `save_path`（流式）不会各自演化出两套规则。
    """
    requested = (expect or kind or "").strip().lower()
    ext = ext_for(mime, url, requested)
    sniffed = sniff_mime(head) if head else None
    final_mime = normalize_mime(sniffed or mime) or "application/octet-stream"
    if sniffed:
        # 文件头优先，并把扩展名改成与真实内容一致的那个。
        header_ext = ext_from_mime(sniffed)
        if header_ext and header_ext != ext:
            ext = header_ext
    final_kind = kind_of(ext, final_mime)

    if requested == "video" and final_kind != "video":
        raise MediaKindError(
            f"期望生成视频，但成品是 {final_kind or '未知类型'}（{ext or '无扩展名'}），"
            "拒绝以封面/图片冒充视频交付"
        )
    if requested == "image" and final_kind == "video":
        raise MediaKindError(f"期望生成图片，但成品是视频（{ext}），拒绝交付")
    if final_kind == "video" and not is_video_ext(ext):
        raise MediaKindError(
            f"视频成品的扩展名 {ext or '空'} 不在允许集合 "
            f"{'/'.join(sorted(VIDEO_EXTS))} 内，拒绝交付"
        )
    if reference_kind(final_mime) == "video" and final_kind != "video":
        # mime 说是视频、扩展名却不认（avi/mkv 这类 muse 不产出的形态）：抓取环节
        # 已经错了。上游 `_pick_ext` 在这里会静默落成一个 `.png` 命名的视频文件，
        # 客户端两头对不上——拒收比猜一个扩展名诚实。
        raise MediaKindError(
            f"声明是视频（{final_mime}），但落盘形态是 {ext or '无扩展名'}，"
            f"不在允许集合 {'/'.join(sorted(VIDEO_EXTS))} 内，拒绝交付"
        )
    if not final_kind:
        final_kind = reference_kind(final_mime) or "document"
    return ext, final_mime, final_kind


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单区间 Range，返回闭区间 (start, end)；不可解析/多区间返回 None（发整文件）。

    只做单区间是有意的：多区间要带 multipart/byteranges 与边界串，
    它在代理链上的坑远多于它在这里的价值。
    """
    if not header or size <= 0:
        return None
    text = str(header).strip()
    if not text.lower().startswith("bytes="):
        return None
    specs = [item.strip() for item in text[6:].split(",") if item.strip()]
    if len(specs) != 1:
        return None
    first, sep, last = specs[0].partition("-")
    if not sep or (not first and not last):
        return None
    if not first:
        # `bytes=-N`：末尾 N 字节。
        try:
            count = int(last)
        except ValueError:
            return None
        if count <= 0:
            return None
        return max(0, size - min(count, size)), size - 1
    try:
        start = int(first)
    except ValueError:
        return None
    if start < 0:
        return None
    if not last:
        return min(start, size - 1), size - 1
    try:
        end = int(last)
    except ValueError:
        return None
    return start, min(end, size - 1)


def serve_headers(*, size: int, filename: str, mime: str = "") -> dict[str, str]:
    """下载响应头。

    Content-Type 恒为 octet-stream（见模块头偏离 2），真实类型走
    `X-Muse-Media-Mime` + 元数据；再配 `nosniff` 与 `attachment`，
    不给浏览器就地渲染的机会。filename 只用 token + 扩展名（本模块自己生成），
    不含任何调用方字符串，所以没有响应头注入面。
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "", filename)
    if not safe or safe != filename:
        # 被改写过就整体不用它：只删危险字符会留下攻击者的文字片段
        # （`a"\r\nX-Evil: 1` 会被删成 `aX-Evil1`，注入没了但文字还在头里）。
        # 名字本身只是给人看的，退回固定名 + 原扩展名最省事也最安全。
        ext = os.path.splitext(filename)[1].lower()
        ext = ext if re.fullmatch(r"\.(png|jpg|jpeg|gif|webp|mp4|webm|mov|3gp|bin)", ext) else ""
        safe = f"media{ext or '.bin'}"
    return {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
        "Content-Disposition": f'attachment; filename="{safe}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Muse-Media-Mime": mime or "application/octet-stream",
    }


# ---------------------------------------------------------------- 记录


@dataclass(frozen=True)
class MediaRef:
    """一份已落盘的成品。`url` 是 bridge 自己的相对路径，不含上游直链。"""

    token: str
    path: str
    filename: str
    size: int
    mime: str
    kind: str
    ext: str
    sha256: str
    created_at: int = 0

    @property
    def url(self) -> str:
        return f"/v1/media/{self.token}"

    def to_dict(self) -> dict[str, object]:
        """对外形状。刻意不给绝对路径：那是服务器目录结构，客户端用不上。"""
        return {
            "token": self.token,
            "url": self.url,
            "filename": self.filename,
            "size": self.size,
            "mime": self.mime,
            "kind": self.kind,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------- 存储


class MediaStore:
    """`MEDIA_DIR` 上的小文件存储：token 寻址、原子写、Range 读、按 mtime 清理。"""

    def __init__(self, root: str | Path = DEFAULT_MEDIA_DIR) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 校验与寻址 ----

    @staticmethod
    def is_token(token: object) -> bool:
        """严格校验：只收 32 位小写 hex。非字符串/带分隔符/带 `..`/超长全拒。"""
        if not isinstance(token, str):
            return False
        if not TOKEN_RE.fullmatch(token):
            return False
        # 正则已经排除了分隔符，这条留给后来改正则的人兜底：宁可双重拒绝。
        return not any(sep in token for sep in ("/", "\\", ".."))

    def _lookup(self, token: str) -> Path:
        """token -> 实际文件路径。扩展名是落盘时才定下来的，所以要按 stem 找。"""
        if not self.is_token(token):
            # 与"文件不存在"给同一个异常、同一句话：两种失败必须不可区分，
            # 否则 token 空间能被逐个探测出"哪些形状合法"（形状合法就说明
            # 它是我们发过的 token，非法形状不说明任何事——这个差异就是 oracle）。
            raise MediaNotFound("媒体文件不存在或已过期")
        matches = sorted(self.root.glob(f"{token}.*"))
        if not matches:
            raise MediaNotFound("媒体文件不存在或已过期")
        for candidate in matches:
            if candidate.is_file():
                return candidate
        raise MediaNotFound("媒体文件不存在或已过期")

    def _existing(self, token: str) -> Path | None:
        """不抛错版本的寻址，给 exists()/delete() 用。非法 token 一律 None。"""
        if not self.is_token(token):
            return None
        for candidate in self.root.glob(f"{token}.*"):
            if candidate.is_file() and candidate.parent == self.root:
                return candidate
        return None

    def _guard(self, path: Path) -> Path:
        """纵深防御：解析后的路径必须还住在 root 里。"""
        resolved = path.resolve()
        if resolved.parent != self.root:
            raise MediaNotFound("媒体文件不存在或已过期")
        return resolved

    # ---- 写 ----

    def save(
        self,
        data: bytes,
        mime: str = "",
        kind: str = "",
        *,
        expect: str = "",
        url: str = "",
    ) -> MediaRef:
        """落盘一份成品，返回 MediaRef。判定规则全在 `decide`（含 U-MVD 拒收）。"""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise MediaError("save 只接受 bytes（文本/文件对象请先读成 bytes）")
        payload = bytes(data)
        if not payload:
            raise MediaError("成品是空文件，拒绝落盘（空文件会被客户端当成功）")
        if len(payload) > MAX_MEDIA_BYTES:
            raise MediaTooLargeError(
                f"成品 {len(payload) / (1024 * 1024):.1f} MiB 超过 "
                f"MUSE_MAX_MEDIA_BYTES={MAX_MEDIA_BYTES // (1024 * 1024)} MiB，已拒绝落盘"
            )
        ext, final_mime, final_kind = decide(
            mime=mime, url=url, expect=expect, kind=kind, head=payload[:_SNIFF_HEAD]
        )
        token = uuid.uuid4().hex
        return self._write(token, ext, payload, final_mime, final_kind)

    def save_path(
        self,
        path: str | Path,
        mime: str = "",
        kind: str = "",
        *,
        expect: str = "",
        url: str = "",
    ) -> MediaRef:
        """从磁盘上的临时下载文件落盘（上游是 shutil.move，engine.py:1907-1910）。

        单独一个入口是为了大视频不必整份进内存：先只读文件头定类型与拒收，
        再通过 `.part` 流式搬运并同步算 sha256 与大小上限。
        """
        source = Path(path)
        if not source.is_file():
            raise MediaNotFound(f"源文件不存在：{source.name}")
        with open(source, "rb") as handle:
            head = handle.read(_SNIFF_HEAD)
        source_ext = source.suffix.lower()
        ext, final_mime, final_kind = decide(
            mime=mime or "", url=url or str(source), expect=expect, kind=kind, head=head
        )
        if not ext:
            ext = source_ext  # 上游附件常带真扩展名，用它兜底比落到 .png 准
        token = uuid.uuid4().hex
        filename = f"{token}{ext}"
        target = self.root / filename
        partial = self.root / f"{filename}{PART_SUFFIX}"
        digest = hashlib.sha256()
        size = 0
        try:
            with open(source, "rb") as reader, open(partial, "wb") as writer:
                while True:
                    block = reader.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_MEDIA_BYTES:
                        raise MediaTooLargeError(
                            f"成品超过 MUSE_MAX_MEDIA_BYTES="
                            f"{MAX_MEDIA_BYTES // (1024 * 1024)} MiB，已中止落盘"
                        )
                    digest.update(block)
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
            with contextlib.suppress(OSError):  # Windows 权限位语义有限，失败不致命
                os.chmod(partial, 0o600)
            os.replace(partial, target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise MediaError(f"成品落盘失败：{type(exc).__name__}: {exc}") from exc
        except MediaError:
            partial.unlink(missing_ok=True)
            raise
        source.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)  # replace 后不该有残留，留一手兜底
        return MediaRef(
            token=token,
            path=str(target),
            filename=filename,
            size=size,
            mime=final_mime,
            kind=final_kind,
            ext=ext,
            sha256=digest.hexdigest(),
            created_at=int(target.stat().st_mtime),
        )

    def _write(
        self, token: str, ext: str, payload: bytes, mime: str, kind: str
    ) -> MediaRef:
        """原子写：`.part` → `os.replace`。读者永远看不到半截文件。"""
        filename = f"{token}{ext}"
        target = self.root / filename
        partial = self.root / f"{filename}{PART_SUFFIX}"
        try:
            with open(partial, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                os.chmod(partial, 0o600)
            os.replace(partial, target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise MediaError(f"成品落盘失败：{type(exc).__name__}: {exc}") from exc
        partial.unlink(missing_ok=True)
        return MediaRef(
            token=token,
            path=str(target),
            filename=filename,
            size=len(payload),
            mime=mime,
            kind=kind,
            ext=ext,
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=int(target.stat().st_mtime),
        )

    # ---- 读 ----

    def read(self, token: str) -> bytes:
        """整份读取。token 非法与文件不在都是 404 语义，不给可探测的差异。"""
        path = self._guard(self._lookup(token))
        return path.read_bytes()

    def read_range(
        self, token: str, start: int, end: int
    ) -> tuple[bytes, int, int, int]:
        """闭区间读取，返回 (data, first, last, total)。

        终点越界 clamp 到 size-1（HTTP 语义）；起点越界或起点大于终点是
        "不可满足"，抛 MediaRangeError 让上层回 416，而不是返回空 200——
        空 200 会被播放器当成一条 0 秒的视频，那比 416 难查得多。
        """
        path = self._guard(self._lookup(token))
        total = path.stat().st_size
        if total <= 0:
            raise MediaRangeError("媒体文件为空，Range 不可满足")
        first = int(start)
        if first < 0:
            # 负起点按 suffix 形式解释（bytes=-N 的 N 已在 parse_range 转过，
            # 走到这里说明调用方自己传了负数）：按"末尾 N 字节"处理，保持一致。
            first = max(0, total + first)
        if first >= total:
            raise MediaRangeError(f"Range 起点 {first} 超出文件大小 {total}")
        last = min(int(end), total - 1)
        if last < first:
            raise MediaRangeError(f"Range 区间非法：{first} > {last}")
        with open(path, "rb") as handle:
            handle.seek(first)
            data = handle.read(last - first + 1)
        return data, first, last, total

    def size(self, token: str) -> int:
        return self._guard(self._lookup(token)).stat().st_size

    def exists(self, token: str) -> bool:
        """给 app 层做前置判定：非法 token 一律 False，不抛错。"""
        return self._existing(token) is not None

    def ref(self, token: str) -> MediaRef:
        """只读元数据（不整份读文件）：只嗅前 32 字节定 mime。"""
        path = self._guard(self._lookup(token))
        stat = path.stat()
        with open(path, "rb") as handle:
            head = handle.read(_SNIFF_HEAD)
        ext = path.suffix.lower()
        return MediaRef(
            token=token,
            path=str(path),
            filename=path.name,
            size=stat.st_size,
            mime=normalize_mime(sniff_mime(head) or "") or "application/octet-stream",
            kind=kind_of(ext) or "document",
            ext=ext,
            sha256="",
            created_at=int(stat.st_mtime),
        )

    def list_media(self) -> list[MediaRef]:
        """按 mtime 升序列出 root 里的成品。只认自己生成的 token 形状文件。"""
        refs: list[MediaRef] = []
        for entry in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime):
            if not entry.is_file() or entry.name.endswith(PART_SUFFIX):
                continue
            stem = entry.stem
            if not self.is_token(stem):
                continue  # 目录里不是我们生成的文件，不参与记账
            with open(entry, "rb") as handle:
                head = handle.read(_SNIFF_HEAD)
            ext = entry.suffix.lower()
            refs.append(
                MediaRef(
                    token=stem,
                    path=str(entry),
                    filename=entry.name,
                    size=entry.stat().st_size,
                    mime=normalize_mime(sniff_mime(head) or "")
                    or "application/octet-stream",
                    kind=kind_of(ext) or "document",
                    ext=ext,
                    sha256="",  # 列表不重算散列：那要把每个成品整读一遍
                    created_at=int(entry.stat().st_mtime),
                )
            )
        return refs

    # ---- 删 ----

    def delete(self, token: str) -> bool:
        path = self._existing(token)
        if path is None:
            return False
        self._guard(path).unlink(missing_ok=True)
        return True

    def sweep(self, max_age_seconds: int | None = None) -> int:
        """按 mtime 清理超龄成品，顺带回收崩溃残留的 `.part`。返回删除数。

        两件事必须一起清：`.part` 是"写完之前进程被杀"的产物，
        只清成品会让半截文件无限堆积，而且它们不带 token 索引，
        之后没有任何路径会再碰到它们。

        只删 `is_token(stem)` 认可的文件：MEDIA_DIR 被配成别的目录（或有人
        往里放了东西）时，清理不得变成任意删除。
        """
        budget = MEDIA_TTL_SECONDS if max_age_seconds is None else int(max_age_seconds)
        if budget < 0:
            raise MediaError("sweep 的 max_age_seconds 不能为负")
        cutoff = time.time() - budget
        removed = 0
        for entry in self.root.iterdir():
            if not entry.is_file():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            if entry.name.endswith(PART_SUFFIX):
                entry.unlink(missing_ok=True)
                removed += 1
                continue
            if self.is_token(entry.stem):
                entry.unlink(missing_ok=True)
                removed += 1
        return removed


async def sweep_forever(
    interval: float | None = None, *, store: MediaStore | None = None
) -> None:
    """后台时钟：定期按 TTL 清理超龄成品与崩溃残留的 `.part`。

    ## 为什么需要它（这是补的一个真实缺口）

    `sweep()` 早就写好了，但**全仓只有测试调用它**（grep `\\.sweep\\(` 只有
    tests/test_media.py）。生产路径上没有任何东西触发清理，于是 MEDIA_TTL_SECONDS
    和 MAX_MEDIA_BYTES 都只是"写的时候生效"的约束：单文件不会超限，但磁盘会随着
    时间无限长——每张图都留在 `data/media/` 里，没人删。

    ## 边界

    * **不在 import 时启动线程**：只提供协程，由 app 的 lifespan 显式建任务。
    * 清理失败不杀后台任务（与 `protocol_hub.sweep_forever` 同一个理由：
      一次磁盘抖动不该让清理永久停摆）。
    * 删除范围仍由 `sweep()` 把关，只删 token 形态的文件与 `.part`。
    """
    period = MEDIA_SWEEP_INTERVAL_SECONDS if interval is None else interval
    while True:
        try:
            await asyncio.sleep(period)
            # 构造放在 try 里面：`MediaStore()` 会 `mkdir`，磁盘满/权限不对时会抛。
            # 放在循环外的话任务当场死掉，而且死得没有日志——表现是"清理从来没跑过"，
            # 与"跑了但没东西可删"完全无法区分。放进来就能失败一次、下一轮再试。
            target = store if store is not None else MediaStore()
            removed = await asyncio.to_thread(target.sweep)
            # 分开记两个量：`media_sweep_runs` 证明清扫**活着**（否则"没删东西"
            # 和"清扫没在跑"在读数上完全一样），`media_sweep_removed` 是回收量。
            observability.bump("media_sweep_runs")
            if removed:
                logger.info("媒体清理：删除 %d 个超龄成品/残留分片", removed)
                observability.bump("media_sweep_removed", removed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 清扫失败不能让后台任务死掉
            logger.warning("媒体清理异常，继续下一轮", exc_info=True)
