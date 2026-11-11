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
import base64
import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from . import refs, selectors, session_http
from .env import env_positive_int
from .quota import parse_quota_text

CDP_URL = os.environ.get("MUSE_CDP_URL", "http://127.0.0.1:9222")
SITE = os.environ.get("MUSE_SITE", "https://muse.ai")
HEADLESS = os.environ.get("MUSE_HEADLESS", "false").lower() == "true"
QUIET_MS = env_positive_int("MUSE_QUIET_MS", "600")
# 发送后多久算"页面毫无反应"。消息没发出去时（按钮禁用、回车变换行、页面改版），
# 原先要空转满整个 timeout 才报"没有产生新回答"，把责任推给模型；
# 页面上连新气泡都没出现，本来就是"没发出去"的证据，早说 45 秒就够。
FIRST_REACTION_SECONDS = env_positive_int("MUSE_FIRST_REACTION_SECONDS", "45")
POLL_INTERVAL = 0.25
MAX_IDLE_SECONDS = 30.0

# 思考透传开关：进程启动时读取一次。默认关闭——关闭时思考容器根本不被查询，
# 流式输出与没有此功能时逐字节一致。字段名候选用 MUSE_REASONING_SELECTORS
# 覆盖（每次调用读取，调选择器不用重启）。
REASONING = os.environ.get("MUSE_REASONING", "0").lower() in ("1", "true", "yes", "on")


def reasoning_selectors() -> tuple[str, ...]:
    raw = os.environ.get("MUSE_REASONING_SELECTORS", "")
    if raw:
        items = tuple(s.strip() for s in raw.split(",") if s.strip())
        if items:
            return items
    return selectors.REASONING_MESSAGES




class BrowserUnavailable(RuntimeError):
    """CDP 连不上，或 playwright 未安装。"""


class ChannelUnavailable(BrowserUnavailable):
    """到 metaaivm 的通道不通：页面停在「正在连接…」或 WS 已掉线。

    与 BrowserUnavailable 分开命名是为了排障语义，但**故意继承它**：
    app.py 已有的 `browser_unavailable → 503` 映射可以直接生效，状态机记
    `BROWSER_UNAVAILABLE`（可重试状态，冷却期满自动回池）。如果改成抛
    TimeoutError，app 会把账号记成 RATE_LIMITED、回 504，运维会去查配额，
    而真因是出口网络——2026-11-11 真机就踩到过这个误判。
    """


class NotLoggedIn(RuntimeError):
    """页面处于未登录状态。"""


