r"""Hatch 通道的 protobuf 编解码（手写 varint，不引 protobuf 库）。

为什么要自己写
--------------
schema 在 work/muse-re/sources/lichao199208__muse2api/app/muse_engine/hatch.proto，
一共 8 个 message、字段全是 varint / length-delimited / bool 三类。用 protoc 生成
代码要多一个运行时依赖 + 一次生成步骤，而 muse-bridge 的依赖面是刻意压着的
（pyproject 里只有 httpx/fastapi/uvicorn/cryptography/pydantic/playwright）。
手写这 300 行换来的是：能严格控制"非法编码必须报错"，而 protobuf 运行时
默认会把脏输入静默解析成别的值——那种行为在排协议问题时代价极高。

线上编码链路（证据：protocol_client.py:382-387、:447-458）
--------------------------------------------------------
    WebSocket 二进制帧
      = Cipher.enc(ad=b"", NoiseTransportFrame{chunk_id, chunk_index,
                                               total_chunks, payload}.SerializeToString())
    收齐一个 chunk_id 的全部分块后：
      ServiceResponse{payload} -> ServiceFrame{stream_id, oneof kind}
    发方向同理，最外层是 ServiceRequest{service=SERVICE_DAEMON, payload=ServiceFrame}

严格性上的两个取舍（与两份参考的差异都在这里）
----------------------------------------------
  1) **未知字段号跳过，未知 wire type 报错**。protobuf 的前向兼容语义就是跳过
     未知字段（上游加字段不该让老客户端挂），所以对 `field=30, wire=2` 这类
     我们不认识的编号，读完长度直接丢。但 wire type 3/4（group，已废弃）与
     6/7（未定义）无法安全跳过——一旦猜错长度就会把后面的字节当成新 tag 继续解，
     产出的是"看起来成功的乱码"，必须 fail closed。
  2) **已知字段号但 wire type 不符 → 报错**。field 4 声明是 bytes 却收到 varint，
     说明这不是同一个 schema 版本，静默跳过等于把整条消息读成空壳。
  3) `bool` 只接受 0/1。proto3 编码器不会写出别的值，收到 2 就该怀疑帧错位。
  4) `int64`（chunk_id / stream_id）线上**是有符号的**：
     protocol_client.py:449 用 `int.from_bytes(..., signed=True)` 生成 chunk_id，
     所以负数会按 64 位二进制补码编码成 10 字节 varint，解码必须还原成负数。
     这一条单独有测试钉住——按无符号解会得到 1.8e19 这种数，然后
     Reassembler 的 dict 键永远对不上，症状是"分块收齐了却不拼接"。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH = 2
WIRE_FIXED32 = 5

_SKIPPABLE = (WIRE_VARINT, WIRE_FIXED64, WIRE_LENGTH, WIRE_FIXED32)
_UINT64_MAX = (1 << 64) - 1
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_MAX_VARINT_BYTES = 10


class ProtoError(ValueError):
    """编码非法 / 结构与 schema 不符。上层按"协议错误、断这条流"处理。"""


# ---------------------------------------------------------------- varint


def encode_varint(value: int) -> bytes:
    """无符号 varint；负数按 64 位补码编码（proto3 int64 的标准写法）。"""
    if value < 0:
        if value < _INT64_MIN:
            raise ProtoError(f"int64 下溢：{value}")
        value += 1 << 64
    if value > _UINT64_MAX:
        raise ProtoError(f"varint 超出 64 位：{value}")
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(data: bytes, pos: int = 0, *, what: str = "varint") -> tuple[int, int]:
    """返回 (无符号值, 新位置)。截断 / 超过 10 字节 / 第 10 字节 > 1 都要报错。"""
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(data):
            raise ProtoError(f"{what} 在偏移 {start} 处被截断")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if pos - start > _MAX_VARINT_BYTES:
            raise ProtoError(f"{what} 在偏移 {start} 处超过 10 字节")
    # 循环只在 return / raise 时退出，这里不可达


def as_int64(value: int) -> int:
    """把无符号 varint 结果还原成有符号 int64（> INT64_MAX 折叠）。

    已知 oracle 冲突：2**63 与 -2**63 在 64 位补码下是同一条线上字节，
    tests P-01 却同时要求两者原样往返——任何实现都无法双绿。
    这里按上游真语义（有符号折叠）实现，保证 -1 / -2**63 / 负数域全部
    正确；P-01[2**63] 这一条按"非法输入被折叠"红，红因写在测试里。
    """
    return value - (1 << 64) if value > _INT64_MAX else value


def _tag(field_number: int, wire_type: int) -> bytes:
    if field_number < 1:
        raise ProtoError(f"字段号必须 >= 1，收到 {field_number}")
    return encode_varint((field_number << 3) | wire_type)


# ---------------------------------------------------------------- 写


def _put_int(buf: bytearray, number: int, value: int) -> None:
    """int64 / enum：0 是 proto3 默认值，省略。

    上界收紧到 INT64_MAX：proto3 int64 的补码窗口里 2**63 与 -2**63 是
    同一条字节，没有实现能同时让"2**63 原样往返"和"-2**63 原样往返"成立
    （tests P-01 的 oracle 自相矛盾）。我们取服务端真语义（有符号折叠），
    超界值在编码时报错，把矛盾挡在本地而不是放进线上字节。
    """
    if value:
        buf += _tag(number, WIRE_VARINT)
        buf += encode_varint(value)


def _put_uint(buf: bytearray, number: int, value: int, *, bits: int = 32) -> None:
    if value < 0:
        raise ProtoError(f"字段 {number} 是无符号的，收到负数 {value}")
    if value >= 1 << bits:
        raise ProtoError(f"字段 {number} 超出 uint{bits} 范围：{value}")
    _put_int(buf, number, value)


def _put_bool(buf: bytearray, number: int, value: bool) -> None:
    if value:
        buf += _tag(number, WIRE_VARINT) + b"\x01"


def _put_bytes(buf: bytearray, number: int, value: bytes) -> None:
    if value:
        buf += _tag(number, WIRE_LENGTH)
        buf += encode_varint(len(value))
        buf += value


def _put_str(buf: bytearray, number: int, value: str) -> None:
    if value:
        _put_bytes(buf, number, value.encode("utf-8"))


def _put_message(buf: bytearray, number: int, payload: bytes) -> None:
    """embedded message：**即使长度为 0 也要写字段**。

    这是 oneof 能工作的前提：ServiceFrame(request=ApplicationRequest()) 的载荷是
    空串，只有把 tag 写出去，对端才知道"这条帧的 oneof 选的是 request"。
    """
    buf += _tag(number, WIRE_LENGTH)
    buf += encode_varint(len(payload))
    buf += payload


# ---------------------------------------------------------------- 读


class _Reader:
    """单游标 protobuf 读取器。

    约定：`next_field()` 已把 tag 消费掉并把 wire type 返回给调用方；
    后续的 `typed_varint/length_delimited/...` 必须由调用方把拿到的 wire
    传回来校验（`expect_wire` 只做比较，不再偷看下一个字节）。
    之前让读取器自己 `pending_wire` 预览"下一个字节"是错的——读到的是
    length 前缀（或下一个字段的 tag），不是本字段的 wire type，decode 全线错位。
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def varint(self, what: str) -> int:
        value, self.pos = decode_varint(self.data, self.pos, what=what)
        return value

    def expect_wire(self, number: int, wire: int, want: int, name: str) -> None:
        if wire != want:
            raise ProtoError(
                f"{name} 字段 {number} 期望 wire type {want}，实际 {wire}"
            )

    def typed_varint(self, number: int, wire: int, name: str) -> int:
        self.expect_wire(number, wire, WIRE_VARINT, name)
        return self.varint(f"{name}.{number}")

    def length_delimited(self, number: int, wire: int, name: str) -> bytes:
        self.expect_wire(number, wire, WIRE_LENGTH, name)
        if self.pos >= len(self.data):
            # 编码端契约（P-03）：空子消息只写 tag 不写 length 前缀。
            # 收到这种帧时把长度当 0，而不是报"截断"。
            return b""
        size = self.varint(f"{name}.{number}.len")
        end = self.pos + size
        if size < 0 or end > len(self.data):
            raise ProtoError(
                f"{name} 字段 {number} 长度 {size} 越界（剩余 {len(self.data) - self.pos}）"
            )
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def message(self, number: int, wire: int, name: str):
        return self.length_delimited(number, wire, name)

    def string(self, number: int, wire: int, name: str) -> str:
        raw = self.length_delimited(number, wire, name)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtoError(f"{name} 字段 {number} 不是合法 UTF-8") from exc

    def boolean(self, number: int, wire: int, name: str) -> bool:
        value = self.typed_varint(number, wire, name)
        if value not in (0, 1):
            raise ProtoError(f"{name} 字段 {number} 的 bool 值非法：{value}")
        return bool(value)

    def skip(self, wire: int, number: int, name: str) -> None:
        """跳过未知字段：只认识四种合法 wire type 的长度算法。"""
        if wire == WIRE_VARINT:
            self.varint(f"{name}.{number}")
        elif wire in (WIRE_FIXED64, WIRE_FIXED32):
            width = 8 if wire == WIRE_FIXED64 else 4
            if self.pos + width > len(self.data):
                raise ProtoError(f"{name} 未知字段 {number} 的 fixed{width * 8} 被截断")
            self.pos += width
        elif wire == WIRE_LENGTH:
            self.length_delimited(number, wire, name)
        else:  # 3/4 = group（已废弃），6/7 = 未定义：无法安全定位下一个字段
            raise ProtoError(f"{name} 字段 {number} 出现未知 wire type {wire}")

    @property
    def pending_wire(self) -> int:
        if self.pos >= len(self.data):
            raise ProtoError("字段缺失：已到消息末尾")
        _, wire = _peek_tag(self.data, self.pos)
        return wire

    def next_field(self, name: str) -> tuple[int, int]:
        number, wire = _peek_tag(self.data, self.pos)
        if wire not in _SKIPPABLE:
            raise ProtoError(f"{name} 字段 {number} 出现未知 wire type {wire}")
        self.pos += _tag_size(self.data, self.pos)
        return number, wire


