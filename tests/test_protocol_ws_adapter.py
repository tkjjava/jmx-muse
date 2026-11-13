"""Exercise real websocket dependency keyword binding without network."""
import pytest

from muse_bridge.protocol_client import HatchProtocolClient

pytestmark = pytest.mark.asyncio


async def test_default_adapter_maps_ua_and_disables_implicit_system_proxy(monkeypatch):
    import websockets

    captured = {}
    sentinel = object()

    async def connect(uri, *, user_agent_header=None, proxy=True, **kwargs):
        captured.update(uri=uri, user_agent_header=user_agent_header, proxy=proxy, rest=kwargs)
        return sentinel

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid.test:1")
    opener = HatchProtocolClient._default_ws_opener()
    result = await opener("wss://example.test/noise", user_agent="CustomUA/1")
    assert result is sentinel
    assert captured["user_agent_header"] == "CustomUA/1"
    assert captured["proxy"] is None
    assert "user_agent" not in captured["rest"]


async def test_default_adapter_preserves_explicit_proxy_and_upgrade_options(monkeypatch):
    import websockets

    captured = {}

    async def connect(uri, *, user_agent_header="library-default", proxy=True, **kwargs):
        captured.update(user_agent_header=user_agent_header, proxy=proxy, rest=kwargs)
        return object()

    monkeypatch.setattr(websockets, "connect", connect)
    opener = HatchProtocolClient._default_ws_opener()
    await opener("wss://example.test/noise", proxy="http://127.0.0.1:7890", open_timeout=12, origin="https://example.test")
    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert captured["user_agent_header"] == "library-default"
    assert captured["rest"] == {"open_timeout": 12, "origin": "https://example.test"}
