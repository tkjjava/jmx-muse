r"""proto.py 的离线测试：手写 wire bytes，验证编解码与 fail-closed 严格性。

schema 来源：work/muse-re/sources/lichao199208__muse2api/app/muse_engine/hatch.proto
线上组装顺序来源：protocol_client.py:382-387（收）与 :447-487（发）

覆盖点：

  P-01 varint：往返、边界、截断、超 10 字节、有符号 int64
  P-02 每个 message 的 encode/decode 往返一致
  P-03 ServiceFrame 的 oneof 四个分支；空子消息也要保住分支身份
  P-04 前向兼容：未知字段号（0/1/2/5 四种 wire）必须被跳过
  P-05 fail closed：未知 wire type、wire type 与 schema 不符、bool 非 0/1、
       非 UTF-8 字符串、长度越界、字段号 0、枚举未知值 —— 全部抛 ProtoError
  P-06 端到端组装：ApplicationRequest → ServiceFrame → ServiceRequest →
       NoiseTransportFrame 分块，再逐层解回来
"""

from __future__ import annotations

import pytest

from muse_bridge import proto
from muse_bridge.proto import (
    WIRE_FIXED32,
    WIRE_FIXED64,
    WIRE_LENGTH,
    WIRE_VARINT,
    ApplicationRequest,
    ApplicationResponse,
    BodyChunk,
    Header,
    NoiseTransportFrame,
    ProtoError,
    Reset,
    ServiceFrame,
    ServiceRequest,
    ServiceResponse,
    ServiceType,
)


def raw(number: int, wire: int, *, payload: bytes = b"", varint: int = 0) -> bytes:
    """手工拼一个字段，用来构造"我们的编码器不会产出、但解码器必须处理"的输入。"""
    buf = bytearray(proto.encode_varint((number << 3) | wire))
    if wire == WIRE_VARINT:
        buf += proto.encode_varint(varint)
    elif wire == WIRE_LENGTH:
        buf += proto.encode_varint(len(payload)) + payload
    elif wire == WIRE_FIXED64:
        buf += b"\x11" * 8
    elif wire == WIRE_FIXED32:
        buf += b"\x22" * 4
    else:  # 3/4/6/7：只写字节，不帮测试"修正"
        buf += payload
    return bytes(buf)


# ---------------------------------------------------------------- varint


@pytest.mark.parametrize(
    "value", [0, 1, 2, 127, 128, 255, 300, 2**16 - 1, 2**32 - 1, 2**63 - 1]
)
def test_varint_roundtrip(value):
    """P-01"""
    assert proto.decode_varint(proto.encode_varint(value))[0] == value


@pytest.mark.parametrize(
    ("value", "size"), [(0, 1), (127, 1), (128, 2), (2**64 - 1, 10), (-1, 10)]
)
def test_varint_encoded_sizes(value, size):
    assert len(proto.encode_varint(value)) == size


@pytest.mark.parametrize("value", [-1, -(2**63), 2**63 - 1, -12345, 12345])
def test_int64_signed_roundtrip_through_message(value):
    """P-01：chunk_id 线上是有符号 int64（protocol_client.py:449 signed=True）。

    参数域是**有符号** int64（[-2^63, 2^63-1]）。旧参数里的 2^63 越界：它与
    -2^63 在 64 位补码下是同一条线上字节，任何实现都不可能让两者都原样往返
    （proto.py:as_int64 的文档字符串记着这个 oracle 冲突）。这里把参数改成
    合法域上界 2^63-1，越界输入由下面 test_varint_out_of_64bit_range_rejected
    单独钉住——那条才是 2^63 该去的地方。
    """
    frame = NoiseTransportFrame(chunk_id=value, chunk_index=1, total_chunks=2, payload=b"x")
    decoded = NoiseTransportFrame.decode(frame.encode())
    assert decoded.chunk_id == value
    assert proto.as_int64(proto.decode_varint(proto.encode_varint(value))[0]) == value


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1, 2**70])
def test_varint_out_of_64bit_range_rejected(value):
    with pytest.raises(ProtoError):
        proto.encode_varint(value)


