""""页面有没有反应"的判据钉：文本相同不等于没反应。

真机事故（2026-11-11 复现）：连续两次问「只回复 OK」，muse 两次都回 "OK"。
驱动原先只比较"最后一条助手正文"是否变化，于是把**已经发出的新回答**判成
"页面还停在旧消息上"，45 秒后抛 504「页面没有任何反应」，账号还被标成
rate_limited。手工往同一标签发同一条消息却能秒回 —— 证明是判据问题，不是通道。

修法：用 `[data-message-item][data-message-role="assistant"]` 的
`data-message-id` 判断"是不是新消息"；id 读不到时（老 DOM）才回退到比正文。

本文件钉住三件事：
  (a) 文本相同但消息 id 变了 = 有新回答，必须产出正文、不得报没反应；
  (b) 文本相同且消息 id 也没变 = 真的没反应，必须仍然报 TimeoutError；
  (c) id 读不到时 `_last_assistant_id()` 返回空串（调用方据此回退旧判据）。
"""

from __future__ import annotations

import pytest

from muse_bridge import driver as driver_mod
from muse_bridge.driver import MuseDriver

ANSWER = "OK"


def _bare_driver() -> MuseDriver:
    driver = MuseDriver.__new__(MuseDriver)
    driver._page = None
    return driver


def _patch_pipeline(monkeypatch, texts, ids) -> list:
    """把发送/等待链路换成"按序列出值"的假实现，返回记录到的事件列表。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(MuseDriver, "connect", _noop)
    monkeypatch.setattr(MuseDriver, "_send", _noop)
    monkeypatch.setattr(MuseDriver, "_reasoning_text", _empty_reasoning)
    monkeypatch.setattr(MuseDriver, "_is_streaming", _not_streaming)

    queue = {"texts": list(texts), "ids": list(ids)}

    async def fake_text(self):
        return queue["texts"].pop(0) if len(queue["texts"]) > 1 else queue["texts"][0]

    async def fake_id(self):
        return queue["ids"].pop(0) if len(queue["ids"]) > 1 else queue["ids"][0]

    monkeypatch.setattr(MuseDriver, "_assistant_text", fake_text)
    monkeypatch.setattr(MuseDriver, "_last_assistant_id", fake_id)
    return queue


async def _noop(self, *args, **kwargs):
    return None


async def _empty_reasoning(self):
    return ""


async def _not_streaming(self):
    return False


@pytest.mark.asyncio
async def test_identical_answer_with_new_message_id_counts_as_reaction(monkeypatch):
    """(a) 新回答与上一条逐字相同，但 id 换了 —— 这是有反应。"""
    _patch_pipeline(monkeypatch, texts=[ANSWER], ids=["assistant-msg-a", "assistant-msg-b"])
    driver = _bare_driver()
    events = []
    async for event in driver.stream_reply_events(
        "只回复 OK", timeout=1.0, quiet_ms=100, first_reaction=0.05
    ):
        events.append(event)
    assert [e["content"] for e in events if e.get("content")] == [ANSWER]


@pytest.mark.asyncio
async def test_same_text_and_same_message_id_still_raises_no_reaction(monkeypatch):
    """(b) 文本与 id 都没变 —— 旧判据必须保留，不能因为修 (a) 就放过真故障。"""
    _patch_pipeline(monkeypatch, texts=[""], ids=["assistant-msg-a"])
    driver = _bare_driver()
    with pytest.raises(TimeoutError) as caught:
        async for _ in driver.stream_reply_events(
            "hi", timeout=0.4, first_reaction=0.05
        ):
            pass
    assert "没有任何反应" in str(caught.value)


@pytest.mark.asyncio
async def test_missing_id_falls_back_to_empty_string(monkeypatch):
    """(c) 页面读不到 id（老 DOM / 查询失败）时返回空串，由调用方回退比正文。"""

    class NoIdPage:
        async def evaluate(self, _js, _arg=None):
            raise RuntimeError("no dom")

    driver = _bare_driver()
    driver._page = NoIdPage()
    assert await driver._last_assistant_id() == ""

    class WeirdPage:
        async def evaluate(self, _js, _arg=None):
            return 12345  # 不是字符串：同样按"读不到"处理

    driver._page = WeirdPage()
    assert await driver._last_assistant_id() == ""

    driver._page = None
    assert await driver._last_assistant_id() == ""
