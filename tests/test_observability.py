"""可观测性面：分批统计端点、healthz 派生量、公开面与管理面的隔离。

为什么必须断言"隔离"：`require_admin` 在没配置 `MUSE_BRIDGE_ADMIN_KEYS` 时会
**放行**（既有降级设计，`admin_key_separated` 会如实报 false）。所以这里只钉
"配置了 admin key 之后普通 key 读不到"，不去假定默认部署是安全的。
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from muse_bridge import app as app_module
from muse_bridge import observability
from muse_bridge.store import AccountStore


@pytest.fixture(autouse=True)
def _clean_counters():
    observability.reset()
    yield
    observability.reset()


@pytest.fixture
def store(tmp_path, monkeypatch):
    # AccountStore 要加密密钥才能开库；这里给临时密钥，绝不碰真实部署那把。
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    instance = AccountStore(tmp_path / "obs.db")
    yield instance
    instance.close()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://t"
    )


def test_snapshot_starts_empty():
    snap = observability.snapshot()
    assert snap == {
        "counters": {},
        "timing": {"count": {}, "average_seconds": {}, "max_seconds": {}},
    }


def test_bump_and_timed_accumulate():
    observability.bump("chat_media_fetch_failed")
    observability.bump("chat_media_fetch_failed")
    with observability.timed("chat_media_fetch"):
        pass

    snap = observability.snapshot()
    assert snap["counters"]["chat_media_fetch_failed"] == 2
    assert snap["timing"]["count"]["chat_media_fetch"] == 1
    assert snap["timing"]["max_seconds"]["chat_media_fetch"] >= 0.0


def test_timed_still_records_on_exception():
    """失败请求同样耗时——异常路径不记，平均值就只统计成功的那部分。"""
    with pytest.raises(ValueError), observability.timed("chat_media_fetch"):
        raise ValueError("boom")
    assert observability.snapshot()["timing"]["count"]["chat_media_fetch"] == 1


async def test_admin_observability_reports_counts(monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_KEYS", "admin-secret")
    observability.bump("warm_acquire_hit", 3)
    async with _client() as client:
        response = await client.get(
            "/admin/observability",
            headers={"Authorization": "Bearer admin-secret"},
        )
    assert response.status_code == 200
    assert response.json()["counters"]["warm_acquire_hit"] == 3


async def test_admin_observability_rejects_plain_key(monkeypatch):
    """配了 admin key 之后，普通调用 key 不得读内部失败率。"""
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_KEYS", "admin-secret")
    monkeypatch.setenv("MUSE_BRIDGE_API_KEYS", "plain-key")
    async with _client() as client:
        denied = await client.get(
            "/admin/observability", headers={"Authorization": "Bearer plain-key"}
        )
        anonymous = await client.get("/admin/observability")
    assert denied.status_code == 401
    assert anonymous.status_code == 401


async def test_healthz_keeps_internal_counters_private(monkeypatch, store):
    """公开 healthz 不能带失败率；但它自己的派生字段必须在。"""
    monkeypatch.setattr(app_module, "store", lambda: store)
    observability.bump("media_download_failed", 7)
    async with _client() as client:
        body = (await client.get("/healthz")).json()
    warm = body["warm_pool"]
    assert set(warm) == {"enabled", "accounts", "refs", "leased_accounts"}
    assert warm["leased_accounts"] == sum(
        1 for value in warm["refs"].values() if value > 0
    )
    assert "media_download_failed" not in str(body)


async def test_healthz_reflects_lease_state(monkeypatch, store):
    """有租用在外时 leased_accounts 必须为 1，归还后回 0。"""
    import asyncio

    from muse_bridge import protocol_hub

    monkeypatch.setattr(app_module, "store", lambda: store)
    monkeypatch.setenv("MUSE_WARM_POOL", "1")

    class Client:
        """够 `usable()` 判定的最小 client（有跑着的接收任务、无订阅）。"""

        closing = False
        ws = object()
        _handshake_done = True
        _sub_stream_id = None

        def __init__(self) -> None:
            self.responses: dict = {}
            self.receiver_task = asyncio.ensure_future(asyncio.sleep(3600))

        def _connection_lost(self) -> bool:
            return False

        async def connect(self) -> None:
            pass

        async def close(self) -> None:
            pass

    await protocol_hub.close_all()
    protocol_hub._SLOTS.clear()
    try:
        await protocol_hub.acquire(
            "muse1", {"hatch_sess": "x"}, factory=lambda *a, **k: Client()
        )
        async with _client() as client:
            leased = (await client.get("/healthz")).json()["warm_pool"]
        assert leased["leased_accounts"] == 1

        await protocol_hub.release("muse1")
        async with _client() as client:
            idle = (await client.get("/healthz")).json()["warm_pool"]
        assert idle["leased_accounts"] == 0
    finally:
        await protocol_hub.close_all()
        protocol_hub._SLOTS.clear()


# ---- 媒体清扫（C2：sweep() 曾经没有任何生产调用点） ----


async def test_media_sweeper_actually_runs_and_counts(tmp_path):
    """后台清扫必须真的被跑起来：`sweep()` 写好了但没人调，等于没有清理。

    旧状态是全仓只有测试调 `MediaStore.sweep()`，生产路径零调用点，
    于是 `data/media/` 只增不减，TTL 形同虚设。
    """
    from muse_bridge import media as media_mod

    store = media_mod.MediaStore(tmp_path / "m")
    saved = store.save(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24, "image/png")
    ref = next(item for item in store.list_media() if item.token == saved.token)
    # 必须超过 MEDIA_TTL_SECONDS（默认 172800 = 2 天）：sweep_forever 用默认 TTL。
    old = time.time() - (media_mod.MEDIA_TTL_SECONDS + 60)
    os.utime(ref.path, (old, old))

    task = asyncio.create_task(media_mod.sweep_forever(0.01, store=store))
    try:
        await asyncio.sleep(0.08)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    snap = observability.snapshot()
    assert snap["counters"]["media_sweep_runs"] >= 1, "清扫时钟必须真的转过"
    assert snap["counters"]["media_sweep_removed"] >= 1, "超龄成品必须被删掉"
    assert not store.exists(saved.token)


async def test_media_sweeper_survives_errors(tmp_path, monkeypatch):
    """一次清扫异常不能让后台任务死掉（磁盘抖动不该永久停摆清理）。"""
    from muse_bridge import media as media_mod

    store = media_mod.MediaStore(tmp_path / "m")
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("磁盘抖了一下")
        return 0

    monkeypatch.setattr(store, "sweep", flaky)
    task = asyncio.create_task(media_mod.sweep_forever(0.01, store=store))
    try:
        await asyncio.sleep(0.09)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert calls["n"] >= 2, "第一次抛异常后必须继续下一轮"
    assert observability.snapshot()["counters"]["media_sweep_runs"] >= 1


async def test_lifespan_starts_and_stops_media_sweeper(monkeypatch, store, tmp_path):
    """lifespan 要建清扫任务，退出时必须收干净（不留悬着的 while True）。"""
    import asyncio as aio

    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(app_module, "store", lambda: store)

    started: list[str] = []
    real_create = aio.create_task

    def spy(coro, *, name=None):
        started.append(name or "")
        return real_create(coro, name=name)

    monkeypatch.setattr(aio, "create_task", spy)
    async with app_module.lifespan(app_module.app):
        pass
    assert "muse-media-sweeper" in started
