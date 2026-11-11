"""助手回答提取链路的离线哨兵（以 2026-11-11 真机 muse.ai DOM 为准）。

真机事实（选择器的唯一权威证据，同见 selectors.py 的同日注释）：

* 用户消息项：DIV 带 ``data-message-item`` / ``data-message-role="user"`` /
  ``data-message-side="user"``，inner_text 是 ``"你：\\n\\n<提问>"``——"你："是渲染
  出来的前缀，不属于提问内容；
* 助手正文：DIV 带 ``data-hatch-assistant-message-body="true"``，藏在
  ``hatch-chat-groupable-bubble … agent-bubble`` 气泡里；
* 通用类名 ``div[class*="hatch-chat-groupable-bubble"]`` 两边气泡都命中。

这里用假 page/locator 钉三件事：

(a) 最后一条是用户气泡时被跳过，返回前一条助手正文；
(b) ``data-hatch-assistant-message-body`` 候选优先命中，气泡外壳的噪声文字
    不进回答；
(c) 所有候选都空时抛 ``AssistantTextMissing``，而不是返回空串——真机事故就是
    空串被当成 200 + 成功回答交付出去。

另外钉两条出口：空正文在 app.py 必须落到 504（不是裸 500、更不是 200），
以及"读不到正文时流不能静默结束"。
"""

from __future__ import annotations

import pytest

from muse_bridge import driver as driver_mod
from muse_bridge.driver import AssistantTextMissing, MuseDriver

ASSISTANT_BODY_SEL = '[data-hatch-assistant-message-body="true"]'
GENERIC_BUBBLE_SEL = 'div[class*="hatch-chat-groupable-bubble"]'

USER_TEXT = "你：\n\n1+1 等于几？"
ANSWER = "1+1 等于 2。"
# 气泡外壳比正文容器多出的文字（头像/标签/复制按钮等），用来区分候选优先级。
BUBBLE_NOISE = "Muse\n1+1 等于 2。"


class _FakeNode:
    """假 DOM 节点：文本 + 是否落在用户标记里 + 命中哪些候选（由 page 决定）。

    ``is_user`` 表示"``closest()`` 会命中用户标记"，不模拟 JS 本身；
    JS 到底覆盖了哪几组标记，由 ``test_user_exclusion_covers_measured_markers`` 钉。
    """

    def __init__(self, text: str, *, is_user: bool = False) -> None:
        self.text = text
        self.is_user = is_user

    async def evaluate(self, _js: str) -> bool:
        return self.is_user

    async def inner_text(self) -> str:
        return self.text


class _FakeLocator:
    def __init__(self, nodes: list[_FakeNode]) -> None:
        self.nodes = nodes

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> _FakeNode:
        return self.nodes[index]


class _FakePage:
    """按选择器返回候选节点。列表顺序 = DOM 顺序（越靠后越新）。"""

    def __init__(self, hits: dict[str, list[_FakeNode]]) -> None:
        self.hits = hits

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.hits.get(selector, []))


def _driver(hits: dict[str, list[_FakeNode]]) -> MuseDriver:
    driver = MuseDriver.__new__(MuseDriver)
    driver._page = _FakePage(hits)
    return driver


async def test_last_message_is_user_bubble_then_previous_answer_wins():
    """(a) 最后一条是用户气泡（``你：…``）时必须回退到前一条助手正文。

    这一版构建里正文容器属性缺失，只能靠通用气泡类名兜底：两个气泡都被命中，
    且用户气泡确实是"最后一条"——这正是旧 bug 的现场（回答里出现提问原文）。
    """
    driver = _driver(
        {
            GENERIC_BUBBLE_SEL: [
                _FakeNode(ANSWER),
                _FakeNode(USER_TEXT, is_user=True),
            ],
        }
    )
    assert await driver._assistant_text() == ANSWER


async def test_assistant_body_candidate_beats_generic_bubble():
    """(b) ``data-hatch-assistant-message-body`` 必须最先命中。

    通用气泡类名也能取到文字，但那是气泡外壳（带 "Muse" 之类噪声）；
    正文容器才是干净的回答。返回 ANSWER 而不是 BUBBLE_NOISE 即证明顺序生效。
    """
    driver = _driver(
        {
            ASSISTANT_BODY_SEL: [_FakeNode(ANSWER)],
            GENERIC_BUBBLE_SEL: [
                _FakeNode(USER_TEXT, is_user=True),
                _FakeNode(BUBBLE_NOISE),
            ],
        }
    )
    assert await driver._assistant_text() == ANSWER


@pytest.mark.parametrize(
    "hits",
    ({}, {ASSISTANT_BODY_SEL: [_FakeNode("")]}),
    ids=("no-candidate", "empty-text"),
)
async def test_reads_nothing_raises_instead_of_returning_empty_string(hits):
    """(c) 扫完所有候选仍读不到正文：抛错，绝不返回 ""。

    真机事故：页面 WS 掉线、提问没发出去，旧实现返回空串，桥回了
    200 + ``content: ""`` + ``completion_tokens: 0``，客户端当成成功。
    """
    driver = _driver(dict(hits))
    with pytest.raises(AssistantTextMissing) as excinfo:
        await driver._assistant_text()
    # app.py 的既有映射按 TimeoutError 处理（非流式 504），这里必须满足该前提。
    assert isinstance(excinfo.value, TimeoutError)
    assert "助手正文" in str(excinfo.value)