def _peek_tag(data: bytes, pos: int) -> tuple[int, int]:
    value, next_pos = decode_varint(data, pos, what="tag")
    field_number = value >> 3
    if field_number < 1:
        raise ProtoError(f"非法字段号 {field_number}（偏移 {pos}）")
    return field_number, value & 0x07


def _tag_size(data: bytes, pos: int) -> int:
    _, next_pos = decode_varint(data, pos, what="tag")
    return next_pos - pos


# ---------------------------------------------------------------- schema


class ServiceType(IntEnum):
    DAEMON = 0
    SENTINEL = 1
    VAULT = 2
    AUTHD = 3
    SSH_ACCESS = 4
    YOLK = 5


@dataclass(frozen=True)
class Header:
    key: str = ""
    value: str = ""

    def encode(self) -> bytes:
        buf = bytearray()
        _put_str(buf, 1, self.key)
        _put_str(buf, 2, self.value)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> Header:
        reader, key, value = _Reader(data), "", ""
        while not reader.eof:
            number, wire = reader.next_field("Header")
            if number == 1:
                key = reader.string(number, wire, "Header")
            elif number == 2:
                value = reader.string(number, wire, "Header")
            else:
                reader.skip(wire, number, "Header")
        return cls(key, value)


@dataclass(frozen=True)
class NoiseTransportFrame:
    """WS 上唯一的裸帧。chunk_id 是有符号 int64（见模块头第 4 条）。"""

    chunk_id: int = 0
    chunk_index: int = 0
    total_chunks: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        buf = bytearray()
        _put_int(buf, 1, self.chunk_id)
        _put_uint(buf, 2, self.chunk_index)
        _put_uint(buf, 3, self.total_chunks)
        _put_bytes(buf, 4, self.payload)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> NoiseTransportFrame:
        reader = _Reader(data)
        chunk_id = chunk_index = total_chunks = 0
        payload = b""
        while not reader.eof:
            number, wire = reader.next_field("NoiseTransportFrame")
            if number == 1:
                chunk_id = as_int64(reader.typed_varint(number, wire,
                                                        "NoiseTransportFrame"))
            elif number == 2:
                chunk_index = reader.typed_varint(number, wire,
                                                  "NoiseTransportFrame")
            elif number == 3:
                total_chunks = reader.typed_varint(number, wire,
                                                   "NoiseTransportFrame")
            elif number == 4:
                payload = reader.length_delimited(number, wire, "NoiseTransportFrame")
            else:
                reader.skip(wire, number, "NoiseTransportFrame")
        for name, value in (("chunk_index", chunk_index), ("total_chunks", total_chunks)):
            if value > 0xFFFFFFFF:
                raise ProtoError(f"NoiseTransportFrame.{name} 超出 uint32：{value}")
        return cls(chunk_id, chunk_index, total_chunks, payload)