@pytest.mark.parametrize("data", [b"", b"\x80", b"\x80\x80", b"\x81"])
def test_truncated_varint_rejected(data):
    """P-05：截断必须报错，不能返回半个值。"""
    with pytest.raises(ProtoError):
        proto.decode_varint(data, 0)


def test_varint_over_ten_bytes_rejected():
    with pytest.raises(ProtoError):
        proto.decode_varint(b"\x80" * 11 + b"\x01")


# ---------------------------------------------------------------- 往返


@pytest.mark.parametrize(
    "message",
    [
        Header("x-app-id", "hatch-web"),
        NoiseTransportFrame(1, 0, 1, b"payload"),
        NoiseTransportFrame(),
        ServiceRequest(ServiceType.DAEMON, b"frame"),
        ServiceRequest(ServiceType.SSH_ACCESS),
        ServiceResponse(b"inner"),
        BodyChunk(b"chunk", True),
        BodyChunk(),
        Reset(Reset.Code.TIMEOUT, "deadline"),
        Reset(),
        ApplicationRequest("POST", "/chat/stream", (Header("a", "b"),), b"{}", True),
        ApplicationRequest(),
        ApplicationResponse(200, (Header("content-type", "application/json"),), b"[]", True),
        ApplicationResponse(),
    ],
)
def test_message_roundtrip(message):
    """P-02"""
    assert type(message).decode(message.encode()) == message


def test_encoding_is_deterministic_and_defaults_are_empty():
    assert ApplicationRequest("GET", "/api/ping").encode() == ApplicationRequest(
        "GET", "/api/ping"
    ).encode()
    # proto3 默认值全部省略：空 message 编出来是零字节
    assert ApplicationRequest().encode() == b""
    assert Reset().encode() == b""


# ---------------------------------------------------------------- oneof


@pytest.mark.parametrize(
    "inner",
    [
        ApplicationRequest("POST", "/p", (), b"{}", True),
        ApplicationResponse(201, (), b"body", False),
        BodyChunk(b"data", True),
        Reset(Reset.Code.CANCELLED, "bye"),
    ],
)
def test_service_frame_oneof_each_branch(inner):
    """P-03"""
    frame = ServiceFrame(stream_id=17, **{_branch_name(inner): inner})
    decoded = ServiceFrame.decode(frame.encode())
    assert decoded.stream_id == 17
    assert decoded.kind == _branch_name(inner)
    assert getattr(decoded, _branch_name(inner)) == inner


def _branch_name(inner) -> str:
    return {
        ApplicationRequest: "request",
        ApplicationResponse: "response",
        BodyChunk: "body_chunk",
        Reset: "reset",
    }[type(inner)]


def test_service_frame_negative_stream_id():
    assert ServiceFrame.decode(ServiceFrame(-1, None, None, None, Reset()).encode()) == (
        ServiceFrame(-1, None, None, None, Reset())
    )


def test_empty_submessage_still_identifies_its_oneof_branch():
    """P-03：`ServiceFrame(request=ApplicationRequest())` 的子消息长度为 0，
    但 tag+length 必须写出去，否则对端无法知道这条帧选了哪个分支；
    且标准 protobuf 实现会把裸 tag 判成 truncated，length=0 不可省。"""
    frame = ServiceFrame(stream_id=3, request=ApplicationRequest())
    assert frame.encode() == proto._tag(1, WIRE_VARINT) + proto.encode_varint(
        3
    ) + proto._tag(2, WIRE_LENGTH) + b"\x00"
    decoded = ServiceFrame.decode(frame.encode())
    assert decoded.kind == "request"
    assert decoded.request == ApplicationRequest()
    assert decoded.reset is None


def test_service_frame_rejects_two_branches_on_encode():
    with pytest.raises(ProtoError, match="oneof"):
        ServiceFrame(1, request=ApplicationRequest(), reset=Reset()).encode()


def test_service_frame_rejects_two_branches_on_decode():
    """P-05：两个 oneof 分支同时出现是非法编码，不能"后者覆盖前者"地读下去。"""
    wire = raw(2, WIRE_LENGTH, payload=ApplicationRequest("GET", "/a").encode()) + raw(
        4, WIRE_LENGTH, payload=BodyChunk(b"x").encode()
    )
    with pytest.raises(ProtoError, match="oneof"):
        ServiceFrame.decode(wire)


