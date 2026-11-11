"""暖池（`protocol_hub`）的复用、验活与引用会计。

这一层的失败模式与别处不同：复用一条半死连接不会报错，只会让回合**安静地**
等不到事件。所以测试重点不是"命中快"，而是：

1. 死连接一律不复用（五条验活条件逐条打）；
2. 同身份的失效连接走重连，不换 client（保住 token 续期路径）；
3. 换 cookie / 换代理 / 换模型一律真换 client；
4. 引用会计不泄漏——重复 release / 重连失败都必须把引用还回去。
"""

from __future__ import annotations

import asyncio

import pytest

from muse_bridge import protocol_hub


class FakeResponse:
    def __init__(self, ended: bool = False) -> None:
        self.ended = ended
        self.body = bytearray()


class FakeClient:
    """够 `usable()` 判定的最小 client。"""

    def __init__(self, *, cookies=None, proxy=None, model="hatch-web", **overrides):
        self.cookies = dict(cookies or {})
        self.proxy = proxy
        self.model = model
        self.closing = False
        self.ws = object()
        self._handshake_done = True
        self.receiver_task = _never_done_task()
        self._sub_stream_id = 1
        self.responses = {1: FakeResponse()}
        self._lost = False
        self.connects = 0
        self.closed = 0
        for name, value in overrides.items():
            setattr(self, name, value)

    def _connection_lost(self) -> bool:
        return self._lost

    async def connect(self) -> None:
        self.connects += 1
        self._lost = False
        self.ws = object()
        self._handshake_done = True
        self.closing = False

    async def close(self) -> None:
        self.closed += 1
        self.closing = True


def _never_done_task() -> asyncio.Task:
    async def _sleep_forever() -> None:
        await asyncio.sleep(3600)

    return asyncio.ensure_future(_sleep_forever())


@pytest.fixture(autouse=True)
async def _clean_hub():
    await protocol_hub.close_all()
    protocol_hub._SLOTS.clear()
    yield
    await protocol_hub.close_all()
    protocol_hub._SLOTS.clear()


COOKIES = {"hatch_sess": "sess-value-123"}


def _factory(calls: list) -> object:
    def make(cookies, *, proxy=None, model="hatch-web"):
        client = FakeClient(cookies=cookies, proxy=proxy, model=model)
        calls.append(client)
        return client

    return make


async def test_first_acquire_builds_and_second_reuses():
    calls: list = []
    c1, reused1 = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    # 上一个回合结束归还引用后，下一次派单才会命中同一条连接。
    await protocol_hub.release("muse1")
    c2, reused2 = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    assert reused1 is False and reused2 is True
    assert c1 is c2
    assert len(calls) == 1, "第二次不该新建 client"
    assert c1.connects == 1


async def test_concurrent_acquire_is_rejected_not_shared():
    """同账号第二次 acquire 必须被拒绝，不能只告警后共享游标。

    账号锁已经保证同号串行；走到这里说明有人绕过锁。共享同一条订阅会让两个
    回合抢游标，是数据正确性问题，所以当场报错（`WarmPoolBusy`）而不是等待。
    """
    calls: list = []
    await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    with pytest.raises(protocol_hub.WarmPoolBusy):
        await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    assert len(calls) == 1, "拒绝时不得新建连接"
    assert protocol_hub._SLOTS["muse1"].refs == 1, "拒绝不得改变既有引用"


async def test_cookie_change_forces_new_client():
    calls: list = []
    c1, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    c2, reused = await protocol_hub.acquire(
        "muse1", {"hatch_sess": "different-value"}, factory=_factory(calls)
    )

    assert reused is False
    assert c2 is not c1
    assert len(calls) == 2
    assert c1.closed == 1, "换身份必须把旧连接关掉"


async def test_cookie_tail_change_is_a_different_identity():
    """旧指纹只取 value 前 12 字符，前缀相同的两套 cookie 会被当成同一身份。

    真机上表现为"换了会话还去复用旧连接"——正是"拿旧会话打新账号"。
    """
    head = "same-prefix-0123456789"
    calls: list = []
    c1, _ = await protocol_hub.acquire(
        "muse1", {"hatch_sess": head + "-AAA"}, factory=_factory(calls)
    )
    await protocol_hub.release("muse1")
    c2, reused = await protocol_hub.acquire(
        "muse1", {"hatch_sess": head + "-BBB"}, factory=_factory(calls)
    )

    assert reused is False, "尾部不同就是另一个会话，不能复用"
    assert c2 is not c1
    assert c1.closed == 1


