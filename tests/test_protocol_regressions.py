"""被并行测试代理挖出来的协议层缺陷的回归钉（4 个 P0/P1）。

这些不是风格问题，每一条都让"纯协议路径"在真实调用下直接崩：
`retryable()` 对任何异常 TypeError（换号决策点没了）、`refs.ext_from_mime`
不存在（参考图上传前就炸）、0 字节上传恒失败、未 connect 发请求抛
AttributeError 而不是协议错误。修完必须钉住，否则下一次重构会悄悄回来。
"""

from __future__ import annotations

import base64

import pytest

from muse_bridge.protocol_client import HatchProtocolClient, ProtocolError
from muse_bridge.protocol_engine import (
    ProtocolEngine,
    ProtocolGenerationError,
    ProtocolModelRefusal,
    ProtocolQuotaExhausted,
    retryable,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 16


# ---- retryable：换号决策 ------------------------------------------------


def test_retryable_returns_true_for_generic_errors():
    """通用生成失败可以换号；frozenset 传给 isinstance 会让这里 TypeError。"""
    assert retryable(ProtocolGenerationError("上游超时")) is True


@pytest.mark.parametrize(
    "exc",
    [
        ProtocolModelRefusal("模型拒绝生成"),
        ProtocolQuotaExhausted("额度不足"),
    ],
)
def test_retryable_is_false_for_terminal_errors(exc):
    """拒绝与额度不足绝不换号：换号只会把其余账号一起烧掉。"""
    assert retryable(exc) is False


# ---- 参考素材上传：data URL --------------------------------------------


class _UploadClient:
    """只提供引擎需要的 fs_write，返回 file_ref。"""

    def __init__(self) -> None:
        self.written: list[tuple[bytes, str, str]] = []

    async def fs_write(self, data, mime, filename):
        self.written.append((bytes(data), mime, filename))
        return {
            "type": "file_ref",
            "path": f"sandbox://workspace/user/files/{filename}",
            "mime_type": mime,
        }


@pytest.mark.asyncio
async def test_stage_references_accepts_data_url():
    """`refs.ext_from_mime` 不存在时，任何 data URL 参考素材都 AttributeError。"""
    client = _UploadClient()
    engine = ProtocolEngine(client)
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    staged = await engine._stage_references([url])

    assert staged[0]["type"] == "file_ref"
    assert staged[0]["mime_type"] == "image/png"
    data, mime, name = client.written[0]
    assert data == PNG and mime == "image/png"
    assert name.endswith(".png"), name
    # `items[].path` 必须是裸 workspace 路径（真机 2026-11-11 反证：带 sandbox://
    # 前缀会被服务端按 URI 解析成 `sandbox:/…` 单斜杠后 500）；`upload_refs` 保留
    # URI 形态是给 fs 清理用的，不是请求体，所以两者形态不同是**设计**而非回归。
    assert staged[0]["path"] == f"workspace/user/files/{name}"
    assert f"sandbox://{staged[0]['path']}" in engine.upload_refs


@pytest.mark.asyncio
async def test_stage_references_passes_remote_path_through():
    """远端引用（workspace/sandbox/http）不在这里下载，交给客户端解析。"""
    engine = ProtocolEngine(_UploadClient())
    staged = await engine._stage_references(["https://example.com/a.png"])
    assert len(staged) == 1
    assert staged[0]["type"] == "path_ref"
    assert staged[0]["path"] == "https://example.com/a.png"


# ---- 0 字节上传 --------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_write_zero_byte_file_succeeds():
    """`size or -1` 会把 0 变成 -1：空文件恒失败且报错自相矛盾。"""
    client = HatchProtocolClient({"hatch_sess": "x"})

    async def fake_request_json(verb, path, body=None, timeout=None):
        if path == "/fs/stat":
            return {"result": {"kind": "file", "size": 0}}
        return {}

    client._request_json = fake_request_json  # type: ignore[method-assign]
    ref = await client.fs_write(b"", "image/png", "empty.png")
    assert ref["type"] == "file_ref"
    assert ref["path"].startswith("sandbox://workspace/user/files/")


@pytest.mark.asyncio
async def test_fs_write_size_mismatch_still_fails():
    """放宽 0 不能顺手把校验放没：真的差一字节仍然要炸。"""
    client = HatchProtocolClient({"hatch_sess": "x"})

    async def fake_request_json(verb, path, body=None, timeout=None):
        if path == "/fs/stat":
            return {"result": {"kind": "file", "size": 5}}
        return {}

    calls: list[str] = []

    async def fake_delete(path):
        calls.append(path)

    client._request_json = fake_request_json  # type: ignore[method-assign]
    client.fs_delete = fake_delete  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="上传大小不符"):
        await client.fs_write(b"1234", "image/png", "a.png")
    assert calls, "失败时必须清理临时文件"


# ---- 未连接时发请求 ----------------------------------------------------


@pytest.mark.asyncio
async def test_request_before_connect_is_protocol_error():
    """未 connect 时 `send_cipher` 是 None：要给协议错误，不是 AttributeError。"""
    client = HatchProtocolClient({"hatch_sess": "x"})
    with pytest.raises(ProtocolError, match="尚未连接"):
        await client.request("POST", "/api/ping", {"presence": "active"}, timeout=1.0)