class AssistantTextMissing(TimeoutError):
    """扫遍所有候选选择器仍读不到非空助手正文。

    2026-11-11 真机事故：页面 WS 掉线时提问根本没发出去，但 `_assistant_text`
    扫完候选返回了空串，桥于是回了 HTTP 200 + `content: ""` +
    `completion_tokens: 0`，客户端把"什么都没发生"当成功回答收下。
    所以"读不到正文"必须是错误，不能是空串。

    继承 TimeoutError 是刻意的：app.py 既有映射直接生效（非流式 504、流式按
    finish_reason=length 收尾），既不会被漏成裸 500，也不必新增分支——
    多一个自定义基类型反而会让"哪些出口算失败"变得难以核对。
    """


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
            raise BrowserUnavailable(f"Chrome 里没有 {SITE} 的标签页。请先打开并登录。")

    async def _pick_muse_page(self, context):
        """优先复用已有 muse.ai 标签页，避免反复开新页。"""
        target = "muse.ai"
        for page in context.pages:
            if target in (page.url or ""):
                return page
        page = await context.new_page()
        try:
            await page.goto(SITE, wait_until="domcontentloaded")
        except Exception as exc:  # playwright 的异常类型随版本变化
            # 这一步以前完全裸奔：CDP 连上了、但没有现成的 muse.ai 标签页，
            # 于是新建一个再导航。此时若出口断网，playwright 抛的裸 Error
            # 会一路冒到 ASGI，用户拿到的是 `500 Internal Server Error`——
            # 协议回退的根因明明已经落进日志，调用方却看不到任何可读信息。
            # 2026-11-11 用失效代理实测复现（ERR_CONNECTION_CLOSED）。
            # 归到 BrowserUnavailable：这是"浏览器这条兜底通道也用不了"，
            # 与 CDP 连不上同属一类，上层已有对应的分类与文案。
            # 关闭失败不该盖住原始错误：用 suppress 而不是 try/except/pass。
            with contextlib.suppress(Exception):
                await page.close()
            raise BrowserUnavailable(                f"打开的 {SITE} 页面加载失败：{exc}\n"
                "出口到 muse.ai 不通时，请先确认代理可用，或手动打开并登录一个 "
                f"{SITE} 标签页后重试。"
            ) from exc
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
        """填入并发送。不用回车，优先点发送按钮（更接近用户行为也更稳）。

        发送前先过"连接就绪门"：2026-11-11 真机上页面停在「Muse 正在连接…」
        时（出口到 *.metaaivm.com 不通），往输入框里填字、点发送都不会真正
        发出去，而调用方要等满超时才拿到"没有产生新回答"，账号还会被标成
        rate_limited。这里最多等 12 秒，等不到就直接报连接问题，让上层 503，
        不再把"通道断"伪装成"模型慢"。
        """
        assert self._page is not None
        if not await self.wait_hydrated(timeout=12.0):
            raise ChannelUnavailable(
                "页面未就绪（可能仍在 Muse 正在连接…）：出口到 metaaivm 的通道不通，"
                "先恢复代理再重试"
            )
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
        """取最后一条**助手**消息的完整文本；读不到非空正文就抛错，不返回空串。

        为什么必须按角色排除用户气泡：muse 的用户气泡里会渲染
        "你：\n\n<提问>"，而 `hatch-chat-groupable-bubble` 这种通用类名两边都
        命中。只取最后一个元素、不判角色时，刚发出提问的那一瞬间"最后一条"就是
        用户气泡——非流式收尾与流式增量都会把用户原始提问混进回答（实测表现
        为 `你：…提问…回答` 粘在一起）。

        2026-11-11 真机 DOM 给了四组用户判据：`data-role="user"`、
        `data-message-role="user"`、`data-message-side="user"`，以及用户气泡自身
        的 `data-hatch-user-message-body="true"`。用 `closest()` 而不是只看节点
        本身：命中的往往是气泡内的正文/子节点，角色属性挂在外层消息项上。

        为什么读不到正文要抛 `AssistantTextMissing` 而不是返回 "":页面 WS 掉线、
        提问没发出去时，旧实现扫完候选返回空串，桥按"成功"回了 200 + 空正文，
        客户端完全看不出异常（见该异常的 docstring）。
        """
        assert self._page is not None
        for selector in selectors.ASSISTANT_MESSAGES:
            locator = self._page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            if count == 0:
                continue
            for index in range(count - 1, -1, -1):
                node = locator.nth(index)
                try:
                    if await node.evaluate(_JS_INSIDE_USER):
                        continue
                    text = await node.inner_text()
                except Exception:
                    continue
                if text:
                    return text
        raise AssistantTextMissing(
            "扫遍所有候选选择器仍读不到非空助手正文：页面可能没收到提问"
            "（WS 掉线/发送没生效）或前端改版。空串会被上层当成成功回答，"
            "所以这里按失败处理。"
        )

    async def _assistant_text_or_empty(self) -> str:
        """允许"还没有回答"的调用点用：发送前基线、发送后轮询。

        这两处空串是合法状态——新会话本来就没有回答，刚点完发送时助手气泡也
        还没渲染出来（空=还没开始，不是读失败）。真正的兜底在
        `stream_reply_events` 收尾：一个字都没读到时不允许静默 return。
        """
        try:
            return await self._assistant_text()
        except AssistantTextMissing:
            return ""

    async def _last_assistant_id(self) -> str:
        """最后一条助手消息的稳定身份（`data-message-id`）；读不到时返回空串。

        为什么需要它：判断"页面有没有反应"不能只看正文变没变。muse 的助手气泡带
        `data-message-id`（实测形如 `assistant-msg-<uuid>`），新回答一定拿到新 id。
        只比正文时，**回答内容与上一条逐字相同**（比如连续问"只回复 OK"）会被判成
        "还是上一条旧消息"——真机表现为发出去明明成功了，却在 45 秒后抛
        「页面没有任何反应」的 504，账号还被标成 rate_limited。2026-11-11 复现：
        手工发同一条消息能出 `OK`，走桥必 504。

        没有 id 的页面（老版本 DOM）返回空串，调用方回退到"比正文"的旧判据，
        行为与改动前一致。
        """
        if self._page is None:
            return ""
        try:
            value = await self._page.evaluate(_JS_LAST_ASSISTANT_ID)
        except Exception:
            return ""
        return value if isinstance(value, str) else ""


    async def _reasoning_text(self) -> str:
        """取最后一段思考文本。开关关闭时恒返回空串——与没有此功能时一致。"""
        if not REASONING:
            return ""
        assert self._page is not None
        for selector in reasoning_selectors():
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


    async def stream_reply_events(
        self,
        prompt: str,
        *,
        timeout: float = 180.0,
        quiet_ms: int | None = None,
        first_reaction: float | None = None,
    ) -> AsyncIterator[dict]:
        """流式产出 {"reasoning": 增量} 或 {"content": 增量}。

        判定结束：正文在 quiet_ms 内没有增长且流式标记消失。思考与正文各自独立做
        单调扩展 diff——上游整体重绘时整段重发该通道，两通道互不污染。

        MUSE_REASONING 关闭时思考容器根本不被查询，reasoning_now 恒为空串，
        content 通道的行为与只有正文时逐字节一致。
        """
        await self.connect()
        quiet = (quiet_ms if quiet_ms is not None else QUIET_MS) / 1000.0

        before = await self._assistant_text_or_empty()
        before_id = await self._last_assistant_id()
        await self._send(prompt)

        reaction = first_reaction if first_reaction is not None else FIRST_REACTION_SECONDS
        reaction = min(float(reaction), timeout)
        reaction_deadline = time.monotonic() + reaction

        deadline = time.monotonic() + timeout
        last_content = ""
        last_reasoning = ""
        started = False
        last_change = time.monotonic()

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            current = await self._assistant_text_or_empty()
            reasoning_now = await self._reasoning_text()
            current_id = await self._last_assistant_id()

            # "页面还停在发送前那条消息上"=本回合没有任何反应。判据优先用消息 id：
            # 新回答必然换 `data-message-id`，所以"内容与上一条相同"（连续问同一句
            # 得到同一个回答）不会再被误判成没反应。两侧 id 都读得到才用 id 判据，
            # 否则回退到比正文——老 DOM 与读取抖动都落回改动前的行为。
            if before_id and current_id:
                unchanged = current_id == before_id and current == before
            else:
                unchanged = current == before

            # 三条证据都指向"页面根本没反应"：没有新气泡、没有思考、也没有
            # 流式标记。此时不必等到 timeout——那会把"没发出去"误报成"模型慢"。
            if (
                unchanged
                and not reasoning_now
                and time.monotonic() >= reaction_deadline
                and not await self._is_streaming()
            ):
                raise TimeoutError(
                    f"发送后 {reaction:g} 秒页面没有任何反应——消息很可能没有发出去"
                    "（页面上连新气泡都没出现，不是模型慢）"
                )

            if unchanged:
                # 正文还是上一条消息——思考容器里装的自然也是上一轮的旧思考。
                # 之前只跳过"思考为空"的情况，导致第二轮开头的整段旧思考
                # 与空基线做差后被当成新思考外泄（last_reasoning 跨请求不共享）。
                continue

            # 思考先出或正文开始渲染都算上游已响应；空 DOM 不算——
            # 否则空页面超时会静默返回空，而不是 TimeoutError
            if current or reasoning_now:
                started = True
            if current.startswith(last_content):
                content_delta = current[len(last_content):]
            elif current != last_content:
                content_delta = current  # 不单调：页面重绘，整段重发
            else:
                content_delta = ""

            if reasoning_now.startswith(last_reasoning):
                reasoning_delta = reasoning_now[len(last_reasoning):]
            elif reasoning_now != last_reasoning:
                reasoning_delta = reasoning_now
            else:
                reasoning_delta = ""

            if content_delta or reasoning_delta:
                last_content = current
                last_reasoning = reasoning_now
                last_change = time.monotonic()
                if reasoning_delta:
                    yield {"reasoning": reasoning_delta}
                if content_delta:
                    yield {"content": content_delta}
                continue

            if not await self._is_streaming() and (time.monotonic() - last_change) >= quiet:
                if last_content:
                    return
                # 静默期满却一个字都没读到：这是"页面根本没回话"，不是"回答说完了"。
                # 旧实现在这里直接 return，collect_reply 于是得到 ""，桥回 200 +
                # 空正文（2026-11-11 真机事故现场）。继续等：反应期限与总超时两条
                # 既有判据会给出有诊断价值的错误，而静默只说明"没在增长"。
                continue

        # 循环只在 deadline 到期时走到这里——静默期满且已有正文才在上面 return。
        # 已经产出过内容却超时，说明回答被截断：把残篇当完整答案返回，
        # 上层会按 finish_reason=stop 计费交付，这就是静默截断。
        # 与上面"空页面超时要抛错而不是静默返回空"是同一条原则。
        received = len(last_content)
        if received or started:
            raise TimeoutError(
                f"{timeout} 秒内回答没有完成（已收到 {received} 字，残篇已丢弃）"
            )
        raise TimeoutError(f"{timeout} 秒内没有产生新回答")

    async def stream_reply(
        self,
        prompt: str,
        *,
        timeout: float = 180.0,
        quiet_ms: int | None = None,
    ) -> AsyncIterator[str]:
        """只要正文的旧接口。委托给 stream_reply_events 丢弃思考，
        使两条路径不会各自演化出不同的差分逻辑。"""
        async for event in self.stream_reply_events(prompt, timeout=timeout, quiet_ms=quiet_ms):
            if event.get("content"):
                yield event["content"]

    async def collect_reply(self, prompt: str, *, timeout: float = 180.0) -> str:
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

    # ==================================================================
    # 生成能力（图 / 视频 / 参考素材 / 额度 / cookie 注入）——本轮新增。
    #
    # 每一条语义都对齐上游 muse2api 的活网实现 engine.py（行号逐条点名），
    # 上面既有的对话流方法一个字节都没动。
    # ==================================================================

    async def _js(self, expression: str, arg=None):
        """跑一段 JS 并把异常吞成 None。

        为什么到处用 JS 而不是 Playwright locator：上游在同一批逻辑里验证过
        "可见性 + 文本/属性匹配 + 只点第一命中"这套组合（engine.py:1073-1080、
        834-865），写成一段脚本比逐个 locator 试更容易保持判定一致，也让离线
        测试可以只伪装 evaluate。
        """
        assert self._page is not None
        try:
            if arg is None:
                return await self._page.evaluate(expression)
            return await self._page.evaluate(expression, arg)
        except Exception:
            return None

    @staticmethod
    def _as_dict(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            with contextlib.suppress(Exception):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        return {}

    @staticmethod
    def _as_list(raw) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("media", "items", "list", "result"):
                if isinstance(raw.get(key), list):
                    return raw[key]
            return []
        if isinstance(raw, str):
            with contextlib.suppress(Exception):
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
        return []

    def _cfg(self) -> dict:
        """把选择器表打包成 JS 参数：选择器只有 selectors.py 一个来源。"""
        return {
            "root": list(selectors.COMPOSER_ROOT),
            "hydrated": list(selectors.HYDRATION),
            "inputs": list(selectors.COMPOSER_INPUTS),
            "attachments": list(selectors.ATTACHMENTS),
            "media": list(selectors.RESULT_MEDIA),
            "exclude": list(selectors.MEDIA_EXCLUDE),
            "remove": list(selectors.REMOVE_ATTACHMENT),
            "fileInputs": list(selectors.FILE_INPUTS),
            "send": list(selectors.SEND_BUTTONS),
            "stop": list(selectors.STREAMING_MARKERS[:1]),
            "quotaEntry": list(selectors.QUOTA_SETTINGS_ENTRY),
            "quotaMenu": list(selectors.QUOTA_MENU_ITEM),
            "quotaDialog": list(selectors.QUOTA_DIALOG),
            "hint": list(selectors.UPLOAD_HINT),
            "errors": list(selectors.ERROR_NOTICES),
        }

    async def wait_hydrated(self, timeout: float = 15.0) -> bool:
        """等 React 真正接管页面（hydration 完成）且不再 Connecting...

        实测来源：engine.py:681-702 的 `_wait_ws_ready`。这一步不能省——页面
        DOM 早就在了但 React 还没挂载时，往 textarea 里填字不会同步到组件状态，
        发送按钮永远禁用，症状是"点了没反应"而不是报错。
        """
        await self.connect()
        deadline = time.monotonic() + timeout
        cfg = self._cfg()
        while time.monotonic() < deadline:
            raw = await self._js(_JS_HYDRATION, cfg)
            state = raw if isinstance(raw, str) else self._as_dict(raw).get("state")
            if state == "ready":
                return True
            await asyncio.sleep(0.1)
        return False

    async def attached_count(self) -> int:
        """composer 里还挂着几个待发送附件（读不到就返回 0，绝不抛）。

        实测来源：engine.py:1729-1740 的 `_ATTACHED_COUNT_JS`——判据是
        "移除附件"按钮的数量，并且要排除消息气泡里那些（消息气泡里的移除按钮
        属于历史消息，不是待发送队列）。
        """
        await self.connect()
        raw = await self._js(_JS_ATTACHED_COUNT, self._cfg())
        if isinstance(raw, (int, float)):
            return int(raw)
        data = self._as_dict(raw)
        for key in ("count", "value", "result"):
            if key in data:
                try:
                    return int(data[key] or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    async def clear_attachments(self) -> int:
        """清掉输入框里遗留的附件与文本框文件，返回点掉的个数。

        实测来源：engine.py:1684-1699。上一轮任务失败时附件常留在 composer 里，
        不清掉就会混进这次的成品（还会把上传件误判成生成结果）。
        """
        await self.connect()
        raw = await self._js(_JS_CLEAR_ATTACHMENTS, self._cfg())
        if isinstance(raw, (int, float)):
            return int(raw)
        data = self._as_dict(raw)
        for key in ("clicked", "count", "result"):
            if key in data:
                try:
                    return int(data[key] or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    async def attach_references(
        self, paths: list[str], *, expect: int, timeout: float = 30.0
    ) -> bool:
        """把参考素材逐个塞进上传控件，并确认真的挂上了。

        实测来源：engine.py:1752-1818（`_attach_images`）——先清空、再逐个
        `set_input_files`、每件间隔 0.35s、然后轮询移除按钮数量 >= 件数。
        "移除按钮存在"只证明附件进了输入框，不证明上游处理成功
        （engine.py:1803 的注释），所以这里不把它当成就。

        素材一律先过 `refs.normalize_references` 的判定（类型白名单、单件与总量
        大小、视频时长），再把它自己的字节写进临时目录上传：这样
        `self._reference_hashes` 里的 sha256 与真正送进浏览器的是同一份内容，
        成品阶段才可能认出"页面渲染回来的就是我刚上传的那张"。
        """
        await self.connect()
        references = refs.normalize_references(list(paths or []))
        if len(references) != int(expect):
            raise ReferenceUploadError(
                f"参考素材判定后数量不符：期望 {expect}，实际 {len(references)}"
            )

        self.clear_hashes()
        self.clear_paths()
        removed = await self.clear_attachments()
        if removed:
            await asyncio.sleep(0.3)

        hashes: set[str] = set()
        staged: list[str] = []
        directory = tempfile.mkdtemp(prefix="muse-bridge-refs-")
        try:
            for index, reference in enumerate(references, start=1):
                target = os.path.join(directory, f"reference_{index}{reference.ext}")
                with open(target, "wb") as handle:
                    handle.write(reference.data)
                hashes.add(reference.sha256)
                staged.append(target)
                if not await self._set_input_files(staged[-1:]):
                    raise ReferenceUploadError(
                        f"找不到上传控件，第 {index} 个参考素材未能附上"
                    )
                await asyncio.sleep(ATTACH_SETTLE_SECONDS)

            self._reference_hashes = hashes
            self._reference_paths = list(staged)

            deadline = time.monotonic() + timeout
            attached = 0
            while time.monotonic() < deadline:
                attached = await self.attached_count()
                if attached >= expect:
                    break
                await asyncio.sleep(ATTACH_POLL_SECONDS)
            if attached < expect:
                raise TimeoutError(
                    f"参考素材上传未确认（{attached}/{expect}）"
                )
            await asyncio.sleep(0.5)
            return True
        except Exception:
            await self.drop_reference_files()
            raise

    def clear_hashes(self) -> None:
        self._reference_hashes = set()

    def clear_paths(self) -> None:
        self._reference_paths: list[str] = []

    async def drop_reference_files(self) -> None:
        """删掉本轮的临时上传副本（Chromium 在发送时才读文件，调用方决定时机）。"""
        directory = os.path.dirname((getattr(self, "_reference_paths", []) or [""])[0])
        self.clear_paths()
        if directory and os.path.basename(directory).startswith("muse-bridge-refs-"):
            with contextlib.suppress(Exception):
                import shutil

                shutil.rmtree(directory, ignore_errors=True)

    async def _set_input_files(self, targets: list[str]) -> bool:
        """把文件塞进 composer 里的 file input。返回是否成功定位到控件。"""
        assert self._page is not None
        for selector in selectors.FILE_INPUTS:
            for scope in selectors.COMPOSER_ROOT + ("",):
                combined = f"{scope} {selector}".strip() if scope else selector
                locator = self._page.locator(combined)
                try:
                    if await locator.count() == 0:
                        continue
                    await locator.first.set_input_files(targets)
                    return True
                except Exception:
                    continue
        return False

    async def media_links(self) -> list[dict]:
        """读页面上所有候选媒体，返回 [{src,vSrc,iSrc,has_video,has_img,w,h,tid}]。

        实测来源：engine.py:971-1004 的 `_ATT_JS`。三个关键细节照搬：
          * 跳过落在 form / 用户气泡 / group-msg 里的容器（那是上传件不是成品）
          * 视频优先取 <video> 的 currentSrc，只有真的没有视频元素才退回 img
          * 按 src 去重，避免同一张图在缩略图和大图里各算一次
        字段名用 snake_case（has_video/has_img）与上游的 JS 驼峰略有差异，
        这是为了和 Python 侧其余返回值一致；语义等价。
        """
        await self.connect()
        raw = await self._js(_JS_MEDIA, self._cfg())
        items = self._as_list(raw)
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "src": str(item.get("src") or ""),
                    "vSrc": str(item.get("vSrc") or ""),
                    "iSrc": str(item.get("iSrc") or ""),
                    "has_video": bool(item.get("has_video") or item.get("hasVideo")),
                    "has_img": bool(item.get("has_img") or item.get("hasImg")),
                    "w": int(item.get("w") or 0),
                    "h": int(item.get("h") or 0),
                    "tid": str(item.get("tid") or ""),
                }
            )
        return out

    async def _media_state(self) -> dict:
        """一次 evaluate 取回等待期需要的全部页面信号。

        实测来源：engine.py:1230-1240。keys 与上游保持一致：
        cnt(助手气泡数)/txt(最后一条助手文本)/stop(是否仍在生成)/
        tail(页面尾部 1200 字)/path/href。
        """
        raw = await self._js(_JS_STATE, self._cfg())
        return self._as_dict(raw)

    async def _tail(self) -> str:
        """页面可见文本的尾部。额度/送达失败这类提示只出现在正文之外的区域。"""
        state = await self._media_state()
        tail = state.get("tail")
        if isinstance(tail, str) and tail:
            return tail
        assert self._page is not None
        try:
            html = await self._page.content()
        except Exception:
            return ""
        return html if isinstance(html, str) else ""

    async def wait_media(
        self,
        expect: str,
        *,
        timeout: float,
        baseline_srcs: list[str],
        first_reaction: float | None = None,
    ) -> dict | None:
        """等一张新的生成结果出现，返回那条 attachment dict；超时无果返回 None。

        实测来源：engine.py:1160-1324 的 `_wait_attachment`。逐条保留的判定：

        1. **严格类型过滤**：expect=video 时只认 has_video / vSrc / 视频后缀，
           绝不用封面静帧冒充成品（engine.py:1187-1214，上游踩过：muse 会先把
           data-testid 标成 ...-presentation-video，此时 <video> 还没挂上，
           primary 退回 img，src/w/h 全是静帧）。
        2. **baseline 排除**：本轮之前就存在的 src 不算新结果。
        3. **额度不足**立刻抛 QuotaExhausted（engine.py:1277-1278），
           这不是超时，重试换号才有意义，继续干等只会白占浏览器。
        4. **没发出去**：25 秒还没有新助手气泡且输入框仍留着附件/文字 → 补点一次
           发送；60 秒依旧 → 抛错交给上层换号（engine.py:1266-1275）。
        5. **未确认送达**：30 秒后 tail 出现 Still sending / Delivery not
           confirmed / 正在发送 / 未确认送达 → 抛错（engine.py:1279-1283）。
        6. **模型只回文字**：拒绝文案连续 4 次一致且已过 8 秒 → GenerationRefused
           （engine.py:1285-1296）；纯文字但明显在"报告状态"时不算拒绝
           （engine.py:1298-1313 + `_media_status_pending`）。
        7. **视频任务线程没建立**：seen_stop 之后 stop 消失、仍在 /thread/new、
           没有新气泡也没有新附件、elapsed>=180s 且静默>=90s → 抛错
           （engine.py:1315-1323）。

        与上游的签名差异：base_agent_cnt / base_att_cnt 由本方法在入口自己采集
        （上游是在 generate 里采好再传进来）。入口正好在 `_send` 之后，用户气泡
        不计入助手气泡数，所以采集点等价。`first_reaction` 默认 None 时完全不
        启用额外的早退判定，保持与上游一致；显式传入才生效。
        """
        await self.connect()
        baseline = {str(s) for s in (baseline_srcs or []) if s}
        initial = await self.media_links()
        state0 = await self._media_state()
        base_att_cnt = len(initial)
        base_agent_cnt = int(state0.get("cnt") or 0)

        deadline = time.monotonic() + timeout
        started = time.monotonic()
        stable_src, stable_n = "", 0
        last_txt, txt_stable = "", 0
        refusal_last, refusal_stable = "", 0
        seen_stop = False
        resent = False
        previous_state = None
        last_activity = started

        while time.monotonic() < deadline:
            await asyncio.sleep(MEDIA_POLL_INTERVAL)
            elapsed = time.monotonic() - started
            atts = await self.media_links()

            matching: list[dict] = []
            for att in atts:
                tid = att["tid"].lower()
                src = att["src"]
                v_src = att["vSrc"]
                if expect == "video":
                    hit = att["has_video"] or bool(v_src) or src.endswith(VIDEO_SUFFIXES)
                else:
                    hit = "image" in tid or (
                        not att["has_video"] and not src.endswith(VIDEO_SUFFIXES)
                    )
                if hit:
                    matching.append(att)

            att = matching[-1] if matching else None
            if att:
                src = att["src"]
                v_src = att["vSrc"]
                if expect == "video":
                    check = v_src or (src if src.endswith(VIDEO_SUFFIXES) else "")
                else:
                    check = src
                if check and check not in baseline:
                    if att["w"] > 0 and att["h"] > 0:
                        return att
                    stable_src, stable_n = (
                        (stable_src, stable_n + 1) if check == stable_src else (check, 0)
                    )
                    if stable_n >= 1:
                        return att

            state = await self._media_state()
            tail = str(state.get("tail") or "")
            cur_cnt = int(state.get("cnt") or 0)
            cur_txt = str(state.get("txt") or "")
            has_stop = bool(state.get("stop"))
            path = str(state.get("path") or "")
            seen_stop = seen_stop or has_stop

            signature = (cur_cnt, cur_txt, len(atts), has_stop, path)
            if previous_state is None or signature != previous_state:
                last_activity = time.monotonic()
                previous_state = signature

            if (
                first_reaction is not None
                and elapsed >= float(first_reaction)
                and cur_cnt <= base_agent_cnt
                and not atts
                and not has_stop
            ):
                raise TimeoutError(
                    f"发送后 {first_reaction:g} 秒页面没有任何反应——生成消息很可能没有发出去"
                )

            if elapsed > SEND_RETRY_AFTER and cur_cnt <= base_agent_cnt:
                unsent = await self._composer_unsent()
                if unsent and not resent:
                    resent = True
                    await self._click_send()
                elif unsent and elapsed > SEND_FAIL_AFTER:
                    raise TimeoutError(
                        f"提示词没有发出去（{unsent} 仍在输入框里），终止等待"
                    )

            if QUOTA_PATTERN.search(tail):
                raise QuotaExhausted("账号额度不足")

            if (
                elapsed > NOT_CONFIRMED_AFTER
                and cur_cnt <= base_agent_cnt
                and NOT_CONFIRMED_PATTERN.search(tail)
            ):
                raise TimeoutError(
                    "提示词未成功送达后端（Still sending / Delivery not confirmed）"
                )

            source = cur_txt if cur_cnt > base_agent_cnt else (
                tail if seen_stop and not has_stop and elapsed > 20.0 else ""
            )
            refusal = _media_refusal_text(source, expect)
            if refusal and not has_stop and len(atts) <= base_att_cnt:
                refusal_last, refusal_stable = (
                    (refusal_last, refusal_stable + 1)
                    if refusal == refusal_last
                    else (refusal, 0)
                )
                if refusal_stable >= 4 and elapsed > 8.0:
                    raise GenerationRefused(f"模型未生成媒体，仅返回文本: {refusal}")
            else:
                refusal_last, refusal_stable = "", 0

            if (
                cur_cnt > base_agent_cnt
                and cur_txt
                and not has_stop
                and len(atts) <= base_att_cnt
            ):
                if _media_status_pending(cur_txt):
                    txt_stable = 0
                else:
                    last_txt, txt_stable = (
                        (last_txt, txt_stable + 1) if cur_txt == last_txt else (cur_txt, 0)
                    )
                    if txt_stable >= 15 and elapsed > 8.0:
                        raise GenerationRefused(
                            f"模型未生成媒体，仅返回文本: {_clean_model_text(cur_txt)}"
                        )
            else:
                txt_stable = 0

            if (
                expect == "video"
                and seen_stop
                and not has_stop
                and path.rstrip("/").endswith("/thread/new")
                and cur_cnt <= base_agent_cnt
                and len(atts) <= base_att_cnt
                and elapsed >= VIDEO_STALL_ELAPSED
                and (time.monotonic() - last_activity) >= VIDEO_STALL_IDLE
            ):
                raise TimeoutError(
                    "Muse 未建立视频任务线程，页面在提交结束后持续无响应（已提前停止等待）"
                )

        return None

    async def _composer_unsent(self) -> str:
        """输入框还留着什么（附件数 / 提示词字数）；空串表示输入框是干净的。

        实测来源：engine.py:1107-1119。读不到时按空处理，不能把"读失败"
        误判成"没发出去"。
        """
        try:
            count = await self.attached_count()
            raw = await self._js(_JS_COMPOSER_TEXT_LEN, self._cfg())
            length = raw if isinstance(raw, int) else self._as_dict(raw).get("length", 0)
            length = int(length or 0)
        except Exception:
            return ""
        parts: list[str] = []
        if count:
            parts.append(f"{count} 个附件")
        if length:
            parts.append(f"{length} 字提示词")
        return "、".join(parts)

    async def _click_send(self) -> str:
        """只点发送按钮（不重填文字）。返回 clicked / disabled / no-button / error。"""
        assert self._page is not None
        raw = await self._js(_JS_CLICK_SEND, self._cfg())
        if isinstance(raw, str):
            return raw
        return "error"

    async def extract_media(
        self, src: str, *, expect: str, retries: int = 4
    ) -> tuple[bytes, str, str]:
        """在页面上下文里把媒体取成字节。

        实测来源：engine.py:1380-1399 的 `_EXTRACT_JS` + engine.py:1551-1564。

        为什么必须"在页面里 fetch"而不是自己发 HTTP 请求：muse 回填的
        src 经常是 `blob:` 或相对地址，它们只在那个页面上下文里有意义
        ——`blob:` 是浏览器为当前页面内存对象生成的临时 URL，脱离页面根本
        解析不出来；相对地址则依赖页面当前的 origin 与 cookie。所以取字节
        这一步天然属于浏览器，不属于协议层。
        """
        last = "未知"
        for _ in range(max(1, int(retries))):
            raw = await self._js(_JS_EXTRACT, [src, expect])
            info = self._as_dict(raw)
            if info.get("ok"):
                b64 = str(info.get("b64") or "")
                try:
                    data = base64.b64decode(b64, validate=True)
                except Exception:
                    last = "base64 解码失败"
                    await asyncio.sleep(2.0)
                    continue
                mime = str(info.get("mime") or "")
                url = str(info.get("url") or src)
                if not data:
                    last = "取回 0 字节"
                    await asyncio.sleep(2.0)
                    continue
                return data, mime, url
            last = str(info.get("err") or "页面未返回结果")
            await asyncio.sleep(2.0)
        raise MediaExtractError(f"未能取回生成结果: {last}")

    async def download_fallback(self, src: str, *, timeout: float = 180.0) -> str | None:
        """取不到字节时的兜底：点这张卡片自己的下载按钮，等文件落盘。

        实测来源：engine.py:1566-1595 的 `_download_fallback`。**只在该 src
        对应的附件卡片子树里找下载按钮**，找不到就返回 None——上游注释
        （engine.py:1570-1571）写得很清楚：点全局/无关的下载按钮会把用户上传的
        参考素材下载回来，等于对外开一个代理。这条是 fail closed，不许放宽。

        与上游的差异：上游给每个账号建独立 browserContext 并设置下载目录，
        muse-bridge 复用用户已经打开的那个 Chrome（一台机器一个登录态），
        因此这里用浏览器级 setDownloadBehavior + 每次调用一个临时目录。
        """
        assert self._page is not None
        directory = tempfile.mkdtemp(prefix="muse-bridge-dl-")
        session = None
        try:
            session = await self._enable_download_behavior(directory)
        except Exception:
            with contextlib.suppress(Exception):
                if session is not None:
                    await session.detach()
            return None

        before = set(os.listdir(directory))
        clicked = await self._js(_JS_DOWNLOAD_CLICK, [src, self._cfg()["hint"]])
        if clicked != "ok":
            with contextlib.suppress(Exception):
                await session.detach()
            return None

        deadline = time.monotonic() + timeout
        picked: str | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            fresh = [
                name
                for name in (set(os.listdir(directory)) - before)
                if not name.endswith(".crdownload")
            ]
            if fresh:
                candidate = os.path.join(
                    directory, max(fresh, key=lambda n: os.path.getmtime(os.path.join(directory, n)))
                )
                if os.path.getsize(candidate) > 0:
                    picked = candidate
                    break
        with contextlib.suppress(Exception):
            await session.detach()
        return picked

    async def _enable_download_behavior(self, directory: str):
        """CDP 打开下载落盘。非 Chromium / CDP 不可用时直接抛错，由调用方兜底。"""
        assert self._page is not None
        session = await self._page.context.new_cdp_session(self._page)
        await session.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": directory},
        )
        return session

    async def generate(
        self,
        prompt: str,
        *,
        expect: str = "image",
        timeout: float = 240.0,
        reference_paths: list[str] | None = None,
        deadline: float | None = None,
    ) -> dict:
        """完整一次生成：挂参考素材 → 发送 → 等成品 → 取字节 → 落盘。

        实测来源：engine.py:1824-1918（`generate` + `_generate_attached`）。
        保留的两条硬规则：

        * **成品哈希撞上上传件就跳过继续等**（engine.py:1876-1880,1901-1906）：
          muse 有时会把刚上传的素材渲染成"结果"，直接交付就是把用户的输入
          当成模型的输出。这里把该 src 并进 baseline，继续等真正的成品；
          整轮都只拿到上传件时抛错，绝不交付假结果。
        * **期望视频而拿到图片一律硬拒绝**（engine.py:1888-1890,1911-1913）。

        sha256 与 kind 判定全部走 `refs.py`（与入站参考素材同一套判定），
        成品 mime 优先按文件头 sniff，其次按页面声明，两者冲突以文件头为准
        （refs.py:437-446 的同一原则）。
        """
        await self.connect()
        limit = deadline if deadline is not None else time.monotonic() + timeout
        paths = [p for p in (reference_paths or []) if p]

        if paths:
            await self.attach_references(paths, expect=len(paths))
            upload_hashes = set(getattr(self, "_reference_hashes", set()) or set())
        else:
            await self.clear_attachments()
            upload_hashes = set()

        if time.monotonic() >= limit:
            raise TimeoutError("任务准备超时，已停止发送")

        before = await self.media_links()
        baseline_sources = {
            att[key]
            for att in before
            for key in ("src", "vSrc", "iSrc")
            if att[key]
        }

        await self._send(prompt)

        while time.monotonic() < limit:
            att = await self.wait_media(
                expect,
                timeout=max(0.0, limit - time.monotonic()),
                baseline_srcs=sorted(baseline_sources),
            )
            if not att:
                raise TimeoutError("等待生成超时，未出现新的生成结果")

            selected = (att["vSrc"] if expect == "video" else None) or att["src"]
            data: bytes | None = None
            mime = ""
            url = ""
            try:
                data, mime, url = await self.extract_media(selected, expect=expect)
            except MediaExtractError:
                data = None

            if data and _digest(data) in upload_hashes:
                baseline_sources |= {att[key] for key in ("src", "vSrc", "iSrc") if att[key]}
                await asyncio.sleep(0.1)
                continue

            if data:
                ext = _pick_ext(mime, url, expect)
                sniffed = refs.sniff_mime(data)
                final_mime = refs.normalize_mime(sniffed or mime or "")
                kind = refs.reference_kind(final_mime) if final_mime else (
                    "video" if ext in refs.VIDEO_EXTS else "image"
                )
                if expect == "video" and kind != "video":
                    raise MediaExtractError(
                        f"期望生成视频，但提取成品类型为图片 ({ext})，拒绝以图片冒充视频交付"
                    )
                return self._deliver(data, ext, final_mime, kind, "blob", att)

            path = await self.download_fallback(selected)
            if not path:
                raise MediaExtractError("已生成但未能取回文件")

            with open(path, "rb") as handle:
                blob = handle.read()
            if _digest(blob) in upload_hashes:
                with contextlib.suppress(Exception):
                    os.remove(path)
                baseline_sources |= {att[key] for key in ("src", "vSrc", "iSrc") if att[key]}
                await asyncio.sleep(0.1)
                continue

            ext = os.path.splitext(path)[1].lower() or ".bin"
            sniffed = refs.sniff_mime(blob)
            kind = (
                refs.reference_kind(refs.normalize_mime(sniffed or ""))
                if sniffed
                else ("video" if ext in refs.VIDEO_EXTS else "image")
            )
            if expect == "video" and kind != "video":
                with contextlib.suppress(Exception):
                    os.remove(path)
                raise MediaExtractError(
                    f"期望生成视频，但下载成品类型为图片 ({ext})，拒绝以图片冒充视频交付"
                )
            destination = os.path.join(_media_dir(), f"{os.urandom(16).hex()}{ext}")
            with open(destination, "wb") as handle:
                handle.write(blob)
            with contextlib.suppress(Exception):
                os.remove(path)
            return {
                "path": destination,
                "filename": os.path.basename(destination),
                "size": len(blob),
                "ext": ext,
                "mime": refs.normalize_mime(sniffed or "") or "",
                "kind": kind,
                "via": "download",
                "w": att.get("w"),
                "h": att.get("h"),
            }

        raise TimeoutError("页面只返回了本次上传的参考素材，未出现新的生成结果")

    def _deliver(
        self, data: bytes, ext: str, mime: str, kind: str, via: str, att: dict
    ) -> dict:
        directory = _media_dir()
        os.makedirs(directory, exist_ok=True)
        name = f"{os.urandom(16).hex()}{ext}"
        destination = os.path.join(directory, name)
        with open(destination, "wb") as handle:
            handle.write(data)
        return {
            "path": destination,
            "filename": name,
            "size": len(data),
            "ext": ext,
            "mime": mime,
            "kind": kind,
            "via": via,
            "w": att.get("w"),
            "h": att.get("h"),
        }

    async def quota(self, *, timeout: float = 12.0) -> dict:
        """打开 Settings → Usage 读额度。

        实测来源：engine.py:888-908（点击序列）与 engine.py:813-820（额度面板
        形态）。muse.ai 没有额度 REST 接口——上游反复确认只能读面板文本，
        所以这里也是"必须浏览器"的一块，见 quota.py 的解析规则。

        读到 found=False 时抛错而不是返回空 dict：上层要保留旧缓存
        （engine.py:966-967 的同一结论）。

        为什么失败要重试一次：2026-11-11 真机上 bridge 刚重启后第一次请求
        504（`quota_unavailable`）、紧接着同一账号再请求一次就 200。入口是
        hover 展开的 dock 按钮，冷启动第一下很容易点在"还没展开/动画中"的
        位置上，这不是账号问题，重试一次比把 504 丢给调用方更贴近实际。
        """
        await self.connect()
        cfg = self._cfg()
        last_error = ""
        for attempt in range(2):
            try:
                # 对话框可能已经开着（上一次读额度成功/失败都留着它）：先看文本，
                # 再决定要不要走"入口→菜单"两跳，否则模态遮罩会把入口按钮挡住。
                opened = await self._open_quota_panel(cfg)
            except TimeoutError as exc:
                last_error = str(exc)
                opened = False
            if opened:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    text = await self._quota_dialog_text(cfg)
                    parsed = parse_quota_text(text or "")
                    if parsed["found"]:
                        await self._close_overlay()
                        return parsed
                    await asyncio.sleep(0.2)
                last_error = "额度面板未读到有效用量；旧缓存未覆盖"
            # 关一次面板再重开：第一次失败几乎都是遮罩/焦点/动画的残留状态。
            await self._close_overlay()
            if attempt == 0:
                await asyncio.sleep(0.4)
        await self._close_overlay()
        raise TimeoutError(last_error or "额度面板未读到有效用量；旧缓存未覆盖")

    async def _open_quota_panel(self, cfg: dict) -> bool:
        """把设置对话框打开到位（入口→菜单两跳），已经开着就直接返回。

        2026-11-11 真机：入口按钮（dock 侧栏的"设置"，data-testid=hatch-dock-more）
        只认真实鼠标事件——JS 的 `el.click()` 不触发它的 React 指针处理，菜单不会开。
        且该按钮先弹「键盘快捷键/下载应用/报告问题/设置」菜单，再点菜单里的"设置"
        才进设置对话框（旧实现按 textContent 全等匹配菜单项，而真实文本带
        "Ctrl+," 快捷键后缀，所以永远匹配不到 → 504）。
        """
        if await self._quota_dialog_text(cfg):
            return True
        if not await self._click_settings_entry(cfg):
            raise TimeoutError("找不到设置入口（Settings／設定／设置）")
        if await self._quota_dialog_text(cfg):
            return True
        if not await self._click_settings_menu(cfg):
            raise TimeoutError("设置菜单未出现可识别的设置项")
        return True

    async def _close_overlay(self) -> None:
        """把设置对话框关掉：留着它，下一轮发送消息会点不到输入框。"""
        assert self._page is not None
        for _ in range(2):
            with contextlib.suppress(Exception):
                await self._page.keyboard.press("Escape")
            await asyncio.sleep(0.15)

    async def _click_settings_entry(self, cfg: dict) -> bool:
        """点侧栏"设置"入口（真实鼠标事件，先 hover 再 click）。

        实测（2026-11-11）：dock 侧栏是 hover 才展开的可点击项，Playwright 的
        click 自带 hover，JS `.click()` 不会触发；候选按 selectors.QUOTA_SETTINGS_ENTRY
        顺序试，全失败再用 aria-label 精确兜底。
        """
        assert self._page is not None
        candidates = [*cfg["quotaEntry"], 'button[aria-label="Settings"]',
                      'button[aria-label="設定"]', 'button[aria-label="设置"]']
        for selector in candidates:
            locator = self._page.locator(selector)
            try:
                count = await locator.count()
            except Exception:
                continue
            for index in range(min(count, 4)):
                node = locator.nth(index)
                try:
                    if not await node.is_visible():
                        continue
                    box = await node.bounding_box(timeout=2000)
                    if box:
                        await self._page.mouse.move(
                            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                        )
                    await node.click(timeout=4000)
                    return True
                except Exception:
                    continue
        return False

    async def _click_settings_menu(self, cfg: dict) -> bool:
        """在 dock 弹出的菜单里点"设置"项（文本可能带 Ctrl+, 快捷键后缀）。"""
        assert self._page is not None
        labels = ("Settings", "設定", "设置")
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            selectors = [*cfg["quotaMenu"],
                         '[role="menu"] button', '[role="menu"] a']
            for selector in selectors:
                locator = self._page.locator(selector)
                try:
                    count = await locator.count()
                except Exception:
                    continue
                for index in range(count):
                    node = locator.nth(index)
                    try:
                        text = " ".join((await node.inner_text()).split())
                    except Exception:
                        continue
                    if not text:
                        continue
                    if any(text == label or text.startswith(f"{label} ")
                           for label in labels):
                        try:
                            await node.click(timeout=4000)
                            return True
                        except Exception:
                            continue
            await asyncio.sleep(0.15)
        return False

    async def _quota_dialog_text(self, cfg: dict) -> str:
        """当前可见设置对话框的纯文本；没有对话框返回空串。"""
        assert self._page is not None
        try:
            raw = await self._js(_JS_QUOTA_DIALOG, cfg)
        except Exception:
            return ""
        text = raw if isinstance(raw, str) else self._as_dict(raw).get("text", "")
        return text or ""

    async def apply_cookies(
        self, cookies: dict, expires: dict | None = None
    ) -> bool:
        """把账号 cookie 注进当前浏览器上下文。

        实测来源：engine.py:626-653（`_apply_cookies`）。三个不能省的点：

        1. **先 clearBrowserCookies**：muse-bridge 复用用户已经打开的 Chrome，
           不彻底清掉上一个账号的 cookie 就会串号。
        2. **expires 必须抬到未来**：落在 now+3600 以内的 expires 会被 Chromium
           静默丢弃——`Network.setCookie` 返回成功、cookie 却不在 jar 里，症状是
           "导入即失效"。判定统一走 `session_http.injection_expires()`。
        3. **两个 domain 各写一遍**（`.muse.ai` 与 `muse.ai`）：muse 的接口有的
           按 host-only 下发，有的按域下发，少一个就会在某些子域上丢会话。

        只在确实是同一个登录态时调用（muse-bridge 的默认形态是"复用已登录
        Chrome、不碰 cookie"）；这条方法是给导号/多号切换场景准备的。
        """
        await self.connect()
        assert self._page is not None
        try:
            session = await self._page.context.new_cdp_session(self._page)
        except Exception:
            return False

        written = 0
        try:
            with contextlib.suppress(Exception):
                await session.send("Network.clearBrowserCookies")
            for name, value in (cookies or {}).items():
                if not value:
                    continue
                expiry = session_http.injection_expires(
                    _as_int((expires or {}).get(name))
                )
                for domain in (".muse.ai", "muse.ai"):
                    try:
                        await session.send(
                            "Network.setCookie",
                            {
                                "name": name,
                                "value": str(value),
                                "domain": domain,
                                "path": "/",
                                "secure": True,
                                "expires": float(expiry),
                            },
                        )
                        written += 1
                    except Exception:
                        continue
        finally:
            with contextlib.suppress(Exception):
                await session.detach()
        return written > 0


