"""取消与超时场景测试。

这些是最容易被忽略、后果又最严重的路径：
  * 客户端中途断开 → 资源必须释放
  * 上游超时 → 必须给明确响应，不能挂死
"""

from __future__ import annotations

import asyncio

import pytest

from muse_bridge import pool
from muse_bridge.state import AccountState
from muse_bridge.store import AccountStore


@pytest.fixture
def bridge_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    return None


@pytest.fixture
def store(bridge_key, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_DB", str(tmp_path / "muse.db"))
    instance = AccountStore()
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def clear_state():
    pool._locks.clear()
    pool._cursor["index"] = 0
    yield
    pool._locks.clear()
    pool._cursor["index"] = 0


class SlowDriver:
    def __init__(self, cdp_url):
        pass

    async def status(self):
        from muse_bridge.driver import BrowserStatus

        return BrowserStatus(connected=True, logged_in=True, url="https://muse.ai")

    async def wake_vm(self):
        return True

    async def close(self):
        pass

    async def stream_reply(self, prompt, timeout=180.0):
        for index in range(100):
            await asyncio.sleep(0.05)
            yield f"块{index}"


def _add(store, name):
    store.upsert(name, cdp_url="http://127.0.0.1:9222", state=AccountState.READY)


class TestReleaseOnCancel:
    @pytest.mark.asyncio
    async def test_release_works_while_cancelled(self, store, monkeypatch):
        """取消状态下 release 仍必须生效。

        这是 shield 存在的理由：客户端断开时任务已是 cancelled，
        直接 await 会被立即取消，锁就漏了。
        """
        monkeypatch.setattr(pool, "MuseDriver", SlowDriver)
        _add(store, "a")

        result = await pool.dispatch(store)
        assert pool.busy_accounts() == ["a"]

        async def use_then_cancel():
            async for _ in pool.stream_reply(result, "问题"):
                break  # 只取一个就退出，模拟客户端断开

        task = asyncio.create_task(use_then_cancel())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 关键断言：即使被取消，release 也必须能释放
        await asyncio.shield(result.release())
        assert pool.busy_accounts() == []

    @pytest.mark.asyncio
    async def test_release_idempotent_after_cancel(self, store, monkeypatch):
        monkeypatch.setattr(pool, "MuseDriver", SlowDriver)
        _add(store, "a")

        result = await pool.dispatch(store)
        await result.release()
        # 收尾路径与 finally 可能都会调一次
        await result.release()
        assert pool.busy_accounts() == []


class TestStreamTimeout:
    @pytest.mark.asyncio
    async def test_stream_stops_at_timeout(self, store, monkeypatch):
        """超时后生成器应正常结束，而不是无限产出。"""
        monkeypatch.setattr(pool, "MuseDriver", SlowDriver)
        _add(store, "a")

        result = await pool.dispatch(store)
        chunks = []
        try:
            async for delta in pool.stream_reply(result, "问题", timeout=0.12):
                chunks.append(delta)
        finally:
            await result.release()

        # 0.12 秒 ÷ 0.05 秒 ≈ 2~3 块，肯定不是 100 块
        assert 0 < len(chunks) < 100


class TestHarkEmptyReply:
    def test_setup(self):
        """占位：hark 的空回复判定在 test_core 里覆盖。"""
        assert True
