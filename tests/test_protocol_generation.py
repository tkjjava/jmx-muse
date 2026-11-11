"""纯协议生成编排的离线契约（不连真实 muse.ai、不起浏览器）。

为什么钉这些：`protocol_generation.py` 的价值全在"没证实的东西必须炸"这条
纪律上。它一旦悄悄把未证实端点发出去、或者把 `workspace/user/` 里用户上传
的图当成成品返回，故障会出现在很远的地方（404 / 一张回声图），排查成本比
现在多写几个断言高得多。所以这里把参数白名单、事件归属、成品挑选、取件闸门
逐条钉死，全部用假 client 驱动。
"""

from __future__ import annotations

import base64

import pytest

from muse_bridge.protocol_generation import (
    ACK_TIMEOUT_SECONDS,
    ASPECT_RATIOS,
    DONE_GRACE_SECONDS,
    EXPECT_IMAGE,
    EXPECT_VIDEO,
    GENERATION_PATH,
    MAX_GENERATION_ITEMS,
    VIDEO_DURATIONS,
    GenerationError,
    GenerationRequest,
    ProtocolGeneration,
    Reference,
    UnverifiedCapabilityError,
    UnverifiedEndpointError,
    build_items,
    should_use_file_ref,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"m" * 32


class _Response:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self.payload = payload or {}
        self.body = b""

    def json(self) -> dict:
        return self.payload


class _Stream:
    """假 NDJSON 流：只吐预先排好的事件，支持 aclose（generate() 会在 finally 里调）。"""

    def __init__(self, events) -> None:
        self._events = list(events)
        self._index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event

    async def aclose(self) -> None:
        self.closed = True


class _Client:
    """假协议客户端。鸭子契约与 protocol_client 一致（request/read_stream/fs_*）。"""

    def __init__(
        self,
        *,
        ack_status: int = 200,
        ack_payload: dict | None = None,
        events=(),
        files: dict[str, bytes] | None = None,
        node_id: str = "node-1",
        capabilities=(),
    ) -> None:
        self.ack_status = ack_status
        self.ack_payload = ack_payload if ack_payload is not None else {"message_id": "m1"}
        self.events = events
        self.files = files or {}
        self.node_id = node_id
        self.capabilities = capabilities
        self.requests: list[tuple] = []
        self.fs_reads: list[str] = []
        self.streams: list[_Stream] = []

    async def request(self, method, path, body=None, timeout=None):
        self.requests.append((method, path, body, timeout))
        return _Response(self.ack_status, dict(self.ack_payload))

    def read_stream(self):
        stream = _Stream(self.events)
        self.streams.append(stream)
        return stream

    async def fs_read(self, path: str) -> bytes:
        self.fs_reads.append(path)
        return self.files[path]

    async def fs_write(self, data, mime, filename):  # 生成只会读，不写
        raise AssertionError("生成流程不应调用 fs_write")


def _presentation(*resources: dict) -> dict:
    return {"name": "delta.presentation", "payload": {"resources": list(resources)}}


def _text(text: str) -> dict:
    return {"name": "delta.text_append", "payload": {"text": text}}


# ---- 请求参数 ----------------------------------------------------------


def test_request_rejects_empty_prompt():
    with pytest.raises(GenerationError, match="提示词"):
        GenerationRequest(prompt="   ")


def test_request_rejects_unknown_expect():
    with pytest.raises(GenerationError, match="expect"):
        GenerationRequest(prompt="画只猫", expect="audio")


@pytest.mark.parametrize("duration", [1, 7, 9, 11])
def test_request_rejects_duration_outside_bridge_whitelist(duration):
    with pytest.raises(GenerationError, match="duration"):
        GenerationRequest(prompt="走路", expect=EXPECT_VIDEO, duration=duration)


@pytest.mark.parametrize("duration", VIDEO_DURATIONS)
def test_request_accepts_whitelisted_durations(duration):
    request = GenerationRequest(prompt="走路", expect=EXPECT_VIDEO, duration=duration)
    assert request.duration == duration


@pytest.mark.parametrize("aspect", ["2:1", "21:9", "", "9:16:1"])
def test_request_rejects_unknown_aspect(aspect):
    with pytest.raises(GenerationError, match="aspect_ratio"):
        GenerationRequest(prompt="画只猫", aspect_ratio=aspect)


@pytest.mark.parametrize("aspect", ASPECT_RATIOS)
def test_request_accepts_known_aspects(aspect):
    assert GenerationRequest(prompt="画只猫", aspect_ratio=aspect).aspect_ratio == aspect


def test_request_caps_reference_count():
    refs = tuple(Reference(PNG, "image/png") for _ in range(MAX_GENERATION_ITEMS + 1))
    with pytest.raises(GenerationError, match="参考素材"):
        GenerationRequest(prompt="画只猫", references=refs)


def test_render_prompt_keeps_params_in_text_not_structured_fields():
    """时长/比例/种子只能进提示词：已观测的 body 没有这些结构化字段。"""
    request = GenerationRequest(
        prompt="海边走路",
        expect=EXPECT_VIDEO,
        duration=8,
        aspect_ratio="9:16",
    )
    text = request.render_prompt()
    assert "9:16" in text
    assert "8 秒" in text
    assert "视频" in text


def test_body_shape_is_items_node_capabilities():
    """提交体只允许出现观测到的三个键（+ 可选的 metadata）。"""
    request = GenerationRequest(prompt="画只猫，带 1 张参考图", references=(Reference(PNG, "image/png"),))
    body = request.body("node-1")
    assert set(body) == {"items", "node_id", "capabilities"}
    assert body["node_id"] == "node-1"
    assert body["capabilities"] == {}


def test_body_adds_metadata_only_when_marker_given():
    body = GenerationRequest(prompt="画", marker="abc").body("node-1")
    assert body["metadata"] == {"client_marker": "abc"}


def test_build_items_puts_references_first_and_text_last():
    items = build_items("画只猫", [Reference(PNG, "image/png", "a.png")])
    assert items[-1] == {"type": "text", "text": "画只猫"}
    assert items[0]["type"] == "image"
    assert items[0]["filename"] == "a.png"
    assert base64.b64decode(items[0]["data_base64"]) == PNG


def test_build_items_names_file_refs_for_unknown_mime():
    items = build_items("画", [Reference(b"raw", "")])
    assert items[0]["type"] == "file"
    assert items[0]["mime_type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "size,mime,caps,expected",
    [
        (1, "image/png", (), False),  # 图片永远 inline
        (11 * 1024 * 1024, "image/png", ("video_file_ref_ingest",), False),
        (10 * 1024 * 1024, "video/mp4", (), False),
        (10 * 1024 * 1024, "video/mp4", ("video_file_ref_ingest",), True),
        (61 * 1024 * 1024, "video/mp4", (), True),
        (10 * 1024 * 1024, "application/pdf", (), True),
    ],
)
def test_should_use_file_ref_thresholds(size, mime, caps, expected):
    assert should_use_file_ref(size, mime, caps) is expected


# ---- 未证实端点 --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/gen/image", "/gen/video", "/image/generate", "/video/generate", "/api/hatch/generate", "/chat/send"],
)
def test_probe_endpoint_refuses_every_speculative_endpoint(path):
    """候选端点全部是**排除结论**：调用必须抛，不许悄悄发出去碰运气。"""
    generation = ProtocolGeneration(_Client())
    with pytest.raises(UnverifiedEndpointError, match=GENERATION_PATH):
        generation.probe_endpoint(path)


