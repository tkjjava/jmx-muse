"""思考透传（MUSE_REASONING，默认关闭）的行为验证。

覆盖四层：driver 事件（ON 交错 / OFF 短路）、pool 包装（字符串 driver 退回包装、
monkeypatch 契约）、app SSE（reasoning_content 帧出现/不出现）。开关是模块级常量，
测试里用 monkeypatch.setattr(muse_bridge.driver, "REASONING", ...) 动态控制。
"""
import json

import pytest

from muse_bridge import app as app_module
from muse_bridge import driver as driver_mod
from muse_bridge import pool as pool_mod


class FakeLocator:
    def __init__(self, text):
        self._text = text

    async def count(self):
        return 1

    def nth(self, index):
        return self

    async def inner_text(self):
        return self._text


class BrokenLocator:
    async def count(self):
        raise RuntimeError("boom")


class FakePage:
    """_reasoning_text 会按顺序查询选择器；每个命中的容器给一段文本。

    None 表示该选择器的 count() 查询失败（对齐真实语义：失败发生在查询阶段，
    locator() 构造本身不会抛——真实实现里 locator() 也在 try 之外）。
    """

    def __init__(self, hits):
        self._hits = hits

    def locator(self, selector):
        hit = self._hits.pop(0) if self._hits else None
        return BrokenLocator() if hit is None else FakeLocator(hit)


def make_driver(page):
    d = driver_mod.MuseDriver.__new__(driver_mod.MuseDriver)
    d._page = page
    return d


@pytest.mark.asyncio
async def test_reasoning_text_reads_first_hit(monkeypatch):
    monkeypatch.setattr(driver_mod, "REASONING", True)
    d = make_driver(FakePage([None, "思考内容"]))
    assert await d._reasoning_text() == "思考内容"


@pytest.mark.asyncio
async def test_reasoning_text_off_short_circuits(monkeypatch):
    monkeypatch.setattr(driver_mod, "REASONING", False)
    d = make_driver(FakePage([Exception]))  # 若被查询会抛——OFF 时根本不该碰
    assert await d._reasoning_text() == ""


class EventsDriver:
    async def stream_reply_events(self, prompt, timeout=180.0):
        yield {"reasoning": "想想"}
        yield {"content": "答案"}


class StringOnlyDriver:
    async def stream_reply(self, prompt, timeout=180.0):
        yield "答案"


class FakeResult:
    ok = True
    driver = None


@pytest.mark.asyncio
async def test_pool_passes_through_native_events():
    r = FakeResult()
    r.driver = EventsDriver()
    got = [e async for e in pool_mod.stream_reply_events(r, "p")]
    assert got == [{"reasoning": "想想"}, {"content": "答案"}]


@pytest.mark.asyncio
async def test_pool_wraps_string_only_driver(monkeypatch):
    async def fake_stream(result, prompt, timeout=180.0):
        yield "答案"

    monkeypatch.setattr(pool_mod, "stream_reply", fake_stream)
    r = FakeResult()
    r.driver = StringOnlyDriver()  # 有 stream_reply 没有 events——但 events 路径优先
    r.driver = None                # 没有 driver → 走包装路径
    got = [e async for e in pool_mod.stream_reply_events(r, "p")]
    assert got == [{"content": "答案"}]


def _sse_frames(text):
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[len("data: "):]))
    return out


@pytest.mark.asyncio
async def test_app_streams_reasoning_content(monkeypatch):
    async def fake_events(result, prompt, timeout=180.0):
        yield {"reasoning": "想想"}
        yield {"content": "答案"}

    async def fake_dispatch(store):
        return pool_mod.DispatchResult(ok=True, account="acc", driver=None)

    monkeypatch.setattr(pool_mod, "stream_reply_events", fake_events)
    monkeypatch.setattr(pool_mod, "dispatch", fake_dispatch)
    monkeypatch.setattr(app_module, "store", lambda: None)
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "hi"}],
                  "stream": True, "stream_options": {"include_usage": True}},
        )
    frames = [f for f in _sse_frames(resp.text) if f.get("choices")]
    deltas = [f["choices"][0]["delta"] for f in frames if f["choices"][0].get("delta")]
    assert {"reasoning_content": "想想"} in deltas
    assert {"content": "答案"} in deltas


# ---- 逆向审视修复的回归钉 ----

