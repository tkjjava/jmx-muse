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
  4. **控制帧可以插在分片中间**（§5.4）。所以重组期间到的控制帧先攒着，
     等这条消息拼完再按序交给上层，不能因为处理了一个 Pong 就丢数据。

已知限制（不做，也不假装做了）：不支持带认证的代理，也不支持 socks。
需要认证代理的部署请装 `websockets`，protocol_client 会优先用它。

作者：**君莫笑** · **永恒之兰**
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
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
        super().__init__(f"websocket closed (code={code} reason={reason!r})")
        self.code = code
        self.reason = reason


class WebSocketProtocolError(RuntimeError):
    """对端违反 RFC 6455，或者压根不是 WS 服务。宁可报错也不猜。"""


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
    reader: asyncio.StreamReader, *, max_size: int = MAX_FRAME_SIZE
) -> tuple[FrameHead, bytes]:
    """解析一帧，返回 (FrameHead, payload)。掩码已在内部剥掉。"""
    head = FrameHead()
    first = await _read_exactly(reader, 2)
    head.fin = bool(first[0] & 0x80)
    head.opcode = first[0] & 0x0F
    if first[0] & 0x70:
        raise WebSocketProtocolError("reserved bits must be zero (RFC 6455 §5.2)")
    head.masked = bool(first[1] & 0x80)
    marker = first[1] & 0x7F
    if marker == 126:
        head.length = struct.unpack(">H", await _read_exactly(reader, 2))[0]
    elif marker == 127:
        head.length = struct.unpack(">Q", await _read_exactly(reader, 8))[0]
    else:
        head.length = marker
    if head.length > max_size:
        raise WebSocketProtocolError(f"frame too large: {head.length} > {max_size}")
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
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_size = max_size
        self._closed = False
        self._frag_opcode: int | None = None
        self._frag = bytearray()
        self._queued: list[tuple[int, bytes]] = []

    @property
    def closed(self) -> bool:
        return self._closed

    async def _write(self, raw: bytes) -> None:
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
        await self._write(encode_frame(data, OP_PING))

    async def recv(self) -> str | bytes:
        while True:
            if self._queued:
                opcode, payload = self._queued.pop(0)
                return _decode(opcode, payload)
            head, payload = await read_frame(self._reader, max_size=self._max_size)
            if head.opcode in (OP_CLOSE, OP_PING, OP_PONG):
                await self._handle_control(head, payload)
                continue
            if head.opcode in (OP_TEXT, OP_BINARY):
                if self._frag_opcode is not None:
                    raise WebSocketProtocolError("new data frame inside a fragment")
                if head.fin:
                    return _decode(head.opcode, payload)
                # 规则 4：分片未结束前到的控制帧走 _queued，不打断这条消息。
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
            await self._write(encode_frame(payload, OP_PONG))  # 规则 3
            return
        if head.opcode == OP_PONG:
            return
        code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
        reason = payload[2:].decode("utf-8", "replace") if len(payload) > 2 else ""
        if not self._closed:
            await self._write(encode_frame(payload or b"", OP_CLOSE))
            self._closed = True
        raise WebSocketClosed(code, reason)

    async def close(self, code: int = NORMAL_CLOSE, reason: str = "") -> None:
        """幂等：已经关过就只收尾，不抛。上层 close() 会被重复调用。"""
        if not self._closed:
            self._closed = True
            body = struct.pack(">H", code) + reason.encode("utf-8")
            with contextlib.suppress(OSError, WebSocketClosed, WebSocketProtocolError):
                await self._write(encode_frame(body, OP_CLOSE))
        self._writer.close()
        with contextlib.suppress(OSError, ssl.SSLError, EOFError):
            await self._writer.wait_closed()


# ---------------------------------------------------------------- 握手


def _split_proxy(proxy: str) -> tuple[str, int]:
    """从代理串取 host:port。带凭据直接拒绝，而不是静默丢掉后连到一个陌生出口。"""
    text = proxy.split("://", 1)[-1]
    if "@" in text:
        raise WebSocketProtocolError(
            "认证代理不被这个兜底客户端支持：请安装 websockets，"
            "或改用无凭据的本地代理"
        )
    host, _, port = text.partition(":")
    if not host:
        raise WebSocketProtocolError("代理地址无法解析")
    return host, int(port or "80")


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
            request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()
            status, _ = await _read_http_head(reader, "proxy CONNECT")
            if status != 200:
                raise WebSocketProtocolError(f"proxy CONNECT failed (HTTP {status})")
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

    `close_timeout` 只是签名兼容用：这里的关闭是直接写 close 帧后 await
    wait_closed()，没有独立的定时器需要它。
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
                "Host": f"{host}:{port}",
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
                raise WebSocketProtocolError(f"handshake rejected (HTTP {status})")
            if fields.get("sec-websocket-accept") != build_accept(key):
                raise WebSocketProtocolError("handshake accept value mismatch")
        return WebSocket(reader, writer, max_size=max_size)
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
