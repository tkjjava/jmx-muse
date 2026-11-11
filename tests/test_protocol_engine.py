r"""protocol_engine.py 的离线测试：假协议客户端 + FakeClock，不联网、不跑浏览器。

为什么这样钉：这个模块的契约几乎全是"判定语义"——提示词白名单与措辞、
生成物提取（上传件不得当成品）、成品模态裁决、以及"拒绝/额度不足绝不换号"。
它们不需要真实网络也能完整复现，而一旦被悄悄改掉，表现是"白等十分钟"
或"同一个号被无限重试"，两者都很难从日志看出来。

两个关键注入点：
  * ``client`` 只按模块 docstring 里的鸭子契约被调用（request / read_stream /
    fs_write / fs_read / media_candidates），用假对象即可跑完整条 generate 流程；
  * ``monotonic`` / ``wall_clock`` 是可注入时钟。FakeClock **同时冻结**
    time.time 与 time.monotonic：引擎的"12 秒晚到宽限 / 超时"读 monotonic，
    "额度过期估算"读 wall clock；只冻一个，另一个断言就会随真实时间漂。
"""

from __future__ import annotations

import base64
import json

import pytest

from muse_bridge import session_http
from muse_bridge.protocol_engine import (
    DURATION_WHITELIST,
    FILE_REF_DEFAULT_BYTES,
    FILE_REF_VIDEO_FALLBACK_BYTES,
    LATE_MEDIA_GRACE_SECONDS,
    MILESTONES,
    NON_RETRYABLE,
    QUOTA_TEXT_RE,
    SIZE_WHITELIST,
    GenerationResult,
    MediaResource,
    ProtocolEngine,
    ProtocolGenerationError,
    ProtocolMediaTypeError,
    ProtocolModelRefusal,
    ProtocolQuotaExhausted,
    _duration_ms,
    build_image_prompt,
    build_video_prompt,
    collect_media,
    decode_reference,
    normalize_media_path,
    resource_kind,
    should_use_file_ref,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
IMAGE_PATH = "sandbox://workspace/gen/cat.png"


def png_data_url(data: bytes = PNG, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def small_mp4(seconds: float = 2.0) -> bytes:
    """能通过 refs.mp4_duration 的最小 moov/mvhd（version 0、timescale 1000）。"""
    timescale = 1000
    duration = int(seconds * timescale)
    mvhd_body = (
        b"\x00" * 12
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
        + b"\x00" * 4
    )
    mvhd = (len(mvhd_body) + 8).to_bytes(4, "big") + b"mvhd" + mvhd_body
    moov_body = mvhd
    return (len(moov_body) + 8).to_bytes(4, "big") + b"moov" + moov_body


class FakeClock:
    """同时冻结 wall clock 与 monotonic 的测试时钟。

    引擎里两套阈值各读一个时钟：monotonic 用于超时/晚到宽限（不受系统改时间影响），
    time.time 用于额度过期估算（要跟服务端时间戳对齐）。只冻结其中一个，
    另一套断言就会随真实时间漂移——这正是本 helper 存在的理由。
    """

    def __init__(self, wall: float = 1_900_000_000.0, mono: float = 0.0) -> None:
        self.wall = float(wall)
        self.mono = float(mono)

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class FakeEngineClient:
    """protocol_engine 鸭子契约的最小实现：事件脚本 + fs 记录 + 预设 HTTP 结果。

    ``events`` 里允许放 callable：它在"取下一条事件"时执行，用来在事件之间
    推进 FakeClock（模拟"message_done 之后又过了 13 秒"这类时序）。
    """

    def __init__(
        self,
        events=(),
        *,
        media=None,
        files=None,
        routes=None,
        async_media=False,
    ) -> None:
        self.events = list(events)
        self.media = list(media or [])
        self.files = dict(files or {})
        self.routes = dict(routes or {})
        self.async_media = async_media
        self.read_calls = []
        self.write_calls = []
        self.read_file_calls = []
        self.request_calls = []
        self.node_calls = 0
        self.capabilities = ["chat_cancel"]

    def new_task_node(self) -> str:
        self.node_calls += 1
        return f"node-{self.node_calls}"

    async def request(self, method, path, body=None, timeout=60.0):
        self.request_calls.append((method, path, body, timeout))
        return self.routes.get((method, path), {"result": {}})

    async def read_stream(self, prompt=None, references=None, *, node_id=None, timeout=None):
        self.read_calls.append(
            {
                "prompt": prompt,
                "references": references,
                "node_id": node_id,
                "timeout": timeout,
            }
        )
        for item in self.events:
            if callable(item):
                item()
                continue
            yield item

    async def fs_write(self, data, mime, filename):
        self.write_calls.append((bytes(data), mime, filename))
        return {
            "type": "file_ref",
            "path": f"sandbox://workspace/user/files/{filename}",
            "mime_type": mime,
            "filename": filename,
            "size": len(data),
        }

    async def fs_read(self, path):
        self.read_file_calls.append(path)
        return self.files.get(path, b"")

    def media_candidates(self, node_id=None):
        if self.async_media:
            return self._async_listing()
        return self.media

    async def _async_listing(self):
        return self.media


def make_engine(client, clock=None, *, grace=12.0):
    clock = clock or FakeClock()
    return ProtocolEngine(
        client,
        monotonic=clock.monotonic,
        wall_clock=clock.time,
        grace_seconds=grace,
    ), clock


def image_event(seq: int, path: str = "workspace/gen/cat.png", **fields) -> dict:
    resource = {"path": path, "mime": "image/png", **fields}
    return {"event": "delta.presentation", "seq": seq, "payload": {"resources": [resource]}}


def done_event(seq: int, *, status="completed", transcript=None) -> dict:
    payload = {"status": status}
    if transcript is not None:
        payload["transcript"] = transcript
    return {"event": "delta.message_done", "seq": seq, "payload": payload}


def transcript_text(text: str) -> dict:
    return {"messages": [{"content": [{"type": "text", "text": text}]}]}


# ---------------------------------------------------------------- 常量与异常


def test_module_constants_match_upstream_contract():
    """这些数字来自上游实测（12 秒晚到宽限、进度里程碑、白名单）。

    改掉会直接改变对外行为（提前判定失败 / 允许非法时长），必须有意识地改。
    """
    assert LATE_MEDIA_GRACE_SECONDS == 12.0
    assert MILESTONES == (25, 40, 50, 75, 100)
    assert DURATION_WHITELIST == (5, 6, 8, 10)
    assert SIZE_WHITELIST == ("16:9", "9:16", "1:1", "4:3", "3:4")
    assert FILE_REF_DEFAULT_BYTES == 4 * 1024 * 1024
    assert FILE_REF_VIDEO_FALLBACK_BYTES == 1024 * 1024
    for text in ("额度不足", "积分不足", "out of credits", "达到上限", "token limit"):
        assert QUOTA_TEXT_RE.search(text), text
    assert not QUOTA_TEXT_RE.search("生成完成")


def test_exception_layering_and_non_retryable_set():
    """拒绝 / 额度不足是**终态**：换号重试只会把同一个号反复打到，且拿不到新结果。
    其余（超时、断连、模态不符）允许换号重试。

    注意：`retryable()` 本身当前是坏的（`isinstance(exc, frozenset)` 会抛
    TypeError，见报告 protocol_engine.py:100），所以这里只能钉「终态集合」这份数据，
    钉不住谓词——谓词要等产品修好才能写绿测。
    """
    for exc in (ProtocolModelRefusal, ProtocolQuotaExhausted, ProtocolMediaTypeError):
        assert issubclass(exc, ProtocolGenerationError)
    assert frozenset({ProtocolModelRefusal, ProtocolQuotaExhausted}) == NON_RETRYABLE
    assert ProtocolModelRefusal in NON_RETRYABLE
    assert ProtocolQuotaExhausted in NON_RETRYABLE
    assert ProtocolGenerationError not in NON_RETRYABLE
    assert ProtocolMediaTypeError not in NON_RETRYABLE


# ---------------------------------------------------------------- 提示词构建


def test_image_prompt_rejects_size_outside_whitelist():
    with pytest.raises(ProtocolGenerationError, match="size='4:5'"):
        build_image_prompt("一只猫", size="4:5")


def test_image_prompt_requires_prompt_or_references():
    with pytest.raises(ProtocolGenerationError, match="prompt 不能为空"):
        build_image_prompt("", size="1:1")


def test_image_prompt_without_references_bans_history_album():
    """纯文生图必须显式禁止翻历史相册/索要原图。

    上游踩过的坑：不写这句，模型会把历史相册里一张旧图当"参考"输出，
    结果看起来"生成成功"但根本不是这次要的内容。
    """
    prompt = build_image_prompt("一只猫")
    assert "全新文生图创作" in prompt
    assert "请勿查找历史相册" in prompt
    assert "1:1" in prompt


def test_image_prompt_with_multiple_references_uses_legend():
    """多张附件必须带对照表：否则模型无法把"[图2]"对应到具体附件。"""
    prompt = build_image_prompt("把[图2]放到左边", kinds=["image", "image"])
    assert "附件对照：" in prompt
    assert "[图2] 是第 2 个附件" in prompt
    assert "基于我本次上传附带的参考图片（" in prompt


def test_image_prompt_with_mixed_materials_names_each_kind():
    prompt = build_image_prompt("融合", kinds=["image", "video"])
    assert "参考素材" in prompt
    assert "1 张图片、1 段视频" in prompt
    assert "只使用本次上传的这些附件" in prompt


def test_image_prompt_appends_style_and_extra():
    prompt = build_image_prompt("猫", style="水彩", extra="不要文字")
    assert "风格要求：水彩" in prompt
    assert prompt.endswith("不要文字")


@pytest.mark.parametrize("duration", [5, 6, 8, 10])
def test_video_prompt_accepts_whitelisted_durations(duration):
    """时长是硬白名单：非法值会让服务端静默失败（白等），必须本地拒绝。"""
    prompt = build_video_prompt("海边", duration=duration)
    assert f"时长严格为 {duration} 秒" in prompt
    assert prompt.count(f"时长严格为 {duration} 秒") == 1, "时长条款不得重复"


@pytest.mark.parametrize("duration", [0, 3, 7, 9, 11, 12, 30])
def test_video_prompt_rejects_non_whitelisted_durations(duration):
    with pytest.raises(ProtocolGenerationError, match="duration"):
        build_video_prompt("海边", duration=duration)


def test_video_prompt_rejects_non_integer_duration_with_actionable_message():
    with pytest.raises(ProtocolGenerationError, match="不是整数") as caught:
        build_video_prompt("海边", duration="abc")
    assert "允许时长" in str(caught.value)


def test_video_prompt_without_references_is_text_to_video():
    prompt = build_video_prompt("海边")
    assert "全新文生视频创作" in prompt
    assert "16:9" in prompt
    assert prompt.count("时长严格为 5 秒") == 1


def test_video_prompt_portrait_with_one_image_is_image_to_video():
    prompt = build_video_prompt("动起来", size="9:16", kinds=["image"])
    assert "严格9:16竖屏" in prompt
    assert "图生视频" in prompt
    assert "第一帧参考图" in prompt


def test_video_prompt_multiple_references_use_first_as_start_frame():
    """多图生视频：第一张是起始画面，其余是主体/场景/风格参考——写清否则模型乱挑。"""
    prompt = build_video_prompt("序列", kinds=["image", "image", "image"])
    assert "3 张参考图片" in prompt
    assert "以第一张作为主要起始画面" in prompt
    assert "图生视频" in prompt


def test_video_prompt_reference_token_gets_legend():
    prompt = build_video_prompt("[图2] 先动", kinds=["image", "image"])
    assert "附件对照：" in prompt
    assert "各图片的用途以下面的描述为准" in prompt


def test_video_prompt_with_mixed_materials_is_not_called_image_to_video():
    """含视频/音频等非图片素材时生成词必须是"视频"，不能谎称图生视频。"""
    prompt = build_video_prompt("融合", kinds=["video", "image"])
    assert "参考素材" in prompt
    assert "1 张图片、1 段视频" in prompt
    assert "图生视频" not in prompt


def test_video_prompt_generic_aspect_clause():
    prompt = build_video_prompt("x", size="4:3", duration=8)
    assert "生成一个视频（时长严格为 8 秒）" in prompt


def test_video_prompt_appends_extra_clause():
    prompt = build_video_prompt("x", extra="结尾留白")
    assert prompt.endswith("结尾留白")


def test_video_prompt_requires_prompt_or_references():
    with pytest.raises(ProtocolGenerationError, match="prompt 不能为空"):
        build_video_prompt("")


# ---------------------------------------------------------------- 媒体提取


def test_normalize_media_path_accepts_only_generated_locations():
    assert normalize_media_path("sandbox://workspace/a.png") == (
        "sandbox://workspace/a.png",
        "workspace/a.png",
    )
    assert normalize_media_path("workspace/a.png") == (
        "sandbox://workspace/a.png",
        "workspace/a.png",
    )
    assert normalize_media_path("workspace/user/ref.png") is None
    assert normalize_media_path("https://cdn/a.png") is None
    assert normalize_media_path(None) is None


def test_resource_kind_falls_back_to_extension_for_generic_mime():
    """声明成 application/octet-stream 时只能靠扩展名；不看扩展名会把视频当图片。"""
    assert resource_kind({"mime": "video/mp4"}) == "video"
    assert resource_kind({"mime": "image/jpeg"}) == "image"
    assert resource_kind({"mime": "application/octet-stream", "path": "a.mp4"}) == "video"
    assert resource_kind({"mime": "application/octet-stream", "path": "a.webm"}) == "video"
    assert resource_kind({"path": "a.png"}) == "image"
    assert resource_kind({}) == "image"


def test_collect_media_decodes_json_string_metadata():
    """metadata 实测可能是 JSON 字符串；不解码会丢掉 mime/宽高/时长。"""
    found = []
    collect_media(
        {
            "resources": [
                {
                    "metadata": json.dumps(
                        {
                            "path": "workspace/gen/a.png",
                            "mime_type": "image/png",
                            "width": 512,
                            "height": "768",
                            "duration_ms": 1000,
                            "seed": 7,
                        }
                    )
                }
            ]
        },
        found,
    )
    assert len(found) == 1
    item = found[0]
    assert item.path == "sandbox://workspace/gen/a.png"
    assert item.mime == "image/png"
    assert item.kind == "image"
    assert item.width == 512
    assert item.height == 768
    assert item.duration_ms == 1000
    assert item.seed == 7


def test_collect_media_excludes_uploads_and_dedupes():
    """workspace/user/ 是输入区：把上传件当成品就是回声；重复路径只收一次。"""
    found = []
    collect_media(
        {
            "resources": [
                {"path": "workspace/user/ref.png", "mime": "image/png"},
                {"path": "workspace/gen/a.png", "mime": "image/png"},
                {"path": "sandbox://workspace/gen/a.png", "mime": "image/png"},
            ]
        },
        found,
    )
    assert [item.path for item in found] == ["sandbox://workspace/gen/a.png"]


def test_collect_media_does_not_recurse_into_request_or_inline_payload():
    found = []
    collect_media(
        {
            "request": {"path": "workspace/gen/inner.png"},
            "data_base64": "AAAA",
            "path": "workspace/gen/real.png",
            "mime": "image/png",
        },
        found,
    )
    assert [item.path for item in found] == ["sandbox://workspace/gen/real.png"]


@pytest.mark.parametrize(
    ("size", "mime", "capabilities", "expected"),
    [
        (0, "image/png", (), False),
        (100 * FILE_REF_DEFAULT_BYTES, "image/png", (), False),
        (FILE_REF_VIDEO_FALLBACK_BYTES - 1, "video/mp4", (), False),
        (FILE_REF_VIDEO_FALLBACK_BYTES, "video/mp4", (), True),
        (FILE_REF_DEFAULT_BYTES - 1, "video/mp4", ("video_file_ref_ingest",), False),
        (FILE_REF_DEFAULT_BYTES, "video/mp4", ("video_file_ref_ingest",), True),
        (FILE_REF_DEFAULT_BYTES, "application/pdf", (), True),
        (FILE_REF_DEFAULT_BYTES - 1, "application/pdf", (), False),
    ],
)
def test_should_use_file_ref_thresholds(size, mime, capabilities, expected):
    """图片走内联；视频默认 1MiB 阈值，账号有 video_file_ref_ingest 能力才升到 4MiB。"""
    assert should_use_file_ref(size, mime, capabilities) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (png_data_url(), ("image", "image/png")),
        (base64.b64encode(PNG).decode(), ("image", "image/png")),
        ("https://cdn/ref.png", ("path", "application/octet-stream")),
        ("workspace/ref.png", ("path", "application/octet-stream")),
        ("sandbox://workspace/ref.png", ("path", "application/octet-stream")),
        ("/abs/ref.png", ("path", "application/octet-stream")),
    ],
)
def test_decode_reference_forms(value, expected):
    """data URL / 裸 base64 → 真字节；远端路径 → 空字节交给客户端解析（不自己抓网）。"""
    data, mime = decode_reference(value)
    if expected[0] == "path":
        assert data == b""
    else:
        assert data == PNG
    assert mime == expected[1]