@pytest.mark.asyncio
async def test_nonstream_response_carries_reasoning_content(monkeypatch):
    async def fake_collect(result, prompt, timeout=180.0):
        return "想想", "答案"

    async def fake_dispatch(store):
        return pool_mod.DispatchResult(ok=True, account="a", driver=None)

    monkeypatch.setattr(pool_mod, "collect_reply_events", fake_collect)
    monkeypatch.setattr(pool_mod, "dispatch", fake_dispatch)
    monkeypatch.setattr(app_module, "store", lambda: None)
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "hi"}]},
        )
    msg = resp.json()["choices"][0]["message"]
    assert msg["content"] == "答案"
    assert msg["reasoning_content"] == "想想"
    # usage 按思考+正文估算：想想(2)+答案(2) = 4 个汉字 token；漏算则只有 2
    usage = resp.json()["usage"]
    assert usage["completion_tokens"] == 4, usage


@pytest.mark.asyncio
async def test_driver_events_timeout_before_start_raises(monkeypatch):
    """整轮没有任何新回答时，事件接口以 TimeoutError 收场（对齐字符串接口语义）。"""
    monkeypatch.setattr(driver_mod, "REASONING", True)

    class TimeoutPage:
        # Playwright 的 `page.locator()` 是**同步**的，只有 `.count()` /
        # `.inner_text()` 等动作是协程。这里写成 `async def locator` 会让实现
        # 拿到一个协程对象而不是 locator，于是每轮查询都抛异常、被实现里的
        # `except Exception: continue` 吞掉——测试仍能通过，但跑起来会刷
        # "coroutine was never awaited" 噪音，且没有真正走到 count()==0 分支。
        def locator(self, selector):
            class L:
                async def count(self):
                    return 0

                def nth(self, i):
                    return self

                async def inner_text(self):
                    return ""

            return L()

    d = make_driver(TimeoutPage())

    async def fake_connect(self):
        return None

    async def fake_send(self, prompt):
        return None

    monkeypatch.setattr(driver_mod.MuseDriver, "connect", fake_connect)
    monkeypatch.setattr(driver_mod.MuseDriver, "_send", fake_send)
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.01)
    with pytest.raises(TimeoutError):
        async for _ in d.stream_reply_events("p", timeout=0.2):
            pass


@pytest.mark.asyncio
async def test_bad_config_returns_explicit_503(monkeypatch):
    """store 的 ConfigError 必须映射为 503+原文案，而不是裸 500。"""
    from muse_bridge.store import ConfigError

    def broken_store():
        raise ConfigError("缺少 MUSE_BRIDGE_KEY。生成方式：python -c ...")

    async def broken_dispatch(_store):
        raise ConfigError("缺少 MUSE_BRIDGE_KEY。生成方式：python -c ...")

    monkeypatch.setattr(app_module, "store", broken_store)
    monkeypatch.setattr(pool_mod, "dispatch", broken_dispatch)
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as c:
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503
    assert r.json()["type"] == "config_error"
    assert "MUSE_BRIDGE_KEY" in r.json()["error"]


@pytest.mark.asyncio
async def test_stale_reasoning_not_leaked_before_new_answer(monkeypatch):
    """第二轮提问、新回答未开始时，上一轮的思考不得当成新思考外泄。"""
    monkeypatch.setattr(driver_mod, "REASONING", True)
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.01)

    state = {"polls": 0}

    async def fake_connect(self):
        return None

    async def fake_send(self, prompt):
        return None

    async def fake_assistant(self):
        state["polls"] += 1
        return "旧答案" if state["polls"] <= 3 else "旧答案新正文"

    async def fake_reasoning(self):
        # _reasoning_text 只取最后一个容器：新回答开始前最后容器=旧思考，之后=新思考
        return "旧思考" if state["polls"] <= 3 else "新思考"

    async def fake_streaming(self):
        return True  # 全程"在流式"，排除提前结束的干扰

    monkeypatch.setattr(driver_mod.MuseDriver, "connect", fake_connect)
    monkeypatch.setattr(driver_mod.MuseDriver, "_send", fake_send)
    monkeypatch.setattr(driver_mod.MuseDriver, "_assistant_text", fake_assistant)
    monkeypatch.setattr(driver_mod.MuseDriver, "_reasoning_text", fake_reasoning)
    monkeypatch.setattr(driver_mod.MuseDriver, "_is_streaming", fake_streaming)

    d = make_driver(None)
    events = []
    async for e in d.stream_reply_events("p", timeout=2.0):
        events.append(e)
        if e.get("content"):
            break
    reasoning_all = "".join(e["reasoning"] for e in events if e.get("reasoning"))
    assert "新思考" in reasoning_all, reasoning_all
    assert "旧思考" not in reasoning_all, f"stale reasoning leaked: {reasoning_all!r}"
