r"""参考素材策略：类型识别、限制校验、来源解析与 SSRF 防护。

为什么单独立一个文件、而且必须在浏览器之前跑完：

muse 的输入框对素材有硬限制（数量 / 单个大小 / 总大小 / 视频时长），但它**不报错**——
越线的文件要么被静默拒绝，要么上传后模型读不到。那时页面表现为"等了 4 分钟什么也没有"，
运维会去查账号、查网络、查上游，而真正的原因是那张 60 MiB 的图。
所以所有判定提前完成，并且每条都能离线测试（不需要真浏览器、不需要真账号）。

限制值来自 muse.ai 前端白名单（上游实测记录：maxFilesPerUpload=10 /
maxFileSize=40 MiB / maxVideoDurationSeconds=120），证据见
work/muse-re/ENGINE_SPEC.md 第 3 节；这里全部做成可配，前端改了不用改代码。

安全边界（默认关闭，必须显式打开）：
  * 远程 URL 只允许公网地址——参考图 URL 是调用方给的，服务端替它发请求就是 SSRF。
    DNS 解析结果会校验，并且连接被钉在"校验过的那个 IP"上，防 DNS rebinding。
  * 本地路径默认拒绝。素材字段能读服务器文件，等于把任意文件读挂在了 API 上。
  * 类型判定只认文件头，不认声明的 Content-Type——声明是别人写的。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import ipaddress
import math
import mimetypes
import os
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .env import env_int, env_positive_int

# ---------------------------------------------------------------- 限制（全部可配）


def _env_bytes(name: str, default: str) -> int:
    """字节数上限。允许写成 40MiB / 41943040 / 100mb 三种，坏值点名报错。"""
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    text = raw.upper().replace(" ", "")
    multiplier = 1
    for suffix, factor in (("GIB", 1024**3), ("MIB", 1024**2), ("KIB", 1024),
                           ("GB", 1000**3), ("MB", 1000**2), ("KB", 1000), ("B", 1)):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break
    try:
        value = int(float(text) * multiplier)
    except ValueError:
        raise SystemExit(f"{name} 必须是字节数（可用 KiB/MiB/GiB 后缀），当前值：{raw!r}") from None
    if value <= 0:
        raise SystemExit(f"{name} 必须大于 0，当前值：{value}")
    return value


MAX_REFERENCE_FILES = env_positive_int("MUSE_MAX_REFERENCE_FILES", "10")
MAX_REFERENCE_BYTES = _env_bytes("MUSE_MAX_REFERENCE_BYTES", "41943040")  # 40 MiB
MAX_REFERENCE_TOTAL_BYTES = _env_bytes("MUSE_MAX_REFERENCE_TOTAL_BYTES", "104857600")  # 100 MiB
MAX_REFERENCE_VIDEO_SECONDS = env_positive_int("MUSE_MAX_REFERENCE_VIDEO_SECONDS", "120")
REF_FETCH_TIMEOUT_SECONDS = env_positive_int("MUSE_REF_FETCH_TIMEOUT", "20")
REF_MAX_REDIRECTS = env_int("MUSE_REF_MAX_REDIRECTS", "10")


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


# 默认拒绝：素材字段能读服务器文件 = 把任意文件读挂在 API 上
ALLOW_LOCAL_FILES = _env_flag("MUSE_REF_LOCAL_FILES")
# 默认只抓公网地址
ALLOW_PRIVATE_FETCH = _env_flag("MUSE_REF_PRIVATE_FETCH")


class ReferenceError(RuntimeError):
    """素材本身不合格。对应 HTTP 400——换账号、重试都没用。"""


class ReferenceSourceError(ReferenceError):
    """来源被策略拒绝（内网地址 / 本地路径 / 重定向越界）。同样 400，但要单独可辨。"""


class ReferenceFetchError(ReferenceError):
    """远端取不回东西：上游 4xx/5xx、连接失败、超时。对应 HTTP 502。

    必须和 ReferenceError(400) 分开：400 的语义是「素材不合格，改请求再来」，
    而图挂了 / 对方 502 / 网络超时是**瞬时**的。按 400 报会给运营「换个写法
    重试」的错误暗示，而正确答案是稍后重试或换一个源。
    上游同样的分层见 engine.py:221-226（MuseProxyError 不得当成 cookie 失效）。

    继承 ReferenceError 是刻意的：现有 `except ReferenceError` 的调用方仍然兜得住，
    只是暂时按 400 处理；app.py 想给 502 就多写一条分支，放在父类之前。
    """

    status_code = 502


# ---------------------------------------------------------------- 类型白名单

# 允许送进 muse 输入框的类型 -> 落盘扩展名。图片/视频/文档取自 muse 前端白名单；
# 音频不在白名单里但输入框接受、模型能读出内容，所以一并放开（上游实测同样放开）。
REFERENCE_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
    "image/avif": ".avif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/mp4": ".m4a",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/aac": ".aac",
    "audio/opus": ".opus",
    "application/pdf": ".pdf", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

# 同一个东西的多种写法。服务端/浏览器经常不统一，不归一会让 sniff 结果被丢弃。
MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
    "video/x-m4v": "video/mp4", "video/mov": "video/quicktime",
    "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/mpeg3": "audio/mpeg",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac", "audio/aacp": "audio/aac",
}

# 这些声明等于没说——按文件头重新判定
GENERIC_MIMES = frozenset({
    "", "application/octet-stream", "binary/octet-stream",
    "application/binary", "application/unknown",
})

VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".3gp"})


def reference_kind(mime: str) -> str:
    """image / video / audio / document。"""
    head = (mime or "").split("/", 1)[0]
    return head if head in ("image", "video", "audio") else "document"


def normalize_mime(mime: str) -> str:
    return MIME_ALIASES.get((mime or "").split(";", 1)[0].strip().lower(),
                            (mime or "").split(";", 1)[0].strip().lower())


def sniff_mime(data: bytes) -> str | None:
    """按文件头识别类型；识别不了返回 None（绝不猜成 image/png 蒙混过关）。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis"):
            return "image/heic"
        if brand in (b"mif1", b"msf1", b"heif"):
            return "image/heif"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"3g"):
            return "video/3gpp"
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    if data.startswith(b"OggS"):
        return "audio/opus" if b"OpusHead" in data[:64] else "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return None


