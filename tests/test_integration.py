"""端到端测试：用假 driver 验证链路，不启动真实浏览器。"""

from __future__ import annotations

import time

import pytest

from muse_bridge import pool
from muse_bridge.state import AccountState
from muse_bridge.store import AccountStore


@pytest.fixture(autouse=True)
def clear_dispatch_state():
    """账号锁与轮转游标是模块级的，测试之间必须清干净。"""
    pool._locks.clear()
    pool._cursor["index"] = 0
    yield
    pool._locks.clear()
    pool._cursor["index"] = 0


@pytest.fixture
def bridge_key(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("MUSE_BRIDGE_KEY", key)
    return key


@pytest.fixture
def store(bridge_key, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_BRIDGE_DB", str(tmp_path / "muse.db"))
    instance = AccountStore()
    yield instance
    instance.close()


def test_upsert_and_read(store, tmp_path):
    store.upsert(
        "muse-main",
        cdp_url="http://127.0.0.1:9222",
        state=AccountState.READY,
        label="主号",
        profile={"site": "https://muse.ai/chat"},
        vm_ready=True,
    )
    record = store.get("muse-main")
    assert record is not None
    assert record.state is AccountState.READY
    assert record.vm_ready is True
    assert record.label == "主号"

    # profile 必须加密落盘
    raw = (tmp_path / "muse.db").read_bytes()
    assert b"muse.ai/chat" not in raw


def test_redacted_has_no_profile(store):
    store.upsert(
        "muse-main",
        cdp_url="http://127.0.0.1:9222",
        state=AccountState.READY,
        profile={"secret": "should-not-leak"},
    )
    snapshot = store.redacted()
    assert "should-not-leak" not in str(snapshot)
    assert "profile" not in snapshot[0]


def test_dispatchable_filters_expired(store):
    store.upsert("good", cdp_url="http://127.0.0.1:9222", state=AccountState.READY)
    store.upsert(
        "bad", cdp_url="http://127.0.0.1:9222", state=AccountState.SESSION_EXPIRED
    )
    names = [record.account for record in store.dispatchable()]
    assert names == ["good"]


def test_stale_probe_not_dispatchable(store):
    store.upsert("old", cdp_url="http://127.0.0.1:9222", state=AccountState.READY)
    # 把探针时间推到 20 分钟前
    store._conn.execute(
        "UPDATE accounts SET last_probe_ok_at=? WHERE account=?",
        (int(time.time()) - (20 * 60), "old"),
    )
    store._conn.commit()
    assert store.dispatchable() == []


def test_touch_refresh_updates_timestamp(store):
    store.upsert("muse-main", cdp_url="http://127.0.0.1:9222", state=AccountState.READY)
    before = store.get("muse-main").refreshed_at
    time.sleep(0.01)
    store.touch_refresh("muse-main")
    after = store.get("muse-main").refreshed_at
    assert after is not None
    assert before is None or after >= before


@pytest.mark.asyncio
async def test_dispatch_raises_when_pool_empty(store):
    with pytest.raises(pool.NoAccountAvailable):
        await pool.dispatch(store)


@pytest.mark.asyncio
async def test_dispatch_marks_browser_unavailable(store, monkeypatch):
    """CDP 连不上时应把账号标成 browser_unavailable，而不是崩掉。"""
    from muse_bridge import driver as driver_module

    store.upsert("muse-main", cdp_url="http://127.0.0.1:9999", state=AccountState.READY)

    class FakeDriver:
        def __init__(self, cdp_url):
            self.cdp_url = cdp_url

        async def status(self):
            raise driver_module.BrowserUnavailable("连接失败")

        async def close(self):
            pass

    monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
    result = await pool.dispatch(store)

    assert not result.ok
    assert result.verdict == "all_accounts_failed"
    record = store.get("muse-main")
    assert record.state is AccountState.BROWSER_UNAVAILABLE


@pytest.mark.asyncio
async def test_dispatch_marks_not_logged_in(store, monkeypatch):
    from muse_bridge import driver as driver_module

    store.upsert("muse-main", cdp_url="http://127.0.0.1:9222", state=AccountState.READY)

    class FakeDriver:
        def __init__(self, cdp_url):
            self.cdp_url = cdp_url

        async def status(self):
            return driver_module.BrowserStatus(
                connected=True, logged_in=False, url="https://muse.ai/login"
            )

        async def close(self):
            pass

    monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
    result = await pool.dispatch(store)

    assert not result.ok
    record = store.get("muse-main")
    assert record.state is AccountState.SESSION_EXPIRED


@pytest.mark.asyncio
async def test_dispatch_success_returns_driver(store, monkeypatch):
    from muse_bridge import driver as driver_module

    store.upsert("muse-main", cdp_url="http://127.0.0.1:9222", state=AccountState.READY)

    class FakeDriver:
        def __init__(self, cdp_url):
            self.cdp_url = cdp_url
            self.woken = False

        async def status(self):
            return driver_module.BrowserStatus(
                connected=True, logged_in=True, url="https://muse.ai/chat"
            )

        async def wake_vm(self):
            self.woken = True
            return True

        async def close(self):
            pass

    monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
    result = await pool.dispatch(store)

    assert result.ok
    assert result.account == "muse-main"
    assert result.driver is not None


@pytest.mark.asyncio
async def test_stream_reply_concatenates(store, monkeypatch):
    """流式增量应能拼成完整回答。"""
    store.upsert("muse-main", cdp_url="http://127.0.0.1:9222", state=AccountState.READY)

    class FakeDriver:
        def __init__(self, cdp_url):
            self.cdp_url = cdp_url

        async def status(self):
            from muse_bridge.driver import BrowserStatus

            return BrowserStatus(connected=True, logged_in=True, url="")

        async def wake_vm(self):
            return True

        async def close(self):
            pass

        async def stream_reply(self, prompt, timeout=180.0):
            for piece in ("你好", "，", "世界"):
                yield piece

    monkeypatch.setattr(pool, "MuseDriver", FakeDriver)
    result = await pool.dispatch(store)
    try:
        text = await pool.collect_reply(result, "测试")
    finally:
        await result.release()
    assert text == "你好，世界"


# ---- DOM 差分逻辑 -----------------------------------------------------


def test_monotonic_growth_produces_deltas():
    """验证"单调扩展才计为增量"的核心规则。

    这里直接测逻辑等价物，避免依赖真实浏览器。
    """
    snapshots = ["你", "你好", "你好，", "你好，世", "你好，世界"]
    last = ""
    deltas = []
    for current in snapshots:
        if current.startswith(last):
            delta = current[len(last) :]
        elif current != last:
            delta = current
        else:
            delta = ""
        if delta:
            last = current
            deltas.append(delta)
    assert "".join(deltas) == "你好，世界"
    assert deltas == ["你", "好", "，", "世", "界"]


def test_non_monotonic_rerender_resends_whole():
    """页面重绘导致文本不以旧值开头时，应整体重发而不是丢弃。"""
    last = "你好世界"
    current = "全新的回答"
    delta = current[len(last) :] if current.startswith(last) else current
    assert delta == "全新的回答"
