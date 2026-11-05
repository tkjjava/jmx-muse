"""CDP 驱动：连接真实 Chrome，操作 muse.ai 页面。

为什么是浏览器：
  muse 的传输是加密 Noise WebSocket，URL 内嵌 notary token。
  纯协议实现需要复刻 X25519/HKDF/AES-GCM/Ed25519 并伪造 notary 签名，
  没有公开实现。所以让真实前端自己做加密，我们只操作页面。

设计要点：
  * 复用已登录的 Chrome（CDP 连接），不自己启动新实例、不碰 cookie
  * 所有 DOM 选择器集中在 selectors.py
  * 流式靠"文本单调扩展 + 静默窗口"判定，不依赖前端事件
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from . import selectors

CDP_URL = os.environ.get("MUSE_CDP_URL", "http://127.0.0.1:9222")
SITE = os.environ.get("MUSE_SITE", "https://muse.ai")
HEADLESS = os.environ.get("MUSE_HEADLESS", "false").lower() == "true"
QUIET_MS = int(os.environ.get("MUSE_QUIET_MS", "600"))
POLL_INTERVAL = 0.25
MAX_IDLE_SECONDS = 30.0


class BrowserUnavailable(RuntimeError):
    """CDP 连不上，或 playwright 未安装。"""


class NotLoggedIn(RuntimeError):
    """页面处于未登录状态。"""


@dataclass
class BrowserStatus:
    connected: bool
    logged_in: bool
    url: str = ""
    note: str = ""


def _import_playwright():
    """延迟导入，没装 playwright 时给出可操作的错误而不是 ImportError 栈。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "未安装 playwright。运行：python -m pip install playwright "
            "&& python -m playwright install chromium"
        ) from exc
    return async_playwright


