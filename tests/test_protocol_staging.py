"""参考素材 staging + 视频生成路径的离线契约测试（不触网、不跑浏览器）。

为什么单开一个文件、而不是塞进既有的 client/engine 测试：

* 这里钉的是**两条路径之间的接缝** —— `参考素材 → fs_write → file_ref → read_stream`
  的上传链，以及 `expect=video → generate_video → 落盘扩展名` 的模态链。
  接缝错了，表现要么是"模型看不到附件"（白等几分钟、无任何报错），要么是
  "视频请求交付了一张封面静帧"。两种情况在真机上极难从日志看出来。
* 假件分两层：`FakeFsWS` 是**真的** HatchProtocolClient 跑在明文假 WS 上
  （只实现 /fs/* 四个端点），所以 `fs_write` 的两种调用姿势、分片标志、
  stat 字节数校验、临时名 rename 全部是产品代码在跑；`FakeEngineClient`
  与 `FakeDriverEngine` 则只实现引擎/驱动的鸭子契约。
* 2 条已知红测试（`tests/test_noise.py`、`tests/test_proto.py`）与本文件无关，
  绝不修改。

已知产品缺口（**2026-11-11 10:2x 起已修复，保留记录**）：

* ~~`protocol_engine._stage_references` 不调用 `refs.normalize_references`~~：
  已由 task-8 接线（数量上限 10、单件 40 MiB / 总量 100 MiB、视频 120 秒现在
  在**任何 fs_write 之前**生效；超限抛 `ProtocolGenerationError`，证明见
  `tests/test_protocol_refs_guard.py`）。
* ~~空载荷 data URL（`data:image/png;base64,`）被当成 `path_ref`~~：
  现在由 `refs.normalize_references` 的"空文件"判定拒绝，不再落到 path_ref。
所以下面的"空/超限拒绝"原本钉在 `refs` 这一层（两条驱动共用判定契约），
现在协议路径也走同一层；本文件保留原断言作为**回归基线**。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re

import pytest

from muse_bridge import noise, proto, refs
from muse_bridge.protocol_client import HatchProtocolClient, ProtocolError
from muse_bridge.protocol_driver import ProtocolDriver
from muse_bridge.protocol_engine import (
    GenerationResult,
    ProtocolEngine,
    ProtocolGenerationError,
    ProtocolMediaTypeError,
)

COOKIES = {"hatch_sess": "sess-value"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PRODUCT_PATH = "sandbox://workspace/gen/out.png"
REFERENCE_NAME_RE = re.compile(r"reference_0_[0-9a-f]{8}\.png")


def data_url(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


# ---------------------------------------------------------------- 假 WS（fs 面）
def _wire_reply(stream_id: int, payload: dict) -> bytes:
    """一条明文响应帧，形状与 tests/test_protocol_client.py 的 wire() 一致。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = proto.ApplicationResponse(status=200, headers=(), body=body, end_body=True)
    frame = proto.ServiceFrame(stream_id=stream_id, response=response)
    service = proto.ServiceResponse(payload=frame.encode())
    return proto.NoiseTransportFrame(
        chunk_id=1, chunk_index=0, total_chunks=1, payload=service.encode()
    ).encode()