def test_user_exclusion_covers_measured_markers():
    """用户判据必须四组齐全，且用 closest() 覆盖祖先节点。

    只看候选节点本身不够：通用类名命中的是气泡内的子节点，角色属性挂在外层
    消息项上。少任何一组，用户提问就会重新混进回答。
    """
    js = driver_mod._JS_INSIDE_USER
    assert "closest(" in js
    for marker in (
        '[data-role="user"]',
        '[data-message-role="user"]',
        '[data-message-side="user"]',
        '[data-hatch-user-message-body="true"]',
    ):
        assert marker in js, f"用户判据漏了 {marker}"
    # Playwright 的 evaluate 会把 "求值结果是函数" 自动调用，所以首字符是 "("。
    assert js.lstrip().startswith("(")


class _NoReplyDriver(MuseDriver):
    """页面永远读不到正文、也没有流式标记 = 真机 WS 掉线时的现场。"""

    def __init__(self) -> None:
        super().__init__("ws://127.0.0.1:0")
        self._page = _FakePage({})

    async def connect(self) -> None:
        return None

    async def _send(self, prompt: str) -> None:
        return None


async def test_no_text_does_not_end_the_stream_silently(monkeypatch):
    """一个字都没读到时，流必须以错误收尾。

    旧实现在静默期满直接 return，上层于是"正常完成"了 0 字节回答——
    这就是 200 + 空正文的那次事故（测试里用空 DOM 复现同一条路径）。
    """
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    driver = _NoReplyDriver()
    seen: list[dict] = []
    with pytest.raises(TimeoutError) as excinfo:
        async for event in driver.stream_reply_events(
            "x", timeout=30.0, first_reaction=0.05
        ):
            seen.append(event)
    assert seen == [], "空回答不该产出任何事件"
    assert "没有任何反应" in str(excinfo.value), (
        f"错误没指向发送没生效：{excinfo.value}"
    )


class _EmptyReplyResult:
    ok = True
    account = "acc"
    driver = None
    verdict = ""
    note = ""
    _lock = None

    def __init__(self) -> None:
        self.released = False

    async def release(self) -> None:
        self.released = True


class _FakeStore:
    def mark_failure(self, account: str, *, state, error: str) -> None:
        return None


async def test_assistant_text_missing_maps_to_504_not_500(monkeypatch):
    """空正文在 HTTP 出口上必须是"上游失败"：504，而不是 500 或 200。

    AssistantTextMissing 继承 TimeoutError，正是为了让 app.py 既有映射直接生效；
    这条用真 ASGI 应用走一遍，钉住"没有被吞、也没有变成裸 500"。
    """
    from httpx import ASGITransport, AsyncClient

    from muse_bridge import app as app_module

    result = _EmptyReplyResult()

    async def fake_dispatch(*_args, **_kwargs):
        return result

    async def fake_collect(*_args, **_kwargs):
        raise AssistantTextMissing("扫遍所有候选仍读不到助手正文")

    monkeypatch.setattr(app_module.pool, "dispatch", fake_dispatch)
    monkeypatch.setattr(app_module.pool, "collect_reply_events", fake_collect)
    monkeypatch.setattr(app_module, "store", lambda: _FakeStore())
    monkeypatch.setattr(app_module, "require_key", lambda: None)

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://t"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "muse-1",
                "messages": [{"role": "user", "content": "1+1 等于几？"}],
            },
        )

    assert response.status_code == 504, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "timeout"
    assert "助手正文" in detail["note"]
    assert result.released, "失败路径也必须释放账号锁"


class _RecordingStore:
    def __init__(self) -> None:
        self.failures: list[tuple[str, object, str]] = []

    def mark_failure(self, account: str, *, state, error: str) -> None:
        self.failures.append((account, state, error))


async def test_channel_unavailable_maps_to_503_and_browser_state(monkeypatch):
    """通道不可用必须是 503 + browser_unavailable，不能是 504 + rate_limited。

    2026-11-11 真机：关掉代理后页面停在「Muse 正在连接…」，旧行为要么等满
    180 秒报 timeout（账号被标 rate_limited，运维会去查配额），要么更早版本
    直接回 200 + 空正文。现在 driver 抛 ChannelUnavailable（BrowserUnavailable
    的子类），HTTP 出口必须落到 503，状态机必须落可重试的 BROWSER_UNAVAILABLE。
    """
    from httpx import ASGITransport, AsyncClient

    from muse_bridge import app as app_module
    from muse_bridge.driver import ChannelUnavailable
    from muse_bridge.state import AccountState

    result = _EmptyReplyResult()
    store = _RecordingStore()

    async def fake_dispatch(*_args, **_kwargs):
        return result

    async def fake_collect(*_args, **_kwargs):
        raise ChannelUnavailable("页面未就绪（可能仍在 Muse 正在连接…）")

    monkeypatch.setattr(app_module.pool, "dispatch", fake_dispatch)
    monkeypatch.setattr(app_module.pool, "collect_reply_events", fake_collect)
    monkeypatch.setattr(app_module, "store", lambda: store)
    monkeypatch.setattr(app_module, "require_key", lambda: None)

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://t"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "muse-1",
                "messages": [{"role": "user", "content": "1+1 等于几？"}],
            },
        )

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "browser_unavailable"
    assert "正在连接" in detail["note"]
    assert store.failures and store.failures[0][1] is AccountState.BROWSER_UNAVAILABLE
    assert result.released, "失败路径也必须释放账号锁"
