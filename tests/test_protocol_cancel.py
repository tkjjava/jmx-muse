"""Cancel payload is frontend-derived; use actual HTTP-over-Noise framing."""
import pytest
from test_protocol_client import json_reply, pipe, request_json

from muse_bridge.protocol_client import ProtocolError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("session_id,expected", [(None, {}), ("thread-123", {"session_id": "thread-123"})])
async def test_cancel_route_body_and_no_device_identifier(session_id, expected):
    async with pipe(lambda *a: json_reply({"cancelled": True})) as (client, ws):
        result = await client.cancel_chat(session_id=session_id)
        assert result == {"cancelled": True}
        request = ws.frames[0].request
        assert request.verb == "POST" and request.path == "/chat/cancel"
        assert request_json(request) == expected
        assert request.end_body is True
        assert "node_id" not in request_json(request)
        assert not client.responses and not client._stream_limits


async def test_noop_does_not_claim_successful_cancellation():
    async with pipe(lambda *a: json_reply({"cancelled": False})) as (client, ws):
        assert (await client.cancel_chat())["cancelled"] is False
        assert len(ws.frames) == 1


@pytest.mark.parametrize("payload", [{}, {"cancelled": "true"}, {"cancelled": 1}, []])
async def test_invalid_ack_cannot_be_reported_as_cancelled(payload):
    async with pipe(lambda *a: json_reply(payload)) as (client, ws):
        with pytest.raises(ProtocolError, match="布尔 cancelled"):
            await client.cancel_chat()
        assert len(ws.frames) == 1


@pytest.mark.parametrize("session_id", ["", "  ", 123])
async def test_invalid_thread_id_rejected_before_send(session_id):
    async with pipe() as (client, ws):
        with pytest.raises(ProtocolError, match="session_id"):
            await client.cancel_chat(session_id=session_id)
        assert not ws.frames


async def test_plain_close_never_implicitly_cancels_shared_session():
    async with pipe() as (client, ws):
        await client.close()
        assert not ws.frames


async def test_cancel_does_not_abort_local_subscription_or_terminal_state():
    """Live evidence: deltas keep arriving after a cancel ACK. Sending cancel must
    not tear down local stream state, mark completion, or poison the connection."""
    async with pipe(lambda *a: json_reply({"cancelled": True})) as (client, ws):
        client._sub_stream_id = 42
        client._turn_complete = False
        assert (await client.cancel_chat())["cancelled"] is True
        assert client._sub_stream_id == 42
        assert client._turn_complete is False
        assert client.turn_terminal_reason is None
        assert not client._connection_lost()
        # Connection stays usable for another control request.
        ws.respond = lambda *a: json_reply({"cancelled": False})
        assert (await client.cancel_chat())["cancelled"] is False
        assert len(ws.frames) == 2


async def test_cancel_denied_preserves_http_error_and_is_not_retried():
    async with pipe(lambda *a: json_reply({"error": "denied"}, status=403)) as (client, ws):
        with pytest.raises(ProtocolError, match="HTTP 403"):
            await client.cancel_chat()
        assert len(ws.frames) == 1
