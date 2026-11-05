"""doctor 自检工具的测试。不访问网络，不连接浏览器。"""

from __future__ import annotations

import pytest

from muse_bridge import doctor


class TestCheckKey:
    def test_missing_key(self, monkeypatch):
        monkeypatch.delenv("MUSE_BRIDGE_KEY", raising=False)
        result = doctor.check_key()
        assert not result.ok
        assert "未设置" in result.detail

    def test_invalid_key(self, monkeypatch):
        monkeypatch.setenv("MUSE_BRIDGE_KEY", "garbage")
        result = doctor.check_key()
        assert not result.ok

    def test_valid_key(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
        result = doctor.check_key()
        assert result.ok


class TestCheckStore:
    def test_passes_and_encrypts(self, monkeypatch):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
        result = doctor.check_store()
        assert result.ok
        # 断言里已经验证过明文不落盘
        assert "加密" in result.detail

    def test_fails_without_key(self, monkeypatch):
        monkeypatch.delenv("MUSE_BRIDGE_KEY", raising=False)
        result = doctor.check_store()
        assert not result.ok


class TestCheckPlaywright:
    def test_reports_missing(self, monkeypatch):
        """模拟 playwright 未安装。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "playwright":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = doctor.check_playwright()
        assert not result.ok
        assert "未安装" in result.detail
        assert "pip install" in result.hint


class TestCheckCdp:
    @pytest.mark.asyncio
    async def test_unreachable_reports_hint(self, monkeypatch):
        import httpx as httpx_module

        import muse_bridge.doctor as mod

        def handler(request):
            raise httpx_module.ConnectError("refused")

        transport = httpx_module.MockTransport(handler)

        class Patched(httpx_module.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        original = httpx_module.AsyncClient
        httpx_module.AsyncClient = Patched
        try:
            result = await mod.check_cdp("http://127.0.0.1:9222")
        finally:
            httpx_module.AsyncClient = original

        assert not result.ok
        assert "remote-debugging-port" in result.hint

    @pytest.mark.asyncio
    async def test_reachable_reports_browser(self):
        import httpx as httpx_module

        import muse_bridge.doctor as mod

        def handler(request):
            return httpx_module.Response(
                200, json={"Browser": "Chrome/131.0.0.0"}
            )

        transport = httpx_module.MockTransport(handler)

        class Patched(httpx_module.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        original = httpx_module.AsyncClient
        httpx_module.AsyncClient = Patched
        try:
            result = await mod.check_cdp("http://127.0.0.1:9222")
        finally:
            httpx_module.AsyncClient = original

        assert result.ok
        assert "Chrome" in result.detail