class FakeFsWS:
    """只实现 /fs/write、/fs/stat、/fs/rename、/fs/delete、/fs/read 的假 WS。

    明文直通（客户端注入 `noise.Cipher()` 的恒等形态），所以断言的是产品代码
    发的**帧内容**：分片标志、声明的字节数、临时名与 rename 目标。
    """

    def __init__(self, *, read_files: dict[str, bytes] | None = None) -> None:
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.requests: list[tuple[str, str, dict]] = []
        self.sizes: dict[str, int] = {}
        self.renames: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.read_files = dict(read_files or {})
        self.closed = False

    async def send(self, data: bytes) -> None:
        transport = proto.NoiseTransportFrame.decode(bytes(data))
        service = proto.ServiceRequest.decode(transport.payload)
        frame = proto.ServiceFrame.decode(service.payload)
        request = frame.request
        body = json.loads(request.body or b"{}")
        self.requests.append((request.verb, request.path, body))
        payload = self._handle(request.path, body)
        await self.inbox.put(_wire_reply(frame.stream_id, payload))

    def _handle(self, path: str, body: dict) -> dict:
        if path == "/fs/write":
            chunk = base64.b64decode(body["data_base64"])
            staging = body["path"]
            if body.get("append"):
                self.sizes[staging] = self.sizes.get(staging, 0) + len(chunk)
            else:
                self.sizes[staging] = len(chunk)
            return {"ok": True}
        if path == "/fs/stat":
            return {"result": {"kind": "file", "size": self.sizes.get(body["path"], 0)}}
        if path == "/fs/rename":
            self.renames.append((body["source"], body["destination"]))
            return {"ok": True}
        if path == "/fs/delete":
            self.deletes.append(body["path"])
            return {"ok": True}
        if path == "/fs/read":
            data = self.read_files.get(body["path"], b"")
            offset = int(body.get("offset") or 0)
            length = int(body.get("len") or 1024 * 1024)
            window = data[offset : offset + length]
            return {
                "result": {
                    "data_base64": base64.b64encode(window).decode("ascii"),
                    "len": len(window),
                    "eof": offset + len(window) >= len(data),
                }
            }
        raise AssertionError(f"未预设端点：{path}")

    async def recv(self) -> bytes:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


@contextlib.asynccontextmanager
async def connected_client(ws: FakeFsWS):
    """一个"已经连上"的客户端：注入明文 cipher + 假 WS + 正在跑的接收循环。"""
    client = HatchProtocolClient(COOKIES, site="https://muse.example")
    client.ws = ws
    client.send_cipher = client.recv_cipher = noise.Cipher()
    client._handshake_done = True
    client.receiver_task = asyncio.create_task(client._receiver())
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------- fs_write：两种姿势
async def test_engine_form_fs_write_lands_in_user_files_and_returns_sandbox_ref():
    """引擎形态 `fs_write(data, mime, filename)`：单个分片 + stat + rename 到位。

    参考素材**必须**落在 `workspace/user/files/` 下才会被会话读到；返回值形状
    是引擎 `_stage_references` 直接塞进 `/chat/stream` 的 items 里的那份。
    """
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        ref = await client.fs_write(PNG, "image/png", "cat")

    assert ref == {
        "type": "file_ref",
        "path": "sandbox://workspace/user/files/cat.png",
        "mime_type": "image/png",
        "filename": "cat.png",
        "size": len(PNG),
    }
    assert [path for _verb, path, _body in ws.requests] == [
        "/fs/write",
        "/fs/stat",
        "/fs/rename",
    ]
    write = ws.requests[0][2]
    assert write["path"].startswith("workspace/user/files/.upload-")
    assert write["path"].endswith(".part")
    assert base64.b64decode(write["data_base64"]) == PNG
    assert write["overwrite"] is True and write["append"] is False
    assert write["create_parent"] is True
    assert ws.renames == [(write["path"], "workspace/user/files/cat.png")]
    assert ws.deletes == []


@pytest.mark.parametrize(
    ("filename", "mime", "expected_name"),
    [
        ("cat", "image/png", "cat.png"),
        ("clip", "video/mp4", "clip.mp4"),
        ("clip.mp4", "video/mp4", "clip.mp4"),
        ("../../etc/passwd", "image/png", "passwd.png"),
        ("blob", "application/x-unlikely", "blob.bin"),
    ],
)
async def test_engine_form_fs_write_derives_extension_and_sanitizes_name(
    filename, mime, expected_name
):
    """文件名压成安全片段、缺扩展名时由 mime 推导，否则服务端拿不到正确类型。"""
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        ref = await client.fs_write(PNG, mime, filename)

    assert ref["filename"] == expected_name
    assert ref["path"] == f"sandbox://workspace/user/files/{expected_name}"
    assert ws.renames[-1][1] == f"workspace/user/files/{expected_name}"