# ---------------------------------------------------------------- 时长解析


def mp4_duration(data: bytes) -> float | None:
    """读 mp4/mov 的 moov/mvhd 时长（秒）。解析不了返回 None——调用方据此拒绝。

    为什么要时长：muse 的参考视频上限是 120 秒，而且**超限的文件会静默失败**。
    判不了就拒绝，不能放行让它变成"等了十分钟什么也没有"。
    """
    def boxes(start: int, end: int):
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            header = 8
            if size == 1 and pos + 16 <= end:
                size = int.from_bytes(data[pos + 8:pos + 16], "big")
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                return  # 结构不合法：停止遍历，别拿脏数据算时长
            yield kind, pos + header, pos + size
            pos += size

    for kind, body, end in boxes(0, len(data)):
        if kind != b"moov":
            continue
        for sub, sbody, send in boxes(body, end):
            if sub != b"mvhd" or send - sbody < 20:
                continue
            if data[sbody] == 1:  # version 1：timescale/duration 都是 64 位
                if send - sbody < 32:
                    return None
                scale = int.from_bytes(data[sbody + 20:sbody + 24], "big")
                duration = int.from_bytes(data[sbody + 24:sbody + 32], "big")
            else:
                scale = int.from_bytes(data[sbody + 12:sbody + 16], "big")
                duration = int.from_bytes(data[sbody + 16:sbody + 20], "big")
            return duration / scale if scale else None
    return None