class MuseDriver:
    """一个 CDP 连接对应一个已登录的 muse.ai 页面。"""

    def __init__(self, cdp_url: str = CDP_URL) -> None:
        self.cdp_url = cdp_url
        self._playwright = None
        self._browser = None
        self._page = None

    async def connect(self) -> None:
        """连接已运行的 Chrome。不启动新实例。"""
        if self._page is not None:
            return

        async_playwright = _import_playwright()
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self.cdp_url
            )
        except Exception as exc:  # playwright 的异常类型随版本变化
            await self.close()
            raise BrowserUnavailable(
                f"无法连接 CDP {self.cdp_url}：{exc}\n"
                "请先启动带 --remote-debugging-port=9222 的 Chrome。"
            ) from exc

        contexts = self._browser.contexts
        if not contexts:
            await self.close()
            raise BrowserUnavailable("Chrome 没有可用上下文")

        context = contexts[0]
        self._page = await self._pick_muse_page(context)
        if self._page is None:
            await self.close()
            raise BrowserUnavailable(
                f"Chrome 里没有 {SITE} 的标签页。请先打开并登录。"
            )

    async def _pick_muse_page(self, context):
        """优先复用已有 muse.ai 标签页，避免反复开新页。"""
        target = "muse.ai"
        for page in context.pages:
            if target in (page.url or ""):
                return page
        page = await context.new_page()
        await page.goto(SITE, wait_until="domcontentloaded")
        return page

    async def close(self) -> None:
        # 只断开连接，不关闭用户的 Chrome
        self._page = None
        self._browser = None
        if self._playwright is not None:
            # 断开时 playwright 可能抛出各种异常，都不该影响调用方
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def status(self) -> BrowserStatus:
        """检查连接与登录态。"""
        try:
            await self.connect()
        except BrowserUnavailable as exc:
            return BrowserStatus(connected=False, logged_in=False, note=str(exc))

        assert self._page is not None
        url = self._page.url or ""
        logged_in = await self._detect_logged_in()
        return BrowserStatus(
            connected=True,
            logged_in=logged_in,
            url=url,
            note="" if logged_in else "页面未显示登录态元素",
        )

    async def _detect_logged_in(self) -> bool:
        assert self._page is not None
        # 先看未登录迹象
        for selector in selectors.LOGGED_OUT_MARKERS:
            try:
                if await self._page.locator(selector).count() > 0:
                    # 有登录按钮不代表一定没登录，继续看正向标记
                    break
            except Exception:
                continue

        for selector in selectors.LOGGED_IN_MARKERS:
            try:
                if await self._page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def _find_input(self):
        assert self._page is not None
        for selector in selectors.COMPOSER_INPUTS:
            locator = self._page.locator(selector)
            try:
                if await locator.count() > 0:
                    return locator.first
            except Exception:
                continue
        raise BrowserUnavailable("找不到输入框，页面可能改版或未加载完成")

    async def _send(self, prompt: str) -> None:
        """填入并发送。不用回车，优先点发送按钮（更接近用户行为也更稳）。"""
        assert self._page is not None
        box = await self._find_input()
        await box.click()
        await box.fill(prompt)
        await asyncio.sleep(0.15)

        for selector in selectors.SEND_BUTTONS:
            button = self._page.locator(selector)
            try:
                if await button.count() > 0 and await button.first.is_enabled():
                    await button.first.click()
                    return
            except Exception:
                continue

        # 找不到按钮才用回车兜底
        await box.press("Enter")

    async def _assistant_text(self) -> str:
        """取最后一条助手消息的完整文本。"""
        assert self._page is not None
        for selector in selectors.ASSISTANT_MESSAGES:
            locator = self._page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            if count == 0:
                continue
            try:
                text = await locator.nth(count - 1).inner_text()
            except Exception:
                continue
            if text:
                return text
        return ""

    async def _is_streaming(self) -> bool:
        assert self._page is not None
        for selector in selectors.STREAMING_MARKERS:
            try:
                if await self._page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def stream_reply(
        self,
        prompt: str,
        *,
        timeout: float = 180.0,
        quiet_ms: int | None = None,
    ) -> AsyncIterator[str]:
        """发送并流式产出增量文本。

        判定结束的两个条件（满足其一）：
          1. 文本在 quiet_ms 内没有增长，且流式标记消失
          2. 超过 timeout

        只认"单调扩展"：如果新文本不以旧文本开头，说明页面重绘了，
        此时不计为增量，避免把重复内容推给客户端。
        """
        await self.connect()
        quiet = (quiet_ms if quiet_ms is not None else QUIET_MS) / 1000.0

        before = await self._assistant_text()
        await self._send(prompt)

        deadline = time.monotonic() + timeout
        last = ""
        last_change = time.monotonic()
        started = False

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            current = await self._assistant_text()

            if current and current == before:
                continue  # 还是上一条消息，尚未产生新回答

            started = True
            if current.startswith(last):
                delta = current[len(last):]
            elif current != last:
                # 不单调：页面重绘或消息被替换，整体重发
                delta = current
            else:
                delta = ""

            if delta:
                last = current
                last_change = time.monotonic()
                yield delta
                continue

            still_streaming = await self._is_streaming()
            if not still_streaming and (time.monotonic() - last_change) >= quiet:
                return

        if started and last:
            return
        if not started:
            raise TimeoutError(f"{timeout} 秒内没有产生新回答")

    async def collect_reply(
        self, prompt: str, *, timeout: float = 180.0
    ) -> str:
        """一次性收全文。"""
        parts: list[str] = []
        async for delta in self.stream_reply(prompt, timeout=timeout):
            parts.append(delta)
        return "".join(parts)

    async def wake_vm(self) -> bool:
        """尝试唤醒云端 VM。

        通过页面内 fetch 触发，让浏览器带上现有 cookie 与签名头。
        不自己构造请求——那样拿不到前端生成的签名。
        """
        await self.connect()
        assert self._page is not None
        try:
            result = await self._page.evaluate(
                """async () => {
                    try {
                        const r = await fetch('/api/hatch/vm/wake', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: '{}',
                            credentials: 'include'
                        });
                        return {ok: r.ok, status: r.status};
                    } catch (e) {
                        return {ok: false, status: 0, error: String(e)};
                    }
                }"""
            )
        except Exception:
            return False
        return bool(result.get("ok"))

    async def touch_session(self) -> bool:
        """打一次 /api/session 续期 hatch_vml（48h）与 hatch_sess（30d）。

        同样走页面内 fetch：续期依赖的是浏览器当前的 cookie 与前端签名，
        外部构造的请求不会被认可。
        """
        await self.connect()
        assert self._page is not None
        try:
            result = await self._page.evaluate(
                """async () => {
                    try {
                        const r = await fetch('/api/session', {
                            method: 'GET',
                            headers: {'Accept': 'application/json'},
                            credentials: 'include'
                        });
                        const data = r.ok ? await r.json().catch(() => null) : null;
                        return {ok: r.ok, status: r.status, data};
                    } catch (e) {
                        return {ok: false, status: 0, error: String(e)};
                    }
                }"""
            )
        except Exception:
            return False
        return bool(result.get("ok"))
