"""Owned stream cleanup and budgets, independently of transport implementation."""
from __future__ import annotations

import asyncio

import pytest

from muse_bridge.protocol_engine import (
    ProtocolEngine,
    ProtocolGenerationError,
    ProtocolMediaTypeError,
)

NODE = "owned-node"
IMAGE = {"path": "workspace/gen/result.png", "mime": "image/png"}


def event(name, **payload):
    return {"event": name, "payload": {"chat_context": {"device_id": NODE}, **payload}}


def done(**payload):
    return event("delta.message_done", status="completed", **payload)


class Client:
    """Retain streams so tests cannot accidentally pass through GC cleanup."""
    def __init__(self, events=(), *, silent=False):
        self.events = events
        self.silent = silent
        self.retained = []
        self.closed = asyncio.Event()
        self.after_script = False

    def new_task_node(self):
        return NODE

    def read_stream(self, *args, **kwargs):
        async def reader():
            try:
                for item in self.events:
                    yield item
                self.after_script = True
                if self.silent:
                    await asyncio.Event().wait()
            finally:
                self.closed.set()
        stream = reader()
        self.retained.append(stream)
        return stream

    async def fs_read(self, path):
        return b"image-data"


@pytest.mark.asyncio
async def test_stream_chat_closes_retained_run_generator(monkeypatch):
    engine = ProtocolEngine(Client())
    closed = asyncio.Event()
    async def inner(*args, **kwargs):
        try:
            yield {"type": "delta", "text": "first"}
            await asyncio.Event().wait()
        finally:
            closed.set()
    retained = inner()
    monkeypatch.setattr(engine, "_run", lambda *a, **kw: retained)
    outer = engine.stream_chat("hello")
    assert await anext(outer) == {"type": "delta", "text": "first"}
    await outer.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_run_closes_retained_client_generator_on_early_close():
    client = Client([event("delta.text_append", text="first")], silent=True)
    stream = ProtocolEngine(client)._run("hello", expect=None, timeout=1)
    assert (await anext(stream))["text"] == "first"
    assert client.retained and not client.closed.is_set()
    await stream.aclose()
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_silent_chat_timeout_closes_reader_and_is_generation_error():
    client = Client(silent=True)
    async with asyncio.timeout(1):
        with pytest.raises(ProtocolGenerationError, match="超时"):
            await anext(ProtocolEngine(client).stream_chat("hello", timeout=0.02))
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_chat_timeout_is_not_active_while_consumer_owns_yield():
    client = Client([event("delta.text_append", text="first")], silent=True)
    stream = ProtocolEngine(client).stream_chat("hello", timeout=0.02)
    assert (await anext(stream))["text"] == "first"
    # A timeout around the whole async-generator body would cancel this consumer.
    await asyncio.sleep(0.04)
    with pytest.raises(ProtocolGenerationError, match="超时"):
        await anext(stream)
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_owned_transcript_done_returns_without_next_upstream_event():
    transcript = {"messages": [{"content": [{"type": "text", "text": "final"}]}]}
    client = Client([done(transcript=transcript)], silent=True)
    async with asyncio.timeout(1):
        events = [item async for item in ProtocolEngine(client).stream_chat("hello", timeout=10)]
    assert events == [{"type": "delta", "text": "final"}, {"type": "done", "text": "final"}]
    assert not client.after_script
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_other_turn_done_does_not_stop_owned_completion():
    other = {"event": "delta.message_done", "payload": {
        "chat_context": {"device_id": "other"}, "status": "completed"}}
    client = Client([other, event("delta.text_append", text="ours"), done()], silent=True)
    events = [item async for item in ProtocolEngine(client).stream_chat("hello", timeout=1)]
    assert events[-1] == {"type": "done", "text": "ours"}
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_generation_closes_retained_reader_before_fetch():
    client = Client([done(resources=[IMAGE])], silent=True)
    async def fetch(path):
        assert client.closed.is_set()
        return b"image-data"
    client.fs_read = fetch
    result = await ProtocolEngine(client).generate_image("make image", timeout=1)
    assert result.data == b"image-data"
    assert not client.after_script


@pytest.mark.asyncio
async def test_generation_grace_bounds_silent_post_done_stream():
    cover = {"path": "workspace/gen/cover.png", "mime": "image/png"}
    client = Client([done(resources=[cover])], silent=True)
    async with asyncio.timeout(1):
        with pytest.raises(ProtocolMediaTypeError, match="期望 video"):
            await ProtocolEngine(client, grace_seconds=0.02).generate_video("make video", timeout=0.5)
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_generation_silent_stream_uses_absolute_budget():
    client = Client(silent=True)
    async with asyncio.timeout(1):
        with pytest.raises(ProtocolGenerationError, match="超时"):
            await ProtocolEngine(client).generate_image("make image", timeout=0.02)
    assert client.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stage", "prefetch", "fetch"])
async def test_generation_budget_includes_stalled_coroutine_phase(phase, monkeypatch):
    client = Client([done(resources=[IMAGE])], silent=True)
    engine = ProtocolEngine(client)
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    async def stall(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    if phase == "stage":
        monkeypatch.setattr(engine, "_stage_references", stall)
    elif phase == "prefetch":
        monkeypatch.setattr(engine, "_prefetch_media", stall)
    else:
        monkeypatch.setattr(engine, "_fetch_bytes", stall)
    async with asyncio.timeout(1):
        with pytest.raises(ProtocolGenerationError, match="超时"):
            await engine.generate_image("make image", timeout=0.02)
    assert entered.is_set() and cleaned.is_set()
    if phase == "fetch":
        assert client.closed.is_set()
    elif phase == "stage":
        assert not client.retained
    else:
        # aclose on an unstarted generator does not enter its body/finally.
        assert client.retained[0].ag_frame is None


@pytest.mark.asyncio
async def test_custom_iterator_without_aclose_remains_compatible():
    class Iterator:
        def __init__(self):
            self.events = iter([done(resources=[IMAGE])])
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration:
                raise StopAsyncIteration from None
    class CustomClient(Client):
        def read_stream(self, *args, **kwargs):
            return Iterator()
    assert [e["type"] async for e in ProtocolEngine(CustomClient()).stream_chat("hello")] == [
        "media", "done"]
    result = await ProtocolEngine(CustomClient()).generate_image("make image", timeout=1)
    assert result.data == b"image-data"


@pytest.mark.asyncio
async def test_upstream_timeout_during_done_grace_is_not_media_mismatch():
    class Failed(Client):
        def read_stream(self, *args, **kwargs):
            async def reader():
                try:
                    yield done(resources=[IMAGE])
                    raise TimeoutError("transport timeout")
                finally:
                    self.closed.set()
            stream = reader()
            self.retained.append(stream)
            return stream
    client = Failed()
    with pytest.raises(ProtocolGenerationError, match="超时") as caught:
        await ProtocolEngine(client, grace_seconds=0.5).generate_video("make video", timeout=1)
    assert type(caught.value) is ProtocolGenerationError
    assert client.closed.is_set()


@pytest.mark.asyncio
async def test_generation_external_cancel_stays_cancelled_and_closes_reader():
    client = Client(silent=True)
    task = asyncio.create_task(ProtocolEngine(client).generate_image("make image", timeout=1))
    while not client.after_script:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed.is_set()