def webm_duration(data: bytes) -> float | None:
    """读 EBML Segment/Info 的 Duration。结构不合法一律 None（fail closed）。"""
    def vint(pos: int, end: int, keep_marker: bool = False):
        if pos >= end or not data[pos]:
            raise ValueError("invalid EBML integer")
        width = 1
        while width <= 8 and not (data[pos] & (0x80 >> (width - 1))):
            width += 1
        if width > 8 or pos + width > end:
            raise ValueError("truncated EBML integer")
        value = int.from_bytes(data[pos:pos + width], "big")
        if not keep_marker:
            value &= (1 << (7 * width)) - 1
            if value == (1 << (7 * width)) - 1:
                value = None
        return value, pos + width

    def elements(start: int, end: int):
        while start < end:
            tag, pos = vint(start, end, True)
            size, body = vint(pos, end)
            stop = end if size is None else body + size
            if stop > end or stop <= start:
                raise ValueError("invalid EBML element size")
            yield tag, body, stop
            start = stop

    try:
        for seg_tag, seg_body, seg_end in elements(0, len(data)):
            if seg_tag != 0x18538067:  # Segment
                continue
            for info_tag, info_body, info_end in elements(seg_body, seg_end):
                if info_tag != 0x1549A966:  # Info
                    continue
                scale, duration = 1_000_000, None
                for field_tag, field_body, field_end in elements(info_body, info_end):
                    if field_tag == 0x2AD7B1 and 0 < field_end - field_body <= 8:
                        scale = int.from_bytes(data[field_body:field_end], "big")  # TimecodeScale
                    elif field_tag == 0x4489 and field_end - field_body in (4, 8):
                        fmt = ">f" if field_end - field_body == 4 else ">d"
                        duration = struct.unpack(fmt, data[field_body:field_end])[0]
                if duration is None or not scale:
                    return None
                return duration * scale / 1e9
    except (ValueError, struct.error):
        return None
    return None


def video_duration(data: bytes, mime: str) -> float | None:
    if mime == "video/webm":
        return webm_duration(data)
    return mp4_duration(data)


# ---------------------------------------------------------------- 来源解析