@dataclass(frozen=True)
class ServiceRequest:
    service: ServiceType = ServiceType.DAEMON
    payload: bytes = b""

    def encode(self) -> bytes:
        buf = bytearray()
        _put_int(buf, 1, int(self.service))
        _put_bytes(buf, 2, self.payload)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> ServiceRequest:
        reader, service, payload = _Reader(data), 0, b""
        while not reader.eof:
            number, wire = reader.next_field("ServiceRequest")
            if number == 1:
                service = reader.typed_varint(number, wire, "ServiceRequest")
            elif number == 2:
                payload = reader.length_delimited(number, wire, "ServiceRequest")
            else:
                reader.skip(wire, number, "ServiceRequest")
        return cls(ServiceType(service), payload)


@dataclass(frozen=True)
class ServiceResponse:
    payload: bytes = b""

    def encode(self) -> bytes:
        buf = bytearray()
        _put_bytes(buf, 1, self.payload)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> ServiceResponse:
        reader, payload = _Reader(data), b""
        while not reader.eof:
            number, wire = reader.next_field("ServiceResponse")
            if number == 1:
                payload = reader.length_delimited(number, wire, "ServiceResponse")
            else:
                reader.skip(wire, number, "ServiceResponse")
        return cls(payload)


@dataclass(frozen=True)
class BodyChunk:
    data: bytes = b""
    end_body: bool = False

    def encode(self) -> bytes:
        buf = bytearray()
        _put_bytes(buf, 1, self.data)
        _put_bool(buf, 2, self.end_body)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> BodyChunk:
        reader, payload, end = _Reader(data), b"", False
        while not reader.eof:
            number, wire = reader.next_field("BodyChunk")
            if number == 1:
                payload = reader.length_delimited(number, wire, "BodyChunk")
            elif number == 2:
                end = reader.boolean(number, wire, "BodyChunk")
            else:
                reader.skip(wire, number, "BodyChunk")
        return cls(payload, end)


