r"""最小 asyncio WebSocket 客户端（RFC 6455），只在 `websockets` 没装时兜底。

为什么要这个文件：

    protocol_client 只需要 WS 的四个动作 —— 握手、发一帧、收一条消息、关闭。
    muse 的 Noise 通道恰好只用到这四个。为了不引入任何新依赖（项目的
    requirements.txt 里没有 websockets，也不打算加），这里手写够用的子集。

为什么只做子集、不做得"完整"：

  * 不做扩展协商（permessage-deflate 等）：Noise 帧是二进制且已经加密过，
    压缩只会把密文变成可被长度分析的材料，宁可不压。客户端因此**不发
    Sec-WebSocket-Extensions**。
  * 不做自动重连、不做请求队列：上层 protocol_client 自己管生命周期。
  * 分片消息按 RFC 6455 §5.4 在客户端内部重组。

四条容易写错、这里钉死的规则：

  1. **客户端发出的每一帧都必须掩码**（§5.1）。漏掉 mask bit 会被服务端直接
     断链，而报错点在服务端，本地看起来像"连接莫名断开"。
  2. **必须校验 Sec-WebSocket-Accept**（§1.3）。透明代理经常对
     `Upgrade: websocket` 回 200 而不是 101；不校验就会把 HTML 当 WS 帧解析，
     得到一串无意义的协议错误。这里的 SHA-1 是协议规定的摘要，不是加密选型。
  3. **收到 Ping 必须回 Pong**（§5.5.2），payload 原样带回。muse 侧空闲检测
     靠它，不回会被判死连。
  4. **控制帧可以插在分片中间**（§5.4）。重组期间即时处理 Ping/Pong，
     保留正文分片状态，不能因为处理控制帧就丢数据。

已知限制（不做，也不假装做了）：不支持带认证的代理，也不支持 socks。
需要认证代理的部署请装 `websockets`，protocol_client 会优先用它。

作者：**君莫笑** · **永恒之兰**
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from urllib.parse import urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 §1.3 固定常量

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

NORMAL_CLOSE = 1000
MAX_FRAME_SIZE = 8 * 1024 * 1024


class WebSocketClosed(RuntimeError):
    """连接已关闭。code/reason 保留对端说的是什么，方便上层区分正常收尾与断链。"""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        # A peer can place credentials or arbitrary text in the close reason.
        super().__init__(f"websocket closed (code={code})")
        self.code = code
        self.reason = reason


class WebSocketProtocolError(RuntimeError):
    """Protocol failure with optional safe HTTP metadata, never peer headers."""

    def __init__(
        self, message: str, *, status_code: int | None = None, stage: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = (
            status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        )
        self.stage = stage


# ---------------------------------------------------------------- 帧编解码
# 拆成纯函数，可以不碰网络单测（见 tests/test_protocol_client.py）。


def build_accept(key: str) -> str:
    """Sec-WebSocket-Key -> Sec-WebSocket-Accept。SHA-1 由协议规定。"""
    digest = hashlib.sha1(key.encode("ascii") + GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def _xor_stream(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % 4] for index, byte in enumerate(data))


def unmask(data: bytes, key: bytes) -> bytes:
    return _xor_stream(data, key)


def encode_frame(
    payload: bytes, opcode: int, *, fin: bool = True, mask: bool = True
) -> bytes:
    """编码一帧。客户端方向必须 mask=True（规则 1）。"""
    first = (0x80 if fin else 0x0) | opcode
    length = len(payload)
    header = bytearray((first,))
    flag = 0x80 if mask else 0x0
    if length < 126:
        header.append(flag | length)
    elif length < 1 << 16:
        header.append(flag | 126)
        header += struct.pack(">H", length)
    else:
        header.append(flag | 127)
        header += struct.pack(">Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    return bytes(header) + key + _xor_stream(payload, key)


class FrameHead:
    __slots__ = ("fin", "length", "masked", "mask_key", "opcode")

    def __init__(self) -> None:
        self.fin = False
        self.opcode = 0
        self.masked = False
        self.mask_key = b""
        self.length = 0


async def _read_exactly(reader: asyncio.StreamReader, size: int) -> bytes:
    """读满 size 字节；EOF 抛 WebSocketClosed，绝不返回短数据。

    半截帧头会被当成新帧继续解析，报错点会漂到几百字节之外，最难查。
    """
    chunks = bytearray()
    while len(chunks) < size:
        chunk = await reader.read(size - len(chunks))
        if not chunk:
            raise WebSocketClosed(None, "connection reached EOF mid-frame")
        chunks += chunk
    return bytes(chunks)


async def read_frame(
    reader: asyncio.StreamReader, *, max_size: int = MAX_FRAME_SIZE,
    expect_mask: bool | None = None, data_limit: int | None = None,
    fragmented: bool | None = None,
) -> tuple[FrameHead, bytes]:
    """Parse one frame; default role-neutral so test servers can read clients.

    Client callers require unmasked frames and pass remaining message capacity.
    All header/state/size checks precede mask-key and payload reads (RFC 6455 §5).
    """
    head = FrameHead()
    first = await _read_exactly(reader, 2)
    head.fin = bool(first[0] & 0x80)
    head.opcode = first[0] & 0x0F
    if first[0] & 0x70:
        raise WebSocketProtocolError("reserved bits must be zero (RFC 6455 §5.2)")
    if head.opcode not in (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
        raise WebSocketProtocolError("reserved websocket opcode")
    head.masked = bool(first[1] & 0x80)
    if expect_mask is not None and head.masked != expect_mask:
        raise WebSocketProtocolError("unexpected websocket masking direction")
    marker = first[1] & 0x7F
    control = head.opcode >= OP_CLOSE
    if control and (not head.fin or marker > 125):
        raise WebSocketProtocolError("control frames must be final and at most 125 bytes")
    if not control and fragmented is not None:
        if head.opcode == OP_CONT and not fragmented:
            raise WebSocketProtocolError("continuation without a start frame")
        if head.opcode in (OP_TEXT, OP_BINARY) and fragmented:
            raise WebSocketProtocolError("new data frame inside a fragment")
    if marker == 126:
        head.length = struct.unpack(">H", await _read_exactly(reader, 2))[0]
        if head.length < 126:
            raise WebSocketProtocolError("noncanonical websocket payload length")
    elif marker == 127:
        head.length = struct.unpack(">Q", await _read_exactly(reader, 8))[0]
        if head.length < 65536 or head.length & (1 << 63):
            raise WebSocketProtocolError("invalid 64-bit websocket payload length")
    else:
        head.length = marker
    limit = min(max_size, data_limit) if not control and data_limit is not None else max_size
    if head.length > limit:
        raise WebSocketProtocolError(f"frame or message too large: {head.length} > {limit}")
    if head.masked:
        head.mask_key = await _read_exactly(reader, 4)
    payload = await _read_exactly(reader, head.length) if head.length else b""
    if head.masked:
        payload = unmask(payload, head.mask_key)
    return head, payload


def _decode(opcode: int, payload: bytes) -> str | bytes:
    if opcode == OP_TEXT:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebSocketProtocolError("text frame is not valid UTF-8") from exc
    return payload


def _valid_close_code(code: int) -> bool:
    # RFC 6455 §7.4: wire-reserved sentinels and 1016..2999 aren't endpoint codes.
    return type(code) is int and (
        code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014)
        or 3000 <= code <= 4999
    )


# ---------------------------------------------------------------- 客户端


class WebSocket:
    """四件套：send / recv / close / 上下文管理。

    `recv()` 每次返回一条**完整消息**（str 或 bytes）。返回类型故意保持"两种
    都可能"：Noise 通道只发二进制，但上层会断言"握手期间收到文本帧就是协议
    错误"，所以不能在这里偷偷转成 bytes。
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_size: int = MAX_FRAME_SIZE,
        close_timeout: float = 3.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_size = max_size
        self._close_timeout = close_timeout
        self._closed = False
        self._transport_closed = False
        self._frag_opcode: int | None = None
        self._frag = bytearray()

    @property
    def closed(self) -> bool:
        return self._closed

    async def _write(self, raw: bytes) -> None:
        if self._closed:
            raise WebSocketClosed(None)
        self._writer.write(raw)
        await self._writer.drain()

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise WebSocketClosed(None, "connection already closed")
        if isinstance(data, str):
            await self._write(encode_frame(data.encode("utf-8"), OP_TEXT))
        else:
            await self._write(encode_frame(bytes(data), OP_BINARY))

    async def ping(self, data: bytes = b"") -> None:
        if len(data) > 125:
            raise WebSocketProtocolError("ping payload exceeds 125 bytes")
        await self._write(encode_frame(data, OP_PING))

    async def recv(self) -> str | bytes:
        if self._closed:
            raise WebSocketClosed(None)
        try:
            return await self._recv_message()
        except (WebSocketProtocolError, WebSocketClosed):
            # A malformed or truncated frame cannot safely be resumed mid-stream.
            self._closed = True
            self._frag.clear()
            self._frag_opcode = None
            await self._finish_transport()
            raise

    async def _recv_message(self) -> str | bytes:
        while True:
            remaining = self._max_size - len(self._frag)
            head, payload = await read_frame(
                self._reader, max_size=max(self._max_size, 125), expect_mask=False,
                data_limit=remaining, fragmented=self._frag_opcode is not None,
            )
            if head.opcode in (OP_CLOSE, OP_PING, OP_PONG):
                await self._handle_control(head, payload)
                continue
            if head.opcode in (OP_TEXT, OP_BINARY):
                if self._frag_opcode is not None:
                    raise WebSocketProtocolError("new data frame inside a fragment")
                if head.fin:
                    return _decode(head.opcode, payload)
                # Control frames are handled immediately without losing fragments.
                self._frag_opcode, self._frag = head.opcode, bytearray(payload)
                continue
            if head.opcode == OP_CONT:
                if self._frag_opcode is None:
                    raise WebSocketProtocolError("continuation without a start frame")
                self._frag += payload
                if head.fin:
                    opcode, message = self._frag_opcode, bytes(self._frag)
                    self._frag_opcode, self._frag = None, bytearray()
                    return _decode(opcode, message)
                continue
            raise WebSocketProtocolError(f"unknown opcode {head.opcode}")

    async def _handle_control(self, head: FrameHead, payload: bytes) -> None:
        if head.opcode == OP_PING:
            await self._write(encode_frame(payload, OP_PONG))
            return
        if head.opcode == OP_PONG:
            return
        if len(payload) == 1:
            raise WebSocketProtocolError("close payload has a one-byte status")
        code = struct.unpack(">H", payload[:2])[0] if payload else None
        if code is not None and not _valid_close_code(code):
            raise WebSocketProtocolError("invalid websocket close code")
        try:
            reason = payload[2:].decode("utf-8") if payload else ""
        except UnicodeDecodeError:
            raise WebSocketProtocolError("close reason is not valid UTF-8") from None
        await self._close_payload(payload)
        raise WebSocketClosed(code, reason)

    def _abort_transport(self) -> None:
        self._writer.close()
        transport = getattr(self._writer, "transport", None)
        if transport is not None:
            transport.abort()
        self._transport_closed = True

    async def _finish_transport(self) -> None:
        if self._transport_closed:
            return
        try:
            async with asyncio.timeout(self._close_timeout):
                self._writer.close()
                await self._writer.wait_closed()
            self._transport_closed = True
        except BaseException as exc:
            self._abort_transport()
            if not isinstance(exc, (OSError, EOFError, TimeoutError)):
                raise

    async def _close_payload(self, payload: bytes) -> None:
        if self._closed:
            await self._finish_transport()
            return
        # Gate every public/data write before the first close await. The one
        # close frame bypasses _write, and no later ping/send/recv may write.
        self._closed = True
        self._frag.clear()
        self._frag_opcode = None
        try:
            async with asyncio.timeout(self._close_timeout):
                self._writer.write(encode_frame(payload, OP_CLOSE))
                await self._writer.drain()
                self._writer.close()
                await self._writer.wait_closed()
            self._transport_closed = True
        except BaseException as exc:
            self._abort_transport()
            if not isinstance(exc, (OSError, EOFError, TimeoutError)):
                raise

    async def close(self, code: int = NORMAL_CLOSE, reason: str = "") -> None:
        """Bound close-frame drain plus transport shutdown; preserve cancellation."""
        if self._closed:
            await self._finish_transport()
            return
        if not _valid_close_code(code):
            raise WebSocketProtocolError("invalid websocket close code")
        try:
            encoded = reason.encode("utf-8")
        except UnicodeEncodeError:
            raise WebSocketProtocolError("close reason is not valid UTF-8") from None
        if len(encoded) > 123:
            raise WebSocketProtocolError("close reason exceeds 123 bytes")
        await self._close_payload(struct.pack(">H", code) + encoded)


