"""Chat media extensions preserve text and authenticated download URLs."""

import json

import httpx
import pytest

from muse_bridge import app as app_module
from muse_bridge import driver as browser_driver
from muse_bridge import media, pool
from muse_bridge.protocol_driver import ProtocolDriver
from muse_bridge.protocol_engine import ProtocolEngine


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_media_reaches_response(monkeypatch, stream):
    item = {"url": "/v1/media/test-token", "mime": "image/png", "kind": "image"}

    class Driver:
        chat_media = [item]

        async def close(self):
            pass

    async def dispatch(_store):
        return pool.DispatchResult(ok=True, account="test", driver=Driver())

    async def collect(*args, **kwargs):
        return "", "picture"

    async def events(*args, **kwargs):
        yield {"content": "picture"}
        yield {"media": [item]}

    monkeypatch.setattr(app_module, "store", lambda: None)
    monkeypatch.setattr(pool, "dispatch", dispatch)
    monkeypatch.setattr(pool, "collect_reply_events", collect)
    monkeypatch.setattr(pool, "stream_reply_events", events)
    monkeypatch.setenv("MUSE_PUBLIC_BASE", "https://bridge.example")
    monkeypatch.delenv("MUSE_BRIDGE_API_KEYS", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "picture"}], "stream": stream},
        )
    assert response.status_code == 200
    if stream:
        chunks = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"]]
        delivered = [entry for delta in deltas for entry in delta.get("media", [])]
        assert "".join(delta.get("content", "") for delta in deltas) == "picture"
    else:
        message = response.json()["choices"][0]["message"]
        assert message["content"] == "picture"
        delivered = message["media"]
    assert delivered == [{**item, "url": "https://bridge.example/v1/media/test-token"}]
    assert item["url"] == "/v1/media/test-token"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reasoning", [False, True])
async def test_chat_attachment_download_roundtrip(tmp_path, monkeypatch, stream, reasoning):
    """Actual driver -> pool -> chat API -> authenticated media download."""
    payload = b"\x89PNG\r\n\x1a\nroundtrip"
    media_store = media.MediaStore(tmp_path)
    monkeypatch.setattr(media, "MediaStore", lambda: media_store)
    monkeypatch.setattr(browser_driver, "REASONING", reasoning)
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "integration-key")
    monkeypatch.delenv("MUSE_PUBLIC_BASE", raising=False)
    reads = []
    closed = []

    class Client:
        async def fs_read(self, path):
            reads.append(path)
            return payload

    engine = ProtocolEngine(Client())

    async def stream_chat(prompt, *, timeout):
        yield {"type": "delta", "text": "picture"}
        for _ in range(2):
            yield {"type": "media", "ref": {"path": "sandbox://workspace/result.png"}}
        yield {"type": "done"}

    engine.stream_chat = stream_chat
    driver = ProtocolDriver("integration", {})
    driver._engine = engine
    driver._connected = True

    async def close():
        closed.append(True)

    driver.close = close

    async def dispatch(_store):
        return pool.DispatchResult(ok=True, account="integration", driver=driver)

    monkeypatch.setattr(pool, "dispatch", dispatch)
    monkeypatch.setattr(app_module, "store", lambda: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer integration-key"},
            json={"messages": [{"role": "user", "content": "picture"}], "stream": stream},
        )
        assert response.status_code == 200
        if stream:
            chunks = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            delivered = [
                item
                for chunk in chunks
                for choice in chunk["choices"]
                for item in choice["delta"].get("media", [])
            ]
        else:
            delivered = response.json()["choices"][0]["message"]["media"]
        assert len(delivered) == 1
        url = delivered[0]["url"]
        assert (await client.get(url)).status_code == 401
        downloaded = await client.get(url, headers={"Authorization": "Bearer integration-key"})
        assert downloaded.status_code == 200
        assert downloaded.content == payload
        assert delivered[0]["mime"] == "image/png"
        # 下载接口固定 octet-stream + nosniff；真实类型在媒体元数据里。
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert reads == ["sandbox://workspace/result.png"]
    assert closed == [True]


@pytest.fixture
async def media_client(tmp_path, monkeypatch):
    """指向临时成品库的客户端，并把鉴权关掉（HEAD 测试只关心下载面本身）。"""
    store = media.MediaStore(tmp_path)
    monkeypatch.setattr(app_module.media, "MediaStore", lambda: store)
    monkeypatch.delenv("MUSE_BRIDGE_API_KEYS", raising=False)
    monkeypatch.delenv("MUSE_BRIDGE_ADMIN_KEYS", raising=False)
    monkeypatch.delenv("MUSE_PUBLIC_BASE", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        yield client, store


@pytest.mark.asyncio
async def test_media_head_probe_reports_length_without_body(media_client):
    """HEAD 必须可用：播放器、CDN 回源、下载器都先用它探测长度再决定续传。

    只挂 GET 会让探测方拿到 405 然后放弃——成品明明在，客户端却播不了。
    这里钉住三件事：状态 200、`Content-Length` 与真实文件等长、**响应体为空**。
    """
    client, store = media_client
    payload = b"\x89PNG\r\n\x1a\n" + b"h" * 4096
    ref = store.save(payload, "image/png")

    response = await client.head(f"/v1/media/{ref.token}")
    assert response.status_code == 200, response.text
    assert response.headers["content-length"] == str(len(payload))
    assert response.headers.get("accept-ranges") == "bytes"
    assert response.content == b""


@pytest.mark.asyncio
async def test_media_head_with_range_reports_partial_span(media_client):
    """带 Range 的 HEAD 要回 206 并给出**区间**长度，不是总长度。

    两者不一致时，探测方按总长度准备缓冲区、按区间读数据，进度条会错乱。
    """
    client, store = media_client
    payload = b"\x00" * 1000
    ref = store.save(payload, "application/octet-stream")

    response = await client.head(
        f"/v1/media/{ref.token}", headers={"Range": "bytes=10-99"}
    )
    assert response.status_code == 206, response.text
    assert response.headers["content-range"] == "bytes 10-99/1000"
    assert response.headers["content-length"] == "90"
    assert response.content == b""


@pytest.mark.asyncio
async def test_media_head_unknown_token_is_404_not_405(media_client):
    """探测不存在的成品要回 404。回 405 会让探测方以为"这个端点不支持 HEAD"，
    从而换一种探测方式，把"文件没了"和"方法不对"混成一件事。"""
    client, _ = media_client
    response = await client.head("/v1/media/" + "0" * 32)
    assert response.status_code == 404, response.text
    assert response.content == b""

