"""`ProtocolDriver` 与暖池的接线：开关关时零变化，开时复用 + 归还引用。

这两点必须分开钉：默认路径一个字节都不能变（现有 50 条驱动测试是那边的人质），
而打开暖池后 `close()` 的语义从"断连接"变成"归还引用"——弄错会把下个回合的
连接提前断掉。
"""

from __future__ import annotations

import pytest

from muse_bridge import protocol_hub
from muse_bridge.protocol_driver import ProtocolDriver
from tests.test_protocol_hub import _factory


@pytest.fixture(autouse=True)
async def _clean_hub():
    await protocol_hub.close_all()
    protocol_hub._SLOTS.clear()
    yield
    await protocol_hub.close_all()
    protocol_hub._SLOTS.clear()


COOKIES = {"hatch_sess": "sess-value-123"}


async def test_warm_pool_off_keeps_dedicated_client(monkeypatch):
    """开关关着：connect() 走自己的 client，池里一条都不该有。"""
    monkeypatch.delenv("MUSE_WARM_POOL", raising=False)
    driver = ProtocolDriver("muse1", COOKIES)

    connected: list[object] = []

    async def fake_connect() -> None:
        connected.append(driver._client)

    driver._client.connect = fake_connect  # type: ignore[method-assign]
    await driver.connect()

    assert connected == [driver._client]
    assert protocol_hub.warm_accounts() == []


async def test_warm_pool_on_reuses_client_across_drivers(monkeypatch):
    """两个回合（两个 driver 实例）拿到的必须是同一条连接。

    预热用 acquire + release：暖池同账号被占用时会拒绝第二次 acquire（不共享
    游标），所以"预热"必须先把引用还回去，模拟上一回合已经结束。
    """
    monkeypatch.setenv("MUSE_WARM_POOL", "1")
    calls: list = []

    shared, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.release("muse1")

    first = ProtocolDriver("muse1", COOKIES)
    await first.connect()
    assert first._client is shared, "第二个回合应命中暖池"

    first._warm_released = False
    await first.close()
    assert shared.closed == 0, "close() 不该断连接"


async def test_warm_pool_close_returns_reference_once(monkeypatch):
    monkeypatch.setenv("MUSE_WARM_POOL", "1")
    calls: list = []
    shared, _ = await protocol_hub.acquire("muse1", COOKIES, factory=_factory(calls))
    await protocol_hub.release("muse1")

    driver = ProtocolDriver("muse1", COOKIES)
    await driver.connect()
    assert driver._client is shared

    slot = protocol_hub._SLOTS["muse1"]
    before = slot.refs
    await driver.close()
    await driver.close()  # 重复调用不能把别人的引用减掉
    assert slot.refs == before - 1


async def test_warm_pool_failure_falls_back_to_error(monkeypatch):
    """暖池取连接失败必须原样抛给 pool（由它决定回退浏览器），不能吞。"""
    monkeypatch.setenv("MUSE_WARM_POOL", "1")

    async def boom(*args, **kwargs):
        raise OSError("握手失败")

    monkeypatch.setattr(protocol_hub, "acquire", boom)
    monkeypatch.setattr("muse_bridge.protocol_driver.warm_acquire", boom)

    driver = ProtocolDriver("muse1", COOKIES)
    with pytest.raises(OSError):
        await driver.connect()
