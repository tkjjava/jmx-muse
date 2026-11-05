"""账号状态机。与 hark-bridge 同构，但 muse 特有 VM 生命周期。

muse 的会话状态多两个概念：
  * VM 是否已唤醒（云端实例可能休眠）
  * hatch_vml 的 48 小时过期时钟

因此状态之外还要记录 vm_ready 与 refreshed_at。
"""

from __future__ import annotations

from enum import Enum

# 探针超过这个时间不再信任
PROBE_TTL_SECONDS = 15 * 60

# hatch_vml 约 48 小时过期，提前 6 小时提醒续期
VML_WARN_SECONDS = 6 * 3600

# 心跳间隔：每 30 分钟打一次 /api/session 续期
HEARTBEAT_INTERVAL_SECONDS = 30 * 60


class AccountState(str, Enum):
    UNVERIFIED = "unverified"
    READY = "ready"  # 已连接且已登录
    VM_SLEEPING = "vm_sleeping"  # VM 休眠，需要 wake
    VML_EXPIRING = "vml_expiring"  # hatch_vml 快过期
    SESSION_EXPIRED = "session_expired"
    NEEDS_INTERACTIVE_VERIFICATION = "needs_interactive_verification"
    BLOCKED = "blocked"
    CREDENTIALS_REJECTED = "credentials_rejected"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    RATE_LIMITED = "rate_limited"


TERMINAL_STATES = frozenset(
    {
        AccountState.NEEDS_INTERACTIVE_VERIFICATION,
        AccountState.BLOCKED,
        AccountState.CREDENTIALS_REJECTED,
    }
)

DISPATCHABLE_STATES = frozenset(
    {AccountState.READY, AccountState.VM_SLEEPING, AccountState.VML_EXPIRING}
)


def is_dispatchable(
    state: AccountState,
    *,
    now: int,
    last_probe_ok_at: int | None,
) -> bool:
    """派单闸门。

    muse 的会话不存在"到期时间"这个字段可读（前端不暴露），
    因此只靠探针新鲜度判定。VM_SLEEPING 允许派单——派单前会先 wake。
    """
    if state not in DISPATCHABLE_STATES:
        return False
    if last_probe_ok_at is None:
        return False
    return now - last_probe_ok_at <= PROBE_TTL_SECONDS


def after_probe(*, logged_in: bool, now: int, vm_ready: bool) -> AccountState:
    if not logged_in:
        return AccountState.SESSION_EXPIRED
    if not vm_ready:
        return AccountState.VM_SLEEPING
    return AccountState.READY


def needs_vml_refresh(*, now: int, refreshed_at: int | None) -> bool:
    """hatch_vml 是否快过期。48 小时窗口减去提醒余量。"""
    if refreshed_at is None:
        return True
    elapsed = now - refreshed_at
    return elapsed >= (48 * 3600 - VML_WARN_SECONDS)
