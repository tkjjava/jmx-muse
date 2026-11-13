"""chat.history request contract (frontend-derived) over the real request pipe."""
import pytest
from test_protocol_client import json_reply, pipe

from muse_bridge.protocol_client import ProtocolError

pytestmark = pytest.mark.asyncio


async def test_chat_history_is_get_with_query_and_no_body():
    async with pipe(lambda *a: json_reply({"ok": True, "data": {"chat_events": []}})) as (
        client,
        ws,
    ):
        payload = await client.chat_history(limit=30)
        request = ws.frames[0].request
        assert request.verb == "GET"
        assert request.path == "/chat/history?limit=30"
        assert request.body == b"", "GET 请求不带 JSON body（参数在 query 里）"
        assert request.end_body is True
        assert payload == {"chat_events": []}
        assert not client.responses and not client._stream_limits


async def test_chat_history_accepts_bare_payload_and_thread_session():
    async with pipe(lambda *a: json_reply({"chat_events": [{"seq": 1}]})) as (
        client,
        ws,
    ):
        payload = await client.chat_history(limit=5, session_id="thread-1")
        assert payload == {"chat_events": [{"seq": 1}]}
        assert ws.frames[0].request.path == (
            "/chat/history?limit=5&session_id=thread-1"
        )


@pytest.mark.parametrize("envelope", ["data", "result"])
async def test_chat_history_unwraps_envelope_key(envelope):
    body = {envelope: {"chat_events": [{"seq": 2}], "has_more": False}}
    async with pipe(lambda *a: json_reply(body)) as (client, _ws):
        payload = await client.chat_history(limit=1)
        assert payload == {"chat_events": [{"seq": 2}], "has_more": False}


@pytest.mark.parametrize("limit", [0, 201, True, 3.5, "30"])
async def test_chat_history_rejects_unusable_limit_before_send(limit):
    async with pipe() as (client, ws):
        with pytest.raises(ProtocolError, match="limit"):
            await client.chat_history(limit=limit)
        assert not ws.frames


async def test_chat_history_surfaces_http_error_without_retry():
    async with pipe(lambda *a: json_reply({"error": "no"}, status=404)) as (client, ws):
        with pytest.raises(ProtocolError, match="HTTP 404"):
            await client.chat_history()
        assert len(ws.frames) == 1