def test_decode_reference_rejects_unusable_base64():
    with pytest.raises(ProtocolGenerationError, match="不是有效的 data URL / base64"):
        decode_reference("!!!!")
    with pytest.raises(ProtocolGenerationError, match="无法识别为已知媒体类型"):
        decode_reference(base64.b64encode(b"hello").decode())


def test_duration_ms_reads_mp4_container_when_payload_has_no_duration():
    """资源没给 duration_ms 时从容器读；读不出返回 None（不编造数字）。"""
    assert _duration_ms(small_mp4(2.0), "video/mp4") == 2000
    assert _duration_ms(b"not-a-container", "video/mp4") is None
    assert _duration_ms(PNG, "image/png") is None


def test_media_resource_to_event_shape():
    """事件里的媒体引用是下游取图用的；键名必须稳定。"""
    item = MediaResource(
        path=IMAGE_PATH,
        mime="image/png",
        kind="image",
        width=10,
        height=20,
        duration_ms=30,
        seed="s",
    )
    assert item.to_event() == {
        "path": IMAGE_PATH,
        "mime": "image/png",
        "kind": "image",
        "width": 10,
        "height": 20,
        "duration_ms": 30,
        "seed": "s",
    }


def test_generation_result_keeps_metadata_and_supports_tuple_unpack():
    """上游把 w/h/mime 全丢了；这里用 __iter__ 兼容二元组解包但不丢元数据。"""
    result = GenerationResult(
        data=PNG,
        mime="image/png",
        kind="image",
        width=512,
        height=512,
        duration_ms=None,
        seed=7,
        source_prompt="一只猫",
        path=IMAGE_PATH,
    )
    data, mime = result
    assert data == PNG
    assert mime == "image/png"
    info = result.to_dict()
    assert info["bytes"] == len(PNG)
    assert info["width"] == 512
    assert info["seed"] == 7
    assert info["path"] == IMAGE_PATH