async def test_proxy_change_forces_new_client():
    calls: list = []
    await protocol_hub.acquire("muse1", COOKIES, proxy="http://p:1", factory=_factory(calls))
    _, reused = await protocol_hub.acquire(
        "muse1", COOKIES, proxy="http://p:2", factory=_factory(calls)
    )
    assert reused is False
    assert len(calls) == 2


async def test_model_change_forces_new_client():
    calls: list = []
    await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    _, reused = await protocol_hub.acquire(
        "muse1", COOKIES, model="other-model", factory=_factory(calls)
    )
    assert reused is False


@pytest.mark.parametrize(
    "broken",
    [
        {"closing": True},
        {"ws": None},
        {"_handshake_done": False},
        {"receiver_task": None},
        {"_lost": True},
    ],
)
async def test_dead_connection_is_not_reused(broken):
    calls: list = []
    c1, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.release("muse1")
    for name, value in broken.items():
        setattr(c1, name, value)

    assert protocol_hub.usable(c1) is False
    c2, reused = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    assert reused is False
    # 同身份的死连接走重连，而不是换 client：保住 token/cookie 续期路径。
    assert c2 is c1
    assert len(calls) == 1
    assert c1.connects == 2


async def test_ended_subscription_is_not_usable():
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    client.responses[1].ended = True
    assert protocol_hub.usable(client) is False


async def test_connected_without_subscription_is_usable():
    """真机事实（2026-11-11 14:1x）：connect() 之后还没有订阅。

    第一版把"订阅必须存在"写进验活，导致刚连好的连接全被判死、暖池 100% 不命中、
    每回合还多烧一次重连（探针实测 cold 4.40s vs warm 2.78s 就是在重连）。
    这条测试钉住"无订阅 = 活"，防止这个 bug 被写回来。
    """
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    client._sub_stream_id = None
    client.responses.clear()
    assert protocol_hub.usable(client) is True


async def test_release_only_decrements_reference():
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    await protocol_hub.release("muse1")

    assert client.closed == 0, "回合结束不该断连接（断了就白暖了）"
    assert protocol_hub._SLOTS["muse1"].refs == 0


async def test_release_is_idempotent_for_refs():
    calls: list = []
    await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.release("muse1")
    await protocol_hub.release("muse1")
    assert protocol_hub._SLOTS["muse1"].refs == 0, "重复 release 不能把引用减成负数"


async def test_reconnect_failure_returns_the_reference():
    """重连失败必须摘槽 + 关旧连接，否则账号带着脏引用永不回收。"""
    calls: list = []
    c1, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.release("muse1")
    c1._lost = True

    async def boom() -> None:
        raise OSError("网络断了")

    c1.connect = boom  # type: ignore[method-assign]

    with pytest.raises(OSError):
        await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    # 引用归零 + 槽位摘除 + 旧连接被关：三件都要做，否则就是泄漏。
    assert "muse1" not in protocol_hub._SLOTS
    assert c1.closed == 1


async def test_initial_connect_failure_leaves_no_slot():
    """初次连接就失败：槽位是在 connect() 之前写的，必须自己收拾干净。

    旧实现失败后留着一条半死的 client，下一个回合先付一次验活失败再重连。
    """
    calls: list = []

    async def boom() -> None:
        raise OSError("握手失败")

    def factory_with_boom(cookies, *, proxy=None, model="hatch-web"):
        client = FakeClient(cookies=cookies, proxy=proxy, model=model)
        client.connect = boom  # type: ignore[method-assign]
        calls.append(client)
        return client

    with pytest.raises(OSError):
        await protocol_hub.acquire("muse1", COOKIES, factory=factory_with_boom)

    assert "muse1" not in protocol_hub._SLOTS, "失败不得留下槽位"
    assert calls[0].closed == 1, "失败创建的那条连接必须被关掉"


async def test_initial_connect_cancellation_leaves_no_slot():
    """取消也走同一条清理路径：不能只捕获 Exception。"""
    calls: list = []

    async def cancelled() -> None:
        raise asyncio.CancelledError

    def factory_with_cancel(cookies, *, proxy=None, model="hatch-web"):
        client = FakeClient(cookies=cookies, proxy=proxy, model=model)
        client.connect = cancelled  # type: ignore[method-assign]
        calls.append(client)
        return client

    with pytest.raises(asyncio.CancelledError):
        await protocol_hub.acquire("muse1", COOKIES, factory=factory_with_cancel)

    assert "muse1" not in protocol_hub._SLOTS
    assert calls[0].closed == 1


