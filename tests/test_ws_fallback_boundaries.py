"""Fallback configuration is explicit; safe numeric failures survive wrapping."""
import asyncio

import pytest
from test_protocol_client import COOKIES
from test_ws_fallback_proxy import Writer, install

from muse_bridge import ws_fallback as ws
from muse_bridge.protocol_client import HatchProtocolClient, ProtocolError


@pytest.mark.parametrize("proxy,expected", [
    ("proxy.test", ("proxy.test", 80)),
    ("proxy.test:8080", ("proxy.test", 8080)),
    ("http://proxy.test:8080/", ("proxy.test", 8080)),
    ("HTTP://proxy.test", ("proxy.test", 80)),
    ("http://[::1]:8080", ("::1", 8080)),
    ("[::1]", ("::1", 80)),
])
def test_http_proxy_addresses(proxy, expected):
    assert ws._split_proxy(proxy) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [
    "socks5://proxy.test:1080", "https://proxy.test:443", "ftp://proxy.test",
    "http://user:SECRET@proxy.test", "http://proxy.test:0", "http://proxy.test:65536",
    "http://proxy.test:", "http://proxy.test:not-port", "http://[::1",
    "http://proxy.test/path", "http://proxy.test?SECRET=value", "http://proxy.test#SECRET",
    "http://", "", "http://white space.test:80",
])
async def test_unsupported_or_ambiguous_proxy_never_opens_socket(monkeypatch, proxy):
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must reject configuration before I/O")

    monkeypatch.setattr(ws.asyncio, "open_connection", forbidden)
    with pytest.raises(ws.WebSocketProtocolError) as caught:
        await ws._proxy_tunnel(proxy, "target.test", 443, 1)
    assert not calls
    assert "SECRET" not in str(caught.value)


@pytest.mark.asyncio
async def test_ipv6_target_has_brackets_in_connect_and_host(monkeypatch):
    reader = asyncio.StreamReader()
    writer = Writer(reader)

    async def opened(*args, **kwargs):
        assert args == ("::1", 8080)
        return reader, writer

    monkeypatch.setattr(ws.asyncio, "open_connection", opened)
    socket = await ws.connect("ws://[2001:db8::1]:8081/noise", proxy="http://[::1]:8080")
    try:
        assert writer.writes[0] == b"CONNECT [2001:db8::1]:8081 HTTP/1.1\r\nHost: [2001:db8::1]:8081\r\n\r\n"
        assert b"\r\nHost: [2001:db8::1]:8081\r\n" in writer.writes[1]
    finally:
        await socket.close()


@pytest.mark.asyncio
async def test_proxy_rejection_metadata_survives_client_wrapping(monkeypatch):
    _, writer = install(monkeypatch, connect_head=b"HTTP/1.1 407 SECRET proxy\r\n\r\n")
    client = HatchProtocolClient(COOKIES, proxy={"server": "http://proxy.test:8080"}, ws_opener=ws.connect)
    client._ws_url = "wss://target.test/noise?auth_token=SECRET"
    try:
        with pytest.raises(ProtocolError, match="HTTP 407") as caught:
            await client._open_noise_ws("hatch_vm")
        assert "SECRET" not in str(caught.value)
        assert writer.closed
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_upgrade_error_has_safe_status_and_stage(monkeypatch):
    reader = asyncio.StreamReader()

    class RejectedWriter(Writer):
        def write(self, data):
            if data.startswith(b"GET"):
                self.writes.append(data)
                reader.feed_data(b"HTTP/1.1 502 SECRET response\r\nX-Secret: SECRET\r\n\r\n")
            else:
                super().write(data)

    writer = RejectedWriter(reader)

    async def opened(**kwargs):
        return reader, writer

    monkeypatch.setattr(ws.asyncio, "open_connection", opened)
    with pytest.raises(ws.WebSocketProtocolError) as caught:
        await ws.connect("ws://target.test/noise?auth_token=SECRET")
    assert caught.value.status_code == 502
    assert caught.value.stage == "websocket_upgrade"
    assert "SECRET" not in str(caught.value)
    assert writer.closed