# ---------------------------------------------------------------- 客户端适配层


def test_response_adapter_accepts_object_and_dict_forms():
    """客户端契约允许 Response 对象或 dict；两种形态都要归一成 (status, body, headers)。"""

    class ObjResponse:
        status = 202
        headers = {"X-Test": "yes"}

        @staticmethod
        def json():
            return {"ok": True}

    assert ProtocolEngine._response(ObjResponse()) == (
        202,
        {"ok": True},
        {"x-test": "yes"},
    )
    assert ProtocolEngine._response(
        {"status": 201, "headers": {"X-B": "2"}, "body": {"x": 1}}
    ) == (201, {"x": 1}, {"x-b": "2"})
    assert ProtocolEngine._response({"body": b'{"x":1}'}) == (200, {"x": 1}, {})
    assert ProtocolEngine._response({"body": [1, 2]}) == (200, {"value": [1, 2]}, {})


async def test_invoke_retries_lowercased_keywords_once():
    """兼容层：客户端实现可能只接受小写关键字（二进制接口的常见形态）。"""

    class CamelClient:
        async def echo(self, value=None):
            return value

    engine = ProtocolEngine(CamelClient())
    assert await engine._invoke("echo", Value=7) == 7


async def test_invoke_does_not_swallow_unrelated_type_errors():
    class BrokenClient:
        async def boom(self, value=None):
            raise TypeError("内部逻辑坏了")

    engine = ProtocolEngine(BrokenClient())
    with pytest.raises(TypeError, match="内部逻辑坏了"):
        await engine._invoke("boom", value=1)


