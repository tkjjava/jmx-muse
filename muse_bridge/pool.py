"""池化调度：挑账号 → 确保浏览器就绪 → 必要时 wake VM → 发问取答。

并发模型：同一账号（同一个 Chrome 窗口）在同一时刻只允许一个请求。
两个请求同时往同一个输入框打字会让 DOM 互相污染，回复也分不清归属。

锁的生命周期覆盖整个请求（含流式输出），所以锁随 DispatchResult 交出去，
由调用方在收尾时通过 release() 释放。

驱动二选一：**协议优先、浏览器兜底**。`_build_driver` 在 `MUSE_PROTOCOL_DRIVER`
打开、且该账号已导入 cookie 时返回 `ProtocolDriver`，**聊天与生成都走协议**
（生成链路 2026-11-11 已真机验收，见 protocol_driver 模块头）。生成若在协议侧
失败且属于"可换号重试"那一类，`generate_media` 会用
`capability="generation-browser"` 回退一次浏览器，任务不因链路选择失败。
默认关闭，既有部署行为不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from . import driver as driver_mod
from . import observability
from .driver import BrowserUnavailable, MuseDriver
from .env import env_flag
from .protocol_client import ProtocolError
from .protocol_driver import ProtocolDriver
from .protocol_engine import ProtocolGenerationError, retryable
from .state import AccountState, after_probe, is_dispatchable
from .store import AccountStore

logger = logging.getLogger(__name__)

MODEL_ALIASES = frozenset(
    {
        "muse-1",
        "muse",
        "muse-spark-1.3",
        "gpt-4o",
        "gpt-4o-mini",
        "claude-3-5-sonnet",
        "claude-opus-4",
        "gemini-pro",
    }
)
DEFAULT_MODEL = "muse-1"

# muse 是单一 agent，任何模型别名都路由到它
UPSTREAM_MODEL = "muse-spark-1.3"


class NoAccountAvailable(RuntimeError):
    pass


class AllAccountsBusy(RuntimeError):
    """所有账号都在处理请求中。muse 只能串行，这是正常的容量限制。"""


class ReplyTimeout(TimeoutError):
    """回复超过了 REPLY_TIMEOUT：已产出的部分是真实输出，但话没说完。

    继承 TimeoutError 是刻意的：既有调用方按 TimeoutError 捕获的语义不变，
    而 app 层可以据此把 finish_reason 报成 length 而不是 stop。
    """

    def __init__(self, account: str = "") -> None:
        super().__init__(f"{account or '账号'} 的回复超过超时上限，已产出部分保留")
        self.account = account


@dataclass
class DispatchResult:
    ok: bool
    account: str = ""
    driver: MuseDriver | ProtocolDriver | None = None
    verdict: str = ""
    note: str = ""
    # 该账号的互斥锁。调用方必须在请求结束时调用 release()。
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    async def release(self) -> None:
        """收尾：关闭驱动连接并释放账号锁。

        只断开连接（CDP 或 Noise），不关闭用户的 Chrome。
        """
        try:
            if self.driver is not None:
                await self.driver.close()
        finally:
            if self._lock is not None and self._lock.locked():
                self._lock.release()
            self._lock = None


_cursor = {"index": 0}

# 账号 → 互斥锁。模块级是刻意的：同一进程内所有请求共享这份调度状态。
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(account: str) -> asyncio.Lock:
    lock = _locks.get(account)
    if lock is None:
        lock = asyncio.Lock()
        _locks[account] = lock
    return lock


def forget_account(account: str) -> None:
    """删号后回收它的调度状态，锁表不随删号无界增长。"""
    _locks.pop(account, None)


def busy_accounts() -> list[str]:
    """当前正在处理请求的账号（给 /healthz 与排障用）。"""
    return [account for account, lock in _locks.items() if lock.locked()]


def _rotate(accounts: list) -> list:
    if not accounts:
        return []
    start = _cursor["index"] % len(accounts)
    _cursor["index"] = start + 1
    return accounts[start:] + accounts[:start]


def _protocol_driver_enabled() -> bool:
    """协议驱动总开关。**默认关**：不配这个变量就还是浏览器。

    每次调用都现读环境变量（不是模块级常量）：测试与运维都能在进程存活期间
    改它，不必重启；env.PROTOCOL_DRIVER 只是同一语义的模块级快照，供 app/doctor
    展示用。真值集合与 env.env_flag 共用一份实现，避免两处判定漂移。
    """
    return env_flag("MUSE_PROTOCOL_DRIVER", "0")


def _build_driver(
    store: AccountStore, record, *, capability: str
) -> MuseDriver | ProtocolDriver:
    """派单驱动二选一：**协议优先、浏览器兜底**，聊天与生成都走协议。

    `capability` 的三个取值各有含义：

      * `"chat"` / `"generation"`：开关打开 + 该账号导入了 cookie → `ProtocolDriver`；
        没有 cookie 就没有 hatch_sess 可续签，建出来只会连一次失败再回退，白白
        多烧一次握手，所以直接浏览器；
      * `"generation-browser"`：**强制浏览器**，仅供 `generate_media` 在协议生成
        失败后回退一次使用。给它单独的取值而不是布尔参数，是为了让"这次为什么
        没走协议"在调用栈里一眼可见。

    **三种"没走协议"的原因分开计数**（2026-11-11 补）：过去只记
    `browser_driver_built`，运维看到这个数涨了也不知道是"开关没开"还是"账号没
    导 cookie"——而这两者的处理办法完全不同。现在：

      * `protocol_disabled`：`MUSE_PROTOCOL_DRIVER=0`（默认），整台机器都没走协议；
      * `protocol_no_cookies`：开关开了但这个账号没有 cookie（导入后再刷新即可）；
      * `browser_driver_forced`：协议生成失败后的**主动**回退，不是缺陷。

    计数放在这里而不是 `dispatch()`：这里是唯一知道原因的地方，放两处会重复计数。
    """
    wants_protocol = capability in ("chat", "generation")
    if wants_protocol:
        if not _protocol_driver_enabled():
            observability.bump("protocol_disabled")
        else:
            cookies = store.cookies(record.account)
            if cookies:
                proxy = os.environ.get("MUSE_PROTOCOL_PROXY", "").strip() or None
                observability.bump("protocol_driver_built")
                return ProtocolDriver(record.account, cookies, proxy=proxy)
            observability.bump("protocol_no_cookies")
            logger.debug(
                "账号 %s 没有导入 cookie：协议路径不可用，本次走浏览器（先 POST "
                "/admin/accounts/%s/cookies 再 refresh 可修）",
                record.account,
                record.account,
            )
        observability.bump("browser_driver_built")
    elif capability == "generation-browser":
        observability.bump("browser_driver_forced")
    return MuseDriver(record.cdp_url)


def _generation_fallback_allowed(result: DispatchResult, error: BaseException) -> bool:
    """协议生成失败后是否允许回退浏览器（只允许一次，由调用方控制次数）。

    条件必须同时成立：① 这次确实是协议驱动；② 异常属于协议生成族；③ 该异常
    在语义上"可换号重试"（拒绝/额度不足是终态，换浏览器只会重复烧一次额度）。
    """
    return (
        isinstance(result.driver, ProtocolDriver)
        and isinstance(error, ProtocolGenerationError)
        and retryable(error)
    )


async def dispatch(
    store: AccountStore,
    account: str | None = None,
    *,
    capability: str = "chat",
) -> DispatchResult:
    """挑一个可用账号并建立驱动。失败时换下一个。

    同一账号（同一个浏览器窗口）同一时刻只允许一个请求——两个请求
    同时往同一个输入框打逐字，DOM 会互相污染，回复也分不清是谁的。

    `capability` 默认 `"chat"`，既有调用点（流式/非流式聊天、keepalive）不必改，
    行为与只有浏览器驱动时一致；只有生成管线显式传 `"generation"`，从而永远
    拿到浏览器驱动（见 `_build_driver`）。
    """
    if account:
        # 点名派单：对话室按发言归属选号，不可用就把原因抛给调用方
        record = store.get(account)
        if record is None:
            raise NoAccountAvailable(f"账号 {account} 不存在（先执行 attach）")
        if not is_dispatchable(
            record.state,
            now=int(time.time()),
            last_probe_ok_at=record.last_probe_ok_at,
            last_failure_at=record.last_failure_at,
        ):
            raise NoAccountAvailable(
                f"账号 {account} 不可派单（{record.state.value}：{record.last_error or '闸门未通过'}）"
            )
        candidates = [record]
    else:
        candidates = store.dispatchable()
    if not candidates:
        raise NoAccountAvailable(
            "账号池里没有可用的 muse 账号。先在 /console 执行 attach。"
        )

    last_error = ""
    busy: list[str] = []

    for record in _rotate(candidates):
        account = record.account

        lock = _lock_for(account)
        if lock.locked():
            # 这个号正忙，换下一个；全部忙则明确告知
            busy.append(account)
            continue

        # 非阻塞抢锁：抢到才是我的，抢不到就当忙处理
        await lock.acquire()
        # 抢到锁之后的一切都必须收干净：只有"成功交出去的"这次调用才保留锁，
        # 其余任何出口（continue / 未预期异常）都由 finally 关驱动 + 放锁。
        # 漏掉任何一条，这个账号就永久停在"忙"，只能重启进程。
        #
        # `driver` 先登记成 None：`_build_driver` 自己也可能抛（构造协议客户端、
        # 读 cookie），抢锁之后抛异常同样必须走 finally，否则同样会把号卡死。
        driver: MuseDriver | ProtocolDriver | None = None
        # 回退原因：协议驱动连不上时已经换了浏览器驱动。成功交出去时并进 note，
        # 调用方因此知道"这次没走协议"；失败分支不并 note，改为写账号状态。
        fallback_note = ""
        handed_off = False
        try:
            try:
                with observability.timed("warm_acquire"):
                    driver = _build_driver(store, record, capability=capability)
            except BrowserUnavailable as exc:
                # **构造期**也会抛 BrowserUnavailable：`MuseDriver.connect()` 里
                # 没有现成 muse.ai 标签页时会新开一个再 goto，出口断网就抛
                # （ERR_CONNECTION_CLOSED）。以前这条路径没人接，异常冲出
                # `dispatch()` 一路冒到 ASGI，用户拿到 500 Internal Server Error，
                # 而"为什么失败"只活在日志里。2026-11-11 用失效代理实测复现。
                # 处理方式与"探状态时抛"保持一致：记账号状态、换下一个号，
                # 不要把单个账号的浏览器故障升级成整个请求的 500。
                last_error = f"{account}: {exc}"
                store.mark_failure(
                    account,
                    state=AccountState.BROWSER_UNAVAILABLE,
                    error=str(exc),
                )
                continue
            # 驱动选择的计数（protocol_driver_built / browser_driver_built /
            # protocol_disabled / protocol_no_cookies / browser_driver_forced）
            # 一律由 `_build_driver` 完成：那里是唯一知道"为什么选了它"的地方。
            # 探一次驱动状态。协议驱动连不上（`ProtocolError`，只可能来自协议驱动）
            # 时**回退浏览器**：先把协议连接关干净再建浏览器驱动（漏关会留下一条
            # 没人收的 Noise 连接），然后重探一次。重探在同一个循环里，所以它失败
            # 时仍由既有的 BrowserUnavailable 分支负责——两个 except 的顺序不能反。
            #
            # 只允许回退一次：回退后浏览器驱动再抛 ProtocolError 说明驱动被注入坏
            # 了，再建第三个驱动只会无限循环，这里按失败收场。
            status = None
            while True:
                try:
                    status = await driver.status()
                    break
                except ProtocolError as exc:
                    if fallback_note:
                        last_error = f"{account}: {exc}"
                        break
                    fallback_note = (
                        "protocol_fallback: 协议连接失败"
                        f"（{type(exc).__name__}: {exc}），已回退浏览器"
                    )
                    observability.bump("protocol_fallback")
                    # 必须留一行日志（2026-11-11 补）。这次排障的实际教训：协议 WS
                    # 被上游重置时，回退到浏览器驱动后的失败是"页面 45 秒没反应"，
                    # 最终只回一个 504；根因（ConnectionResetError）只存在于内存里的
                    # fallback_note，落不到任何日志上。没有这行，就只能靠直连探针
                    # 二分定位。日志里只有异常类型与消息，不含 cookie/提示词。
                    logger.warning(
                        "账号 %s 协议驱动连接失败，回退浏览器：%s: %s",
                        account,
                        type(exc).__name__,
                        exc,
                    )
                    await driver.close()
                    driver = MuseDriver(record.cdp_url)
                except BrowserUnavailable as exc:
                    last_error = f"{account}: {exc}"
                    store.mark_failure(
                        account,
                        state=AccountState.BROWSER_UNAVAILABLE,
                        error=str(exc),
                    )
                    break
            if status is None:
                continue

            # `connected` 必须先判：真实 `MuseDriver.status()` 把 `connect()` 抛出的
            # `BrowserUnavailable` **吞掉**，返回 `connected=False, logged_in=False`。
            # 只看 `logged_in` 的话，"浏览器连不上"会被记成 SESSION_EXPIRED（要求
            # 重新登录），而真正该做的是等浏览器/网络恢复。两种状态的冷却与重试
            # 语义不同，误记会让账号一直卡在"等你重新登录"上。
            if not status.connected:
                last_error = f"{account}: 浏览器连接不可用"
                store.mark_failure(
                    account,
                    state=AccountState.BROWSER_UNAVAILABLE,
                    error=status.note or "浏览器连接不可用",
                )
                continue

            if not status.logged_in:
                last_error = f"{account}: 浏览器里 muse.ai 未登录"
                store.mark_failure(
                    account,
                    state=AccountState.SESSION_EXPIRED,
                    error=status.note or "未登录",
                )
                continue

            # VM 可能休眠，先尝试唤醒。唤醒是尽力而为——失败不阻断派单，但异常
            # 不能带着锁逃出去（此前这里裸调用，CDP 中途掉线就漏锁）。
            #
            # 唤醒失败**不能记成"已唤醒"**：派单路径只在 not record.vm_ready 时才
            # 尝试唤醒，谎报一次就等于此后每次派单都跳过唤醒，直接打到休眠中的
            # VM 上，账号会一直失败到有人手工 refresh。宁可如实记为 VM_SLEEPING
            # ——它同样在 DISPATCHABLE_STATES 里，下次派单会再试一次唤醒。
            vm_ready = bool(record.vm_ready)
            wake_note = ""
            if not vm_ready:
                try:
                    await driver.wake_vm()
                    vm_ready = True
                except Exception as exc:  # noqa: BLE001 - 唤醒失败不是派单失败
                    last_error = f"{account}: 唤醒失败 {type(exc).__name__}: {exc}"
                    wake_note = last_error  # 不静默：这条会随结果交回调用方

            state = after_probe(logged_in=True, now=int(time.time()), vm_ready=vm_ready)
            store.mark_ready(account, vm_ready=vm_ready, state=state)
            handed_off = True
            # 锁不在这里释放——调用方在整个请求结束后通过 release() 释放
            note = "；".join(part for part in (fallback_note, wake_note) if part)
            return DispatchResult(
                ok=True,
                account=account,
                driver=driver,
                note=note,
                _lock=lock,
            )
        finally:
            if not handed_off:
                try:
                    if driver is not None:
                        await driver.close()
                finally:
                    if lock.locked():
                        lock.release()

    if busy and not last_error:
        raise AllAccountsBusy(
            f"所有可用账号都在处理请求中（{len(busy)} 个）。muse 只能串行，请稍后重试。"
        )

    return DispatchResult(ok=False, verdict="all_accounts_failed", note=last_error)


async def stream_reply(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> AsyncIterator[str]:
    """流式产出增量。

    不负责关闭驱动或释放锁——那是调用方 release() 的事，
    这样锁能覆盖整个流式输出期间。

    这里再套一层超时保护：driver 自己也有 deadline，但那是它的事。
    如果 driver 实现有 bug 或前端一直显示"生成中"，没有这层就会无限产出，
    把连接和锁永远占住。
    """
    if not result.ok or result.driver is None:
        return

    deadline = time.monotonic() + timeout
    generator = result.driver.stream_reply(prompt, timeout=timeout)

    try:
        async for delta in generator:
            yield delta
            if time.monotonic() >= deadline:
                raise ReplyTimeout(account=getattr(result, "account", ""))
    finally:
        # 生成器被提前丢弃时要显式关闭，否则内部的循环不会退出
        await generator.aclose()





async def stream_reply_events(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> AsyncIterator[dict]:
    """流式产出 {"reasoning": 增量} 或 {"content": 增量}。

    driver 原生提供 stream_reply_events 时直接透传；否则包装模块全局 stream_reply
    （刻意用模块全局名调用，tests 里 monkeypatch.setattr(pool, "stream_reply", ...)
    的契约继续生效——字符串接口是既有契约，事件接口是它之上的兼容层）。

    与 stream_reply 一样不负责关锁/关驱动（那是调用方 release() 的事），并额外套
    deadline 保护：driver 若卡在"一直生成中"，到点即停，不无限占住账号。
    """
    if not result.ok:
        return

    deadline = time.monotonic() + timeout
    driver = result.driver

    if driver is not None and hasattr(driver, "stream_reply_events"):
        generator = driver.stream_reply_events(prompt, timeout=timeout)
    else:
        async def _wrap():
            async for delta in stream_reply(result, prompt, timeout=timeout):
                if delta:
                    yield {"content": delta}

        generator = _wrap()

    try:
        async for event in generator:
            yield event
            if time.monotonic() >= deadline:
                # 到点截断必须让调用方知道："话没说完"和"说完了"是两种结果，
                # 静默 return 会让上层报 finish_reason=stop，把半句话当成完整回复。
                raise ReplyTimeout(account=getattr(result, "account", ""))
    finally:
        await generator.aclose()
async def collect_reply_events(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> tuple[str, str]:
    """非流式收集。返回 (思考, 正文)。

    REASONING 关闭时正文走 collect_reply，思考为空串；打开时遍历事件接口——
    muse 的 content 增量没有打字机 sleep，非流式收集无额外延迟。
    """
    if not result.ok:
        return "", ""
    if not driver_mod.REASONING:
        return "", await collect_reply(result, prompt, timeout=timeout)
    reasoning = ""
    text = ""
    async for event in stream_reply_events(result, prompt, timeout=timeout):
        if event.get("reasoning"):
            reasoning += event["reasoning"]
        if event.get("content"):
            text += event["content"]
    return reasoning, text


async def collect_reply(
    result: DispatchResult, prompt: str, *, timeout: float = 180.0
) -> str:
    parts: list[str] = []
    async for delta in stream_reply(result, prompt, timeout=timeout):
        parts.append(delta)
    return "".join(parts)


# ---- 生成管线（图像/视频）------------------------------------------------
#
# 与聊天路径共用同一套账号锁（_lock_for / dispatch / release），但多两样东西：
# 任务表（jobs.TaskStore）与成品落盘（media.MediaStore）。上游 app.py:1100
# 的"排队+生成共用 timeout"在这里被拆开：抢锁等 QUEUE_WAIT_SECONDS，生成等
# IMAGE/VIDEO_TIMEOUT，两者互不挤占。


class GenerationError(RuntimeError):
    """生成管线自身的问题（类型不支持、开关关闭、编排失败）。映射 400/503。"""


class GenerationQuotaExhausted(GenerationError):
    """额度耗尽。**不换号重试**：换号只会把另一个号的额度也烧完。"""


class GenerationRefused(GenerationError):
    """模型拒绝。语义与聊天路径的 MuseModelRefusalError 一致：不重试。"""


# 额度类失败的特征词。驱动层的 QuotaExhausted 直接 isinstance 判定；
# 协议层/页面文本里冒出来的关键词走这里兜底。
_QUOTA_MARKERS = ("额度不足", "积分不足", "out of credits", "达到上限", "token limit")


def _classify(error: BaseException) -> str:
    """失败分类标记，写进 tasks.error 前缀，调用方按前缀决定换不换号。"""
    from .driver import QuotaExhausted as _Quota  # 局部导入避免循环

    lowered = str(error).lower()
    if isinstance(error, _Quota) or any(m in lowered for m in _QUOTA_MARKERS):
        return "quota"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "error"


async def generate_media(
    store: AccountStore,
    *,
    kind: str,
    prompt: str,
    references: list[str] | None = None,
    timeout: float | None = None,
    account: str | None = None,
    on_progress=None,
    job_id: str | None = None,
) -> dict:
    """一次图像/视频生成：入队 → 抢号 → 驱动生成 → 落盘 → 任务收尾。

    返回 job 的对外形状（含 media_token / url）。生成过程中的任何失败都会
    先把任务标成 failed 再把异常抛回调用方——任务表与实际状态必须一致，

    `job_id`：调用方已经有任务行时（`app._submit_task` 先建任务再起后台
    runner），这里**不要**再建一条。旧实现在两边各建一条，于是对外轮询的
    那条永远停在 queued、真正干活的那条躲在任务表里没人看——异步任务端点
    因此"永远排队"。给 `job_id` 就复用，不给才新建（脚本/同步调用路径不变）。
    不允许"异常抛了但任务还挂在 processing"。
    """
    from . import env, jobs, media

    if not env.GENERATION_ENABLED:
        raise GenerationError("生成能力已关闭（MUSE_GENERATION_ENABLED=0）")
    normalized = (kind or "").strip().lower()
    if normalized not in ("image", "video"):
        raise GenerationError(f"生成类型不支持：{kind!r}（只接受 image/video）")
    if not prompt.strip():
        raise GenerationError("提示词为空")

    tasks = jobs.TaskStore(store)
    refs_list = [str(r) for r in (references or []) if str(r).strip()]
    if job_id is None:
        job = tasks.create(
            normalized,
            prompt,
            params={"references": refs_list},
            account=account or "",
            ttl_seconds=env.TASK_TTL_SECONDS,
        )
        job_id = job.id
    else:
        job = tasks.must_get(job_id)

    # ---- 抢号：排队预算独立于生成预算 ----
    queue_deadline = time.monotonic() + env.QUEUE_WAIT_SECONDS
    while True:
        try:
            result = await dispatch(store, account=account, capability="generation")
            break
        except AllAccountsBusy:
            if on_progress is not None:
                on_progress(5)
            if time.monotonic() >= queue_deadline:
                tasks.fail(job_id, "queue: 排队超时，所有账号都忙")
                raise
            await asyncio.sleep(0.5)
        except NoAccountAvailable:
            tasks.fail(job_id, "pool: 没有可派单的账号")
            raise

    try:
        if not result.ok:
            tasks.fail(job_id, f"driver: {result.verdict or result.note or '派单失败'}")
            raise GenerationError(result.verdict or result.note or "派单失败")
        tasks.start(job_id, result.account)
        if on_progress is not None:
            on_progress(25)

        budget = timeout if timeout is not None else (
            env.VIDEO_TIMEOUT if normalized == "video" else env.IMAGE_TIMEOUT
        )
        # 协议优先生成 + 一次浏览器回退：协议链路失败（可重试类）时，先释放
        # 本号锁，再显式用 "generation-browser" 重新派单。为什么必须释放重派：
        # 驱动已经由 dispatch 建好并锁住账号，不释放就直接建第二个驱动会
        # 绕过账号互斥；而直接改 driver 对象又拿不到浏览器的 CDP 初始化。
        outcome: dict | None = None
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                outcome = await result.driver.generate(
                    prompt,
                    expect=normalized,
                    timeout=budget,
                    reference_paths=refs_list,
                )
                break
            except Exception as exc:  # noqa: BLE001 - 统一分类后再抛出
                last_error = exc
                if attempt == 1 or not _generation_fallback_allowed(result, exc):
                    break
                previous = f"{type(exc).__name__}: {exc}"
                previous_account = result.account
                await result.release()
                try:
                    result = await dispatch(
                        store, account=previous_account, capability="generation-browser"
                    )
                except (AllAccountsBusy, NoAccountAvailable) as dispatch_exc:
                    tasks.fail(
                        job_id,
                        "protocol_fallback: 协议生成失败"
                        f"（{previous}），回退浏览器派单也失败（{dispatch_exc}）",
                    )
                    raise GenerationError(
                        f"协议生成失败且无法回退浏览器：{previous}"
                    ) from None
                if not result.ok:
                    tasks.fail(
                        job_id,
                        "protocol_fallback: 协议生成失败"
                        f"（{previous}），浏览器派单失败（{result.verdict or result.note}）",
                    )
                    raise GenerationError(
                        f"协议生成失败且无法回退浏览器：{previous}"
                    ) from None
                tasks.start(job_id, result.account)

        if outcome is None:
            assert last_error is not None  # noqa: S101 - 循环必然留下异常
            if isinstance(last_error, TimeoutError):
                tasks.fail(job_id, f"timeout: {last_error}")
                raise ReplyTimeout(result.account) from None
            tasks.fail(job_id, f"{_classify(last_error)}: {last_error}")
            if _classify(last_error) == "quota":
                raise GenerationQuotaExhausted(str(last_error)) from None
            raise last_error
        if on_progress is not None:
            on_progress(90)

        # ---- 成品转入 MediaStore：对外只发随机 token，不发上游直链 ----
        with open(outcome["path"], "rb") as handle:
            blob = handle.read()
        ref = media.MediaStore().save(
            blob,
            str(outcome.get("mime") or ""),
            str(outcome.get("kind") or ""),
            expect=normalized,
        )
        if os.path.abspath(outcome["path"]) != os.path.abspath(ref.path):
            with contextlib.suppress(OSError):
                os.remove(outcome["path"])
        tasks.finish(
            job_id,
            state=jobs.JobStatus.COMPLETED.value,
            media_token=ref.token,
            progress=100,
        )
        payload = tasks.must_get(job_id).to_dict()
        payload["media"] = ref.to_dict()
        payload["url"] = ref.url
        return payload
    finally:
        await result.release()