async def test_spec_form_fs_write_chunks_payload_into_ordered_appends():
    """规格形态 `fs_write(path, data, chunk=...)`：首片 overwrite、后续 append。

    分片标志写错（全部 overwrite）时服务端只会留下最后一片，文件静默截断，
    而截断的参考图会让模型"看到"半张图，失败点离原因很远。
    """
    payload = bytes(range(10))
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        ref = await client.fs_write("workspace/gen/big.bin", payload, chunk=4)

    writes = [body for verb, path, body in ws.requests if path == "/fs/write"]
    assert len(writes) == 3
    assert [len(base64.b64decode(item["data_base64"])) for item in writes] == [4, 4, 2]
    assert [(item["overwrite"], item["append"]) for item in writes] == [
        (True, False),
        (False, True),
        (False, True),
    ]
    assert [item["create_parent"] for item in writes] == [True, False, False]
    assert writes[0]["path"] == writes[1]["path"] == writes[2]["path"]
    assert ws.sizes[writes[0]["path"]] == len(payload)
    assert ref["path"] == "sandbox://workspace/gen/big.bin"
    assert ref["mime_type"] == "application/octet-stream"
    assert ref["size"] == len(payload)
    assert ws.renames == [(writes[0]["path"], "workspace/gen/big.bin")]


async def test_spec_form_fs_write_accepts_sandbox_prefix():
    """`sandbox://workspace/...` 是同一目标的另一种写法，不该被路径校验误拒。"""
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        ref = await client.fs_write("sandbox://workspace/gen/ref.png", PNG)

    assert ref["path"] == "sandbox://workspace/gen/ref.png"
    assert ref["mime_type"] == "image/png"
    assert ws.renames[-1][1] == "workspace/gen/ref.png"


@pytest.mark.parametrize(
    ("path", "needle"),
    [("/tmp/x.png", "workspace/"), ("", "路径为空"), (None, "路径为空")],
)
async def test_spec_form_fs_write_rejects_non_workspace_paths_without_send(path, needle):
    """写路径只认 workspace/；越界必须在发第一帧之前拒绝（这是 fail-closed）。"""
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        with pytest.raises(ProtocolError, match=needle):
            await client.fs_write(path, PNG)
    assert ws.requests == []


async def test_spec_form_fs_write_allows_zero_byte_payload_and_verifies_zero_stat():
    """0 字节是合法上传（不是"缺参数"）；stat 声明的 0 必须被当成相等而不是假值。

    旧实现用 `info.get("size") or -1` 会把 0 变成 -1，于是空文件永远失败，
    且报错文案自相矛盾。
    """
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        ref = await client.fs_write("workspace/gen/empty.bin", b"")

    assert ref["size"] == 0
    writes = [body for _verb, path, body in ws.requests if path == "/fs/write"]
    assert len(writes) == 1
    assert writes[0]["data_base64"] == ""
    assert ws.sizes[writes[0]["path"]] == 0


# ---------------------------------------------------------------- fs_read：上传区边界
@pytest.mark.parametrize(
    "path", ["workspace/user/files/cat.png", "sandbox://workspace/user/files/cat.png"]
)
async def test_fs_read_rejects_user_upload_area_without_send(path):
    """`workspace/user/` 是**输入**区：生成物取件不得从这里读，否则就是回显输入。"""
    ws = FakeFsWS()
    async with connected_client(ws) as client:
        with pytest.raises(ProtocolError, match="用户上传区"):
            await client.fs_read(path)
    assert ws.requests == []


async def test_fs_read_accepts_sandbox_workspace_product_path():
    """对照：真正落在 workspace/ 生成区的成品可以被分页读回。"""
    ws = FakeFsWS(read_files={"workspace/gen/out.png": PNG})
    async with connected_client(ws) as client:
        blob = await client.fs_read(PRODUCT_PATH)

    assert blob == PNG
    assert [path for _verb, path, _body in ws.requests] == ["/fs/read"]


