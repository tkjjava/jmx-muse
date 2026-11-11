"""deadline 到期时的残篇不能当完整回答交出去。

上层把驱动器返回的正文按 finish_reason=stop 计费交付；
流到一半超时却正常 return，客户端会拿到一段看不出被截断的半句话。
"""

from __future__ import annotations

import pytest

from muse_bridge import driver as driver_mod
from muse_bridge.driver import MuseDriver


class _Stub(MuseDriver):
    """只替换 DOM 读取，保留真实的差分 / 超时 / 静默判定逻辑。"""

    def __init__(self, texts: list[str]) -> None:
        super().__init__("ws://127.0.0.1:0")
        self._texts = texts
        self._i = 0
        self._streaming = True

    async def connect(self) -> None:  # noqa: D102 - 桩
        return None

    async def _send(self, prompt: str) -> None:  # noqa: D102 - 桩
        return None

    async def _assistant_text(self) -> str:
        value = self._texts[min(self._i, len(self._texts) - 1)]
        self._i += 1
        return value

    async def _reasoning_text(self) -> str:
        return ""

    async def _is_streaming(self) -> bool:
        return self._streaming


@pytest.mark.asyncio
async def test_deadline_mid_stream_raises_instead_of_returning_partial(monkeypatch):
    """正文还在增长时撞上 deadline：必须抛错，不能 return 残篇。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    d = _Stub(["H", "He", "Hel", "Hell", "Hello", "Hello w"])
    chunks: list[dict] = []
    with pytest.raises(TimeoutError) as exc:
        async for event in d.stream_reply_events("x", timeout=0.06):
            chunks.append(event)
    assert chunks, "没产出过增量说明测的不是截断路径"
    assert "没有完成" in str(exc.value), f"错误信息没说清是残篇：{exc.value}"
    assert "6" in str(exc.value) or "字" in str(exc.value)


@pytest.mark.asyncio
async def test_quiet_completion_still_returns_normally(monkeypatch):
    """静默期满的正常收尾不能被上面的修复连带打死。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    d = _Stub(["H", "He", "Hello"])
    d._streaming = False
    chunks: list[dict] = []
    async for event in d.stream_reply_events("x", timeout=5.0, quiet_ms=10):
        chunks.append(event)
    assert "".join(c.get("content", "") for c in chunks) == "Hello"


@pytest.mark.asyncio
async def test_deadline_with_no_output_still_names_the_empty_case(monkeypatch):
    """一个字都没出：错误信息要说"没有产生新回答"，而不是"残篇"。"""
    monkeypatch.setattr(driver_mod, "POLL_INTERVAL", 0.005)
    d = _Stub([""])
    with pytest.raises(TimeoutError) as exc:
        async for _ in d.stream_reply_events("x", timeout=0.03):
            pass
    assert "没有产生新回答" in str(exc.value)
