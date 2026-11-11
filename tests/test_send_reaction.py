"""发送没生效时，错误必须在几秒内指向"没发出去"，而不是等满 timeout 后
报"没有产生新回答"——那会把发送问题误报成模型慢。

同时钉住两份抑制证据：页面显示流式标记、思考通道已有内容，都不能被误判成
"没发出去"。
"""

from __future__ import annotations

import time

import pytest

from muse_bridge import driver as driver_mod
from muse_bridge.driver import MuseDriver

OLD_TEXT = "上一条回答"


class _Stub(MuseDriver):
    def __init__(self, *, streaming: bool = False, reasoning: str = "") -> None:
        super().__init__("ws://127.0.0.1:0")
        self._streaming = streaming
        self._reasoning = reasoning
        self.streaming_checks = 0

    async def connect(self) -> None:
        return None

    async def _send(self, prompt: str) -> None:
        return None

    async def _assistant_text(self) -> str:
        return OLD_TEXT  # 页面纹丝不动：新气泡没出现

    async def _reasoning_text(self) -> str:
        return self._reasoning

    async def _is_streaming(self) -> bool:
        self.streaming_checks += 1
        return self._streaming


@pytest.mark.asyncio
async def test_no_reaction_fails_fast_instead_of_waiting_the_full_timeout(monkeypatch):
    """三条证据都不支持"已发送"时必须早抛，而不是空转满 timeout。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    d = _Stub()
    started_at = time.monotonic()
    with pytest.raises(TimeoutError) as exc:
        async for _ in d.stream_reply_events("x", timeout=30.0, first_reaction=0.05):
            pass
    elapsed = time.monotonic() - started_at
    assert "没有任何反应" in str(exc.value), f"错误没指向发送：{exc.value}"
    assert "没有发出去" in str(exc.value)
    assert elapsed < 2.0, f"没有快速失败，耗了 {elapsed:.2f} 秒"


@pytest.mark.asyncio
async def test_streaming_indicator_suppresses_the_fast_failure(monkeypatch):
    """页面已经在显示流式标记：这是"发出去了、模型还没吐字"，不能报发送问题。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    d = _Stub(streaming=True)
    with pytest.raises(TimeoutError) as exc:
        async for _ in d.stream_reply_events("x", timeout=0.12, first_reaction=0.02):
            pass
    assert "没有任何反应" not in str(exc.value), "流式标记存在却报了发送没生效"
    assert "没有产生新回答" in str(exc.value)
    assert d.streaming_checks > 0, "抑制判断根本没去查流式标记"


@pytest.mark.asyncio
async def test_reasoning_without_text_suppresses_the_fast_failure(monkeypatch):
    """思考通道已有内容：上游确实响应了，不能报发送问题。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    d = _Stub(reasoning="正在思考")
    with pytest.raises(TimeoutError) as exc:
        async for _ in d.stream_reply_events("x", timeout=0.12, first_reaction=0.02):
            pass
    assert "没有任何反应" not in str(exc.value), "思考已出现却报了发送没生效"