# ======================================================================
# 生成能力的模块级实现：常量、异常、JS 片段、纯函数判定。
# ======================================================================

# 成品落盘目录。项目里没有现成的 MEDIA_DIR（env.py 不归本轮改），
# 所以在此读取环境变量，默认 data/media ——与 store.py 的 data 目录同级。
MEDIA_DIR = os.environ.get("MUSE_MEDIA_DIR", os.path.join("data", "media"))

ATTACH_SETTLE_SECONDS = 0.35  # 上游 engine.py:1794 的每件间隔
ATTACH_POLL_SECONDS = 0.5     # 上游 engine.py:1810 用 0.3，这里少一轮空转
MEDIA_POLL_INTERVAL = 0.6     # 上游 engine.py:1177
SEND_RETRY_AFTER = 25.0       # 上游 engine.py:1268
SEND_FAIL_AFTER = 60.0        # 上游 engine.py:1274
NOT_CONFIRMED_AFTER = 30.0    # 上游 engine.py:1280
VIDEO_STALL_ELAPSED = 180.0   # 上游 engine.py:1321
VIDEO_STALL_IDLE = 90.0       # 上游 engine.py:1321

VIDEO_SUFFIXES = (".mp4", ".webm", ".mov")

QUOTA_PATTERN = re.compile(r"额度不足|积分不足|out of credits|达到上限|token limit", re.I)
NOT_CONFIRMED_PATTERN = re.compile(
    r"Still sending|Delivery not confirmed|正在发送|未确认送达", re.I
)