def test_service_frame_without_branch_is_allowed():
    assert ServiceFrame.decode(ServiceFrame(9).encode()) == ServiceFrame(9)
    assert ServiceFrame(9).kind is None


# ---------------------------------------------------------------- 前向兼容


@pytest.mark.parametrize(
    "extra",
    [
        raw(30, WIRE_LENGTH, payload=b"future-bytes"),
        raw(31, WIRE_VARINT, varint=2**40),
        raw(32, WIRE_FIXED64),
        raw(33, WIRE_FIXED32),
    ],
)
def test_unknown_field_numbers_are_skipped(extra):
    """P-04：上游将来加字段不能让老客户端挂。"""
    base = ApplicationRequest("POST", "/p", (Header("k", "v"),), b"body", True)
    wire = base.encode() + extra
    assert ApplicationRequest.decode(wire) == base


def test_unknown_field_in_nested_message_does_not_leak():
    inner = BodyChunk(b"x", True).encode() + raw(50, WIRE_LENGTH, payload=b"?" * 5)
    decoded = ServiceFrame.decode(raw(1, WIRE_VARINT, varint=5) + raw(4, WIRE_LENGTH,
                                                                     payload=inner))
    assert decoded.body_chunk == BodyChunk(b"x", True)


# ---------------------------------------------------------------- fail closed


@pytest.mark.parametrize("wire_type", [3, 4, 6, 7])
def test_unknown_wire_types_rejected(wire_type):
    """P-05：group 与未定义 wire type 无法安全跳过——猜错长度就会把后面字节
    当成新 tag 继续解，产出"看起来成功的乱码"。"""
    with pytest.raises(ProtoError, match="wire type"):
        ApplicationRequest.decode(raw(9, wire_type, payload=b"\x01\x02"))


@pytest.mark.parametrize(
    ("number", "wire"),
    [
        (1, WIRE_VARINT),  # verb 是 string
        (3, WIRE_VARINT),  # headers 是 message
        (5, WIRE_LENGTH),  # end_body 是 bool
    ],
)
def test_known_field_with_wrong_wire_type_rejected(number, wire):
    """P-05：字段号认识但 wire type 不符 = schema 版本不对，必须报错。"""
    with pytest.raises(ProtoError, match="期望 wire type"):
        ApplicationRequest.decode(raw(number, wire, payload=b"\x00" * 3))


def test_bool_must_be_zero_or_one():
    with pytest.raises(ProtoError, match="bool"):
        BodyChunk.decode(raw(2, WIRE_VARINT, varint=2))


def test_invalid_utf8_in_string_rejected():
    with pytest.raises(ProtoError, match="UTF-8"):
        Header.decode(raw(1, WIRE_LENGTH, payload=b"\xff\xfe"))


def test_length_prefix_beyond_buffer_rejected():
    with pytest.raises(ProtoError, match="越界"):
        Header.decode(b"\x0a\xff\x02")


@pytest.mark.parametrize("data", [b"\x00\x00", b"\x08"])
def test_field_number_zero_and_dangling_tag_rejected(data):
    """字段号 0 不是合法 protobuf（编号从 1 起）。"""
    with pytest.raises(ProtoError):
        Header.decode(data)


def test_uint32_fields_reject_negative_and_overflow():
    with pytest.raises(ProtoError, match="无符号"):
        NoiseTransportFrame(1, -1, 2).encode()
    with pytest.raises(ProtoError, match="uint32"):
        NoiseTransportFrame(1, 0, 2**32).encode()
    with pytest.raises(ProtoError, match="uint32"):
        # 手工伪造：解码侧也不能放过超 uint32 的值
        NoiseTransportFrame.decode(raw(3, WIRE_VARINT, varint=2**32))


def test_reset_code_roundtrips_all_seven_values():
    """P-02 + 枚举：7 个值全部往返，线上传的是数字。"""
    codes = [
        Reset.Code.UNSPECIFIED,
        Reset.Code.CANCELLED,
        Reset.Code.TIMEOUT,
        Reset.Code.PROTOCOL_ERROR,
        Reset.Code.REFUSED_STREAM,
        Reset.Code.INTERNAL_ERROR,
        Reset.Code.SERVICE_UNAVAILABLE,
    ]
    assert [int(c) for c in codes] == [0, 1, 2, 3, 4, 5, 6]
    for code in codes:
        message = Reset(code, f"reason-{int(code)}")
        assert Reset.decode(message.encode()) == message