# ---------------------------------------------------------------- 参考素材前置校验
@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("", "参考素材不能为空"),
        ("data:image/png;base64,", "空文件"),
    ],
)
def test_reference_validation_rejects_empty_payload(value, needle):
    """空参考素材必须在 staging 之前拒绝；"空"不等于"远端路径"，不该被当引用发出去。"""
    with pytest.raises(refs.ReferenceError, match=needle):
        refs.normalize_reference(value)


def test_reference_validation_rejects_oversize_single_file(monkeypatch):
    """单件上限是 refs 的契约（浏览器驱动 staging 前会过这道校验）。"""
    monkeypatch.setattr(refs, "MAX_REFERENCE_BYTES", len(PNG) - 1)
    with pytest.raises(refs.ReferenceError, match="超过"):
        refs.normalize_reference(data_url(PNG))


def test_reference_validation_rejects_oversize_total(monkeypatch):
    """总量上限独立于单件：每一件都合格、加起来超了也必须拒绝。"""
    monkeypatch.setattr(refs, "MAX_REFERENCE_TOTAL_BYTES", len(PNG) * 2 - 1)
    with pytest.raises(refs.ReferenceError, match="总大小超过"):
        refs.normalize_references([data_url(PNG), data_url(PNG)])


def test_reference_validation_caps_file_count(monkeypatch):
    monkeypatch.setattr(refs, "MAX_REFERENCE_FILES", 1)
    with pytest.raises(refs.ReferenceError, match="最多 1 个"):
        refs.normalize_references([data_url(PNG), data_url(PNG)])


def test_reference_validation_rejects_unlisted_type():
    """类型不在白名单时拒绝：送进输入框不报错、只会白等（见 refs 模块头）。"""
    with pytest.raises(refs.ReferenceError, match="类型不受支持"):
        refs.normalize_reference(data_url(b"hello", "text/plain"))


# ---------------------------------------------------------------- 引擎：staging → 生成
class FakeEngineClient:
    """protocol_engine 鸭子契约的最小实现，记录调用顺序（staging 必须先于生成）。"""

    capabilities = ["chat_cancel"]

    def __init__(
        self,
        *,
        events: list | None = None,
        files: dict[str, bytes] | None = None,
        write_error: BaseException | None = None,
    ) -> None:
        self.events = list(events or [])
        self.files = dict(files or {})
        self.write_error = write_error
        self.order: list[str] = []
        self.write_calls: list[tuple[bytes, str, str]] = []
        self.read_calls: list[dict] = []
        self.read_file_calls: list[str] = []

    def new_task_node(self) -> str:
        return "node-1"

    async def read_stream(self, prompt=None, references=None, *, node_id=None, timeout=None):
        self.order.append("read_stream")
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
        self.order.append("fs_write")
        self.write_calls.append((bytes(data), mime, filename))
        if self.write_error is not None:
            raise self.write_error
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


class FakeClock:
    """同时冻结 monotonic 与 wall clock：晚到宽限期的判定不能靠真实时间。"""

    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = 1_900_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def make_engine(client, clock: FakeClock | None = None, *, grace: float = 12.0):
    clock = clock or FakeClock()
    return ProtocolEngine(
        client,
        monotonic=clock.monotonic,
        wall_clock=clock.time,
        grace_seconds=grace,
    ), clock


def image_event(seq: int, path: str = "workspace/gen/out.png", **fields) -> dict:
    resource = {"path": path, "mime": "image/png", **fields}
    return {"event": "delta.presentation", "seq": seq, "payload": {"resources": [resource]}}


def done_event(seq: int) -> dict:
    return {"event": "delta.message_done", "seq": seq, "payload": {"status": "completed"}}