class QuotaExhausted(RuntimeError):
    """账号额度不足。重试换号才有意义，继续等只会白占浏览器。"""


class MediaExtractError(RuntimeError):
    """已经生成，但字节取不回来（或取回来的成品类型不对）。"""


class GenerationRefused(RuntimeError):
    """模型明确拒绝生成媒体，只用文字回复。换提示词/换号，不是超时。"""


class ReferenceUploadError(RuntimeError):
    """参考素材没能进入输入框（类型/大小不合格，或找不到上传控件）。"""


def _media_dir() -> str:
    return os.environ.get("MUSE_MEDIA_DIR", MEDIA_DIR)


def _as_int(value) -> int | None:
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _clean_model_text(text: str, limit: int = 240) -> str:
    """上游 engine.py:1326-1329：折叠空白、截到 240 字再进错误信息。"""
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _media_status_pending(text: str) -> bool:
    """"已经生成好了"这类话可能出现在附件之前，它不是交付证据，也不能当拒绝。

    逐字对齐上游 engine.py:1331-1357：先用否定式模式排除"没生成/生成失败"，
    再用肯定式模式判定"这是在报告媒体状态"，此时要继续等而不是报错。
    """
    if re.search(
        r"(?:未|没(?:有)?|无法|不能|未能)[^。！？!?\n]{0,16}(?:生成|制作|提供|完成)|"
        r"(?:生成|制作|渲染|处理)[^。！？!?\n]{0,12}(?:失败|未成功)|"
        r"\b(?:not|never|hasn['’]t|wasn['’]t|isn['’]t)\s+(?:(?:be|been|successfully)\s+){0,3}"
        r"(?:generated|created|completed|ready)|"
        r"\b(?:no|without)\s+(?:video|clip|image|picture|animation)\b"
        r"[^.\n]{0,60}\b(?:generated|created|ready)|"
        r"\b(?:failed|cannot|can['’]t|could not|couldn['’]t|unable to)\b"
        r"[^.\n]{0,60}\b(?:generat(?:e|ed|ing)|creat(?:e|ed|ing)|render|provide|video|image)\b|"
        r"\b(?:video|image|render|upload)\b[^.\n]{0,32}\bfailed\b",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"\.(?:webp|png|jpe?g|mp4|webm)|imagine_media|deliverable|"
            r"generated\s+.*image|verified\s+generated|artifact|"
            r"^(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)(?:[，。！\s]|$)|"
            r"(?:视频|影片|短片|图片|图像)[^。！？!?\n]{0,40}"
            r"(?:已(?:经)?(?:成功)?生成|生成(?:成功|好了|完毕|完成)|已(?:经)?完成|做好了)|"
            r"(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)"
            r"[^。！？!?\n]{0,32}(?:视频|影片|短片|图片|图像)|"
            r"\b(?:video|clip|image|picture|animation)\b[^.\n]{0,60}"
            r"\b(?:generated|created|rendered|completed|ready)\b|"
            r"\b(?:generated|created|rendered|completed)\b[^.\n]{0,32}"
            r"\b(?:video|clip|image|picture|animation)\b",
            text,
            re.I,
        )
    )


