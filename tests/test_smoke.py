"""CI 冒烟模块的单元测试。

smoke 本身是给 CI 用的端到端检查，这里只测它不依赖网络的部分。
"""

from __future__ import annotations

from muse_bridge import smoke


class TestSmokeHelpers:
    def test_base_url_follows_port(self):
        assert smoke.BASE.endswith(str(smoke.PORT))
        assert smoke.BASE.startswith("http://127.0.0.1:")

    def test_timeout_is_generous(self):
        """CI 上冷启动慢，超时不能太短。"""
        assert smoke.TIMEOUT_SECONDS >= 30

    def test_interval_between_polls(self):
        assert 0 < smoke.INTERVAL <= 2.0

    def test_has_main(self):
        assert callable(smoke.main)


class TestSmokeReadiness:
    def test_wait_until_ready_returns_false_on_dead_port(self, monkeypatch):
        """端口没人监听时必须明确返回 False，而不是一直等。"""
        monkeypatch.setattr(smoke, "TIMEOUT_SECONDS", 0.6)
        monkeypatch.setattr(smoke, "INTERVAL", 0.2)
        monkeypatch.setattr(smoke, "BASE", "http://127.0.0.1:1")

        assert smoke.wait_until_ready() is False