async def test_stage_references_uploads_data_url_before_generation_and_passes_ref():
    """data URL 参考素材：先 fs_write 成 file_ref，再把它塞进 read_stream 的 references。

    两个硬约束（2026-11-11 真机反证）：
    * `items[].path` 必须是 **workspace 相对路径**（`workspace/user/files/...`），
      不能带 `sandbox://` 前缀 —— 带前缀时服务端 500 "failed to resolve file_ref
      path sandbox:/workspace/…"；hatch-web 07hpppij0ssd9.js@67023 序列化的就是
      uploadPath（无 URI 前缀）。
    * `items[].type` 是 `file_ref`，字段名 `mime_type`/`filename`（同上锚点）。
    `engine.upload_refs` 仍记 URI 形态（它服务于 fs 清理，不是请求体）。
    """
    client = FakeEngineClient(
        events=[image_event(1), done_event(2)],
        files={PRODUCT_PATH: PNG},
    )
    engine, _clock = make_engine(client)

    result = await engine.generate_image("按参考图重绘", references=[data_url(PNG)])

    assert client.order == ["fs_write", "read_stream"]
    assert len(client.write_calls) == 1
    written, mime, name = client.write_calls[0]
    assert written == PNG and mime == "image/png"
    assert REFERENCE_NAME_RE.fullmatch(name), name
    staged = client.read_calls[0]["references"]
    assert len(staged) == 1
    assert staged[0]["type"] == "file_ref"
    assert staged[0]["path"] == f"workspace/user/files/{name}"
    assert staged[0]["mime_type"] == "image/png"
    assert staged[0]["filename"] == name
    assert "size" not in staged[0]
    assert engine.upload_refs == {f"sandbox://workspace/user/files/{name}"}
    assert result.data == PNG


async def test_unknown_reference_payload_rejected_before_upload_or_generation():
    """裸 base64 但不是已知媒体类型：staging 之前就拒绝，不发上传也不发起生成。"""
    client = FakeEngineClient()
    engine, _clock = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="无法识别为已知媒体类型"):
        await engine.generate_image("x", references=[base64.b64encode(b"hello").decode()])

    assert client.write_calls == []
    assert client.read_calls == []


async def test_stage_failure_does_not_start_generation():
    """上传失败必须整条中止：继续发起生成会让模型看不到附件、白等一次额度。"""
    client = FakeEngineClient(write_error=ProtocolError("fs.write 上传失败"))
    engine, _clock = make_engine(client)

    with pytest.raises(ProtocolError, match="上传失败"):
        await engine.generate_image("x", references=[data_url(PNG)])

    assert client.order == ["fs_write"]
    assert client.read_calls == []
    assert engine.upload_refs == set()


async def test_uploaded_reference_path_never_accepted_as_product():
    """事件里即使回显了上传路径，也不能当成成品——否则生成退化成回声输入图。"""
    clock = FakeClock()
    client = FakeEngineClient(
        events=[
            image_event(1, path="workspace/user/files/reference_0_deadbeef.png"),
            done_event(2),
            lambda: clock.advance(13),
        ]
    )
    engine, _clock = make_engine(client, clock)

    with pytest.raises(ProtocolGenerationError, match="未收到 image 成品"):
        await engine.generate_image("x", references=[data_url(PNG)])

    assert client.read_file_calls == []


async def test_generate_video_rejects_image_only_presentation_after_grace():
    """期望视频却只拿到封面静帧：宽限期后报"模态不符"，绝不把 png 当视频交付。"""
    clock = FakeClock()
    client = FakeEngineClient(
        events=[
            image_event(1, path="workspace/gen/cover.png"),
            done_event(2),
            lambda: clock.advance(13),
        ]
    )
    engine, _clock = make_engine(client, clock)

    with pytest.raises(ProtocolMediaTypeError, match="只拿到 image，期望 video") as caught:
        await engine.generate_video("一段海浪")

    assert "封面静帧不得冒充成品" in str(caught.value)