# 拒绝文案（上游 engine.py:1359-1377）：只认明确的媒体拒绝行，
# 绝不把任意页面尾部当成拒绝——否则页面滚动一下就变成"模型拒绝"。
_REFUSAL_VIDEO = (
    r"^(?:没生成成|视频没生成成功|视频未生成成功|视频生成失败)[^\n]{0,240}"
    r"(?:国家|地区|country|region)[^\n]{0,160}(?:不支持|不可用|无法|not available|unavailable|not supported)",
    r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}video[^\n]{0,160}"
    r"(?:country|region|not available|unavailable|not supported)",
)
_REFUSAL_IMAGE = (
    r"^(?:图片没生成成功|图片未生成成功|图片生成失败|无法生成图片)",
    r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}image",
)


def _media_refusal_text(text: str, expect: str) -> str:
    patterns = _REFUSAL_VIDEO if expect == "video" else _REFUSAL_IMAGE
    for line in reversed((text or "").splitlines()):
        clean = _clean_model_text(line)
        if clean and any(re.search(pattern, clean, re.I) for pattern in patterns):
            return clean
    return ""


def _pick_ext(mime: str, url: str, expect: str) -> str:
    """上游 engine.py:1920-1931：先按 mime，再按 URL 片段，最后按期望类型兜底。"""
    lowered = (mime or "").lower()
    for key, ext in (
        ("mp4", ".mp4"),
        ("webm", ".webm"),
        ("png", ".png"),
        ("jpeg", ".jpg"),
        ("jpg", ".jpg"),
        ("webp", ".webp"),
        ("gif", ".gif"),
    ):
        if key in lowered:
            return ext
    haystack = (url or "").lower()
    for ext in (".mp4", ".webm", ".png", ".jpg", ".webp"):
        if ext in haystack:
            return ext
    return ".mp4" if expect == "video" else ".png"