# ---------------------------------------------------------------- 握手


def _split_proxy(proxy: str) -> tuple[str, int]:
    """Accept only unauthenticated HTTP CONNECT proxies; fail before socket I/O."""
    try:
        text = proxy.strip()
        parts = urlsplit(text if "://" in text else "http://" + text)
        if parts.scheme.lower() != "http":
            raise WebSocketProtocolError("兜底客户端只支持 HTTP CONNECT 代理")
        if parts.username is not None or parts.password is not None:
            raise WebSocketProtocolError("认证代理不被兜底客户端支持，请安装 websockets")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise WebSocketProtocolError("代理地址不能包含路径、查询串或片段")
        host = parts.hostname
        port = parts.port
        if not host or any(char.isspace() for char in host) or parts.netloc.endswith(":"):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        return host, port if port is not None else 80
    except (ValueError, AttributeError, TypeError):
        # Parsing errors can embed the original URL, including proxy passwords.
        raise WebSocketProtocolError("代理地址无法解析") from None


def _authority(host: str, port: int) -> str:
    """IPv6 literals require brackets in CONNECT targets and HTTP Host fields."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


async def _read_http_head(
    reader: asyncio.StreamReader, stage: str
) -> tuple[int, dict[str, str]]:
    """Bound header bytes as well as time (the caller owns the deadline).

    Never copy a peer's status line into errors: it may echo the token URL.
    """
    size = 0
    status: int | None = None
    fields: dict[str, str] = {}
    while True:
        line = await reader.readline()
        size += len(line)
        if size > 64 * 1024:
            raise WebSocketProtocolError(f"{stage} headers exceed 65536 bytes")
        if not line:
            raise WebSocketProtocolError(f"{stage} headers ended prematurely")
        if status is None:
            parts = line.split(None, 2)
            if (len(parts) < 2 or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1")
                    or len(parts[1]) != 3 or not parts[1].isdigit()):
                raise WebSocketProtocolError(f"{stage} invalid HTTP status")
            status = int(parts[1])
        elif line in (b"\r\n", b"\n"):
            return status, fields
        else:
            name, separator, value = line.partition(b":")
            if not separator:
                raise WebSocketProtocolError(f"{stage} invalid HTTP header")
            fields[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()


async def _proxy_tunnel(
    proxy: str, host: str, port: int, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """CONNECT creates a plain tunnel; wss must upgrade it to TLS before GET."""
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout):
            phost, pport = _split_proxy(proxy)
            reader, writer = await asyncio.open_connection(phost, pport)
            authority = _authority(host, port)
            request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()
            status, _ = await _read_http_head(reader, "proxy CONNECT")
            if status != 200:
                raise WebSocketProtocolError(
                    f"proxy CONNECT failed (HTTP {status})",
                    status_code=status, stage="proxy_connect",
                )
        return reader, writer
    except BaseException as exc:
        if writer is not None:
            await _abort(writer)
        if isinstance(exc, (asyncio.CancelledError, WebSocketProtocolError)):
            raise
        if isinstance(exc, Exception):
            raise WebSocketProtocolError(f"proxy CONNECT failed ({type(exc).__name__})") from None
        raise


async def connect(
    url: str,
    *,
    origin: str | None = None,
    user_agent: str | None = None,
    additional_headers: dict[str, str] | None = None,
    subprotocols: list[str] | None = None,
    max_size: int = MAX_FRAME_SIZE,
    open_timeout: float = 30.0,
    ssl_context: ssl.SSLContext | None = None,
    proxy: str | None = None,
    close_timeout: float = 3.0,
) -> WebSocket:
    """握手并返回 WebSocket。参数名对齐 `websockets.connect` 我们用得到的子集。

    `close_timeout` bounds close-frame drain and transport shutdown after connect.
    """
    writer: asyncio.StreamWriter | None = None
    stage = "URL validation"
    try:
        # One budget covers TCP, CONNECT headers, TLS and HTTP Upgrade. Slow
        # peers cannot reset it by sending one more header line each time.
        async with asyncio.timeout(open_timeout):
            parts = urlsplit(url)
            if parts.scheme not in ("ws", "wss"):
                raise WebSocketProtocolError("unsupported websocket scheme")
            secure = parts.scheme == "wss"
            host = parts.hostname
            if not host:
                raise WebSocketProtocolError("websocket URL has no host")
            port = parts.port or (443 if secure else 80)
            path = parts.path or "/"
            if parts.query:
                path = f"{path}?{parts.query}"

            context = (ssl_context or ssl.create_default_context()) if secure else None
            stage = "proxy CONNECT" if proxy else "TCP/TLS connect"
            if proxy:
                reader, writer = await _proxy_tunnel(proxy, host, port, open_timeout)
                if secure:
                    stage = "TLS handshake"
                    # CONNECT only establishes TCP. Authenticate the destination
                    # before sending a GET whose query can contain auth_token.
                    await writer.start_tls(
                        context, server_hostname=host,
                        ssl_handshake_timeout=open_timeout,
                    )
            else:
                kwargs = {"host": host, "port": port}
                if secure:
                    kwargs.update(ssl=context, server_hostname=host)
                reader, writer = await asyncio.open_connection(**kwargs)

            stage = "WebSocket Upgrade"
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            headers = {
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": key,
                "Sec-WebSocket-Version": "13",
                "Host": _authority(host, port),
            }
            if origin:
                headers["Origin"] = origin
            if user_agent:
                headers["User-Agent"] = user_agent
            if subprotocols:
                headers["Sec-WebSocket-Protocol"] = ", ".join(subprotocols)
            headers.update(additional_headers or {})
            blob = f"GET {path} HTTP/1.1\r\n" + "".join(
                f"{name}: {value}\r\n" for name, value in headers.items()
            ) + "\r\n"
            writer.write(blob.encode("ascii"))
            await writer.drain()
            status, fields = await _read_http_head(reader, "WebSocket Upgrade")
            if status != 101:
                raise WebSocketProtocolError(
                    f"handshake rejected (HTTP {status})",
                    status_code=status, stage="websocket_upgrade",
                )
            if fields.get("sec-websocket-accept") != build_accept(key):
                raise WebSocketProtocolError("handshake accept value mismatch")
        return WebSocket(reader, writer, max_size=max_size, close_timeout=close_timeout)
    except BaseException as exc:
        if writer is not None:
            await _abort(writer)
        if isinstance(exc, (asyncio.CancelledError, WebSocketProtocolError)):
            raise
        if isinstance(exc, Exception):
            # Do not expose raw SSL/proxy/response text or the credential URL.
            raise WebSocketProtocolError(f"{stage} failed ({type(exc).__name__})") from None
        raise


async def _abort(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        # A failed TLS peer may never acknowledge shutdown. Cleanup has a
        # separate bounded grace; it must not hang a cancelled bootstrap.
        async with asyncio.timeout(1.0):
            await writer.wait_closed()
    except (OSError, ssl.SSLError, EOFError, TimeoutError):
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.abort()
