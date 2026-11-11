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
    store.upsert(name, cdp_url="http://127.0.0.1:9222", state=AccountState.READY)


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


def _driver_cls(*, wake_raises=None, mark_ready_raises=False, store=None):
    """构造一个假的 MuseDriver：status 成功、wake 可选抛错。"""

    class _Status:
        connected = True
        logged_in = True
        note = ""
        url = "https://muse.ai/"

    class _Driver:
        def __init__(self, cdp_url: str) -> None:
            self.cdp_url = cdp_url
            self.closed = False

        async def status(self):
            return _Status()

        async def wake_vm(self):
            if wake_raises is not None:
                raise wake_raises
            return True

        async def close(self):
            self.closed = True

    return _Driver


class _Record:
    def __init__(self, name: str, vm_ready: bool = True) -> None:
        self.account = name
        self.cdp_url = "http://127.0.0.1:9222"
        self.vm_ready = vm_ready


class _Store:
    def __init__(self, record, *, mark_ready_raises=False):
        self._record = record
        self._mark_ready_raises = mark_ready_raises
        self.failures = []
        self.ready_calls = []

    def dispatchable(self):
        return [self._record]

    def mark_failure(self, account, *, state, error):
        self.failures.append((account, state, error))

    def mark_ready(self, account, *, vm_ready, state):
        if self._mark_ready_raises:
            raise RuntimeError("数据库炸了")
        self.ready_calls.append((account, vm_ready, state))
        return None


@pytest.mark.asyncio
async def test_dispatch_survives_wake_failure_and_keeps_lock_for_caller(monkeypatch):
    """唤醒失败不阻断派单（注释承诺的行为），且锁交给调用方。"""
    name = "唤醒失败号"
    monkeypatch.setattr(pool, "MuseDriver", _driver_cls(wake_raises=RuntimeError("页面崩了")))
    store = _Store(_Record(name, vm_ready=False))

    result = await pool.dispatch(store)

    assert result.ok is True
    assert name in pool.busy_accounts()  # 锁覆盖整个请求，由调用方释放
    await result.release()
    assert name not in pool.busy_accounts()


@pytest.mark.asyncio
async def test_failed_wake_is_not_recorded_as_vm_ready(monkeypatch):
    """唤醒失败必须如实记录成 VM_SLEEPING。

    派单路径只在 not record.vm_ready 时才尝试唤醒，所以谎报一次"已唤醒"
    等于此后每次派单都跳过唤醒，直接打到休眠中的 VM 上——账号会一直失败到
    有人手工 refresh。修复前这里既记成 vm_ready=True，也不留任何提示。
    """
    name = "假唤醒号"
    monkeypatch.setattr(pool, "MuseDriver", _driver_cls(wake_raises=RuntimeError("CDP 掉了")))
    store = _Store(_Record(name, vm_ready=False))

    result = await pool.dispatch(store)
    try:
        assert result.ok is True, "唤醒失败不阻断派单，这条契约不变"
        assert store.ready_calls, "必须写过一次账号状态"
        account, vm_ready, state = store.ready_calls[-1]
        assert account == name
        assert vm_ready is False, "唤醒失败不能记成已唤醒"
        assert state is AccountState.VM_SLEEPING, f"实际 state={state}"
        assert result.note, "唤醒失败不能静默：调用方要能看到"
    finally:
        await result.release()


@pytest.mark.asyncio
async def test_successful_wake_is_recorded_as_vm_ready(monkeypatch):
    """反向约束：唤醒成功必须如实记成 vm_ready=True，否则每次派单都会重复唤醒。"""
    name = "正常唤醒号"
    monkeypatch.setattr(pool, "MuseDriver", _driver_cls())
    store = _Store(_Record(name, vm_ready=False))

    result = await pool.dispatch(store)
    try:
        account, vm_ready, state = store.ready_calls[-1]
        assert account == name
        assert vm_ready is True
        assert state is AccountState.READY
        assert not result.note, f"成功路径不该带预警，实际 {result.note!r}"
    finally:
        await result.release()


@pytest.mark.asyncio
async def test_dispatch_releases_lock_when_unexpected_error(monkeypatch):
    """未预期异常可以冒出去，但绝不能把锁留下（否则账号永久忙）。"""
    name = "异常路径号"
    monkeypatch.setattr(pool, "MuseDriver", _driver_cls())
    store = _Store(_Record(name), mark_ready_raises=True)

    with pytest.raises(RuntimeError):
        await pool.dispatch(store)

    assert name not in pool.busy_accounts()


def test_forget_account_drops_scheduler_state():
    pool._lock_for("回收号")
    assert "回收号" in pool._locks
    pool.forget_account("回收号")
    assert "回收号" not in pool._locks
