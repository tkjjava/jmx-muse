"""管理端点鉴权：配置了 API 密钥之后，写操作与 /v1/* 同样校验。

不配密钥时 require_key 直接放行——默认行为一字不变，控制台照旧可用。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from muse_bridge import app as app_module

ENV = "MUSE_BRIDGE_API_KEYS"


@pytest.mark.asyncio
async def test_require_key_gate(monkeypatch):
    monkeypatch.setenv(ENV, "s3cret")
    with pytest.raises(HTTPException) as excinfo:
        await app_module.require_key(None)
    assert excinfo.value.status_code == 401
    await app_module.require_key("Bearer s3cret")  # 正确密钥不抛
    monkeypatch.delenv(ENV, raising=False)
    await app_module.require_key(None)  # 未配置 = 放行（向后兼容）


@pytest.mark.asyncio
async def test_mutating_admin_requires_key_when_configured(monkeypatch):
    """42 号钉子：写操作不能被一个未鉴权的 POST 干掉。"""
    monkeypatch.setenv(ENV, "s3cret")
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        calls = [
            await c.post("/admin/attach", json={"account": "a"}),
            await c.delete("/admin/accounts/a"),
            await c.post("/admin/accounts/a/refresh"),
            await c.post("/admin/accounts/a/wake"),
        ]
    for response in calls:
        assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_admin_gate_accepts_correct_key(monkeypatch):
    """带正确密钥要能过闸门（这里只验闸门，不真的连浏览器）。"""
    monkeypatch.setenv(ENV, "s3cret")
    transport = ASGITransport(app=app_module.app)
    headers = {"Authorization": "Bearer s3cret"}
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.delete("/admin/accounts/不存在的号", headers=headers)
    assert response.status_code != 401, response.text


@pytest.mark.asyncio
async def test_admin_refresh_and_wake_map_browser_unavailable_to_503(monkeypatch):
    """CDP 拿不到时管理端点必须 503，不能裸 500（与 /admin/attach 同语义）。"""
    monkeypatch.setenv(ENV, "s3cret")

    from muse_bridge.driver import BrowserUnavailable

    class _DeadDriver:
        def __init__(self, cdp_url: str) -> None:
            self.cdp_url = cdp_url

        async def touch_session(self) -> bool:
            raise BrowserUnavailable("cdp 连不上")

        async def wake_vm(self) -> bool:
            raise BrowserUnavailable("cdp 连不上")

        async def close(self) -> None:
            return None

    class _Record:
        cdp_url = "http://127.0.0.1:9222"

    class _FakeStore:
        def get(self, account: str):
            return _Record()

        def cookies(self, account: str) -> dict:
            # 这条用例走的是 CDP 路径：没有导入 cookie，wake 才会落到浏览器分支
            # 并在这里被 _DeadDriver 抛 BrowserUnavailable → 503。
            return {}

    monkeypatch.setattr(app_module, "MuseDriver", _DeadDriver)
    monkeypatch.setattr(app_module, "store", lambda: _FakeStore())

    transport = ASGITransport(app=app_module.app)
    headers = {"Authorization": "Bearer s3cret"}
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        refresh = await c.post("/admin/accounts/a/refresh", headers=headers)
        wake = await c.post("/admin/accounts/a/wake", headers=headers)
    assert refresh.status_code == 503, refresh.text
    assert wake.status_code == 503, wake.text
