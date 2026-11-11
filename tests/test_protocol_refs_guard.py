"""协议路径的参考素材前置校验 + upload_refs 有界：离线钉（task-8）。

为什么单开一个文件：

* 这里钉的是**两条驱动之间的判定一致性**。浏览器路径在 `driver.py:660` 用
  `refs.normalize_references` 拦住数量 / 单件 40 MiB / 总量 100 MiB / 类型白名单 /
  视频 120 秒，协议路径此前只调 `decode_reference`，于是同一批素材在协议路径上
  全部放行：11 张图 → 11 次 fs_write → 上传成功但 muse 静默不认（页面表现为
  “等了十分钟什么也没有”）。
* 空载荷 Data URL（`data:image/png;base64,`）此前落在“远端路径”分支里被当成
  `path_ref` 发了出去（`if not data:` 同时承担两种语义），等于把一个空文件伪装成
  引用。这里钉住“空 → 拒绝”“真远端路径 → 保留”必须分开。
* `upload_refs` 服务于“回显的上传件不得当成品”的过滤，但此前只增不清；engine
  被账号级驱动长期持有，同账号多次生成会无界增长。这里钉有界策略（FIFO 淘汰）。

所有断言都在**任何 fs_write 之前**发生（fake client 的 `write_calls` 必须为空）：
参考素材超限时上传成功的副作用只会白烧额度/带宽，提前拒绝才能把失败留在本地。

2 条已知红测试（tests/test_noise.py、tests/test_proto.py）与本文件无关，绝不修改。
"""

from __future__ import annotations

import base64

import pytest

from muse_bridge import protocol_engine as pe
from muse_bridge import refs
from muse_bridge.protocol_engine import (
    UPLOAD_REFS_MAX,
    ProtocolEngine,
    ProtocolGenerationError,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PRODUCT_PATH = "sandbox://workspace/gen/out.png"


def data_url(data: bytes = PNG, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


# ---------------------------------------------------------------- 假件
class FakeEngineClient:
    """protocol_engine 鸭子契约的最小实现，记录 staging/生成的调用顺序。

    与 tests/test_protocol_staging.py 的 FakeEngineClient 同形：`write_calls`
    为空即“一次 fs_write 都没发出去”，这是“先校验后上传”的直接证据。
    """

    capabilities = ["chat_cancel"]

    def __init__(self, *, events: list | None = None, files: dict[str, bytes] | None = None) -> None:
        self.events = list(events or [])
        self.files = dict(files or {})
        self.order: list[str] = []
        self.write_calls: list[tuple[bytes, str, str]] = []
        self.read_calls: list[dict] = []
        self.read_file_calls: list[str] = []

    def new_task_node(self) -> str:
        return "node-1"

    async def read_stream(self, prompt=None, references=None, *, node_id=None, timeout=None):
        self.order.append("read_stream")
        self.read_calls.append(
            {"prompt": prompt, "references": references, "node_id": node_id, "timeout": timeout}
        )
        for item in self.events:
            yield item

    async def fs_write(self, data, mime, filename):
        self.order.append("fs_write")
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


def make_engine(client) -> ProtocolEngine:
    return ProtocolEngine(client)


def image_event(seq: int, path: str = PRODUCT_PATH, **fields) -> dict:
    resource = {"path": path, "mime": "image/png", **fields}
    return {"event": "delta.presentation", "seq": seq, "payload": {"resources": [resource]}}


def done_event(seq: int) -> dict:
    return {"event": "delta.message_done", "seq": seq, "payload": {"status": "completed"}}


# ---------------------------------------------------------------- 数量 / 大小 / 类型
#
# 默认上限来自 refs（MUSE_MAX_REFERENCE_FILES=10 等，见 refs.py:66-71）。
# 这些用例 monkeypatch 的是 **refs 的模块常量**，证明 protocol_engine 读的是
# refs 的判定而不是自带一套数字。


async def test_eleven_data_urls_rejected_before_any_fs_write(monkeypatch):
    """11 个 data URL（默认上限 10）：整批拒绝，fake fs_write 调用数为 0。"""
    monkeypatch.setattr(refs, "MAX_REFERENCE_FILES", 10)
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="最多 10 个") as caught:
        await engine._stage_references([data_url() for _ in range(11)])

    assert isinstance(caught.value, ProtocolGenerationError)
    assert client.write_calls == []
    assert client.read_calls == []
    assert engine.upload_refs == set()


async def test_oversize_single_reference_rejected_before_any_fs_write(monkeypatch):
    """单件超限：拒绝，且不落一次 fs_write（上传成功也只会白占带宽）。"""
    monkeypatch.setattr(refs, "MAX_REFERENCE_BYTES", len(PNG) - 1)
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="参考素材校验未通过") as caught:
        await engine._stage_references([data_url()])

    assert "超过" in str(caught.value), str(caught.value)
    assert client.write_calls == []


async def test_oversize_total_rejected_before_any_fs_write(monkeypatch):
    """总量超限独立于单件：每件都合格、加起来超了也必须拒绝。"""
    monkeypatch.setattr(refs, "MAX_REFERENCE_TOTAL_BYTES", len(PNG) * 2 - 1)
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="总大小超过"):
        await engine._stage_references([data_url(), data_url()])

    assert client.write_calls == []


async def test_unlisted_reference_type_rejected_before_any_fs_write():
    """类型白名单：送进 muse 输入框不会报错、只会白等，必须在这里拒绝。"""
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="类型不受支持"):
        await engine._stage_references([data_url(b"hello", "text/plain")])

    assert client.write_calls == []


async def test_invalid_later_reference_prevents_earlier_valid_upload():
    """校验是整批快照式的：第 1 条合法也不能先 fs_write，第 2 条失败整批中止。"""
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="空文件"):
        await engine._stage_references([data_url(), "data:image/png;base64,"])

    assert client.write_calls == [], "校验必须在任何 fs_write 之前完成"


