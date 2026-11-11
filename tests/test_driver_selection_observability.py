"""`_build_driver` 的三种"没走协议"必须分开可数。

2026-11-11 之前只有一个 `browser_driver_built`（且只在 `capability == "chat"` 时计），
运维看到它涨了也分不清是"开关没开"还是"账号没导 cookie"，更看不到协议生成失败后
的主动回退。三个原因处理办法完全不同：

  * `protocol_disabled`   → 整台机器 `MUSE_PROTOCOL_DRIVER=0`（默认），改配置；
  * `protocol_no_cookies` → 该账号没导 cookie，导 cookie + refresh 可修；
  * `browser_driver_forced` → 协议生成失败后的主动回退，不是缺陷。

这里逐条钉住：每种情形只涨对应计数，不误记其他计数。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from muse_bridge import observability, pool


class FakeStore:
    def __init__(self, cookies: list | None) -> None:
        self._cookies = cookies or []

    def cookies(self, account: str):
        return list(self._cookies)


def record():
    return SimpleNamespace(account="muse1", cdp_url="http://127.0.0.1:9222")


@pytest.fixture(autouse=True)
def _clean_counters(monkeypatch):
    observability.reset()
    monkeypatch.delenv("MUSE_PROTOCOL_PROXY", raising=False)
    yield
    observability.reset()


def counters() -> dict:
    return observability.snapshot()["counters"]


def build(monkeypatch, *, capability: str, cookies: list | None):
    """把两个驱动类换成可识别的替身，避免真去连浏览器/协议。"""
    monkeypatch.setattr(pool, "MuseDriver", lambda cdp: SimpleNamespace(kind="browser"))
    monkeypatch.setattr(
        pool, "ProtocolDriver", lambda account, cookie, proxy=None: SimpleNamespace(kind="protocol")
    )
    return pool._build_driver(FakeStore(cookies), record(), capability=capability)


def test_protocol_switch_off_counts_disabled_and_browser(monkeypatch) -> None:
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "0")
    driver = build(monkeypatch, capability="chat", cookies=["hatch_sess=x"])
    assert driver.kind == "browser"
    assert counters()["protocol_disabled"] == 1
    assert counters()["browser_driver_built"] == 1
    assert "protocol_no_cookies" not in counters()


def test_switch_on_without_cookies_counts_no_cookies(monkeypatch) -> None:
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    driver = build(monkeypatch, capability="chat", cookies=[])
    assert driver.kind == "browser"
    assert counters()["protocol_no_cookies"] == 1
    assert counters()["browser_driver_built"] == 1
    # 开关是开的，就不能记成"被关掉"
    assert "protocol_disabled" not in counters()


def test_switch_on_with_cookies_builds_protocol_driver(monkeypatch) -> None:
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    driver = build(monkeypatch, capability="chat", cookies=["hatch_sess=x"])
    assert driver.kind == "protocol"
    assert counters()["protocol_driver_built"] == 1
    assert "browser_driver_built" not in counters()
    assert "protocol_no_cookies" not in counters()


@pytest.mark.parametrize("capability", ["chat", "generation"])
def test_protocol_counts_apply_to_generation_too(monkeypatch, capability) -> None:
    """过去只有 chat 计 browser_driver_built，生成路径上是瞎的。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "0")
    driver = build(monkeypatch, capability=capability, cookies=["hatch_sess=x"])
    assert driver.kind == "browser"
    assert counters()["browser_driver_built"] == 1


def test_forced_browser_fallback_is_its_own_counter(monkeypatch) -> None:
    """协议生成失败后的主动回退：不能记成"协议没生效"，那是误报。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    driver = build(monkeypatch, capability="generation-browser", cookies=["hatch_sess=x"])
    assert driver.kind == "browser"
    assert counters()["browser_driver_forced"] == 1
    assert "protocol_disabled" not in counters()
    assert "protocol_no_cookies" not in counters()
    assert "browser_driver_built" not in counters()


def test_switch_is_read_per_call_not_cached(monkeypatch) -> None:
    """运维能在进程存活期间改开关：两次调用必须按当时的环境判定。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "0")
    assert build(monkeypatch, capability="chat", cookies=["hatch_sess=x"]).kind == "browser"
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    assert build(monkeypatch, capability="chat", cookies=["hatch_sess=x"]).kind == "protocol"
    assert counters()["protocol_disabled"] == 1
    assert counters()["protocol_driver_built"] == 1
