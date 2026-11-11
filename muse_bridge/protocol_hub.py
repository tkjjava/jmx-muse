"""跨派单复用同一条 Noise 会话（"暖池"）。

## 先读这个：为什么这一层可以很薄

`HatchProtocolClient.read_stream(prompt=...)` **本来就支持跨回合复用同一条订阅**
（`protocol_client.py:1737-1742`：`reused` 分支先 `_seek_to_end()` 把游标推到
本回合起点，跳过上一回合没消费完的尾巴；`_resume_subscription()` 处理空闲期
断线）。所以"暖池"不需要自己管游标、seq 去重或订阅重建——那些既有代码已经做完了。

这一层只负责三件既有代码不管的事：

1. **别每次派单都新建 client**。`pool.dispatch()` 现在每次 `_build_driver()` 都
   `ProtocolDriver(...)` → `HatchProtocolClient(...)` → `connect()`，一次 WS 升级
   + Noise XX 握手（真机实测约 3.3 s，见 `protocol_client.py` 模块头）。复用后
   只有**第一次**付这个成本。
2. **验活**。一条"看起来还在"的连接（接收任务已死 / 服务端 end 了订阅 / 半关）
   复用起来是最贵的故障：请求发出去了，事件永远不来。`usable()` 五条全真才算活。
3. **引用计数 + 空闲回收**。回合结束只减引用不断连接（断了就白暖了）；空闲超过
   TTL 才真关，避免长期占着服务端会话。

## 为什么不需要参考实现的 fan-out

参考实现（`work/muse2api_pure/app/muse_engine/protocol_pool.py`）要按
`chat_context.device_id` 把一条共享订阅的事件 fan-out 给多个 listener、每个
listener 一份 seq。那是因为它允许**同号并发**。

本仓不是：`pool.dispatch()` 用账号级锁（`pool.py:217-224`，非阻塞抢锁，抢不到就
当"这个号忙"换下一个）保证同一账号同一时刻只有一个请求。所以这里是单订阅单
消费者，游标天然独占，不需要 fan-out。

## 语义边界（写清楚，避免误用）

* **只在账号锁之内可用**。同账号第二个 `acquire()` 会被**明确拒绝**
  （`WarmPoolBusy`），而不是仅告警后共享游标：两个回合抢同一条订阅的游标是
  数据正确性问题，本模块兜不住，所以不让它发生。见 `acquire()` 的文档字符串。
* **cookie / 代理 / 模型变了就不复用**：键里带**完整** cookie 集合的 sha256
  指纹与代理串（2026-11-11 之前只取 value 前 12 字符，前缀相同的两套 cookie
  会被误判成同一身份）。换 cookie 还复用，等于拿旧会话去打新账号。
* **租用有归属**：`acquire()` 返回的 `lease` 标记这次租用。`release()` 传 lease
  时只减**它自己**那一份；不传时减"该账号当前租用"（向后兼容
  `protocol_driver.py` 的既有调用）。旧租用晚归还不会减到新槽位的引用。
* 开关默认关（`MUSE_WARM_POOL`），关掉时本模块一个函数都不被调用。

## 验证等级（别把两种混为一谈）

* **源码已确认**：A1 初次连接失败/取消清理、A2 完整 cookie 指纹、A2 租用归属、
  A2 并发拒绝——这些都有离线测试覆盖（`tests/test_protocol_hub.py`）。
* **待真机验证**：并发拒绝在真实账号锁之外被触发时的行为、租用晚归还的真实
  时序、TTL 清扫与上游会话回收节奏的关系。离线测试只能证明本模块自己的不变量，
  不能证明上游在那些时序下的反应。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass, field

from .env import env_flag, env_positive_int
from .protocol_client import HatchProtocolClient

logger = logging.getLogger("muse-bridge.protocol_hub")


def enabled() -> bool:
    """暖池总开关。**默认关**：不配就维持"每次派单一条新连接"的既有行为。

    每次调用现读环境变量（与 `pool._protocol_driver_enabled()` 同一套做法），
    测试与运维都能在进程存活期间切换，不必重启。
    """
    return env_flag("MUSE_WARM_POOL", "0")


def idle_ttl() -> float:
    return float(env_positive_int("MUSE_WARM_POOL_TTL", "300"))


@dataclass
class WarmSlot:
    account: str
    key: tuple[str, str, str]
    client: HatchProtocolClient
    refs: int = 0
    last_used: float = field(default_factory=time.monotonic)
    # 租用代次：每次 acquire 命中/新建都从 `_next_lease()` 取一个**全局单调**
    # 的新号，release 凭它认领自己那一份。为什么不用引用计数本身当凭据：计数
    # 会在 +1/-1 之间来回，晚到的旧租用无法与"恰好同值"的新租用区分；为什么不用
    # 槽位内自增：槽位会被 drop 重建，重建后从 1 重来就和旧租用撞号了。
    lease_id: int = 0


class WarmPoolBusy(RuntimeError):
    """同账号已有租用在外。不是连接故障，是调用纪律问题——**不重试、不等待**。

    为什么拒绝而不是等待：本仓的账号锁（`pool.dispatch`）已经保证同号串行，
    走到这里说明有人绕过了锁。等待会把"纪律问题"变成"随机变慢"，还会让两个
    回合在同一条订阅上抢游标；拒绝则是当场暴露问题。
    """


_SLOTS: dict[str, WarmSlot] = {}
_LOCK = asyncio.Lock()
# 租用代次的全局发号器。**必须单调递增、不随槽位销毁而复位**：槽位被 drop 后
# 重建时如果又从 1 开始编号，旧租用晚归还就会撞上新租用的号，把别人的引用减掉。
_LEASE_COUNTER = 0


def _next_lease() -> int:
    global _LEASE_COUNTER
    _LEASE_COUNTER += 1
    return _LEASE_COUNTER


def cookie_fingerprint(cookies: dict[str, str] | None) -> str:
    """cookie 集合的稳定指纹（覆盖**完整**的 name 和 value）。

    用 sha256 而不是内置 `hash()`：内置 hash 带进程级随机盐，跨进程不可比，
    排障时对不上号。

    为什么用完整值而不是前缀（2026-11-11 改）：旧实现只取 value 前 12 个字符，
    两套 cookie 只要前缀相同就判成同一身份，于是换了会话还去复用旧连接——那正是
    "拿旧会话打新账号"。指纹本身不写日志、不落盘，只在内存里做比较，所以覆盖
    完整值没有泄漏面。
    """
    items = sorted(
        (str(name), str(value)) for name, value in (cookies or {}).items() if value
    )
    digest = hashlib.sha256(repr(tuple(items)).encode("utf-8")).digest()
    return digest.hex()[:16]


def usable(client: HatchProtocolClient | None) -> bool:
    """这条连接现在还能**直接**用来派单吗？

    少任何一条就重建——复用一条半死的连接比重新握手更贵，因为故障形态是
    "请求发出去了但永远等不到事件"，最后烧掉整个回合预算。

    **订阅不存在不算死**（真机 2026-11-11 14:1x 实测的坑）：`connect()` 只做
    WS 升级 + Noise 握手 + 前奏，`_sub_stream_id` 此时是 None、`responses` 为空，
    订阅要到 `read_stream(prompt=...)` 才建。第一版把"订阅必须存在"写进验活，
    结果刚连好的连接全被判死，暖池 100% 不命中，每回合还多烧一次重连
    （探针实测 cold 4.40s / warm 2.78s，就是它在重连）。
    只有**订阅存在且已被服务端 end** 才算不可复用。
    """
    if client is None or client.closing:
        return False
    if getattr(client, "ws", None) is None:
        return False
    if not client._handshake_done:
        return False
    task = client.receiver_task
    if task is None or task.done():
        return False
    stream_id = client._sub_stream_id
    if stream_id is not None:
        response = client.responses.get(stream_id)
        if response is not None and response.ended:
            return False
    # 空闲期被对端关掉：`_connection_lost()` 是既有的断线判定，别自己再写一套。
    return not client._connection_lost()


def key_for(
    cookies: dict[str, str] | None, *, proxy: str | None, model: str
) -> tuple[str, str, str]:
    return (cookie_fingerprint(cookies), proxy or "", model or "")


async def _shutdown(client: HatchProtocolClient | None) -> None:
    """真关，且不抛。

    只允许在**锁外**调用：`close()` 要等接收任务退出，在持锁期间 await 会把
    整池堵住。本模块所有调用点都在 `async with _LOCK` 之外。
    """
    if client is None:
        return
    with contextlib.suppress(Exception):
        await client.close()


async def acquire(
    account: str,
    cookies: dict[str, str],
    *,
    proxy: str | None = None,
    model: str = "hatch-web",
    factory=None,
) -> tuple[HatchProtocolClient, bool]:
    """拿到该账号可用的 client；返回 `(client, reused)`。

    命中条件：键一致 + 验活通过。键一致但验活不过时**不重建 client**，而是调
    `client.connect()` —— 它开头就 `_drop_connection()`（`protocol_client.py:857-865`），
    本身就是"用同一个 client 重新握手"的入口，能续用 token/cookie 续期路径。

    只有键变了（换 cookie / 换代理 / 换模型）才真换 client：那种情况下旧会话
    属于另一个身份，留着是隐患。

    **同账号已有租用在外时抛 `WarmPoolBusy`**（2026-11-11 改）。旧实现只打一条
    WARNING 然后共享同一条订阅，两个回合会抢游标。账号锁已经保证同号串行，
    所以到这一步就是纪律问题，当场拒绝比随机变慢诚实。

    **初次连接失败或取消必须清理**：槽位在 `connect()` 之前就写进池，旧实现在
    失败时留着一条半死的 client，下一个回合先付一次验活失败再重连。现在失败/
    取消都摘掉本次槽位并关掉本次连接；摘槽前用身份比较，绝不误删后来者。
    """
    key = key_for(cookies, proxy=proxy, model=model)
    make = factory or HatchProtocolClient
    stale: HatchProtocolClient | None = None
    reconnect: HatchProtocolClient | None = None

    async with _LOCK:
        slot = _SLOTS.get(account)
        if slot is not None and slot.key == key:
            if slot.refs > 0:
                # 已经有人在用这条订阅：拒绝，不共享游标，也不等待。
                raise WarmPoolBusy(
                    f"账号 {account} 的暖池会话正被使用（refs={slot.refs}）；"
                    "同一账号必须串行派单"
                )
            if usable(slot.client):
                slot.refs = 1
                slot.lease_id = _next_lease()
                slot.last_used = time.monotonic()
                return slot.client, True
            # 同一身份的死连接：标记重连，锁外做（重连要等网络，不能堵整池）。
            # 不在这里加引用：死连接不可能还在给任何回合干活，它身上的旧引用是
            # 上一批回合留下的脏账。重连成功后引用归一为 1（只有本回合持有）。
            slot.last_used = time.monotonic()
            reconnect = slot.client
        elif slot is not None:
            _SLOTS.pop(account, None)
            stale = slot.client

        if reconnect is not None:
            client = reconnect
        else:
            client = make(
                dict(cookies),
                proxy={"server": proxy} if proxy else None,
                model=model,
            )
            _SLOTS[account] = WarmSlot(
                account=account, key=key, client=client, refs=1, lease_id=_next_lease()
            )

    # 锁外：先关掉换了身份的那条，再（必要时）重连同一身份的死连接。
    if stale is not None:
        await _shutdown(stale)
    if reconnect is not None:
        try:
            await reconnect.connect()
        except BaseException:
            # 重连失败（含取消）：这条连接已经判死，直接摘槽 + 关掉，而不是只把
            # 引用还回去。只还原引用的话，槽位会带着一条死连接留在池里，下一个
            # 回合再验活一次失败再重连——每回合白烧一次握手，最后还是要人来清。
            await _discard_locked(account, reconnect)
            await _shutdown(reconnect)
            raise
        logger.info("暖池：账号 %s 会话已失效，已重连", account)
        async with _LOCK:
            slot = _SLOTS.get(account)
            if slot is not None and slot.client is reconnect:
                # 引用归一到 1：死连接不可能还有回合在持有它，旧计数是脏账。
                slot.refs = 1
                slot.lease_id = _next_lease()
                slot.last_used = time.monotonic()
        return reconnect, False

    try:
        await client.connect()
    except BaseException:
        # 初次连接失败或取消：槽位是在 connect() 之前写的，必须自己收拾干净。
        # 用身份比较摘槽，避免误删"取消期间别人已经替换掉的新槽位"。
        await _discard_locked(account, client)
        await _shutdown(client)
        raise
    return client, False


async def _discard_locked(account: str, client: HatchProtocolClient) -> None:
    """仅当槽位仍是这条 client 时移除它。身份比较，绝不误删后来者。"""
    async with _LOCK:
        slot = _SLOTS.get(account)
        if slot is not None and slot.client is client:
            _SLOTS.pop(account, None)


async def release(account: str, lease: int | None = None) -> bool:
    """一个回合结束：只减引用，不断连接（断了就白暖了）。

    `lease` 是 `acquire()` 时槽位上的租用代次（可从 `lease_of()` 取）。给了就
    只在代次匹配时减引用——旧租用晚归还、或者槽位已经换代，都不会减到别人的引用。
    不给则按"该账号当前租用"处理，保持 `protocol_driver.py` 既有调用的语义。

    返回是否真的减了引用：False 表示这次归还没有归属（槽位已重建或被丢弃），
    调用方可以据此打点，而不是把它当成功。
    """
    released = False
    async with _LOCK:
        slot = _SLOTS.get(account)
        if slot is None:
            return False
        if lease is not None and lease != slot.lease_id:
            logger.warning(
                "暖池：账号 %s 收到过期租用 %s 的归还（当前 %s），已忽略",
                account,
                lease,
                slot.lease_id,
            )
            return False
        if slot.refs > 0:
            slot.refs -= 1
            released = True
        slot.last_used = time.monotonic()
    await evict_idle()
    return released


async def drop(account: str) -> None:
    """强制丢弃（cookie 轮换 / 账号删除 / 连接被判定不可用）。"""
    async with _LOCK:
        slot = _SLOTS.pop(account, None)
    await _shutdown(slot.client if slot else None)


async def evict_idle(ttl: float | None = None) -> int:
    """关掉空闲超过 ttl 的连接；返回关掉几条。"""
    limit = idle_ttl() if ttl is None else ttl
    now = time.monotonic()
    stale: list[WarmSlot] = []
    async with _LOCK:
        for account, slot in list(_SLOTS.items()):
            if slot.refs <= 0 and now - slot.last_used >= limit:
                stale.append(_SLOTS.pop(account))
    for slot in stale:
        logger.info(
            "暖池：账号 %s 空闲 %.0fs，关闭会话", slot.account, now - slot.last_used
        )
        await _shutdown(slot.client)
    return len(stale)


async def close_all() -> int:
    """进程退出兜底。返回关掉几条。"""
    async with _LOCK:
        slots = list(_SLOTS.values())
        _SLOTS.clear()
    for slot in slots:
        await _shutdown(slot.client)
    return len(slots)


async def sweep_forever(interval: float = 60.0) -> None:
    """后台清扫：空闲 TTL 到点就关，**不依赖有没有新请求**。

    为什么需要它：`release()` 里也会调 `evict_idle()`，看起来够用了，但那意味着
    只有"下一个请求到来"时才会回收。进程彻底空闲时（夜间、低峰）连接会一直挂着：
    TTL 形同虚设，服务端那边我们占着会话，重连时还可能撞上"同一账号两条连接抢
    事件路由"。所以必须有一个与请求无关的时钟。
    """
    while True:
        try:
            await asyncio.sleep(interval)
            await evict_idle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 清扫失败不能让后台任务死掉
            logger.warning("暖池：空闲清扫异常，继续下一轮", exc_info=True)


def warm_accounts() -> list[str]:
    """当前持有可用会话的账号（运维面/健康检查用）。"""
    return sorted(account for account, slot in _SLOTS.items() if usable(slot.client))


def lease_of(account: str) -> int | None:
    """该账号当前租用代次；没有槽位时 None。给 release(account, lease) 用。"""
    slot = _SLOTS.get(account)
    return slot.lease_id if slot is not None else None


def stats() -> dict[str, object]:
    return {
        "enabled": enabled(),
        "accounts": warm_accounts(),
        "refs": {account: slot.refs for account, slot in _SLOTS.items()},
    }


__all__ = [
    "WarmPoolBusy",
    "WarmSlot",
    "acquire",
    "close_all",
    "cookie_fingerprint",
    "drop",
    "enabled",
    "evict_idle",
    "idle_ttl",
    "key_for",
    "lease_of",
    "release",
    "stats",
    "sweep_forever",
    "usable",
    "warm_accounts",
]
