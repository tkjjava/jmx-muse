"""Offline chat attachment retrieval, persistence, and collection contracts."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from muse_bridge import media, pool, protocol_driver
from muse_bridge.protocol_driver import ProtocolDriver, ProtocolRefusal
from muse_bridge.protocol_engine import ProtocolEngine, ProtocolGenerationError

PNG = b"\x89PNG\r\n\x1a\nattachment"
WEBP = b"RIFF\x00\x00\x00\x00WEBPattachment"
PATH = "sandbox://workspace/imagine_media/reply.png"


def attachment(path=PATH, **metadata):
    return {"type": "media", "ref": {"path": path, "mime": "image/png", **metadata}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    instance = media.MediaStore(tmp_path)
    monkeypatch.setattr(protocol_driver.media, "MediaStore", lambda: instance)
    return instance


def make_driver(events, *, data=PNG, error=None, delay=0):
    class Client:
        def __init__(self):
            self.reads = []
            self.cancelled = False

        async def fs_read(self, path):
            self.reads.append(path)
            try:
                if delay:
                    await asyncio.sleep(delay)
                if error:
                    raise error
                return data
            finally:
                self.cancelled = True

    client = Client()
    engine = ProtocolEngine(client)

    async def stream_chat(prompt, *, timeout):
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event

    engine.stream_chat = stream_chat
    driver = ProtocolDriver("offline", {})
    driver._connected = True
    driver._client = client
    driver._engine = engine
    return driver, client, events


@pytest.mark.asyncio
async def test_stream_persists_actual_bytes_and_local_metadata(store):
    driver, client, _ = make_driver([
        {"type": "delta", "text": "Here"},
        attachment(width=32, height=16, seed="s", duration_ms=123),
        {"type": "done"},
    ])
    events = [event async for event in driver.stream_reply_events("hi")]
    assert events[0] == {"content": "Here"}
    metadata = events[1]["media"][0]
    assert metadata["url"] == f"/v1/media/{metadata['token']}"
    assert metadata["mime"] == "image/png"
    assert metadata["kind"] == "image"
    assert metadata["size"] == len(PNG)
    assert metadata["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert metadata["width"] == 32 and metadata["height"] == 16
    assert metadata["duration_ms"] == 123 and metadata["seed"] == "s"
    assert metadata["via"] == "protocol"
    assert "path" not in metadata
    assert store.read(metadata["token"]) == PNG
    assert client.reads == [PATH]
    assert driver.chat_media == [metadata]


@pytest.mark.asyncio
async def test_deduplicates_normalized_paths_and_resets_each_turn(store):
    driver, client, events = make_driver([
        attachment(), attachment("workspace/imagine_media/reply.png"),
        {"type": "done"},
    ])
    first = [event async for event in driver.stream_reply_events("hi")]
    assert len(first) == len(driver.chat_media) == len(client.reads) == 1
    first_token = driver.chat_media[0]["token"]
    await driver.collect_reply("again")
    assert len(client.reads) == 2
    assert len(driver.chat_media) == 1
    assert driver.chat_media[0]["token"] != first_token
    events[:] = [{"type": "delta", "text": "text"}, {"type": "done"}]
    assert await driver.collect_reply("text") == "text"
    assert driver.chat_media == []


@pytest.mark.asyncio
async def test_collect_reply_uses_stream_and_caches_media(store):
    driver, _, _ = make_driver([
        {"type": "delta", "text": "a"}, attachment(),
        {"type": "delta", "text": "b"}, {"type": "done"},
    ])
    async def forbidden(*args, **kwargs):
        pytest.fail("collect_chat must not bypass attachment storage")
    driver._engine.collect_chat = forbidden
    assert await driver.collect_reply("hi") == "ab"
    assert store.read(driver.chat_media[0]["token"]) == PNG


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning", [False, True])
async def test_pool_collection_preserves_driver_media_cache(store, monkeypatch, reasoning):
    driver, _, _ = make_driver([
        attachment(), {"type": "delta", "text": "answer"}, {"type": "done"},
    ])
    monkeypatch.setattr(pool.driver_mod, "REASONING", reasoning)
    result = pool.DispatchResult(ok=True, account="offline", driver=driver)
    assert await pool.collect_reply_events(result, "hi") == ("", "answer")
    assert store.read(driver.chat_media[0]["token"]) == PNG


@pytest.mark.asyncio
async def test_signature_overrides_declared_mime(store):
    driver, _, _ = make_driver([attachment(), {"type": "done"}], data=WEBP)
    await driver.collect_reply("hi")
    assert driver.chat_media[0]["mime"] == "image/webp"
    assert driver.chat_media[0]["filename"].endswith(".webp")


@pytest.mark.asyncio
async def test_video_signature_delivers_video_metadata(store):
    payload = b"\x00\x00\x00\x18ftypisom" + b"video"
    driver, _, _ = make_driver([
        attachment(kind="video", mime="video/mp4", duration_ms=5000),
        {"type": "done"},
    ], data=payload)
    await driver.collect_reply("hi")
    metadata = driver.chat_media[0]
    assert metadata["mime"] == "video/mp4"
    assert metadata["kind"] == "video"
    assert metadata["filename"].endswith(".mp4")
    assert metadata["duration_ms"] == 5000
    assert store.read(metadata["token"]) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"<html>error</html>", "not bytes"])
async def test_failed_or_unrecognized_retrieval_is_explicit(store, payload):
    driver, _, _ = make_driver([attachment(), {"type": "done"}], data=payload)
    with pytest.raises(ProtocolGenerationError):
        await driver.collect_reply("hi")
    assert driver.chat_media == []
    assert store.list_media() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["https://example.com/image.png", "http://127.0.0.1/x", "file:///tmp/x", "sandbox://workspace/user/x.png", "sandbox://https://example.com/x.png", "sandbox://workspace/../x.png", "sandbox://workspace/x.png?url=http://example.com", "sandbox://workspace/dir\\\\x.png"])
async def test_remote_and_upload_paths_never_reach_fetcher(store, path):
    driver, client, _ = make_driver([attachment(path), {"type": "done"}])
    with pytest.raises(ProtocolGenerationError, match="sandbox"):
        await driver.collect_reply("hi")
    assert client.reads == []
    assert store.list_media() == []


@pytest.mark.asyncio
async def test_fetch_errors_propagate_without_cached_delivery(store):
    error = ProtocolGenerationError("fs read failed")
    driver, _, _ = make_driver([attachment(), {"type": "done"}], error=error)
    with pytest.raises(ProtocolGenerationError) as exc:
        await driver.collect_reply("hi")
    assert exc.value is error
    assert driver.chat_media == []


@pytest.mark.asyncio
async def test_retrieval_is_cancelled_at_remaining_turn_deadline(store):
    driver, client, _ = make_driver([attachment(), {"type": "done"}], delay=10)
    with pytest.raises(ProtocolGenerationError, match="超时"):
        await driver.collect_reply("hi", timeout=0.02)
    assert client.cancelled
    assert driver.chat_media == []
    assert store.list_media() == []


@pytest.mark.asyncio
async def test_elapsed_stream_time_is_deducted_before_retrieval(store):
    driver, client, _ = make_driver([])
    async def stream_chat(prompt, *, timeout):
        await asyncio.sleep(0.02)
        yield attachment()
    driver._engine.stream_chat = stream_chat
    with pytest.raises(ProtocolGenerationError, match="超时"):
        await driver.collect_reply("hi", timeout=0.001)
    assert client.reads == []


@pytest.mark.asyncio
async def test_legacy_download_bytes_engine_contract(store):
    calls = []
    async def download_bytes(resource):
        calls.append(resource)
        return PNG
    driver, _, _ = make_driver([attachment(), {"type": "done"}])
    driver._engine.client = SimpleNamespace(download_bytes=download_bytes)
    await driver.collect_reply("hi")
    assert calls == [{"path": PATH, "mime": "image/png"}]
    assert store.read(driver.chat_media[0]["token"]) == PNG


@pytest.mark.asyncio
async def test_turn_cache_resets_before_connection_failure(store):
    driver, _, _ = make_driver([attachment(), {"type": "done"}])
    await driver.collect_reply("hi")
    async def failed_connect():
        raise ProtocolGenerationError("connect failed")
    driver.connect = failed_connect
    with pytest.raises(ProtocolGenerationError, match="connect failed"):
        await driver.collect_reply("again")
    assert driver.chat_media == []


@pytest.mark.asyncio
async def test_storage_failure_is_not_delivered_or_cached(store, monkeypatch):
    driver, _, _ = make_driver([attachment(), {"type": "done"}])
    def failed_save(*args, **kwargs):
        raise media.MediaError("disk failure")
    monkeypatch.setattr(store, "save", failed_save)
    with pytest.raises(media.MediaError, match="disk failure"):
        await driver.collect_reply("hi")
    assert driver.chat_media == []
    assert store.list_media() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [None, "not a resource", {}])
async def test_malformed_media_reference_is_explicit(store, ref):
    driver, client, _ = make_driver([{"type": "media", "ref": ref}, {"type": "done"}])
    with pytest.raises(ProtocolGenerationError):
        await driver.collect_reply("hi")
    assert driver.chat_media == []
    assert client.reads == []


@pytest.mark.asyncio
async def test_collect_refusal_uses_shared_stream_semantics(store):
    driver, _, _ = make_driver([{"type": "refused", "text": "declined"}])
    with pytest.raises(ProtocolRefusal, match="declined"):
        await driver.collect_reply("hi")
    assert driver.chat_media == []


@pytest.mark.asyncio
async def test_media_only_reply_delivers_without_text(store):
    """只有附件、没有正文的回复必须正常交付。

    引擎的完成判定以 delta/text 为依据，"仅媒体"是最容易被误判成失败的一类：
    如果它被当成"没收到完整结果"，用户拿到的会是一条报错而不是那张图。
    """
    driver, _, _ = make_driver([attachment(), {"type": "done"}])
    assert await driver.collect_reply("hi") == ""
    assert len(driver.chat_media) == 1
    assert store.read(driver.chat_media[0]["token"]) == PNG


@pytest.mark.asyncio
async def test_stream_yields_text_before_late_failure(store):
    """部分成功后失败：已发出的内容不能撤回，失败必须冒泡。

    上层据此把 finish_reason 标成未完成；这里钉的是驱动不会把异常吞成正常结束。
    """
    driver, _, _ = make_driver([
        {"type": "delta", "text": "已经开始"},
        attachment(),
        RuntimeError("上游断线"),
    ])
    seen: list = []
    with pytest.raises(RuntimeError, match="上游断线"):
        async for event in driver.stream_reply_events("hi"):
            seen.append(event)
    assert seen[0] == {"content": "已经开始"}
    assert len(driver.chat_media) == 1, "取到的附件仍然算数"


@pytest.mark.asyncio
async def test_consumer_close_cancels_retrieval_and_keeps_no_partial(store):
    """客户端中途断开：生成器被 aclose，取件被取消，不留半成品元数据。"""
    driver, client, _ = make_driver(
        [attachment(), {"type": "done"}], delay=5.0
    )
    generator = driver.stream_reply_events("hi")
    task = asyncio.ensure_future(generator.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await generator.aclose()

    assert driver.chat_media == []
    assert store.list_media() == []
    assert client.cancelled is True, "取件必须真的被取消，不能留在后台跑"