class _PinnedRedirectBlock(urllib.request.HTTPRedirectHandler):
    """不自动跟随重定向：每一跳都重新做一次地址校验。

    自动跟随等于让远端服务器把我们带到它想去的地址——那里可能在内网。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001,D102
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连到"已校验过 global 的那个 IP"，Host 头仍是原域名。"""

    def __init__(self, host: str, pinned: str, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._pinned = pinned

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """目的地址换成已校验的 IP，但 SNI 与证书校验仍按原域名。

    只做地址校验而不钉连接，挡不住 DNS rebinding：第二次解析可以返回内网地址，
    urllib 会照着它建连。这里 Host/SNI/证书都是原 host，只有 socket 目的被钉住。
    """

    def __init__(self, host: str, pinned: str, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._pinned = pinned

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            return
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _pinned_openers(pinned: str):
    """返回钉到 pinned 地址的 http/https handler。"""
    http_handler = urllib.request.HTTPHandler()
    https_handler = urllib.request.HTTPSHandler()
    http_handler.http_open = lambda req: http_handler.do_open(
        lambda host, **kw: _PinnedHTTPConnection(host, pinned, **kw), req)
    https_handler.https_open = lambda req: https_handler.do_open(
        lambda host, **kw: _PinnedHTTPSConnection(host, pinned, **kw), req)
    return http_handler, https_handler


def _assert_public(hostname: str, port: int) -> str:
    """解析并校验：默认只允许公网地址。返回被批准的那个 IP（连接要钉在它上面）。"""
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ReferenceSourceError(f"参考素材域名无法解析：{hostname}") from exc
    if not addresses:
        raise ReferenceSourceError(f"参考素材域名没有解析到地址：{hostname}")
    approved: str | None = None
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # v4-mapped 要按 v4 规则判定，否则 ::ffff:127.0.0.1 溜过去
        if not ALLOW_PRIVATE_FETCH and not ip.is_global:
            raise ReferenceSourceError(
                f"参考素材地址被拒绝（{ip} 不是公网地址）。"
                "确需抓取内网/元数据地址时，显式设置 MUSE_REF_PRIVATE_FETCH=1"
            )
        approved = approved or str(ip)
    if approved is None:  # 理论不可达，留一手：不返回"没校验过的地址"
        raise ReferenceSourceError(f"参考素材域名没有可用地址：{hostname}")
    return approved


def fetch_public(url: str, *, timeout: int | None = None) -> tuple[bytes, str]:
    """抓取一个远程素材，逐跳校验地址。返回 (bytes, 声明的 mime)。

    类型不在这里判定——由调用方按文件头重新判定（声明是别人写的）。
    """
    budget = timeout or REF_FETCH_TIMEOUT_SECONDS
    target = url
    for _ in range(REF_MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ReferenceSourceError("参考素材只允许 http(s) 地址")
        if parsed.username or parsed.password:
            raise ReferenceSourceError("参考素材地址不允许携带凭据")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        approved = _assert_public(parsed.hostname, port)
        http_handler, https_handler = _pinned_openers(approved)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _PinnedRedirectBlock(),
            http_handler,
            https_handler,
        )
        request = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with opener.open(request, timeout=budget) as response:
                data = response.read(MAX_REFERENCE_BYTES + 1)
                declared = response.headers.get_content_type() if response.headers.get(
                    "Content-Type") else ""
                return data, normalize_mime(declared)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                exc.close()
                target = urllib.parse.urljoin(target, location)
                continue
            raise ReferenceFetchError(f"下载远程参考素材失败 (HTTP {exc.code})") from exc
        except (TimeoutError, urllib.error.URLError, http.client.HTTPException, ssl.SSLError, OSError) as exc:
            raise ReferenceFetchError(
                f"下载远程参考素材失败：{type(exc).__name__}") from exc
    raise ReferenceSourceError(
        f"参考素材重定向超过 {REF_MAX_REDIRECTS} 跳，已拒绝")


@dataclass(frozen=True)
class Reference:
    """一份已判定合格的素材。origin 只用于报错定位，不落盘、不外发。"""

    data: bytes
    mime: str
    ext: str
    kind: str
    sha256: str
    origin: str = ""

    @property
    def bytes(self) -> int:
        return len(self.data)

    def data_url(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii")


def _decide_mime(data: bytes, declared: str, name: str = "") -> str:
    """声明只作参考：不可信时按文件头，再按文件名，都不中就 None。"""
    mime = normalize_mime(declared)
    if mime in GENERIC_MIMES:
        mime = normalize_mime(sniff_mime(data) or "") or \
            normalize_mime(mimetypes.guess_type(name)[0] or "")
    if sniff_mime(data) and sniff_mime(data) != mime:
        # 文件头与声明冲突时以文件头为准：送错类型进 muse 输入框不会报错，只会白等
        mime = sniff_mime(data) or mime
    return mime


def _checked(data: bytes, mime: str, *, origin: str = "", name: str = "") -> Reference:
    if not data:
        raise ReferenceError(f"参考素材是空文件（{origin or '未命名'}）")
    mime = _decide_mime(data, mime, name)
    if mime not in REFERENCE_MIME_EXT:
        raise ReferenceError(
            f"参考素材类型不受支持（{mime or 'unknown'}）。支持："
            "图片 / 视频(mp4·mov·webm·3gp) / 音频(mp3·wav·m4a·ogg·flac·aac·opus) / PDF·Word")
    if len(data) > MAX_REFERENCE_BYTES:
        raise ReferenceError(
            f"单个参考素材超过 {MAX_REFERENCE_BYTES // (1024 * 1024)} MiB 限制"
            f"（实际 {len(data) / (1024 * 1024):.1f} MiB）")
    if mime.startswith("video/"):
        seconds = video_duration(data, mime)
        if seconds is None or not math.isfinite(seconds) or seconds <= 0:
            raise ReferenceError(
                "无法确认参考视频时长，请提供带有效时长字段的 MP4/MOV/WebM/3GP")
        if seconds > MAX_REFERENCE_VIDEO_SECONDS:
            raise ReferenceError(
                f"参考视频时长 {seconds:.0f} 秒，超过 {MAX_REFERENCE_VIDEO_SECONDS} 秒限制")
    return Reference(
        data=data,
        mime=mime,
        ext=REFERENCE_MIME_EXT[mime],
        kind=reference_kind(mime),
        sha256=hashlib.sha256(data).hexdigest(),
        origin=origin,
    )


def normalize_reference(value: str, *, allow_local_files: bool | None = None) -> Reference:
    """把一种写法（Data URL / http(s) URL / 裸 base64 / 本地路径）变成 Reference。

    判定顺序是刻意的：先明确形态（data:/http:），再试裸 base64（它没有路径，
    不存在读到服务器文件的可能），最后才碰本地路径，而且本地路径默认拒绝。
    """
    if value is None:
        raise ReferenceError("参考素材不能为空")
    text = str(value).strip()
    if not text:
        raise ReferenceError("参考素材不能为空")
    gate = ALLOW_LOCAL_FILES if allow_local_files is None else allow_local_files

    if text.startswith("data:"):
        head, sep, payload = text.partition(",")
        if not sep:
            raise ReferenceError("参考素材 Data URL 缺少逗号分隔")
        mime = head[5:].split(";", 1)[0]
        try:
            data = (base64.b64decode(payload.strip(), validate=True)
                    if ";base64" in head.lower()
                    else urllib.parse.unquote_to_bytes(payload))
        except (binascii.Error, ValueError) as exc:
            raise ReferenceError("参考素材 Data URL 编码无效") from exc
        return _checked(data, mime, origin="data-url")

    if text.startswith(("http://", "https://")):
        data, declared = fetch_public(text)
        return _checked(data, declared, origin="remote", name=urllib.parse.urlsplit(text).path)

    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        data = None
    if data:
        # 裸 base64：没有服务器路径，安全，直接按文件头判定
        return _checked(data, "", origin="base64")

    if not gate:
        raise ReferenceSourceError(
            "参考素材的本地路径被拒绝。确需读取服务器上的文件，"
            "请显式设置 MUSE_REF_LOCAL_FILES=1（并把该服务限制在可信网络内）")
    if not os.path.isfile(text):
        raise ReferenceError(
            "参考素材必须是 Data URL、http(s) 地址、有效 base64 或存在的本地文件")
    try:
        size = os.path.getsize(text)
        if size > MAX_REFERENCE_BYTES:
            raise ReferenceError(
                f"单个参考素材超过 {MAX_REFERENCE_BYTES // (1024 * 1024)} MiB 限制"
                f"（实际 {size / (1024 * 1024):.1f} MiB）")
        with open(text, "rb") as handle:
            data = handle.read(MAX_REFERENCE_BYTES + 1)
    except ReferenceError:
        raise
    except OSError as exc:
        raise ReferenceError(f"读取本地参考素材失败：{type(exc).__name__}") from exc
    return _checked(data, mimetypes.guess_type(text)[0] or "", origin="local", name=text)


def normalize_references(values: list[str] | None,
                         *, allow_local_files: bool | None = None) -> list[Reference]:
    """整批判定：数量上限、总大小上限，逐个报错并带上有第几个。"""
    refs: list[Reference] = []
    total = 0
    items = [v for v in (values or []) if v not in (None, "")]
    if len(items) > MAX_REFERENCE_FILES:
        raise ReferenceError(f"参考素材最多 {MAX_REFERENCE_FILES} 个（收到 {len(items)} 个）")
    for index, value in enumerate(items, start=1):
        try:
            reference = normalize_reference(value, allow_local_files=allow_local_files)
        except ReferenceError as exc:
            raise ReferenceError(f"第 {index} 个参考素材不合格：{exc}") from exc
        total += reference.bytes
        if total > MAX_REFERENCE_TOTAL_BYTES:
            raise ReferenceError(
                f"参考素材总大小超过 {MAX_REFERENCE_TOTAL_BYTES // (1024 * 1024)} MiB 限制"
                f"（到第 {index} 个累计 {total / (1024 * 1024):.1f} MiB）")
        refs.append(reference)
    return refs
