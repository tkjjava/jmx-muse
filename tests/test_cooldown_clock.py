"""冷却时钟必须锚定在失败发生的那一刻，而不是"行最后被写"的那一刻。

旧实现把 updated_at 当 last_failure_at 用，而 upsert / mark_ready / touch_refresh
都会碰 updated_at。state.py:26 的契约是"冷却期满自动回池"——任何无关写入
（比如计划中的 30 分钟心跳）都会把回池无限推迟。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from muse_bridge import store as store_mod
from muse_bridge.state import AccountState

T0 = 1_800_000_000
CDP = "http://127.0.0.1:9222"


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    clock = [T0]
    monkeypatch.setattr(store_mod, "time", SimpleNamespace(time=lambda: clock[0]))
    return clock


def _limited(s: store_mod.AccountStore) -> None:
    s.upsert("a", cdp_url=CDP, state=AccountState.READY)
    s.mark_failure("a", state=AccountState.RATE_LIMITED, error="429")


def test_touch_refresh_does_not_restart_cooldown(tmp_path, clock):
    """无关写入不得重置冷却时钟。修复前：updated_at 被碰，回池被推迟。"""
    s = store_mod.AccountStore(tmp_path / "a.db")
    _limited(s)
    clock[0] = T0 + 30
    s.touch_refresh("a")  # 与失败无关的行写入
    clock[0] = T0 + 61  # 默认冷却 60 秒已过
    assert "a" in [r.account for r in s.dispatchable()], (
        "touch_refresh 重置了冷却时钟，账号没能按期回池"
    )
    s.close()


def test_repeated_touches_cannot_hold_account_out_of_pool(tmp_path, clock):
    """连续无关写入也不能把账号永久摁在池外。"""
    s = store_mod.AccountStore(tmp_path / "a.db")
    _limited(s)
    for _ in range(4):
        clock[0] += 30
        s.touch_refresh("a")
    clock[0] = T0 + 121
    assert "a" in [r.account for r in s.dispatchable()]
    s.close()


def test_upsert_keeps_recorded_failure(tmp_path, clock):
    """upsert 保留已记录的失败时刻，并且保证 retryable 行不为 NULL。"""
    s = store_mod.AccountStore(tmp_path / "a.db")
    _limited(s)
    clock[0] = T0 + 40
    s.upsert("a", cdp_url=CDP, state=AccountState.RATE_LIMITED)
    record = s.get("a")
    assert record.last_failure_at == T0, "upsert 重置了失败时刻"
    clock[0] = T0 + 61
    assert "a" in [r.account for r in s.dispatchable()]
    s.close()


def test_cooldown_clock_survives_restart(tmp_path, clock):
    """冷却在库里，不在内存里：重开存储后时钟原样可用。"""
    path = tmp_path / "a.db"
    s = store_mod.AccountStore(path)
    _limited(s)
    s.close()
    s2 = store_mod.AccountStore(path)
    clock[0] = T0 + 61
    assert "a" in [r.account for r in s2.dispatchable()]
    s2.close()


def test_old_schema_rows_are_backfilled(tmp_path, clock):
    """老库没有这一列：打开时自动增列，并用 updated_at 回填（旧时钟语义）。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE accounts (
            account TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, cdp_url TEXT NOT NULL, profile_enc BLOB,
            vm_ready INTEGER NOT NULL DEFAULT 0, refreshed_at INTEGER,
            last_probe_ok_at INTEGER, last_error TEXT,
            probe_attempts INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO accounts (account,state,cdp_url,created_at,updated_at)"
        " VALUES ('legacy','rate_limited',?, ?, ?)",
        (CDP, T0, T0),
    )
    conn.commit()
    conn.close()

    s = store_mod.AccountStore(db)
    record = s.get("legacy")
    assert record.last_failure_at == T0, "回填没有沿用旧实现的时钟"
    clock[0] = T0 + 61
    assert "legacy" in [r.account for r in s.dispatchable()]
    s.close()
