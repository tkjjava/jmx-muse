"""`read_stream` 的 idle 超时（连续无新字节才收尾）与默认关闭语义。

安全边界：这些测试只验证"多久没有新字节就停"，不验证时间精度；用 0.2–0.5 s
量级并把等待包进 `asyncio.wait_for`，避免挂死 CI。
"""

from __future__ import annotations

import asyncio

from muse_bridge.proto import BodyChunk
from tests.test_protocol_client import drain, pipe, reply, wire


def test_idle_timeout_defaults_to_disabled():
    """不传 `idle_timeout` 时签名默认是 None —— 旧调用点行为零变化。"""
    import inspect

    sig = inspect.signature(
        __import__(
            "muse_bridge.protocol_client", fromlist=["HatchProtocolClient"]
        ).HatchProtocolClient.read_stream
    )
    assert sig.parameters["idle_timeout"].default is None


async def test_idle_timeout_stops_a_silent_stream():
    """订阅建立后一直没有新字节：到 idle 预算就收尾，不能挂到总 deadline。"""
    responder = {"__table__": {"/chat/subscribe": [reply(end_body=False)]}}

    async with pipe(lambda sid, req: responder["__table__"][req.path][0]) as (client, _ws):
        await client.subscribe()

        async def consume():
            return await drain(client.read_stream(idle_timeout=0.2))

        events = await asyncio.wait_for(consume(), timeout=3.0)
        assert events == []
        assert client._idle_timeout_hit is True


async def test_idle_timeout_keeps_events_already_delivered():
    """idle 收尾只结束回合，不回收已经到的事件/正文。"""
    def respond(stream_id, request):
        # 订阅 ack 之后给一条完整事件，然后彻底安静
        if request.path == "/chat/subscribe":
            return reply(end_body=False)
        return reply(status=404, body=b"{}")

    async with pipe(respond) as (client, ws):
        stream_id = await client.subscribe()
        await ws.inbox.put(
            wire(
                stream_id,
                body_chunk=BodyChunk(
                    data=b'{"event":"delta.text_append",'
                    b'"payload":{"text":"hi"},"seq":1}\n',
                    end_body=False,
                ),
            )
        )

        async def consume():
            return await drain(client.read_stream(idle_timeout=0.2))

        events = await asyncio.wait_for(consume(), timeout=3.0)
        assert len(events) == 1
        assert events[0]["text"] == "hi"
        assert client._idle_timeout_hit is True


async def test_idle_timeout_resets_per_turn():
    """上一回合 idle 命中过，下一回合必须重新从 False 开始（不能误报）。"""
    def respond(stream_id, request):
        return reply(end_body=False)

    async with pipe(respond) as (client, _ws):
        await client.subscribe()
        await asyncio.wait_for(drain(client.read_stream(idle_timeout=0.2)), timeout=3.0)
        assert client._idle_timeout_hit is True

        # 第二回合：总 timeout 先到（比 idle 小），不应把 hit 留成 True
        await asyncio.wait_for(drain(client.read_stream(timeout=0.15)), timeout=3.0)
        assert client._idle_timeout_hit is False


async def test_idle_timeout_not_triggered_by_polling_alone():
    """轮询空转不算活动，但也不该在没有 idle_timeout 时提前收尾。"""
    def respond(stream_id, request):
        return reply(end_body=False)

    async with pipe(respond) as (client, _ws):
        await client.subscribe()
        events = await asyncio.wait_for(
            drain(client.read_stream(timeout=0.6)), timeout=3.0
        )
        assert events == []
        assert client._idle_timeout_hit is False