def test_probe_endpoint_refuses_unknown_path():
    generation = ProtocolGeneration(_Client())
    with pytest.raises(UnverifiedEndpointError, match="不在已证实端点表里"):
        generation.probe_endpoint("/something/new")


def test_known_endpoints_are_the_evidenced_ones():
    assert ProtocolGeneration.known_endpoints() == (
        GENERATION_PATH,
        "/chat/subscribe",
        "/fs/read",
        "/fs/write",
        "/api/ping",
    )


def test_constructor_refuses_client_without_duck_contract():
    with pytest.raises(GenerationError, match="request"):
        ProtocolGeneration(object())


# ---- 生成主流程 --------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_image_reads_result_through_fs():
    path = "sandbox://workspace/out.png"
    client = _Client(
        events=[
            _text("好的，"),
            _presentation({"path": path, "mime": "image/png", "width": 512, "height": 512}),
        ],
        files={path: PNG},
    )
    generation = ProtocolGeneration(client)
    result = await generation.generate(GenerationRequest(prompt="画只猫"))

    assert result.data == PNG
    assert result.kind == EXPECT_IMAGE
    assert result.width == 512 and result.height == 512
    assert result.text == "好的，"
    assert result.via == "protocol.noise.fs.read"
    # 提交体必须是 /chat/stream，且只带观测到的三个键。
    method, request_path, body, timeout = client.requests[0]
    assert (method, request_path) == ("POST", GENERATION_PATH)
    assert set(body) == {"items", "node_id", "capabilities"}
    assert timeout == ACK_TIMEOUT_SECONDS
    # 取件走 /fs/read，不发 CDN 请求。
    assert client.fs_reads == [path]