def test_node_id_uses_client_factory_or_uuid():
    """事件按 node_id 归属；没有 new_task_node 的客户端也要拿到一个唯一的 hex。"""
    client = FakeEngineClient()
    client.node_calls = 0
    engine = ProtocolEngine(client)
    assert engine._node_id() == "node-1"
    assert client.node_calls == 1

    spare = ProtocolEngine(object())
    first, second = spare._node_id(), spare._node_id()
    assert len(first) == 32
    assert first != second


# ---------------------------------------------------------------- 额度


async def test_quota_estimates_expiry_with_wall_clock_not_monotonic():
    """拿不到 expires_at 时按 wall clock + 48h 估算。

    这里 FakeClock 的 monotonic 故意是 0，只有 wall 是真实量级：
    实现若误用 monotonic，得到的会是一个 1970 年附近的过期时间。
    """
    clock = FakeClock(wall=1_900_000_000.0, mono=0.0)
    client = FakeEngineClient(
        routes={
            ("GET", "/api/session"): {
                "status": 200,
                "headers": {"x-hatch-session-refreshed": "1"},
                "body": {"status": "assigned", "vm_state": "running"},
            }
        }
    )
    engine, _ = make_engine(client, clock)
    quota = await engine.quota()
    assert quota["renewable"] is True
    assert quota["vm_state"] == "RUNNING"
    assert quota["estimated_expiry_at"] == int(clock.wall) + session_http.VML_TTL_SECONDS
    assert client.request_calls[0][:2] == ("GET", "/api/session")