# ---------------------------------------------------------------- JS 片段
# 统一签名：(cfg) => ... 或 (arg) => ...，cfg 由 MuseDriver._cfg() 提供，
# 选择器一律来自 selectors.py，脚本里不内联任何 DOM 字面量。

_JS_HYDRATION = """(cfg) => {
  const pick = (list) => {
    for (const s of (list || [])) {
      const el = document.querySelector(s);
      if (el) return el;
    }
    return null;
  };
  if (document.readyState !== 'complete') return 'loading';
  const ta = pick(cfg.inputs) || document.querySelector('textarea');
  if (!ta) return 'no-ta';
  const h = pick(cfg.hydrated) || document.querySelector('[data-hatch-shell-hydration-state]');
  if (h && h.getAttribute('data-hatch-shell-hydration-state') !== 'hydrated') return 'hydrating';
  const body = document.body ? (document.body.innerText || '') : '';
  if (body.indexOf('Connecting...') !== -1) return 'connecting';
  // 2026-11-11 真机：中文界面显示「Muse 正在连接…」，只认英文时这个状态会被
  // 误判成 ready，于是发送路径静默失败、最后报"180 秒内没有产生新回答"。
  if (body.indexOf('正在连接') !== -1) return 'connecting';
  if (ta.disabled) return 'input-disabled';
  return 'ready';
}"""

