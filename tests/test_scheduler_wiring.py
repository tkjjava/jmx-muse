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


@pytest.mark.parametrize("value,expected", [("0", 0), ("3", 3), ("-2", 0),
                                             ("typo", 0), ("999", 128)])
def test_attempt_limit_config(monkeypatch, value, expected):
    monkeypatch.setenv("MUSE_DISPATCH_MAX_ATTEMPTS", value)
    assert pool.dispatch_attempt_limit() == expected


@pytest.mark.asyncio
async def test_budget_stops_before_next_free_account_and_releases_locks(store, monkeypatch):
    monkeypatch.setenv("MUSE_DISPATCH_MAX_ATTEMPTS", "1")
    built = []
    class Broken(Driver):
        async def status(self):
            return SimpleNamespace(connected=False, logged_in=False, note="offline")
    def build(store, record, **kw):
        built.append(record.account)
        return Broken()
    monkeypatch.setattr(pool, "_build_driver", build)
    result = await pool.dispatch(store)
    assert not result.ok and result.verdict == "dispatch_budget_exhausted"
    assert len(built) == 1
    assert not pool.busy_accounts()
    names = [r.account for r in store.list_all() if r.account not in built]
    assert pool._health.snapshot(names[0]).samples == 0


@pytest.mark.asyncio
async def test_busy_accounts_do_not_consume_budget(store, monkeypatch):
    monkeypatch.setenv("MUSE_DISPATCH_MAX_ATTEMPTS", "1")
    names = [r.account for r in store.dispatchable()]
    lock = pool._lock_for(names[0])
    await lock.acquire()
    monkeypatch.setattr(pool, "_build_driver", lambda *a, **kw: Driver())
    try:
        result = await pool.dispatch(store)
        assert result.ok and result.account == names[1]
        await result.release()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_all_attempted_failure_is_not_budget_exhaustion(store, monkeypatch):
    monkeypatch.setenv("MUSE_DISPATCH_MAX_ATTEMPTS", "1")
    class Broken(Driver):
        async def status(self):
            return SimpleNamespace(connected=False, logged_in=False, note="offline")
    monkeypatch.setattr(pool, "_build_driver", lambda *a, **kw: Broken())
    result = await pool.dispatch(store, account="good")
    assert result.verdict == "all_accounts_failed"
    assert not pool.busy_accounts()


@pytest.mark.asyncio
async def test_duplicate_candidates_are_attempted_once(store, monkeypatch):
    records = store.dispatchable()
    monkeypatch.setattr(pool, "_selection_order", lambda *a: [records[0], records[0], records[1]])
    calls = []
    class Broken(Driver):
        async def status(self):
            return SimpleNamespace(connected=False, logged_in=False, note="offline")
    def build(store, record, **kw):
        calls.append(record.account)
        return Broken()
    monkeypatch.setattr(pool, "_build_driver", build)
    result = await pool.dispatch(store)
    assert result.verdict == "all_accounts_failed"
    assert calls == [record.account for record in records]


@pytest.mark.asyncio
async def test_budget_exhaustion_has_local_503_api_contract(store, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from muse_bridge import app as app_module
    monkeypatch.setenv("MUSE_DISPATCH_MAX_ATTEMPTS", "1")
    monkeypatch.setattr(app_module, "store", lambda: store)
    class Broken(Driver):
        async def status(self):
            return SimpleNamespace(connected=False, logged_in=False, note="offline")
    monkeypatch.setattr(pool, "_build_driver", lambda *a, **kw: Broken())
    async def no_key():
        return None
    previous = app_module.app.dependency_overrides.copy()
    app_module.app.dependency_overrides[app_module.require_key] = no_key
    try:
        async with AsyncClient(transport=ASGITransport(app=app_module.app),
                               base_url="http://local", trust_env=False) as api:
            response = await api.post("/v1/chat/completions", json={
                "model": "muse-1", "messages": [{"role": "user", "content": "test"}]})
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "dispatch_budget_exhausted"
        assert response.json()["detail"]["type"] == "server_error"
        assert not pool.busy_accounts()
    finally:
        app_module.app.dependency_overrides.clear()
        app_module.app.dependency_overrides.update(previous)


def test_invalid_mode_falls_back(monkeypatch):
    monkeypatch.setenv("MUSE_SCHEDULER", "typo")
    assert pool.scheduler_mode() == "legacy"
