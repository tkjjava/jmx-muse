"""Distinguish actual engine deadlines from independent upstream timeouts."""
import asyncio

import pytest
from test_protocol_engine_lifecycle import IMAGE, Client, done

from muse_bridge.protocol_engine import ProtocolEngine, ProtocolGenerationError

pytestmark = pytest.mark.asyncio


class TimedOutReader(Client):
    def read_stream(self, *args, **kwargs):
        async def reader():
            try:
                raise TimeoutError("SECRET_URL?auth_token=SECRET")
                yield  # Make this an async generator, with a retained frame.
            finally:
                self.closed.set()
        stream = reader()
        self.retained.append(stream)
        return stream


async def test_chat_transport_timeout_does_not_claim_generation_budget_exhaustion():
    client = TimedOutReader()
    with pytest.raises(ProtocolGenerationError, match="上游操作超时") as caught:
        await anext(ProtocolEngine(client).stream_chat("hello", timeout=60))
    assert "SECRET" not in str(caught.value)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert client.closed.is_set()
    assert client.retained[0].ag_frame is None


@pytest.mark.parametrize("phase", ["stage", "prefetch", "read", "fetch"])
async def test_generation_independent_upstream_timeout_at_each_phase(phase, monkeypatch):
    client = TimedOutReader() if phase == "read" else Client([done(resources=[IMAGE])])
    engine = ProtocolEngine(client)
    failure = TimeoutError("SECRET_URL?auth_token=SECRET")

    async def fail(*args):
        raise failure

    if phase == "stage":
        monkeypatch.setattr(engine, "_stage_references", fail)
    elif phase == "prefetch":
        monkeypatch.setattr(engine, "_prefetch_media", fail)
    elif phase == "fetch":
        monkeypatch.setattr(engine, "_fetch_bytes", fail)

    with pytest.raises(ProtocolGenerationError, match="上游操作超时") as caught:
        await engine.generate_image("image", timeout=60)
    assert "SECRET" not in str(caught.value)
    assert isinstance(caught.value.__cause__, TimeoutError)
    if phase != "read":
        assert caught.value.__cause__ is failure
    if phase in ("read", "fetch"):
        assert client.closed.is_set()
    if client.retained:
        assert client.retained[0].ag_frame is None


@pytest.mark.parametrize("kind", ["chat", "image"])
async def test_actual_engine_deadline_retains_generation_timeout_classification(kind):
    client = Client(silent=True)
    engine = ProtocolEngine(client)
    async with asyncio.timeout(1):
        with pytest.raises(ProtocolGenerationError, match="生成超时") as caught:
            if kind == "chat":
                await anext(engine.stream_chat("hello", timeout=0.01))
            else:
                await engine.generate_image("image", timeout=0.01)
    assert "上游操作" not in str(caught.value)
    assert client.closed.is_set()
