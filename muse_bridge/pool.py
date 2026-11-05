"""池化调度：挑账号 → 确保浏览器就绪 → 必要时 wake VM → 发问取答。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .driver import BrowserUnavailable, MuseDriver, NotLoggedIn
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


@dataclass
class DispatchResult:
    ok: bool
    account: str = ""
    driver: MuseDriver | None = None
    verdict: str = ""
    note: str = ""


_cursor = {"index": 0}


def _rotate(accounts: list) -> list:
    if not accounts:
        return []
    start = _cursor["index"] % len(accounts)
    _cursor["index"] = start + 1
    return accounts[start:] + accounts[:start]


async def dispatch(store: AccountStore) -> DispatchResult:
    """挑一个可用账号并建立驱动。失败时换下一个。"""
    candidates = store.dispatchable()
    if not candidates:
        raise NoAccountAvailable(
            "账号池里没有可用的 muse 账号。先在 /console 执行 attach。"
        )

    last_error = ""
    for record in _rotate(candidates):
        driver = MuseDriver(record.cdp_url)
        try:
            status = await driver.status()
        except BrowserUnavailable as exc:
            last_error = f"{record.account}: {exc}"
            store.mark_failure(
                record.account,
                state=AccountState.BROWSER_UNAVAILABLE,
                error=str(exc),
            )
            await driver.close()
            continue

        if not status.logged_in:
            last_error = f"{record.account}: 浏览器里 muse.ai 未登录"
            store.mark_failure(
                record.account,
                state=AccountState.SESSION_EXPIRED,
                error=status.note or "未登录",
            )
            await driver.close()
            continue

        # VM 可能休眠，先尝试唤醒（失败不阻断——可能本来就在跑）
        if not record.vm_ready:
            await driver.wake_vm()

        state = after_probe(logged_in=True, now=int(time.time()), vm_ready=True)
        store.mark_ready(record.account, vm_ready=True, state=state)
        return DispatchResult(ok=True, account=record.account, driver=driver)

    return DispatchResult(ok=False, verdict="all_accounts_failed", note=last_error)


async def stream_reply(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> AsyncIterator[str]:
    if not result.ok or result.driver is None:
        return
    try:
        async for delta in result.driver.stream_reply(prompt, timeout=timeout):
            yield delta
    except NotLoggedIn as exc:
        if result.account:
            pass  # 状态由 app 层负责落库
        raise exc


async def collect_reply(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> str:
    parts: list[str] = []
    async for delta in stream_reply(result, prompt, timeout=timeout):
        parts.append(delta)
    return "".join(parts)
