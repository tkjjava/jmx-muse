"""Dispatch-level scheduling checks with real locks and an isolated SQLite store."""
import asyncio
from types import SimpleNamespace

import pytest

from muse_bridge import pool
from muse_bridge.scheduler_health import HealthTracker
from muse_bridge.state import AccountState
from muse_bridge.store import AccountStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MUSE_BRIDGE_DB", str(tmp_path / "accounts.db"))
    instance = AccountStore()
    pool._locks.clear()
    pool._cursor["index"] = 0
    for name in ("good", "sick"):
        instance.upsert(name, cdp_url="http://localhost:9222", state=AccountState.READY)
    yield instance
    instance.close()
    pool._locks.clear()


class Driver:
    async def status(self):
        return SimpleNamespace(connected=True, logged_in=True, note="")

    async def wake_vm(self):
        return True

    async def close(self):
        pass

    async def stream_reply(self, prompt, *, timeout):
        yield "ok"


def seed_failure():
    for _ in range(10):
        pool._health.report("sick", success=False)


@pytest.mark.asyncio
async def test_score_uses_feedback_retains_fallback_and_respects_locks(store, monkeypatch):
    monkeypatch.setenv("MUSE_SCHEDULER", "score")
    monkeypatch.setattr(pool, "_build_driver", lambda *a, **kw: Driver())
    seed_failure()
    # Fix randomness to inspect candidate order, not one lucky roulette draw.
    original = pool.scheduler.rank
    monkeypatch.setattr(pool.scheduler, "rank", lambda candidates, **kw:
                        original(candidates, top_k=1, **kw))
    first = await pool.dispatch(store)
    second = await pool.dispatch(store)
    assert (first.account, second.account) == ("good", "sick")
    with pytest.raises(pool.AllAccountsBusy):
        await pool.dispatch(store)
    await first.release()
    await second.release()
    assert not pool.busy_accounts()


@pytest.mark.asyncio
async def test_named_dispatch_bypasses_ranking(store, monkeypatch):
    monkeypatch.setenv("MUSE_SCHEDULER", "score")
    monkeypatch.setattr(pool, "_selection_order", lambda *a: pytest.fail("named ranking"))
    monkeypatch.setattr(pool, "_build_driver", lambda *a, **kw: Driver())
    result = await pool.dispatch(store, account="sick")
    assert result.account == "sick"
    await result.release()


@pytest.mark.asyncio
async def test_shadow_keeps_rotation_without_extra_connections(store, monkeypatch):
    monkeypatch.setenv("MUSE_SCHEDULER", "shadow")
    built = []
    def build(store, record, **kw):
        built.append(record.account)
        return Driver()
    monkeypatch.setattr(pool, "_build_driver", build)
    expected = store.dispatchable()[0].account
    result = await pool.dispatch(store)
    assert built == [expected]
    assert result.account == expected
    await result.release()


@pytest.mark.asyncio
async def test_dispatch_failure_records_sample_and_tries_next(store, monkeypatch):
    class Broken(Driver):
        async def status(self):
            return SimpleNamespace(connected=False, logged_in=False, note="offline")
    names = [record.account for record in store.dispatchable()]
    monkeypatch.setattr(pool, "_build_driver", lambda store, record, **kw:
                        Broken() if record.account == names[0] else Driver())
    result = await pool.dispatch(store)
    assert result.account == names[1]
    assert pool._health.snapshot(names[0]).error_rate == pytest.approx(0.3)
    await result.release()
    assert not pool.busy_accounts()


@pytest.mark.asyncio
async def test_string_event_adapter_counts_once_and_recovery_decays_error():
    pool._health.report("a", success=False)
    result = pool.DispatchResult(ok=True, account="a", driver=Driver())
    assert [e async for e in pool.stream_reply_events(result, "hi")] == [{"content": "ok"}]
    sample = pool._health.snapshot("a")
    assert sample.samples == 2
    assert sample.error_rate == pytest.approx(0.21)
    assert sample.has_ttft


@pytest.mark.asyncio
async def test_native_event_error_recorded_once():
    class Broken(Driver):
        async def stream_reply_events(self, prompt, *, timeout):
            yield {"reasoning": "thinking"}
            raise TimeoutError("upstream timeout")
    result = pool.DispatchResult(ok=True, account="a", driver=Broken())
    with pytest.raises(TimeoutError):
        async for _ in pool.stream_reply_events(result, "hi"):
            pass
    sample = pool._health.snapshot("a")
    assert sample.samples == 1 and sample.has_ttft
    assert sample.error_rate == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_cancel_and_early_close_are_neutral_and_close_generator():
    closed = []
    async def cancelled():
        try:
            raise asyncio.CancelledError
            yield
        finally:
            closed.append(True)
    result = pool.DispatchResult(ok=True, account="a")
    with pytest.raises(asyncio.CancelledError):
        async for _ in pool._observe_reply(result, cancelled(), timeout=5):
            pass
    async def partial():
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(True)
    stream = pool._observe_reply(result, partial(), timeout=5)
    assert await anext(stream) == "first"
    await stream.aclose()
    assert len(closed) == 2
    assert pool._health.snapshot("a").samples == 0


def test_tracker_expiry_capacity_and_capability_separation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("muse_bridge.scheduler_health.time.monotonic", lambda: now[0])
    tracker = HealthTracker(capacity=2, ttl=10)
    tracker.report("a", success=False)
    assert tracker.snapshot("a", "generation").samples == 0
    tracker.report("b", success=True)
    tracker.report("c", success=True)
    assert tracker.snapshot("a").samples == 0
    now[0] += 10
    assert tracker.snapshot("c").samples == 0
    tracker.report("x", success=True, ttft_ms=float("nan"))
    assert not tracker.snapshot("x").has_ttft
    tracker.forget("x")
    assert tracker.snapshot("x").samples == 0


@pytest.mark.asyncio
async def test_cancel_during_dispatch_releases_lock_without_failure(store, monkeypatch):
    class Cancelled(Driver):
        async def status(self):
            raise asyncio.CancelledError
    monkeypatch.setattr(pool, "_build_driver", lambda *a, **kw: Cancelled())
    with pytest.raises(asyncio.CancelledError):
        await pool.dispatch(store, account="good")
    assert not pool.busy_accounts()
    assert pool._health.snapshot("good").samples == 0


def test_forget_account_removes_both_capability_buckets():
    pool._health.report("a", success=False)
    pool._health.report("a", success=False, capability="generation")
    pool.forget_account("a")
    assert pool._health.snapshot("a").samples == 0
    assert pool._health.snapshot("a", "generation").samples == 0


def test_invalid_mode_falls_back(monkeypatch):
    monkeypatch.setenv("MUSE_SCHEDULER", "typo")
    assert pool.scheduler_mode() == "legacy"