async def test_quota_passes_through_server_expiry_and_nested_result():
    clock = FakeClock()
    client = FakeEngineClient(
        routes={
            ("GET", "/api/session"): {
                "status": 200,
                "body": {
                    "result": {
                        "status": "assigned",
                        "vm_state": "RUNNING",
                        "expires_at": 1_800_000_100,
                    }
                },
            }
        }
    )
    engine, _ = make_engine(client, clock)
    quota = await engine.quota()
    assert quota["estimated_expiry_at"] == 1_800_000_100


async def test_quota_does_not_invent_expiry_when_vm_not_running():
    """vm 没在跑时给一个"48 小时后过期"是编造；拿不到就返回 None。"""
    client = FakeEngineClient(
        routes={
            ("GET", "/api/session"): {
                "status": 200,
                "body": {"status": "pending", "vm_state": "BOOTING"},
            }
        }
    )
    engine, _ = make_engine(client)
    quota = await engine.quota()
    assert quota == {"renewable": False, "vm_state": "BOOTING", "estimated_expiry_at": None}


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "401"), (500, "HTTP 500"), (503, "HTTP 503")],
)
async def test_quota_surfaces_http_failures(status, needle):
    client = FakeEngineClient(routes={("GET", "/api/session"): {"status": status}})
    engine, _ = make_engine(client)
    with pytest.raises(ProtocolGenerationError, match=needle):
        await engine.quota()


async def test_quota_prefers_client_outer_session_reading():
    """真机内层 `/api/session` 是 404；客户端提供外层读法时引擎必须走它。

    证据：`docs/PROTOCOL_QUOTA.md` 候选表 + `work/canvas_re/protocol-route-dump/`
    —— 内层 GET /api/session 与对照路由同形 `404 route not found`；能读它的是
    外层 HTTP（`session_http.renew_session`，带 hatch_sess cookie，由
    `HatchProtocolClient.session_quota()` 暴露）。
    """
    client = FakeEngineClient()

    async def session_quota():
        return {
            "renewable": True,
            "vm_state": "RUNNING",
            "estimated_expiry_at": 123,
        }

    client.session_quota = session_quota
    engine, _ = make_engine(client)
    quota = await engine.quota()
    assert quota == {"renewable": True, "vm_state": "RUNNING", "estimated_expiry_at": 123}
    # 内层一个请求都不能发：发了就是真机上的 404（且浪费一个 RTT）。
    assert client.request_calls == []


# ---------------------------------------------------------------- 参考素材


async def test_stage_references_passes_remote_paths_through_without_upload():
    """http(s)/workspace/sandbox 引用是"已知路径"，不该被本模块下载或改写。

    注意：data URL 形态当前会在 refs.ext_from_mime 处 AttributeError（见报告
    protocol_engine.py:642），所以这一组只能钉住"路径引用"这条能走通的链路。
    """
    client = FakeEngineClient()
    engine, _ = make_engine(client)
    staged = await engine._stage_references(
        ["https://cdn/ref.png", "workspace/user/ref.png"]
    )
    assert [item["type"] for item in staged] == ["path_ref", "path_ref"]
    assert staged[0]["mime_type"] == "application/octet-stream"
    assert staged[1]["path"] == "workspace/user/ref.png"
    assert client.write_calls == []
    assert engine.upload_refs == set()


async def test_generate_forwards_path_references_into_stream_call():
    """参考素材必须出现在 read_stream 的 references 里，否则模型看不到附件。"""
    clock = FakeClock()
    events = [image_event(1), done_event(2)]
    client = FakeEngineClient(events, files={IMAGE_PATH: PNG})
    engine, _ = make_engine(client, clock)
    result = await engine.generate_image(
        "按参考图改", references=["workspace/user/ref.png"]
    )
    assert client.read_calls[0]["references"] == [
        {
            "type": "path_ref",
            "mime_type": "application/octet-stream",
            "path": "workspace/user/ref.png",
        }
    ]
    assert client.write_calls == []
    assert result.data == PNG