@pytest.mark.asyncio
async def test_generate_video_matches_video_kind_only():
    path = "sandbox://workspace/out.mp4"
    client = _Client(
        events=[_presentation({"path": path, "mime": "video/mp4", "byte_len": len(MP4)})],
        files={path: MP4},
    )
    generation = ProtocolGeneration(client)
    result = await generation.generate(GenerationRequest(prompt="海边走路", expect=EXPECT_VIDEO))
    assert result.kind == EXPECT_VIDEO
    assert result.data == MP4
    assert result.byte_len == len(MP4)


@pytest.mark.asyncio
async def test_generate_rejects_non_200_ack_and_records_quota_observation():
    client = _Client(ack_status=429, ack_payload={"error": "rate limited"})
    generation = ProtocolGeneration(client)
    with pytest.raises(GenerationError, match="HTTP 429"):
        await generation.generate(GenerationRequest(prompt="画只猫"))
    assert generation.quota_observations[-1].stage == "submit"
    assert generation.quota_observations[-1].status == 429


@pytest.mark.asyncio
async def test_generate_ignores_workspace_user_upload_area():
    """用户上传区里的文件是输入不是成品：拿它当结果就是回声一张输入图。"""
    client = _Client(
        events=[_presentation({"path": "workspace/user/ref.png", "mime": "image/png"})],
        files={"workspace/user/ref.png": PNG},
    )
    generation = ProtocolGeneration(client)
    with pytest.raises(GenerationError, match="事件流结束"):
        await generation.generate(GenerationRequest(prompt="画只猫"))
    assert client.fs_reads == []


@pytest.mark.asyncio
async def test_generate_filters_events_by_device_id():
    """归属不符的 presentation 必须被丢掉，否则会把别人的成品当自己的交出去。"""
    client = _Client(
        events=[
            {"name": "delta.presentation", "payload": {"device_id": "other", "resources": [{"path": "sandbox://workspace/x.png", "mime": "image/png"}]}},
            _presentation({"path": "sandbox://workspace/ok.png", "mime": "image/png"}),
        ],
        files={"sandbox://workspace/ok.png": PNG},
    )
    generation = ProtocolGeneration(client)
    result = await generation.generate(GenerationRequest(prompt="画只猫"))
    assert result.path == "sandbox://workspace/ok.png"


@pytest.mark.asyncio
async def test_generate_raises_on_pool_error():
    client = _Client(events=[{"name": "pool_error", "payload": {"reason": "容量不足"}}])
    generation = ProtocolGeneration(client)
    with pytest.raises(GenerationError, match="pool_error"):
        await generation.generate(GenerationRequest(prompt="画只猫"))


