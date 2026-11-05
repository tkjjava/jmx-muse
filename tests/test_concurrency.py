"""并发控制测试：同一账号同一时刻只允许一个请求。"""

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


class FakeDriver:
    closed = 0

    def __init__(self, cdp_url):
        self.cdp_url = cdp_url

    async def status(self):
        from muse_bridge.driver import BrowserStatus

        return BrowserStatus(connected=True, logged_in=True, url="https://muse.ai")

    async def wake_vm(self):
        return True

    async def close(self):
        FakeDriver.closed += 1

    async def stream_reply(self, prompt, timeout=180.0):
        yield "回答"


def _add(store: AccountStore, name: str) -> None:
    store.upsert(
        name, cdp_url="http://127.0.0.1:9222", state=AccountState.READY
    )


class TestLocking:
    @pytest.mark.asyncio
    async def test_lock_held_across_request(self, store, monkeypatch):
        """dispatch 返回后锁仍被持有，直到 release 才释放。"""
        monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
        _add(store, "a")

        result = await pool.dispatch(store)
        assert result.ok
        assert pool.busy_accounts() == ["a"]

        await result.release()
        assert pool.busy_accounts() == []

    @pytest.mark.asyncio
    async def test_busy_account_raises(self, store, monkeypatch):
        """唯一账号被占用时抛出 AllAccountsBusy。"""
        monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
        _add(store, "a")

        first = await pool.dispatch(store)
        assert first.ok

        with pytest.raises(pool.AllAccountsBusy) as exc:
            await pool.dispatch(store)
        assert "串行" in str(exc.value)

        await first.release()

    @pytest.mark.asyncio
    async def test_second_account_used_when_first_busy(self, store, monkeypatch):
        """一个号忙时应该轮到另一个号，而不是直接失败。"""
        monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
        _add(store, "a")
        _add(store, "b")

        first = await pool.dispatch(store)
        second = await pool.dispatch(store)

        assert first.ok and second.ok
        assert first.account != second.account

        await first.release()
        await second.release()

    @pytest.mark.asyncio
    async def test_release_closes_driver(self, store, monkeypatch):
        monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
        _add(store, "a")

        FakeDriver.closed = 0
        result = await pool.dispatch(store)
        await result.release()

        assert FakeDriver.closed == 1

    @pytest.mark.asyncio
    async def test_release_is_idempotent(self, store, monkeypatch):
        """重复 release 不该抛错。"""
        monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
        _add(store, "a")

        result = await pool.dispatch(store)
        await result.release()
        await result.release()  # 第二次应该什么都不做

        assert pool.busy_accounts() == []

    @pytest.mark.asyncio
    async def test_failed_dispatch_releases_lock(self, store, monkeypatch):
        """建驱动失败时不能把锁漏掉，否则那个号永远不可用。"""
        from muse_bridge import driver as driver_module

        _add(store, "a")

        class FailingDriver:
            def __init__(self, cdp_url):
                pass

            async def status(self):
                raise driver_module.BrowserUnavailable("连接失败")

            async def close(self):
                pass

        monkeypatch.setattr(pool, "MuseDriver", FailingDriver)
        result = await pool.dispatch(store)

        assert not result.ok
        assert pool.busy_accounts() == []

    @pytest.mark.asyncio
    async def test_concurrent_requests_serialized(self, store, monkeypatch):
        """两个并发请求不能同时进入同一个账号。"""
        monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
        _add(store, "a")

        order: list[str] = []

        async def one(tag: str):
            try:
                result = await pool.dispatch(store)
            except pool.AllAccountsBusy:
                order.append(f"{tag}:busy")
                return
            order.append(f"{tag}:got")
            await asyncio.sleep(0.05)
            await result.release()

        await asyncio.gather(one("x"), one("y"))

        # 一个拿到，一个被告知忙
        assert sorted(order) == ["x:busy", "y:got"] or sorted(order) == [
            "x:got",
            "y:busy",
        ]