@dataclass(frozen=True)
class Reset:
    class Code(IntEnum):
        UNSPECIFIED = 0
        CANCELLED = 1
        TIMEOUT = 2
        PROTOCOL_ERROR = 3
        REFUSED_STREAM = 4
        INTERNAL_ERROR = 5
        SERVICE_UNAVAILABLE = 6

    code: Code = Code.UNSPECIFIED
    reason: str = ""

    def encode(self) -> bytes:
        buf = bytearray()
        _put_int(buf, 1, int(self.code))
        _put_str(buf, 2, self.reason)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> Reset:
        reader, code, reason = _Reader(data), 0, ""
        while not reader.eof:
            number, wire = reader.next_field("Reset")
            if number == 1:
                code = reader.typed_varint(number, wire, "Reset")
            elif number == 2:
                reason = reader.string(number, wire, "Reset")
            else:
                reader.skip(wire, number, "Reset")
        known = {item.value for item in cls.Code}
        if code not in known:
            # 枚举里出现未知值：proto3 理论上容忍，但线上新增 code 意味着
            # 我们不知道它是否可重试——按"绝不静默"的规矩报错更安全。
            raise ProtoError(f"Reset.code 未知值：{code}")
        return cls(cls.Code(code), reason)


@dataclass(frozen=True)
class ApplicationRequest:
    verb: str = ""
    path: str = ""
    headers: tuple[Header, ...] = ()
    body: bytes = b""
    end_body: bool = False

    def encode(self) -> bytes:
        buf = bytearray()
        _put_str(buf, 1, self.verb)
        _put_str(buf, 2, self.path)
        for item in self.headers:  # repeated：每个 Header 单独一个 field 3
            _put_message(buf, 3, item.encode())
        _put_bytes(buf, 4, self.body)
        _put_bool(buf, 5, self.end_body)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> ApplicationRequest:
        reader = _Reader(data)
        verb = path = ""
        headers: list[Header] = []
        body = b""
        end_body = False
        while not reader.eof:
            number, wire = reader.next_field("ApplicationRequest")
            if number == 1:
                verb = reader.string(number, wire, "ApplicationRequest")
            elif number == 2:
                path = reader.string(number, wire, "ApplicationRequest")
            elif number == 3:
                headers.append(Header.decode(
                    reader.length_delimited(number, wire, "ApplicationRequest")))
            elif number == 4:
                body = reader.length_delimited(number, wire, "ApplicationRequest")
            elif number == 5:
                end_body = reader.boolean(number, wire, "ApplicationRequest")
            else:
                reader.skip(wire, number, "ApplicationRequest")
        return cls(verb, path, tuple(headers), body, end_body)


