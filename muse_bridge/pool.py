"""池化调度：挑账号 → 确保浏览器就绪 → 必要时 wake VM → 发问取答。

并发模型：同一账号（同一个 Chrome 窗口）在同一时刻只允许一个请求。
两个请求同时往同一个输入框打字会让 DOM 互相污染，回复也分不清归属。

锁的生命周期覆盖整个请求（含流式输出），所以锁随 DispatchResult 交出去，
由调用方在收尾时通过 release() 释放。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .driver import BrowserUnavailable, MuseDriver
from .state import AccountState, after_probe
from .store import AccountStore

MODEL_ALIASES = frozenset(
    {
        "muse-1",
        "muse",
        "muse-spark-1.3",
        "gpt-4o",
        "gpt-4o-mini",
        "claude-3-5-sonnet",
        "claude-opus-4",
        "gemini-pro",
    }
)
DEFAULT_MODEL = "muse-1"

# muse 是单一 agent，任何模型别名都路由到它
UPSTREAM_MODEL = "muse-spark-1.3"


class NoAccountAvailable(RuntimeError):
    pass


class AllAccountsBusy(RuntimeError):
    """所有账号都在处理请求中。muse 只能串行，这是正常的容量限制。"""


@dataclass
class DispatchResult:
    ok: bool
    account: str = ""
    driver: MuseDriver | None = None
    verdict: str = ""
    note: str = ""
    # 该账号的互斥锁。调用方必须在请求结束时调用 release()。
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    async def release(self) -> None:
        """收尾：关闭驱动连接并释放账号锁。

        只断开 CDP，不关闭用户的 Chrome。
        """
        try:
            if self.driver is not None:
                await self.driver.close()
        finally:
            if self._lock is not None and self._lock.locked():
                self._lock.release()
            self._lock = None


_cursor = {"index": 0}

# 账号 → 互斥锁。模块级是刻意的：同一进程内所有请求共享这份调度状态。
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(account: str) -> asyncio.Lock:
    lock = _locks.get(account)
    if lock is None:
        lock = asyncio.Lock()
        _locks[account] = lock
    return lock


def busy_accounts() -> list[str]:
    """当前正在处理请求的账号（给 /healthz 与排障用）。"""
    return [account for account, lock in _locks.items() if lock.locked()]


def _rotate(accounts: list) -> list:
    if not accounts:
        return []
    start = _cursor["index"] % len(accounts)
    _cursor["index"] = start + 1
    return accounts[start:] + accounts[:start]


async def dispatch(store: AccountStore) -> DispatchResult:
    """挑一个可用账号并建立驱动。失败时换下一个。

    同一账号（同一个浏览器窗口）同一时刻只允许一个请求——两个请求
    同时往同一个输入框打逐字，DOM 会互相污染，回复也分不清是谁的。
    """
    candidates = store.dispatchable()
    if not candidates:
        raise NoAccountAvailable(
            "账号池里没有可用的 muse 账号。先在 /console 执行 attach。"
        )

    last_error = ""
    busy: list[str] = []

    for record in _rotate(candidates):
        account = record.account

        lock = _lock_for(account)
        if lock.locked():
            # 这个号正忙，换下一个；全部忙则明确告知
            busy.append(account)
            continue

        # 非阻塞抢锁：抢到才是我的，抢不到就当忙处理
        await lock.acquire()
        driver = MuseDriver(record.cdp_url)
        try:
            status = await driver.status()
        except BrowserUnavailable as exc:
            last_error = f"{account}: {exc}"
            store.mark_failure(
                account,
                state=AccountState.BROWSER_UNAVAILABLE,
                error=str(exc),
            )
            await driver.close()
            lock.release()
            continue

        if not status.logged_in:
            last_error = f"{account}: 浏览器里 muse.ai 未登录"
            store.mark_failure(
                account,
                state=AccountState.SESSION_EXPIRED,
                error=status.note or "未登录",
            )
            await driver.close()
            lock.release()
            continue

        # VM 可能休眠，先尝试唤醒（失败不阻断——可能本来就在跑）
        if not record.vm_ready:
            await driver.wake_vm()

        state = after_probe(logged_in=True, now=int(time.time()), vm_ready=True)
        store.mark_ready(account, vm_ready=True, state=state)
        # 锁不在这里释放——调用方在整个请求结束后通过 release() 释放
        return DispatchResult(
            ok=True,
            account=account,
            driver=driver,
            _lock=lock,
        )

    if busy and not last_error:
        raise AllAccountsBusy(
            f"所有可用账号都在处理请求中（{len(busy)} 个）。"
            "muse 只能串行，请稍后重试。"
        )

    return DispatchResult(ok=False, verdict="all_accounts_failed", note=last_error)


async def stream_reply(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> AsyncIterator[str]:
    """流式产出增量。

    不负责关闭驱动或释放锁——那是调用方 release() 的事，
    这样锁能覆盖整个流式输出期间。
    """
    if not result.ok or result.driver is None:
        return
    async for delta in result.driver.stream_reply(prompt, timeout=timeout):
        yield delta


async def collect_reply(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> str:
    parts: list[str] = []
    async for delta in stream_reply(result, prompt, timeout=timeout):
        parts.append(delta)
    return "".join(parts)