# ---------------------------------------------------------------- 生成：成功路径


async def test_generate_image_returns_metadata_bytes_and_progress():
    """一次成功的生图：事件顺序 → 选最后一张匹配图 → fs_read 取字节 → 保留元数据。

    元数据（w/h/mime/seed）是刻意偏离上游保留的：丢了之后调用方只能靠猜。
    """
    clock = FakeClock()
    files = {IMAGE_PATH: PNG}
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "好的"}},
        image_event(2, seed=42, width=512, height=768),
        done_event(3),
    ]
    client = FakeEngineClient(events, files=files)
    engine, _ = make_engine(client, clock)
    progress = []
    result = await engine.generate_image("一只猫", on_progress=progress.append)

    assert result.data == PNG
    assert result.mime == "image/png"
    assert result.kind == "image"
    assert result.width == 512
    assert result.height == 768
    assert result.seed == 42
    assert result.source_prompt == "一只猫"
    assert result.path == IMAGE_PATH
    assert result.duration_ms is None
    assert progress == [25, 40, 50, 75, 100]
    assert client.read_file_calls == [IMAGE_PATH]
    assert client.read_calls[0]["prompt"] == "一只猫"
    assert client.read_calls[0]["node_id"] == "node-1"
    assert client.read_calls[0]["references"] == []


async def test_generate_image_picks_last_matching_candidate():
    """多轮补发时最后一张才是成品；取第一张会返回上一轮的旧图。"""
    clock = FakeClock()
    files = {
        "sandbox://workspace/gen/first.png": PNG + b"first",
        IMAGE_PATH: PNG,
    }
    events = [
        image_event(1, path="workspace/gen/first.png"),
        image_event(2),
        done_event(3),
    ]
    client = FakeEngineClient(events, files=files)
    engine, _ = make_engine(client, clock)
    result = await engine.generate_image("一只猫")
    assert result.path == IMAGE_PATH
    assert result.data == PNG
    assert client.read_file_calls == [IMAGE_PATH]


async def test_generate_video_derives_duration_from_container():
    clock = FakeClock()
    video_path = "sandbox://workspace/gen/clip.mp4"
    events = [
        {
            "event": "delta.presentation",
            "seq": 1,
            "payload": {
                "resources": [{"path": "workspace/gen/clip.mp4", "mime": "video/mp4"}]
            },
        },
        done_event(2),
    ]
    client = FakeEngineClient(events, files={video_path: small_mp4(2.0)})
    engine, _ = make_engine(client, clock)
    result = await engine.generate_video("一段海浪")
    assert result.kind == "video"
    assert result.mime == "video/mp4"
    assert result.duration_ms == 2000


# ---------------------------------------------------------------- 生成：失败裁决


async def test_generate_video_with_only_cover_image_raises_media_type_error():
    """期望视频却只拿到封面静帧：必须报"模态不符"，不能把 jpg 当视频交出去。

    这条要求"完成后再宽限 12 秒"：晚到的成品可能在 message_done 之后才出现，
    立刻判失败会把一次正常生成判成失败。
    """
    clock = FakeClock()
    events = [
        image_event(1, path="workspace/gen/cover.png"),
        done_event(2),
        lambda: clock.advance(13),
    ]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client, clock)
    with pytest.raises(ProtocolMediaTypeError, match="只拿到 image，期望 video") as caught:
        await engine.generate_video("一段海浪")
    assert "封面静帧不得冒充成品" in str(caught.value)


async def test_generate_reports_each_kind_of_missing_media_distinctly():
    """"什么都没拿到"和"拿到错误模态"要分开：前者可换号，后者语义完全不同。"""
    clock = FakeClock()
    events = [done_event(1), lambda: clock.advance(13)]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client, clock)
    with pytest.raises(ProtocolGenerationError, match="生成完成但未收到 video 成品"):
        await engine.generate_video("一段海浪")


async def test_generate_without_message_done_reports_missing_result():
    clock = FakeClock()
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "想想"}},
    ]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client, clock)
    with pytest.raises(ProtocolGenerationError, match="未收到 image 成品"):
        await engine.generate_image("一只猫")


async def test_uploaded_reference_never_counts_as_product():
    """上传件在 workspace/user/ 下，永远不能当成品；否则就是回声一张输入图。"""
    clock = FakeClock()
    events = [
        image_event(1, path="workspace/user/input.png"),
        done_event(2),
        lambda: clock.advance(13),
    ]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client, clock)
    with pytest.raises(ProtocolGenerationError, match="未收到 image 成品"):
        await engine.generate_image("一只猫")


async def test_quota_wording_in_delta_is_terminal():
    """delta 里出现额度关键词就必须立刻判终态：继续等只会白等到超时。"""
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "抱歉，积分不足"}}
    ]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client)
    with pytest.raises(ProtocolQuotaExhausted, match="账号额度不足"):
        await engine.generate_image("一只猫")


async def test_model_refusal_from_message_done_is_not_retryable():
    """status 非 completed 且只回文本 = 模型拒绝生成；换号重试只会得到同样的拒绝。"""
    events = [
        done_event(1, status="failed", transcript=transcript_text("我只能聊天"))
    ]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client, clock=FakeClock())
    with pytest.raises(ProtocolModelRefusal, match="模型未生成媒体，仅返回文本"):
        await engine.generate_image("一只猫")