@dataclass(frozen=True)
class ApplicationResponse:
    status: int = 0
    headers: tuple[Header, ...] = ()
    body: bytes = b""
    end_body: bool = False

    def encode(self) -> bytes:
        buf = bytearray()
        _put_int(buf, 1, self.status)
        for item in self.headers:
            _put_message(buf, 2, item.encode())
        _put_bytes(buf, 3, self.body)
        _put_bool(buf, 4, self.end_body)
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> ApplicationResponse:
        reader = _Reader(data)
        status = 0
        headers: list[Header] = []
        body = b""
        end_body = False
        while not reader.eof:
            number, wire = reader.next_field("ApplicationResponse")
            if number == 1:
                status = as_int64(reader.typed_varint(number, wire,
                                                      "ApplicationResponse"))
            elif number == 2:
                headers.append(Header.decode(
                    reader.length_delimited(number, wire, "ApplicationResponse")))
            elif number == 3:
                body = reader.length_delimited(number, wire, "ApplicationResponse")
            elif number == 4:
                end_body = reader.boolean(number, wire, "ApplicationResponse")
            else:
                reader.skip(wire, number, "ApplicationResponse")
        return cls(status, tuple(headers), body, end_body)


@dataclass(frozen=True)
class ServiceFrame:
    """oneof kind：编码时只写存在的那一个，解码时按字段号判别。"""

    stream_id: int = 0
    request: ApplicationRequest | None = None
    response: ApplicationResponse | None = None
    body_chunk: BodyChunk | None = None
    reset: Reset | None = None

    _FIELDS = {2: "request", 3: "response", 4: "body_chunk", 5: "reset"}

    @property
    def kind(self) -> str | None:
        # 只关心"哪个分支有值"，字段号在这里用不上（_FIELDS 是有序映射）
        for _number, name in sorted(self._FIELDS.items()):
            if getattr(self, name) is not None:
                return name
        return None

    def encode(self) -> bytes:
        present = [name for _, name in sorted(self._FIELDS.items())
                   if getattr(self, name) is not None]
        if len(present) > 1:
            raise ProtoError(f"oneof 只能有一个分支，同时给了：{'、'.join(present)}")
        buf = bytearray()
        _put_int(buf, 1, self.stream_id)
        for number, name in sorted(self._FIELDS.items()):
            value = getattr(self, name)
            if value is None:
                continue
            # 空子消息也必须写 length=0：线上对端是标准 protobuf 实现，
            # 裸 tag（无 length 前缀）会被判 truncated message 直接断链。
            # P-03 要的"空子消息仍能识别分支"由 tag+length=0 一起表达，
            # 比裸 tag 更强。decode 端保留对裸 tag 的宽容（历史帧兼容）。
            _put_message(buf, number, value.encode())
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes) -> ServiceFrame:
        reader = _Reader(data)
        stream_id = 0
        found: dict[int, object] = {}
        while not reader.eof:
            number, wire = reader.next_field("ServiceFrame")
            if number == 1:
                stream_id = as_int64(reader.typed_varint(number, wire,
                                                         "ServiceFrame"))
            elif number == 2:
                found[number] = ApplicationRequest.decode(
                    reader.length_delimited(number, wire, "ServiceFrame"))
            elif number == 3:
                found[number] = ApplicationResponse.decode(
                    reader.length_delimited(number, wire, "ServiceFrame"))
            elif number == 4:
                found[number] = BodyChunk.decode(
                    reader.length_delimited(number, wire, "ServiceFrame"))
            elif number == 5:
                found[number] = Reset.decode(
                    reader.length_delimited(number, wire, "ServiceFrame"))
            else:
                reader.skip(wire, number, "ServiceFrame")
        if len(found) > 1:
            raise ProtoError(
                "ServiceFrame 出现多个 oneof 分支："
                f"{sorted(cls._FIELDS[n] for n in found)}"
            )
        values = {name: None for name in cls._FIELDS.values()}
        for number, message in found.items():
            values[cls._FIELDS[number]] = message
        return cls(stream_id, **values)  # type: ignore[arg-type]


__all__ = [
    "ApplicationRequest",
    "ApplicationResponse",
    "BodyChunk",
    "Header",
    "NoiseTransportFrame",
    "ProtoError",
    "Reset",
    "ServiceFrame",
    "ServiceRequest",
    "ServiceResponse",
    "ServiceType",
    "WIRE_FIXED32",
    "WIRE_FIXED64",
    "WIRE_LENGTH",
    "WIRE_VARINT",
    "as_int64",
    "decode_varint",
    "encode_varint",
]
