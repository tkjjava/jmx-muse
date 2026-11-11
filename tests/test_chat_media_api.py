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