_JS_COMPOSER_ROOTS = """
const composerRoots = (cfg) => {
  const out = [];
  for (const s of (cfg.root || [])) {
    document.querySelectorAll(s).forEach((el) => out.push(el));
  }
  if (out.length) return out;
  const ta = document.querySelector('textarea');
  if (!ta) return [];
  const form = ta.closest('form');
  if (form) return [form];
  let el = ta.parentElement;
  for (let i = 0; i < 4 && el && el !== document.body; i++) { out.push(el); el = el.parentElement; }
  return out;
};
"""

_JS_ATTACHED_COUNT = """(cfg) => {
""" + _JS_COMPOSER_ROOTS + """
  const rx = /remove\\s*attachment|移除附件|删除附件/i;
  const inScope = (el, list) => (list || []).some((s) => el.closest(s));
  const roots = composerRoots(cfg);
  for (const root of roots) {
    let n = 0;
    for (const b of root.querySelectorAll('button[aria-label]')) {
      const al = b.getAttribute('aria-label') || '';
      if (rx.test(al) && !inScope(b, cfg.exclude)) n++;
    }
    if (n) return n;
  }
  return 0;
}"""

_JS_CLEAR_ATTACHMENTS = """(cfg) => {
""" + _JS_COMPOSER_ROOTS + """
  const rx = /remove\\s*attachment|移除附件|删除附件|移除|删除/i;
  const roots = composerRoots(cfg);
  const seen = new Set();
  let clicked = 0;
  for (const root of roots) {
    for (const b of root.querySelectorAll('button[aria-label]')) {
      if (seen.has(b)) continue;
      const al = b.getAttribute('aria-label') || '';
      if (!rx.test(al)) continue;
      if ((cfg.exclude || []).some((s) => b.closest(s))) continue;
      seen.add(b);
      try { b.click(); clicked++; } catch (e) {}
    }
  }
  for (const s of (cfg.fileInputs || [])) {
    document.querySelectorAll(s).forEach((inp) => { inp.value = ''; });
  }
  return clicked;
}"""