# ---------------------------------------------------------------- 空素材 vs 远端路径


async def test_empty_data_url_is_rejected_instead_of_becoming_path_ref():
    """空载荷 Data URL 必须拒绝，不能因为 decode_reference 返回 b"" 就当远端路径发出。"""
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="空文件"):
        await engine._stage_references(["data:image/png;base64,"])

    assert client.write_calls == [], "空素材不得触发 fs_write"
    assert engine.upload_refs == set()


async def test_empty_reference_string_is_rejected():
    client = FakeEngineClient()
    engine = make_engine(client)

    with pytest.raises(ProtocolGenerationError, match="为空"):
        await engine._stage_references(["   "])

    assert client.write_calls == []


async def test_remote_path_references_pass_through_without_upload_or_fetch(monkeypatch):
    """http(s)/workspace/sandbox 是“已知路径”：不下载、不改写、不占上传额度。"""
    calls: list[str] = []

    def fake_fetch(url, **kwargs):
        calls.append(url)
        raise AssertionError(f"协议路径不应下载远端素材：{url}")

    monkeypatch.setattr(refs, "fetch_public", fake_fetch)
    client = FakeEngineClient()
    engine = make_engine(client)
    references = [
        "https://cdn.example/ref.png",
        "workspace/user/ref.png",
        "sandbox://workspace/user/ref.png",
    ]

    staged = await engine._stage_references(references)

    assert [item["type"] for item in staged] == ["path_ref", "path_ref", "path_ref"]
    assert [item["path"] for item in staged] == references, "路径不得被改写"
    assert all(item["mime_type"] == "application/octet-stream" for item in staged)
    assert calls == []
    assert client.write_calls == []
    assert engine.upload_refs == set()


async def test_mixed_path_and_inline_references_keep_caller_order():
    """混合批次的 items 顺序必须与调用方一致：错位会让 [图1]/[图2] 指到别的附件。"""
    client = FakeEngineClient()
    engine = make_engine(client)

    staged = await engine._stage_references(["workspace/user/ref.png", data_url()])

    assert [item["type"] for item in staged] == ["path_ref", "file_ref"]
    assert staged[0]["path"] == "workspace/user/ref.png"
    name = client.write_calls[0][2]
    assert staged[1]["path"] == f"workspace/user/files/{name}"


# ---------------------------------------------------------------- 合法路径：顺序不变


async def test_legal_reference_keeps_staging_order_and_bare_workspace_path():
    """合法参考素材：先 fs_write 再 read_stream；items[].path 是不带前缀的裸路径。

    真机 2026-11-11 反证：带 `sandbox://` 前缀时服务端 500
    "failed to resolve file_ref path sandbox:/workspace/…"，而 hatch-web
    07hpppij0ssd9.js@67023 序列化的就是 `uploadPath`（无 URI 前缀）。
    `upload_refs` 仍记 URI 形态（它服务于 fs 清理/回显过滤，不是请求体）。
    """
    client = FakeEngineClient(
        events=[image_event(1), done_event(2)],
        files={PRODUCT_PATH: PNG},
    )
    engine = make_engine(client)

    result = await engine.generate_image("按参考图重绘", references=[data_url()])

    assert client.order == ["fs_write", "read_stream"]
    written, mime, name = client.write_calls[0]
    assert written == PNG and mime == "image/png"
    assert name.endswith(".png"), name
    staged = client.read_calls[0]["references"]
    assert len(staged) == 1
    assert staged[0]["type"] == "file_ref"
    assert staged[0]["path"] == f"workspace/user/files/{name}"
    assert not staged[0]["path"].startswith("sandbox://")
    assert staged[0]["mime_type"] == "image/png"
    assert staged[0]["filename"] == name
    assert engine.upload_refs == {f"sandbox://workspace/user/files/{name}"}
    assert result.data == PNG


# ---------------------------------------------------------------- upload_refs 有界


def test_upload_refs_limit_is_derived_from_reference_file_limit():
    """上限必须由 refs 的数量上限推出（8 轮满额），不另造一个孤立的魔法数。"""
    assert UPLOAD_REFS_MAX == refs.MAX_REFERENCE_FILES * 8


async def test_upload_refs_stays_bounded_across_generations(monkeypatch):
    """同 engine 多次生成：upload_refs 按 FIFO 淘汰，不会无界增长。

    为什么关心：engine 挂在账号级驱动上长期存活，一次生成最多留
    `refs.MAX_REFERENCE_FILES` 条上传路径；只增不清会随进程寿命无限涨。
    同时**最新一轮**的上传路径必须还在集合里——它负责把回显的上传件排除在
    成品候选之外（旧集合丢得太多，生成会退化成“把输入图当输出”）。
    """
    cap = 3
    monkeypatch.setattr(pe, "UPLOAD_REFS_MAX", cap)
    client = FakeEngineClient(
        events=[image_event(1), done_event(2)],
        files={PRODUCT_PATH: PNG},
    )
    engine = make_engine(client)

    names: list[str] = []
    for _ in range(cap + 2):
        result = await engine.generate_image("生成", references=[data_url()])
        assert result.data == PNG
        names.append(client.write_calls[-1][2])

    staged_uris = [f"sandbox://workspace/user/files/{name}" for name in names]
    assert len(engine.upload_refs) == cap, "集合大小必须停在上限"
    assert staged_uris[-1] in engine.upload_refs, "最新一轮必须保留（回显过滤靠它）"
    assert staged_uris[0] not in engine.upload_refs, "最旧的必须被淘汰（否则就是无界）"
    assert engine.upload_refs == set(staged_uris[-cap:])
