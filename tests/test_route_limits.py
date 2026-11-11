"""逐路由预算（超时 / 响应体上限）的离线测试。

两条独立的契约：

1. `limits_for()` 的查表语义（默认域、前缀、查询串剥离、按方法登记）；
2. `HatchProtocolClient.request()` 真的在**接收路径**上执法上限——用一个超出
   上限的假响应验证"抛 ProtocolError + 不把整段 body 收进内存"，而不是只测表。
"""

from __future__ import annotations

import json

import pytest

from muse_bridge import protocol_client as pc
from muse_bridge.route_limits import DEFAULT_MAX_BYTES, DEFAULT_TIMEOUT, limits_for
from tests.test_protocol_client import pipe, reply


def test_default_limits_for_unknown_route():
    limits = limits_for("POST", "/api/zzz-not-a-route")
    assert limits.timeout == DEFAULT_TIMEOUT
    assert limits.max_bytes == DEFAULT_MAX_BYTES


def test_exact_route_limits_are_used():
    assert limits_for("POST", "/chat/subscribe").timeout == 30.0
    assert limits_for("POST", "/api/dyi/export").timeout == 3900.0


def test_query_string_is_stripped():
    assert limits_for("GET", "/fs/read?path=a.png").max_bytes == 32 * 1024 * 1024


def test_prefix_rule_beats_default():
    thumb = limits_for("GET", "/fs/thumbnail/ab/cd.png")
    assert thumb.max_bytes == 614_400
    assert thumb.timeout == 30.0


def test_verb_must_match():
    # 同一路径换个方法就落回默认域：路由预算是按方法登记的。
    assert limits_for("PUT", "/fs/read").max_bytes == DEFAULT_MAX_BYTES


async def test_oversized_response_raises_and_drops_body():
    """超限响应：抛 ProtocolError，且该 stream 的上限登记被清理。"""
    payload = b"x" * 4096

    async with pipe(lambda stream_id, request: reply(body=payload)) as (client, _ws):
        with pytest.raises(pc.ProtocolError) as excinfo:
            await client.request("GET", "/api/zzz-huge", max_bytes=1024)
        assert "超过上限" in str(excinfo.value)
        assert client._stream_limits == {}


async def test_response_under_limit_passes_untouched():
    body = json.dumps({"ok": True}).encode()

    async with pipe(lambda stream_id, request: reply(body=body)) as (client, _ws):
        response = await client.request("GET", "/api/zzz-small", max_bytes=1024)
        assert response.status == 200
        assert json.loads(bytes(response.body)) == {"ok": True}
        assert response.limit_exceeded is None
        assert client._stream_limits == {}