_JS_MEDIA = """(cfg) => {
  const list = [];
  const seen = new Set();
  const inScope = (el, items) => (items || []).some((s) => el.closest(s));
  const addEl = (el, tid) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    if (inScope(el, cfg.exclude)) return;
    const v = el.querySelector('video') || (el.tagName === 'VIDEO' ? el : null);
    const img = el.querySelector('img') || (el.tagName === 'IMG' ? el : null);
    const isVid = (tid || '').includes('video') || !!v;
    const primary = isVid ? (v || img) : (img || v);
    const src = primary ? (primary.currentSrc || primary.src || '') : '';
    if (!src || seen.has(src)) return;
    seen.add(src);
    list.push({
      tid: tid || el.getAttribute('data-testid') || (isVid ? 'video' : 'image'),
      has_video: !!v,
      has_img: !!img,
      src: src,
      vSrc: v ? (v.currentSrc || v.src || '') : '',
      iSrc: img ? (img.currentSrc || img.src || '') : '',
      w: primary ? (primary.videoWidth || primary.naturalWidth || 0) : 0,
      h: primary ? (primary.videoHeight || primary.naturalHeight || 0) : 0
    });
  };
  for (const s of (cfg.attachments || [])) {
    document.querySelectorAll(s).forEach((a) => addEl(a, a.getAttribute('data-testid')));
  }
  for (const s of (cfg.media || [])) {
    document.querySelectorAll(s).forEach((m) => {
      const src = m.currentSrc || m.src || '';
      if (!src || src.includes('avatar') || src.includes('emoji')) return;
      addEl(m.parentElement || m, 'agent-media');
    });
  }
  return list;
}"""

_JS_STATE = """(cfg) => {
  const scope = document.querySelector('main') || document.body;
  const bubbles = Array.from(scope ? scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]') : []);
  const agents = bubbles.filter((b) => /hatch-agent-bubble-bg/.test(b.className || ''));
  let stop = false;
  for (const s of (cfg.stop || [])) {
    if (document.querySelector(s)) { stop = true; break; }
  }
  if (!stop) {
    stop = Array.from(document.querySelectorAll('button[aria-label]')).some((b) =>
      /stop|停止/i.test(b.getAttribute('aria-label') || ''));
  }
  let err = '';
  for (const s of (cfg.errors || [])) {
    const e = document.querySelector(s);
    if (e) { err = e.innerText || ''; break; }
  }
  return {
    cnt: agents.length,
    txt: agents.length ? (agents[agents.length - 1].innerText || '').trim() : '',
    stop: stop,
    tail: ((document.body ? (document.body.innerText || '') : '') + (err ? '\\n' + err : '')).slice(-1200),
    path: location.pathname || '',
    href: location.href || ''
  };
}"""

_JS_COMPOSER_TEXT_LEN = """(cfg) => {
  const pick = (list) => {
    for (const s of (list || [])) {
      const el = document.querySelector(s);
      if (el) return el;
    }
    return null;
  };
  const ta = pick(cfg.inputs) || document.querySelector('textarea');
  return { length: ta ? (ta.value || '').length : 0 };
}"""

_JS_CLICK_SEND = """(cfg) => {
  const visible = (e) => e.offsetParent !== null;
  const wanted = (el) => {
    const label = (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('data-testid') || '');
    return /发送|send/i.test(label) || /send/i.test(el.innerText || '');
  };
  const candidates = Array.from(document.querySelectorAll('button,[role=button]')).filter(visible);
  let b = candidates.find(wanted);
  if (!b) {
    for (const s of (cfg.send || [])) {
      const hit = candidates.find((el) => el.matches(s));
      if (hit) { b = hit; break; }
    }
  }
  if (!b) return 'no-button';
  if (b.disabled) return 'disabled';
  b.click();
  return 'clicked';
}"""

_JS_DOWNLOAD_CLICK = """(pair) => {
  const src = pair[0];
  const hints = pair[1] || [];
  const medias = Array.from(document.querySelectorAll('img,video'));
  const media = medias.find((m) => (m.currentSrc || m.src || '') === src);
  if (!media) return 'none';
  let node = media.closest('[data-testid^="hatch-chat-attachment-presentation-"]');
  if (!node) return 'none';
  for (const s of hints) {
    const up = node.closest(s);
    if (up) { node = up; break; }
  }
  const b = Array.from(node.querySelectorAll('button,[role=button]')).find((x) =>
    /下载|保存|download/i.test(x.getAttribute('aria-label') || ''));
  if (!b) return 'none';
  b.click();
  return 'ok';
}"""

_JS_EXTRACT = """(async (pair) => {
  const src = pair[0];
  try {
    if (!src) return {ok: false, err: 'no-media-src'};
    const r = await fetch(src);
    if (!r.ok) return {ok: false, err: 'media-http-' + r.status};
    const b = await r.blob();
    const bytes = new Uint8Array(await b.arrayBuffer());
    let s = '';
    for (let i = 0; i < bytes.length; i += 65536) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 65536));
    }
    return {ok: true, mime: b.type || '', size: b.size, url: src, b64: btoa(s)};
  } catch (e) {
    return {ok: false, err: String(e)};
  }
})"""

# 设置入口与菜单项改为真实鼠标点击（见 `_click_settings_entry`），
# 旧的 JS `.click()` 脚本已删除：它对 dock 侧栏不生效。

# 助手消息的角色判定：命中节点自己或任一祖先落在"用户"标记里就跳过。
# 四组标记全取 2026-11-11 真机 muse.ai DOM：用户消息项带 data-role="user" /
# data-message-role="user" / data-message-side="user"，用户气泡自身带
# data-hatch-user-message-body="true"。用 closest() 是因为通用类名命中的往往是
# 气泡内部的子节点，角色属性挂在外层消息项上——只看节点本身会漏判。
_JS_INSIDE_USER = (
    "(el) => Boolean(el.closest('"
    '[data-role="user"],'
    '[data-message-role="user"],'
    '[data-message-side="user"],'
    '[data-hatch-user-message-body="true"]'
    "'))"
)

#: 最后一条助手消息的 `data-message-id`。`[data-message-item]` 是 2026-11-11
#: 真机确认的消息项容器，id 形如 `assistant-msg-<uuid>`；第二个选择器只是
#: 兜底（万一这版构建没给 message-item 包壳）。
_JS_LAST_ASSISTANT_ID = """
() => {
  const pick = (nodes) => nodes.length
    ? nodes[nodes.length - 1].getAttribute('data-message-id') || ''
    : '';
  const items = document.querySelectorAll(
    '[data-message-item][data-message-role="assistant"]'
  );
  if (items.length) return pick(items);
  return pick(document.querySelectorAll('[data-message-role="assistant"]'));
}
"""


_JS_QUOTA_DIALOG = """(cfg) => {
  for (const s of (cfg.quotaDialog || [])) {
    const d = Array.from(document.querySelectorAll(s)).find((e) =>
      e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
    if (d) return {text: d.innerText || ''};
  }
  return {text: ''};
}"""

