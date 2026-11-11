"""VM 重启窗口的窄重试：只对 `status=unavailable + kind=retryable` 生效。

为什么单独钉：`/api/session` 的 200 不代表可用（U-RMH-06）。VM 重启窗口里
上游会回 `unavailable`，此时直接失败会触发一次毫无意义的浏览器回退；
但把**所有**非 assigned 都重试，又会把"cookie 失效/号坏了"拖成 3 秒延迟。
所以这里断言三件事：retryable 重试、非 retryable 不重试、重试耗尽仍抛。
"""

from __future__ import annotations

import pytest

from muse_bridge import protocol_client as pc
from muse_bridge.session_http import SessionUnconfirmed


@pytest.mark.asyncio
async def test_renew_with_retry_retries_retryable_until_success(monkeypatch):
    calls: list[int] = []
    delays: list[float] = []

    async def fake_renew(cookies, *, site, client):
        calls.append(len(calls) + 1)
        if len(calls) <= 2:
            raise SessionUnconfirmed("VM 正在重启", retryable=True)
        return "session-ok"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(pc, "renew_session", fake_renew)
    result = await pc.renew_with_retry(
        {"hatch_sess": "s"}, site="https://muse.ai", client=object(), sleep=fake_sleep
    )
    assert result == "session-ok"
    assert calls == [1, 2, 3]
    assert delays == list(pc.SESSION_UNAVAILABLE_DELAYS)


@pytest.mark.asyncio
async def test_renew_with_retry_does_not_retry_non_retryable(monkeypatch):
    calls: list[int] = []

    async def fake_renew(cookies, *, site, client):
        calls.append(len(calls) + 1)
        raise SessionUnconfirmed("cookie 失效/号坏了", retryable=False)

    async def fake_sleep(delay: float) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError(f"非 retryable 不应 sleep({delay})")

    monkeypatch.setattr(pc, "renew_session", fake_renew)
    with pytest.raises(SessionUnconfirmed):
        await pc.renew_with_retry(
            {"hatch_sess": "s"}, site="https://muse.ai", client=object(), sleep=fake_sleep
        )
    assert calls == [1]


@pytest.mark.asyncio
async def test_renew_with_retry_gives_up_after_budget(monkeypatch):
    calls: list[int] = []

    async def fake_renew(cookies, *, site, client):
        calls.append(len(calls) + 1)
        raise SessionUnconfirmed("还在重启", retryable=True)

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(pc, "renew_session", fake_renew)
    with pytest.raises(SessionUnconfirmed):
        await pc.renew_with_retry(
            {"hatch_sess": "s"}, site="https://muse.ai", client=object(), sleep=fake_sleep
        )
    assert calls == [1, 2, 3], "重试预算 = 2 次，第三次失败必须抛出"
