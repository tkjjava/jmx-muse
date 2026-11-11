"""核心逻辑测试。不连接真实浏览器，全部用假对象。"""

from __future__ import annotations

import time

import pytest

from muse_bridge import pool, selectors
from muse_bridge.state import (
    HEARTBEAT_INTERVAL_SECONDS,
    RETRY_COOLDOWN_SECONDS,
    VML_WARN_SECONDS,
    AccountState,
    after_probe,
    is_dispatchable,
    needs_vml_refresh,
)

# ---- 选择器 -----------------------------------------------------------


def test_first_match_builds_is_selector():
    result = selectors.first_match(("a", "b"))
    assert result == ":is(a, b)"


def test_first_match_rejects_empty():
    with pytest.raises(ValueError):
        selectors.first_match(())


def test_selector_tables_are_non_empty():
    for name in (
        "LOGGED_IN_MARKERS",
        "COMPOSER_INPUTS",
        "SEND_BUTTONS",
        "ASSISTANT_MESSAGES",
        "STREAMING_MARKERS",
    ):
        table = getattr(selectors, name)
        assert isinstance(table, tuple)
        assert len(table) > 0, f"{name} 不能为空"


# ---- 状态机 -----------------------------------------------------------


def test_dispatchable_ready():
    now = int(time.time())
    assert is_dispatchable(AccountState.READY, now=now, last_probe_ok_at=now - 60)


def test_dispatchable_requires_fresh_probe():
    now = int(time.time())
    assert not is_dispatchable(
        AccountState.READY, now=now, last_probe_ok_at=now - (16 * 60)
    )
    assert not is_dispatchable(AccountState.READY, now=now, last_probe_ok_at=None)


@pytest.mark.parametrize(
    "state",
    [
        AccountState.BLOCKED,
        AccountState.CREDENTIALS_REJECTED,
        AccountState.SESSION_EXPIRED,
        AccountState.UNVERIFIED,
    ],
)
def test_terminal_states_not_dispatchable(state):
    now = int(time.time())
    assert not is_dispatchable(state, now=now, last_probe_ok_at=now)


def test_vm_sleeping_still_dispatchable():
    """VM 休眠可以派单——派单前会先 wake。"""
    now = int(time.time())
    assert is_dispatchable(AccountState.VM_SLEEPING, now=now, last_probe_ok_at=now - 30)


def test_transient_states_return_after_cooldown():
    """限流 / 浏览器暂时拿不到不该永久踢号：冷却期满自动回池。"""
    now = int(time.time())
    for state in (AccountState.RATE_LIMITED, AccountState.BROWSER_UNAVAILABLE):
        assert not is_dispatchable(
            state, now=now, last_probe_ok_at=now - 60, last_failure_at=now
        )
        assert not is_dispatchable(
            state,
            now=now + RETRY_COOLDOWN_SECONDS - 1,
            last_probe_ok_at=now - 60,
            last_failure_at=now,
        )
        assert is_dispatchable(
            state,
            now=now + RETRY_COOLDOWN_SECONDS,
            last_probe_ok_at=now - 60,
            last_failure_at=now,
        )


def test_transient_without_failure_time_stays_out():
    """没有失败时间戳就无从判断冷却，宁可先不派单。"""
    now = int(time.time())
    assert not is_dispatchable(
        AccountState.RATE_LIMITED, now=now, last_probe_ok_at=now, last_failure_at=None
    )


def test_session_expired_never_auto_recovers():
    """会话真失效需要人工重新登录，冷却不能把它放回来。"""
    now = int(time.time())
    assert not is_dispatchable(
        AccountState.SESSION_EXPIRED,
        now=now + 10 * RETRY_COOLDOWN_SECONDS,
        last_probe_ok_at=now,
        last_failure_at=now,
    )


def test_after_probe_maps_states():
    now = int(time.time())
    assert after_probe(logged_in=True, now=now, vm_ready=True) is AccountState.READY
    assert after_probe(logged_in=True, now=now, vm_ready=False) is (
        AccountState.VM_SLEEPING
    )
    assert after_probe(logged_in=False, now=now, vm_ready=True) is (
        AccountState.SESSION_EXPIRED
    )


def test_vml_refresh_window():
    now = int(time.time())
    # 刚续期过——不需要刷新
    assert not needs_vml_refresh(now=now, refreshed_at=now - 60)
    # 从未续期——需要
    assert needs_vml_refresh(now=now, refreshed_at=None)
    # 超过 42 小时（48-6）——进入提醒窗口
    assert needs_vml_refresh(now=now, refreshed_at=now - (43 * 3600))


def test_heartbeat_shorter_than_refresh_window():
    """心跳间隔必须明显短于 48 小时刷新窗口，否则续不上。"""
    assert HEARTBEAT_INTERVAL_SECONDS < (48 * 3600 - VML_WARN_SECONDS)


# ---- 模型映射 ---------------------------------------------------------


def test_model_aliases_map_to_single_upstream():
    """muse 只有一个 agent，所有别名必须指向同一个上游模型。"""
    assert pool.UPSTREAM_MODEL == "muse-spark-1.3"
    assert pool.DEFAULT_MODEL in pool.MODEL_ALIASES
    assert "gpt-4o" in pool.MODEL_ALIASES
    assert "claude-opus-4" in pool.MODEL_ALIASES


def test_rotation_spreads_across_accounts():
    accounts = ["a", "b", "c"]
    pool._cursor["index"] = 0
    seen = []
    for _ in range(3):
        seen.append(pool._rotate(accounts)[0])
    assert seen == ["a", "b", "c"]