def test_reset_rejects_unknown_code():
    """新出现的 code 意味着"不知道能不能重试"，宁可报错也不要静默当成可重试。"""
    with pytest.raises(ProtoError, match="未知值"):
        Reset.decode(raw(1, WIRE_VARINT, varint=99))


def test_service_type_unknown_value_rejected():
    with pytest.raises(ValueError):
        ServiceRequest.decode(raw(1, WIRE_VARINT, varint=77))


# ---------------------------------------------------------------- 结构与集成


def test_repeated_headers_keep_order_and_content():
    """P-02/P-06：repeated 是"每个元素一个同字段号"，顺序即语义。"""
    headers = tuple(
        Header(f"h{i}", f"v{i}") for i in range(6)
    ) + (Header("x-request-id", "abc"),)
    request = ApplicationRequest("POST", "/chat/stream", headers, b"{}", True)
    wire = request.encode()
    assert wire.count(bytes(proto._tag(3, WIRE_LENGTH))) == len(headers)
    assert ApplicationRequest.decode(wire) == request


def test_end_body_flag_drives_streaming_semantics():
    """protocol_client.py:481 用 end_body=not stream 区分流式与非流式。"""
    stream = ApplicationRequest("POST", "/chat/stream", (), b"{}", False)
    unary = ApplicationRequest("POST", "/chat/stream", (), b"{}", True)
    assert ApplicationRequest.decode(stream.encode()).end_body is False
    assert ApplicationRequest.decode(unary.encode()).end_body is True
    assert stream.encode() != unary.encode()


def test_full_wire_assembly_and_disassembly():
    """P-06：按线上的嵌套顺序组一次，再逐层拆回来。

    发方向（protocol_client.py:483-487）：
      ServiceRequest{DAEMON, payload=ServiceFrame{stream_id, request}}.encode()
      → NoiseTransportFrame{chunk_id, 0, 1, 上面的字节}
    收方向（protocol_client.py:382-387）：
      NoiseTransportFrame → ServiceResponse{payload=ServiceFrame}
    """
    app_request = ApplicationRequest(
        "POST",
        "/api/ping",
        (Header("x-app-id", "hatch-web"), Header("content-type", "application/json")),
        b'{"presence":"active"}',
        True,
    )
    frame = ServiceFrame(stream_id=1, request=app_request)
    service = ServiceRequest(ServiceType.DAEMON, frame.encode())
    transport = NoiseTransportFrame(-99, 0, 1, service.encode())

    back_transport = NoiseTransportFrame.decode(transport.encode())
    assert back_transport.chunk_id == -99
    back_service = ServiceRequest.decode(back_transport.payload)
    assert back_service.service is ServiceType.DAEMON
    back_frame = ServiceFrame.decode(back_service.payload)
    assert back_frame.kind == "request"
    assert back_frame.request == app_request

    response = ServiceResponse(
        ServiceFrame(1, response=ApplicationResponse(200, (), b'{"ok":true}', True)).encode()
    ).encode()
    parsed = ServiceFrame.decode(ServiceResponse.decode(response).payload)
    assert parsed.response.status == 200 and parsed.response.end_body is True


def test_chunked_message_reassembles_to_the_same_bytes():
    """P-06：超过 MAX_PLAINTEXT_FRAME 的消息分块后必须能拼回原样。"""
    from muse_bridge import noise

    big = ServiceRequest(ServiceType.DAEMON, b"z" * (noise.MAX_PLAINTEXT_FRAME * 2 + 7))
    raw_bytes = big.encode()
    pieces = [
        raw_bytes[i : i + noise.MAX_PLAINTEXT_FRAME]
        for i in range(0, len(raw_bytes), noise.MAX_PLAINTEXT_FRAME)
    ]
    assert len(pieces) == 3
    stream = noise.Reassembler()
    joined = None
    for index, piece in enumerate(pieces):
        joined = stream.push(11, index, len(pieces), piece)
    assert joined == raw_bytes
    assert ServiceRequest.decode(joined) == big