# ---------------------------------------------------------------- 驱动：expect=video
class FakeDriverClient:
    """ProtocolDriver 只用的那几件：connect / close + 只读属性。"""

    ws_url = "wss://hatch.metaaivm.com/v1/noise?vm_id=vm-1"
    vm_state = "RUNNING"
    wake_ok = True

    def __init__(self) -> None:
        self.connect_calls = 0
        self.close_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class FakeDriverEngine:
    """只实现驱动生成路径会调的两个方法，记录 (expect, prompt, refs, timeout)。"""

    def __init__(
        self,
        *,
        result: GenerationResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or GenerationResult(
            data=b"fake-image-bytes", mime="image/png", kind="image"
        )
        self.error = error
        self.generation_calls: list[tuple[str, str, list[str], float]] = []

    def _outcome(self) -> GenerationResult:
        if self.error is not None:
            raise self.error
        return self.result

    async def generate_image(self, prompt, *, references=None, timeout=600.0, on_progress=None):
        self.generation_calls.append(("image", prompt, list(references or []), timeout))
        return self._outcome()

    async def generate_video(self, prompt, *, references=None, timeout=600.0, on_progress=None):
        self.generation_calls.append(("video", prompt, list(references or []), timeout))
        return self._outcome()


def make_driver(engine: FakeDriverEngine) -> tuple[ProtocolDriver, FakeDriverClient]:
    driver = ProtocolDriver("测试号", {"hatch_sess": "cookie-value"})
    client = FakeDriverClient()
    driver._client = client
    driver._engine = engine
    return driver, client


def _media_files(directory) -> list[str]:
    return [str(item) for item in directory.iterdir()]


async def test_expect_video_dispatches_to_generate_video_and_writes_mp4(tmp_path, monkeypatch):
    """expect=video 必须打到 generate_video（模态走错会交付静帧），并按 mime 落盘。"""
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    payload = b"fake-mp4-bytes"
    engine = FakeDriverEngine(
        result=GenerationResult(
            data=payload,
            mime="video/mp4",
            kind="video",
            width=720,
            height=1280,
            duration_ms=5_000,
        )
    )
    driver, client = make_driver(engine)

    result = await driver.generate(
        "一段海浪", expect="video", timeout=7.0, reference_paths=["data:image/png;base64,"]
    )
    await driver.close()

    assert engine.generation_calls == [
        ("video", "一段海浪", ["data:image/png;base64,"], 7.0)
    ]
    assert client.connect_calls == 1
    assert result["via"] == "protocol"
    assert result["kind"] == "video"
    assert result["mime"] == "video/mp4"
    assert result["ext"] == ".mp4"
    assert result["size"] == len(payload)
    assert result["w"] == 720 and result["h"] == 1280
    assert result["filename"].endswith(".mp4")
    assert result["path"].endswith(".mp4")
    with open(result["path"], "rb") as handle:
        assert handle.read() == payload
    assert len(_media_files(tmp_path)) == 1


@pytest.mark.parametrize(
    ("mime", "expected_ext"),
    [
        ("video/mp4", ".mp4"),
        ("video/webm", ".webm"),
        ("video/quicktime", ".mov"),
        ("video/3gpp", ".3gp"),
        ("video/x-matroska", ".mp4"),  # 未登记的视频类型退到 mp4，不写无扩展名文件
    ],
)
async def test_video_result_extension_follows_mime(tmp_path, monkeypatch, mime, expected_ext):
    """落盘扩展名由 mime 的登记表决定：写错会让播放器/媒体库判成未知类型。"""
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    engine = FakeDriverEngine(
        result=GenerationResult(data=b"video-bytes", mime=mime, kind="video")
    )
    driver, _client = make_driver(engine)
    try:
        result = await driver.generate("动起来", expect="video", timeout=5.0)
    finally:
        await driver.close()

    assert result["ext"] == expected_ext
    assert result["filename"].endswith(expected_ext)
    assert result["path"].endswith(expected_ext)
    assert _media_files(tmp_path) == [result["path"]]


async def test_expected_video_got_image_raises_media_type_error_and_writes_nothing(
    tmp_path, monkeypatch
):
    """模态不符必须原样冒泡（pool 据此决定换号重试），且不得留下任何"半成品"文件。"""
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    engine = FakeDriverEngine(
        error=ProtocolMediaTypeError("生成完成但只拿到 image，期望 video（封面静帧不得冒充成品）")
    )
    driver, _client = make_driver(engine)

    with pytest.raises(ProtocolMediaTypeError, match="期望 video"):
        await driver.generate("一段海浪", expect="video", timeout=5.0)
    await driver.close()

    assert _media_files(tmp_path) == [], "失败前不得落盘"