async def test_quota_wording_in_transcript_is_quota_exhausted():
    events = [done_event(1, status="failed", transcript=transcript_text("out of credits"))]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client)
    with pytest.raises(ProtocolQuotaExhausted):
        await engine.generate_image("一只猫")


async def test_message_removed_is_refusal():
    events = [{"event": "delta.message_removed", "seq": 1, "payload": {}}]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client)
    with pytest.raises(ProtocolModelRefusal, match="移除了这次生成"):
        await engine.generate_video("一段海浪")


async def test_pool_error_means_connection_is_gone():
    """pool_error 是池层信号：立刻失败让上层换号/重连，不要傻等。"""
    events = [{"type": "pool_error", "seq": 1}]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client)
    with pytest.raises(ProtocolGenerationError, match="连接已断开"):
        await engine.generate_image("一只猫")


async def test_generate_deduplicates_replayed_sequence():
    """同一 seq 被重放（断线重连常见）只处理一次，否则文本会重复。"""
    clock = FakeClock()
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "a"}},
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "b"}},
        image_event(2),
        done_event(3),
    ]
    client = FakeEngineClient(events, files={IMAGE_PATH: PNG})
    engine, _ = make_engine(client, clock)
    result = await engine.generate_image("一只猫")
    assert result.data == PNG
    # 通过 _run 的路径能直接观察文本：重复 seq 的第二条必须被丢弃
    text, _media = await ProtocolEngine(FakeEngineClient(events)).collect_chat("x")
    assert text == "a"


async def test_foreign_device_events_are_ignored():
    """同一订阅流上别的 node 的事件不得进本轮回答（否则串台）。"""
    events = [
        {
            "event": "delta.text_append",
            "seq": 1,
            "payload": {"text": "别人的回答", "chat_context": {"device_id": "other"}},
        },
        {
            "event": "delta.text_append",
            "seq": 2,
            "payload": {"text": "我的回答", "chat_context": {"device_id": "node-1"}},
        },
        done_event(3),
    ]
    engine = ProtocolEngine(FakeEngineClient(events))
    text, _media = await engine.collect_chat("x")
    assert text == "我的回答"


async def test_foreign_device_media_is_ignored():
    """媒体归属：别的 node 的 presentation 不能当成这回合的成品。

    正文那条分支一直有 `ours` 过滤，媒体那条**没有**（2026-11-11 补）。缺口形态
    是"问了 A，取回来的附件属于上一个请求"——在本机看起来一切正常，只是附件错。
    """
    events = [
        {
            "event": "delta.presentation",
            "seq": 1,
            "payload": {
                "resources": [{"path": "workspace/gen/other.png", "mime": "image/png"}],
                "chat_context": {"device_id": "other"},
            },
        },
        {
            "event": "delta.presentation",
            "seq": 2,
            "payload": {
                "resources": [{"path": "workspace/gen/mine.png", "mime": "image/png"}],
                "chat_context": {"device_id": "node-1"},
            },
        },
        {"event": "delta.text_append", "seq": 3, "payload": {"text": "给你"}},
        done_event(4),
    ]
    engine = ProtocolEngine(FakeEngineClient(events))
    text, media = await engine.collect_chat("x")
    assert text == "给你"
    assert [item["path"] for item in media] == ["sandbox://workspace/gen/mine.png"]


async def test_generate_times_out_while_stream_keeps_talking():
    """超过 timeout 还在来事件 → 必须主动失败，不能继续等到天荒地老。"""
    clock = FakeClock()
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "a"}},
        lambda: clock.advance(400),
        {"event": "delta.text_append", "seq": 2, "payload": {"text": "b"}},
        done_event(3),
    ]
    client = FakeEngineClient(events)
    engine, _ = make_engine(client, clock)
    with pytest.raises(ProtocolGenerationError, match="纯协议生成超时"):
        await engine.generate_image("一只猫", timeout=300)


async def test_generate_rejects_empty_prompt_and_unsupported_kind():
    engine = ProtocolEngine(FakeEngineClient())
    with pytest.raises(ProtocolGenerationError, match="生成提示词为空"):
        await engine.generate_image("   ")
    with pytest.raises(ProtocolGenerationError, match="不支持的生成类型"):
        await engine._generate(
            "x", expect="audio", references=None, timeout=1.0, on_progress=None
        )


# ---------------------------------------------------------------- 事件流（chat 路径）


async def test_stream_chat_emits_delta_media_and_done():
    """chat 路径只出文本与媒体引用，不做模态裁决。"""
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "你"}},
        {"event": "delta.text_append", "seq": 2, "payload": {"text": "好"}},
        image_event(3),
        done_event(4),
    ]
    engine = ProtocolEngine(FakeEngineClient(events))
    seen = []
    async for event in engine.stream_chat("hi"):
        seen.append(event)
    assert [item["type"] for item in seen] == ["delta", "delta", "media", "done"]
    assert seen[0]["text"] == "你"
    assert seen[1]["text"] == "好"
    assert seen[2]["ref"]["path"] == "sandbox://workspace/gen/cat.png"
    assert seen[3]["text"] == "你好"


