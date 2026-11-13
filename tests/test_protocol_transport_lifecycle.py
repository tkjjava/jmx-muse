"""Replay protocol frames through the actual request multiplexer (no network)."""
import asyncio

import pytest
from test_protocol_client import ApplicationResponse, pipe, reply, wire

from muse_bridge.proto import BodyChunk
from muse_bridge.protocol_client import ProtocolError

pytestmark = pytest.mark.asyncio


async def test_timeout_discards_response_limits_and_waiters_and_ignores_late_frame():
    async with pipe() as (client, ws):
        with pytest.raises(ProtocolError, match="超时"):
            await client.request("GET", "/slow", timeout=0.01, max_bytes=4)
        assert not client.responses and not client.waiters and not client._stream_limits
        await ws.inbox.put(wire(1, response=ApplicationResponse(status=200, body=b"late", end_body=True)))
        # A barrier request ensures the late frame has passed through the receiver.
        ws.respond = lambda *a: reply(body=b"ok")
        assert bytes((await client.request("GET", "/barrier")).body) == b"ok"
        assert not client.responses


async def test_cancelled_request_does_not_leave_unbounded_stream_state():
    async with pipe() as (client, ws):
        task = asyncio.create_task(client.request("GET", "/cancel", max_bytes=4))
        while not ws.frames:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.responses and not client._stream_limits and not client.waiters


async def test_body_cap_is_terminal_even_without_end_body_and_other_stream_survives():
    async with pipe(lambda *a: reply(body=b"12345", end_body=False)) as (client, ws):
        with pytest.raises(ProtocolError, match="超过上限"):
            await client.request("GET", "/big", max_bytes=4, timeout=0.1)
        await ws.inbox.put(wire(1, body_chunk=BodyChunk(data=b"tail", end_body=False)))
        ws.respond = lambda *a: reply(body=b"ok")
        assert bytes((await client.request("GET", "/small")).body) == b"ok"
        assert not client.responses


async def test_send_failure_poisoned_connection_cannot_reuse_consumed_nonces():
    async with pipe() as (client, ws):
        calls = []
        async def fail(data):
            calls.append(data)
            raise OSError("write interrupted")
        ws.send = fail
        with pytest.raises(OSError):
            await client.request("POST", "/action", {"message": "once"})
        assert client._connection_lost()
        assert not client.responses and not client._stream_limits
        with pytest.raises(ProtocolError, match="失效"):
            await client.request("POST", "/action", {"message": "once"})
        assert len(calls) == 1  # Never auto-replay an ambiguously delivered action.


async def test_turn_complete_requires_owned_known_status_and_resets():
    import json
    async with pipe(lambda *a: reply(end_body=False)) as (client, ws):
        sid = await client.subscribe()
        def event(name, payload):
            return (json.dumps({"event": name, "payload": payload}) + "\n").encode()
        data = event("delta.text_append", {"text": "partial"})
        data += event("agent.status", {"activity_code": "", "chat_context": {"device_id": client.node_id}})
        data += event("delta.message_done", {"chat_context": {"device_id": "foreign"}})
        data += event("agent.status", {"activity_code": "online"})
        await ws.inbox.put(wire(sid, body_chunk=BodyChunk(data=data, end_body=False)))
        events = [e async for e in client.read_stream(timeout=0.5)]
        assert len(events) == 4 and client._turn_complete is True
        client._seek_to_end(sid)
        assert [e async for e in client.read_stream(timeout=0.01)] == []
        assert client._turn_complete is False


async def test_unknown_status_or_idle_after_text_is_not_completion():
    async with pipe(lambda *a: reply(end_body=False)) as (client, ws):
        sid = await client.subscribe()
        await ws.inbox.put(wire(sid, body_chunk=BodyChunk(
            data=b'{"event":"delta.text_append","payload":{"text":"partial"}}\n'
                 b'{"event":"agent.status","payload":{}}\n', end_body=False)))
        events = [e async for e in client.read_stream(timeout=0.2, idle_timeout=0.01)]
        assert len(events) == 2 and not client._turn_complete


async def test_encrypted_unknown_frame_consumes_nonce_but_cannot_allocate_response():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from muse_bridge.noise import Cipher

    key = bytes(range(32))
    async with pipe() as (client, ws):
        client.recv_cipher = Cipher(key)
        # Independent AESGCM peer, no use of production Cipher.enc/nonce helper.
        encryptor = AESGCM(key)
        unknown = wire(999, response=ApplicationResponse(status=200, body=b"unsolicited", end_body=True))
        await ws.inbox.put(encryptor.encrypt(b"\0" * 12, unknown, b""))
        async def respond(data):
            # Outbound is plaintext in this test; sid allocated before send.
            frame = wire(1, response=ApplicationResponse(status=200, body=b"verified", end_body=True))
            await ws.inbox.put(encryptor.encrypt(b"\0" * 4 + (1).to_bytes(8, "big"), frame, b""))
        ws.send = respond
        response = await client.request("GET", "/encrypted")
        assert bytes(response.body) == b"verified"
        assert client.recv_cipher.counter == 2
        assert not client.responses


async def test_cancelled_subscribe_cleans_allocated_response():
    async with pipe() as (client, ws):
        task = asyncio.create_task(client.subscribe())
        while not ws.frames:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.responses and not client.waiters