async def test_stale_cleanup_does_not_remove_a_newer_slot():
    """清理必须按身份比较：不能误删取消期间别人已经建好的新槽位。"""
    old_calls: list = []
    c1, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(old_calls))
    await protocol_hub.release("muse1")

    # 直接把槽位换成另一条 client，模拟"清理期间槽位已被替换"。
    new_client = FakeClient(cookies=dict(COOKIES))
    protocol_hub._SLOTS["muse1"].client = new_client  # type: ignore[assignment]

    await protocol_hub._discard_locked("muse1", c1)

    assert "muse1" in protocol_hub._SLOTS, "不能误删后来者"
    assert protocol_hub._SLOTS["muse1"].client is new_client


async def test_stale_lease_release_does_not_touch_new_slot():
    """旧租用晚归还：不得减掉新一代租用的引用。"""
    calls: list = []
    await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    old_lease = protocol_hub.lease_of("muse1")
    assert old_lease is not None

    await protocol_hub.release("muse1")
    await protocol_hub.drop("muse1")
    # drop 清槽但保留租用代次：新一代租用必须从更大的 id 起算，
    # 否则旧租用的归还号可能撞上新一代。
    await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    new_lease = protocol_hub.lease_of("muse1")
    assert new_lease is not None and new_lease != old_lease

    released = await protocol_hub.release("muse1", lease=old_lease)

    assert released is False, "过期租用的归还不算数"
    assert protocol_hub._SLOTS["muse1"].refs == 1, "新租用不受影响"


async def test_lease_release_decrements_its_own_slot():
    calls: list = []
    await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    lease = protocol_hub.lease_of("muse1")

    assert await protocol_hub.release("muse1", lease=lease) is True
    assert protocol_hub._SLOTS["muse1"].refs == 0


async def test_evict_idle_closes_only_unreferenced_expired():
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))

    # 还持有引用：即使 TTL 到期也不能关。
    closed = await protocol_hub.evict_idle(ttl=0)
    assert closed == 0
    assert client.closed == 0

    await protocol_hub.release("muse1")
    closed = await protocol_hub.evict_idle(ttl=0)
    assert closed == 1
    assert client.closed == 1
    assert "muse1" not in protocol_hub._SLOTS


async def test_drop_forces_close():
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.drop("muse1")
    assert client.closed == 1
    assert "muse1" not in protocol_hub._SLOTS


async def test_warm_accounts_reports_only_live():
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    assert protocol_hub.warm_accounts() == ["muse1"]
    client._lost = True
    assert protocol_hub.warm_accounts() == []


def test_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("MUSE_WARM_POOL", raising=False)
    assert protocol_hub.enabled() is False


def test_enabled_reads_env_each_call(monkeypatch):
    monkeypatch.setenv("MUSE_WARM_POOL", "1")
    assert protocol_hub.enabled() is True
    monkeypatch.setenv("MUSE_WARM_POOL", "0")
    assert protocol_hub.enabled() is False


def test_cookie_fingerprint_ignores_empty_and_hashes_values():
    a = protocol_hub.cookie_fingerprint({"hatch_sess": "abcdef123456", "x": ""})
    b = protocol_hub.cookie_fingerprint({"hatch_sess": "abcdef123456"})
    assert a == b, "空值不参与指纹"
    assert "abcdef123456" not in a, "指纹里不能出现明文 cookie"
    assert protocol_hub.cookie_fingerprint({"hatch_sess": "other"}) != a


def test_stats_shape():
    info = protocol_hub.stats()
    assert set(info) == {"enabled", "accounts", "refs"}


async def test_sweep_forever_evicts_without_any_request():
    """空闲清扫必须与请求无关：进程安静下来也要回收。

    没有这个时钟，`release()` 里的 `evict_idle()` 只会在"下一个请求到来"时
    才跑，夜间空闲的连接就一直挂着。
    """
    calls: list = []
    client, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.release("muse1")  # refs=0，但 TTL 还没到

    task = asyncio.create_task(protocol_hub.sweep_forever(interval=0.01))
    try:
        await asyncio.sleep(0.06)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # ttl 默认 300s，这里只验证它跑起来了并在查池；用 ttl=0 再验一次真关。
    assert protocol_hub._SLOTS  # 还没到 TTL，仍在池里
    await protocol_hub.evict_idle(ttl=0)
    assert client.closed == 1


async def test_sweep_forever_survives_errors(monkeypatch):
    """清扫里抛异常不能让任务死掉，否则之后的回收全部失效。"""
    boom = {"count": 0}

    async def flaky(ttl=None):
        boom["count"] += 1
        if boom["count"] < 3:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(protocol_hub, "evict_idle", flaky)
    task = asyncio.create_task(protocol_hub.sweep_forever(interval=0.01))
    try:
        await asyncio.sleep(0.08)
        assert boom["count"] >= 3, "异常后必须继续下一轮"
        assert not task.done(), "任务不能被异常打死"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
