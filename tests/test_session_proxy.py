"""会话面（`GET /api/session`）的出口代理接线。

背景（2026-11-11 13:4x 真机）：`/admin/accounts/{a}/refresh` 报
`/api/session 网络请求失败（ConnectError）`，而同一个本机代理经 `muse.ai`
是 200。原因不是网络抖动，是 `_OwnClient` 建 `httpx.AsyncClient` 时**从来不传
proxy**——直连被墙时这条链路必死，而协议 WS 那条路早就有自己的代理。

这里钉住三条语义：
1. 自建 client 时把代理传进去；
2. 注入了 client 就**不**再套代理（那是调用方的连接池/出口）；
3. 没显式传时回落到 `env.SESSION_PROXY`。
"""

from __future__ import annotations

import contextlib

import httpx

from muse_bridge import session_http


class _Recorder:
    """替身 AsyncClient：只记录构造 kwargs，行为足够 _OwnClient 用完就关。"""

    last: dict[str, object] = {}

    def __init__(self, **kwargs):
        type(self).last = dict(kwargs)

    async def aclose(self) -> None:
        return None


async def test_own_client_passes_proxy(monkeypatch):
    monkeypatch.setattr(session_http.httpx, "AsyncClient", _Recorder)
    async with session_http._OwnClient(None, 5.0, "http://127.0.0.1:7890") as http:
        assert isinstance(http, _Recorder)
    assert _Recorder.last["proxy"] == "http://127.0.0.1:7890"
    assert _Recorder.last["trust_env"] is False


async def test_own_client_omits_proxy_when_empty(monkeypatch):
    monkeypatch.setattr(session_http.httpx, "AsyncClient", _Recorder)
    async with session_http._OwnClient(None, 5.0, None) as _http:
        pass
    # 关键：是"没有这个键"，不是 proxy=None —— 后者在 httpx 里是另一层语义。
    assert "proxy" not in _Recorder.last


async def test_injected_client_is_not_wrapped_or_proxied(monkeypatch):
    sentinel = object()

    def _boom(**kwargs):  # 注入路径不该建新 client
        raise AssertionError("注入了 client 却还在新建 AsyncClient")

    monkeypatch.setattr(session_http.httpx, "AsyncClient", _boom)
    async with session_http._OwnClient(sentinel, 5.0, "http://p:1") as http:
        assert http is sentinel


async def test_renew_session_defaults_to_session_proxy(monkeypatch):
    """没显式传 proxy 时用 env.SESSION_PROXY；显式传则优先显式值。"""
    seen: list[object] = []

    class SpyOwn:
        def __init__(self, client, timeout, proxy=None):
            seen.append(proxy)

        async def __aenter__(self):
            class _Http:
                async def get(self, *args, **kwargs):
                    raise httpx.ConnectError("stop here")

            return _Http()

        async def __aexit__(self, *exc_info):
            return None

    monkeypatch.setattr(session_http, "_OwnClient", SpyOwn)
    # 注意 patch 的是 session_http 里那个名字：`from .env import SESSION_PROXY`
    # 是 import 时绑定，改 env 模块的属性影响不到它（进程级配置，重启生效）。
    monkeypatch.setattr(session_http, "SESSION_PROXY", "http://env-proxy:1")

    for kwargs, expected in (
        ({}, "http://env-proxy:1"),
        ({"proxy": "http://explicit:2"}, "http://explicit:2"),
    ):
        with contextlib.suppress(session_http.SessionUnconfirmed):
            await session_http.renew_session({"hatch_sess": "x"}, **kwargs)
        assert seen[-1] == expected
