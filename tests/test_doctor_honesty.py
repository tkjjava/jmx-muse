"""自检工具：真实错误必须如实暴露，跳过项必须显式报告。"""

from __future__ import annotations

import pytest

from muse_bridge import doctor
from muse_bridge import store as store_mod


@pytest.fixture
def key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MUSE_BRIDGE_DB", str(__import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "doctor.db"))
    return None


def test_store_check_reports_the_real_error_not_a_cleanup_error(monkeypatch, key):
    """写库失败时，报告里必须是那条错误本身。

    修复前：store 没被关闭 -> Windows 删临时目录抛 WinError 32 ->
    那个清理异常盖掉真实原因，doctor 对任何写库故障都报"检查 data 目录写权限"。
    """

    def boom(self, *args, **kwargs):
        raise RuntimeError("真正的原因")

    monkeypatch.setattr(store_mod.AccountStore, "upsert", boom)
    result = doctor.check_store()

    assert not result.ok
    assert "RuntimeError" in result.detail, result.detail
    assert "真正的原因" in result.detail, result.detail
    assert "PermissionError" not in result.detail, result.detail


def test_store_check_passes_in_a_clean_environment(key):
    result = doctor.check_store()
    assert result.ok, result.detail


def test_failure_makes_run_return_one(key, monkeypatch):
    """任一检查失败，退出码必须是 1——自检工具静默返回 0 会让运维脚本误判健康。"""
    import asyncio

    def bad():
        item = doctor.CheckResult("加密密钥")
        item.detail = "合成的失败"
        return item

    monkeypatch.setattr(doctor, "check_key", bad)
    monkeypatch.setattr(doctor, "check_store", bad)
    code = asyncio.run(doctor._run("http://127.0.0.1:9222"))
    assert code == 1