@pytest.mark.asyncio
async def test_generate_times_out_without_candidates():
    """文本 delta 不算进度：settle 事件没成品、deadline 到了必须抛超时。"""
    client = _Client(events=[_presentation()])
    generation = ProtocolGeneration(client, timeout=0.0)
    with pytest.raises(GenerationError, match="超时"):
        await generation.generate(GenerationRequest(prompt="画只猫"))


@pytest.mark.asyncio
async def test_generate_done_grace_expiry_mentions_missing_kind():
    """message_done 之后宽限期到点还没成品：必须点名"没等到哪种成品"。"""
    client = _Client(events=[{"name": "delta.message_done", "payload": {}}, _text("只有文字")])
    generation = ProtocolGeneration(client, done_grace=0.0)
    with pytest.raises(GenerationError, match="没等到 image 成品"):
        await generation.generate(GenerationRequest(prompt="画只猫"))
    assert generation.quota_observations


@pytest.mark.asyncio
async def test_generate_cdn_direct_url_is_unverified():
    """直链取件未证实：必须抛 UnverifiedCapabilityError，不许偷偷 GET。"""
    generation = ProtocolGeneration(_Client())
    with pytest.raises(UnverifiedCapabilityError, match="CDN"):
        await generation._download({"path": "https://cdn.example/x.png"})


@pytest.mark.asyncio
async def test_generate_rejects_zero_byte_result():
    path = "sandbox://workspace/empty.png"
    client = _Client(events=[_presentation({"path": path, "mime": "image/png"})], files={path: b""})
    generation = ProtocolGeneration(client)
    with pytest.raises(GenerationError, match="0 字节"):
        await generation.generate(GenerationRequest(prompt="画只猫"))


@pytest.mark.asyncio
async def test_generate_accepts_wrapped_fs_read_payload():
    """假 client（以及未来的传输层）可能把字节包在 dict 里：要认。"""
    path = "sandbox://workspace/wrapped.png"
    client = _Client(events=[_presentation({"path": path, "mime": "image/png"})], files={path: {"data": PNG}})
    generation = ProtocolGeneration(client)
    result = await generation.generate(GenerationRequest(prompt="画只猫"))
    assert result.data == PNG


@pytest.mark.asyncio
async def test_read_stream_must_be_async_iterator():
    class _BadClient(_Client):
        def read_stream(self):
            return [{"name": "delta.text_append", "payload": {"text": "x"}}]

    generation = ProtocolGeneration(_BadClient())
    with pytest.raises(GenerationError, match="异步迭代器"):
        await generation.generate(GenerationRequest(prompt="画只猫"))


@pytest.mark.asyncio
async def test_generate_finds_resource_from_later_settle_event():
    """空 presentation 不构成成品；同一个 node 后面 message_done 带资源时要取到它。"""
    first = "sandbox://workspace/v1.png"
    later = "sandbox://workspace/v2.png"
    client = _Client(
        events=[
            _presentation(),  # 只有壳、没有 resources
            {"name": "delta.message_done", "payload": {"resources": [{"path": later, "mime": "image/png"}]}},
        ],
        files={first: PNG, later: PNG},
    )
    generation = ProtocolGeneration(client, done_grace=10.0)
    result = await generation.generate(GenerationRequest(prompt="画只猫"))
    assert result.path == later


@pytest.mark.asyncio
async def test_generate_returns_first_ready_presentation_without_waiting_done():
    """presentation 一就绪就可以交付：等 message_done 只是白等一轮宽限。"""
    path = "sandbox://workspace/early.png"
    client = _Client(
        events=[
            _presentation({"path": path, "mime": "image/png"}),
            {"name": "delta.message_done", "payload": {}},
        ],
        files={path: PNG},
    )
    generation = ProtocolGeneration(client)
    result = await generation.generate(GenerationRequest(prompt="画只猫"))
    assert result.path == path
    assert result.events_seen == {"delta.presentation": 1}


def test_done_grace_constant_is_the_observed_twelve_seconds():
    """12 秒是参考实现里的观测值，不是拍脑袋的默认值。"""
    assert DONE_GRACE_SECONDS == 12.0
    assert ACK_TIMEOUT_SECONDS == 60.0