async def test_collect_chat_returns_text_and_media_refs():
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "看这张"}},
        image_event(2, mime="image/png"),
        done_event(3),
    ]
    engine = ProtocolEngine(FakeEngineClient(events))
    text, media = await engine.collect_chat("hi")
    assert text == "看这张"
    assert [item["path"] for item in media] == ["sandbox://workspace/gen/cat.png"]


async def test_stream_chat_surfaces_refusal_then_raises():
    """拒绝要先给调用方一个 refused 事件（能展示给用户），再抛终态错误。"""
    events = [
        {"event": "delta.text_append", "seq": 1, "payload": {"text": "抱歉"}},
        done_event(2, status="refused", transcript=transcript_text("不生成")),
    ]
    engine = ProtocolEngine(FakeEngineClient(events))
    seen = []
    with pytest.raises(ProtocolModelRefusal):
        async for event in engine.stream_chat("hi"):
            seen.append(event)
    assert [item["type"] for item in seen] == ["delta", "refused"]
    assert seen[-1]["text"] == "不生成"


async def test_chat_stream_ending_after_text_is_complete_without_message_done():
    """真机序列没有 delta.message_done：流按 agent.status 收尾后必须算完成。

    2026-11-11 活网：13+13 的回答只有 2 条 delta.text_append，之后订阅流自己
    结束（客户端 `_turn_ended` 命中 agent.status 回到 online）。旧实现把"流结束
    但没见到 message_done"一律判超时，把一次成功回答变成报错；这条钉住修复。
    """
    engine = ProtocolEngine(
        FakeEngineClient([{"event": "delta.text_append", "seq": 1, "payload": {"text": "a"}}])
    )
    events = [event async for event in engine.stream_chat("hi")]
    assert [event["type"] for event in events] == ["delta", "done"]
    assert events[0]["text"] == "a"
    assert events[-1]["text"] == "a"


async def test_chat_stream_ending_without_any_text_is_still_an_error():
    """流结束但一段正文都没有：仍必须报错（不能把空回答当成功）。"""
    engine = ProtocolEngine(FakeEngineClient([]))
    with pytest.raises(ProtocolGenerationError, match="未收到完整结果"):
        await engine.collect_chat("hi")


async def test_chat_falls_back_to_transcript_when_no_deltas():
    """上游 message_done 可能不带 delta，正文在 transcript 里；不读就会回空串。"""
    events = [done_event(1, transcript=transcript_text("来自 transcript"))]
    engine = ProtocolEngine(FakeEngineClient(events))
    text, _media = await engine.collect_chat("hi")
    assert text == "来自 transcript"


async def test_chat_ignores_unusable_events():
    """非 dict / res 占位 / 缺 payload 的事件都不该影响结果，也不该中断流。"""
    events = [
        42,
        {"event": "res", "seq": 1, "payload": {}},
        {"event": "delta.text_append", "seq": 2},
        {"event": "delta.text_append", "seq": 3, "payload": {"text": "ok"}},
        done_event(4),
    ]
    engine = ProtocolEngine(FakeEngineClient(events))
    text, _media = await engine.collect_chat("hi")
    assert text == "ok"


def test_transcript_text_collects_only_text_parts():
    assert ProtocolEngine._transcript_text(transcript_text("a")) == "a"
    assert (
        ProtocolEngine._transcript_text(
            {"messages": [{"content": [{"type": "image"}, {"type": "text", "text": "b"}]}]}
        )
        == "b"
    )
    assert ProtocolEngine._transcript_text(None) == ""
    assert ProtocolEngine._transcript_text({"messages": [1, {"content": []}]}) == ""


# ---------------------------------------------------------------- 取字节与补充来源


async def test_fetch_bytes_requires_bytes_from_client():
    """fs_read 必须返回字节；返回包装 dict 直接报错，不能猜字段。"""

    class DictReadClient:
        async def fs_read(self, path):
            return {"data": b"x"}

    engine = ProtocolEngine(DictReadClient())
    item = MediaResource(path=IMAGE_PATH, raw={"path": IMAGE_PATH})
    with pytest.raises(ProtocolGenerationError, match="不是字节"):
        await engine._fetch_bytes(item)


async def test_fetch_bytes_falls_back_to_download_bytes():
    """兼容上游 client.download_bytes(resource)：没有 fs_read 时走这条。"""

    class LegacyClient:
        def __init__(self):
            self.calls = []

        async def download_bytes(self, resource):
            self.calls.append(resource)
            return b"z"

    client = LegacyClient()
    engine = ProtocolEngine(client)
    item = MediaResource(path=IMAGE_PATH, raw={"path": IMAGE_PATH})
    assert await engine._fetch_bytes(item) == b"z"
    assert client.calls == [{"path": IMAGE_PATH}]


@pytest.mark.parametrize("async_media", [False, True])
async def test_prefetch_media_accepts_sync_and_async_listing(async_media):
    """客户端若提供 media_candidates，作为补充来源；异步实现也要被 await 掉。"""
    clock = FakeClock()
    media = [{"path": "workspace/gen/extra.png", "mime": "image/png"}]
    extra = "sandbox://workspace/gen/extra.png"
    client = FakeEngineClient(media=media, async_media=async_media, files={extra: PNG})
    engine, _ = make_engine(client, clock)
    result = await engine.generate_image("一只猫")
    assert result.path == extra
    assert result.data == PNG
