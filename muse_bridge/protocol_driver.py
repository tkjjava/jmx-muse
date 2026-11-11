"""协议驱动：把**已经真机打通**的纯协议客户端接到 pool 的派单路径上。

定位是「协议优先、浏览器兜底」的**适配层**，不是第二个浏览器驱动：

  * 它只实现 pool/app 依赖的那几件鸭子接口（status / wake_vm / touch_session /
    stream_reply_events / stream_reply / collect_reply / quota / close），把
    `HatchProtocolClient` + `ProtocolEngine` 的结果翻译成浏览器驱动的形状；
  * 默认行为不变：`pool._build_driver` 只在 `MUSE_PROTOCOL_DRIVER` 打开、且该账号
    已经导入 cookie 时才返回本类，否则仍返回 `MuseDriver`；生成（图像/视频）
    在同一次开关下也走协议（**2026-11-11 已真机验收**，见下），失败时 pool 会
    用 `capability="generation-browser"` 回退一次浏览器；
  * 真机结论（2026-11-11，唯一权威）：WS 必须经本机代理到
    `hatch.metaaivm.com`（直连 TLS 不通），`muse.ai` 的 HTTP 走同一代理没问题。

生成（图像/视频）**已接协议并真机跑通**（2026-11-11 05:5x，同一账号）：
`/chat/stream` 发提示 → 订阅流里回 `delta.presentation`
（`image_path: sandbox://workspace/imagine_media/…webp`）→ `/fs/read` 分页取到
**181,726 B** WebP（`image/webp`，嗅探一致）→ 引擎返回 `GenerationResult`。
所以 `generate()` 不再是 NotImplementedError，而是把引擎结果落盘后翻译成
浏览器驱动同形的 dict（`path/mime/kind/size/ext/via`），`pool.generate_media`
不需要知道下面走的是 Noise 还是 CDP。

为什么 `status()` 里的 `logged_in` 恒为 True：纯协议没有"页面登录元素"可看，
能连上 Noise（`/api/session` 续签成功 + `/api/hatch/token` 换到 token + 握手
完成）就说明 hatch 会话被服务端接受了。401 一类会在 `connect()` 里以
`ProtocolAuthError` 抛出去——本类**绝不吞** connect 的异常，由 pool 决定要不要
回退浏览器；吞掉就等于把"协议连不上"伪装成"账号没登录"。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from . import media, observability, refs
from .driver import _media_dir
from .protocol_client import HatchProtocolClient, ProtocolError
from .protocol_engine import (
    MediaResource,
    ProtocolEngine,
    ProtocolGenerationError,
    normalize_media_path,
)
from .protocol_hub import acquire as warm_acquire
from .protocol_hub import enabled as warm_pool_enabled
from .protocol_hub import release as warm_release

__all__ = ["DriverStatus", "ProtocolDriver", "ProtocolRefusal"]


class ProtocolRefusal(ProtocolError):
    """模型明确拒绝（引擎事件流里的 `refused`）。

    选择"抛异常"而不是"产出一条 error 事件"：pool/app 的流式契约只认
    `reasoning`/`content` 两个键，凭空多出一种 `error` 键会静默改变上层行为
    （`collect_reply_events` 会把它当成空正文，桥回 200 + 空回答）；异常则沿用
    既有出口，调用方已有的 `except ProtocolError` 不必改。拒绝原因在消息里
    （`reason` 属性同值），绝不静默。

    继承 `ProtocolError` 与 `protocol_generation.GenerationError` 同一取舍：
    协议侧的错误归一族，上层不必再记一个新基类。`status_code` 继承来的 502
    只是异常族的既有约定，本轮 app 还没有接协议路径。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason.strip() or "模型拒绝了这次请求（没有给出原因）"
        super().__init__(self.reason)


@dataclass
class DriverStatus:
    """与 `driver.BrowserStatus` 同形，pool 只读 `logged_in` / `note`。

    `url` 是 Noise WS 地址（含 vm_id/auth_token 查询串）：只给排障看，
    **不得**进对外响应（那里有一次性 token）。
    """

    connected: bool
    logged_in: bool
    url: str = ""
    note: str = ""


class ProtocolDriver:
    """一个账号一条 Noise 连接（按需建立），对 pool 暴露浏览器驱动的鸭子接口。"""

    def __init__(
        self,
        account: str,
        cookies: dict[str, str],
        *,
        proxy: str | None = None,
        model: str = "hatch-web",
    ) -> None:
        self.account = account
        # cookie 里有 hatch_sess：不写日志、不进异常消息（客户端自己也不打印）。
        # proxy 串可能带凭据，同样只交给客户端。真机 2026-11-11：WS 不走这个
        # 本机代理就握手不通，所以 proxy 由调用方（pool）从环境变量显式传入。
        self._cookies = dict(cookies)
        self._proxy = proxy
        self._model = model
        # 暖池开关在构造时**不快照**：`connect()` 现读，这样测试与运维可以在
        # 进程存活期间切换（与 pool._protocol_driver_enabled 同一套纪律）。
        # 暖池关着时（默认）这里建的就是这一回合专属的 client，与旧版一字不差。
        # 暖池打开时照样先建一个：**构造不联网**（只有 connect() 才握手），所以
        # 这一步零成本，换来 `_client`/`_engine` 永不为 None —— 否则本文件里
        # 七八处直接访问都要加 None 分支，白白引入一片新故障面。真正的替换发生在
        # connect()：从池里拿到可复用连接后覆盖这两个属性。
        self._client = HatchProtocolClient(
            dict(cookies),
            proxy={"server": proxy} if proxy else None,
            model=model,
        )
        self._engine = ProtocolEngine(self._client)
        # 并发派单共用同一条连接：不加这道门，两个同时到达的请求会各建一条
        # Noise 连接，然后把服务端的订阅流分不清归谁（还会白烧一次握手）。
        self._connect_lock = asyncio.Lock()
        self._connected = False
        # 暖池引用是否已归还：close() 可重复调用，重复 release 会把别的回合
        # 的引用减掉（表现为连接被提前断掉）。
        self._warm_released = False
        # 本回合是否真的从暖池拿到了连接（关开关 / 连池失败时为 False，
        # close() 据此决定"归还引用"还是"直接关掉自己的 client"）。
        self._warm_active = False
        # Delivered attachments survive collection's text-only projection for app callers.
        self.chat_media: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 连接
    async def connect(self) -> None:
        """建立连接，**只建一次**。失败时异常原样抛给调用方（不吞）。"""
        if self._connected:
            return
        async with self._connect_lock:
            # 双重检查：抢先拿到锁的那个已经连好了，后来的不必重连。
            if self._connected:
                return
            if warm_pool_enabled():
                # 暖池命中就直接用（可能一次网络都不发）；未命中时 acquire 内部
                # 已 connect 过，这里不再重复握手。
                client, _reused = await warm_acquire(
                    self.account,
                    self._cookies,
                    proxy=self._proxy,
                    model=self._model,
                )
                if client is not self._client:
                    # 丢开 __init__ 预建的那条：它从未 connect 过，只有一个空壳，
                    # 但留着容易让人误以为还有两条连接。
                    with contextlib.suppress(Exception):
                        await self._client.close()
                self._client = client
                self._engine = ProtocolEngine(client)
                self._warm_active = True
            else:
                await self._client.connect()
            self._connected = True

    async def close(self) -> None:
        """可重复调用、不抛：收尾失败不能把账号锁一起卡住。

        暖池真正接管了这条连接时，这里**只归还引用**，不断连接——断了就白暖了；
        真正的关闭由 `protocol_hub.evict_idle()` 在空闲 TTL 之后做。
        """
        self._connected = False
        if self._warm_active:
            if not self._warm_released:
                self._warm_released = True
                with contextlib.suppress(Exception):
                    await warm_release(self.account)
            return
        with contextlib.suppress(Exception):
            await self._client.close()

    # ------------------------------------------------------------ 探针
    async def status(self) -> DriverStatus:
        """探测连接与登录态。`connect()` 的 `ProtocolError` **不吞**，交给 pool 回退。"""
        await self.connect()
        return DriverStatus(
            connected=True,
            logged_in=True,
            url=self._client.ws_url,
            note=f"protocol: vm_state={self._client.vm_state}",
        )

    async def wake_vm(self) -> bool:
        """`connect()` 内部已经做过 wake，这里只把它确认过的结论如实返回。

        为什么不在 pool 再唤醒一次：wake 的 HTTP 200 只是受理，`wake_ok` 是
        "确实发过 wake 且二次确认 RUNNING"（`protocol_client.py` 模块头第 1 条）。
        VM 本来就 RUNNING 时它恒为 False——没唤醒过就不该声称唤醒成功，
        所以 False 是**如实**，不是失败。pool 也不看返回值（唤醒失败不阻断派单）。
        """
        await self.connect()
        return bool(self._client.wake_ok)

    async def touch_session(self) -> bool:
        """打一次 `/api/session` 当探针：拿到 <400 的响应就算成功。

        401 的语义（cookie 真失效）交给上层，这里只回答"探针是否成功"，所以
        不写账号状态、不猜"是不是过期了"。没有 HTTP 状态码（响应被重置/未结束）
        一律算探针失败——不能把"没结论"报成成功。

        注意 `HatchProtocolClient.request` 的 `timeout` 是**关键字参数**（在 `*`
        之后），按位置传会 TypeError；这里显式用关键字。
        """
        await self.connect()
        response = await self._client.request(
            "GET", "/api/session", None, timeout=20.0
        )
        status = getattr(response, "status", None)
        if status is None:
            return False
        return int(status) < 400

    # ------------------------------------------------------------ 聊天
    async def stream_reply_events(
        self, prompt: str, *, timeout: float = 180.0
    ) -> AsyncIterator[dict]:
        """Translate text and persist chat attachments before delivering local URLs.

        Attachment retrieval shares the engine's fs_read/download_bytes logic and the
        remaining turn budget. Errors propagate; an unresolved attachment is never
        reported as a successful delivery. chat_media caches only delivered metadata.
        """
        self.chat_media = []
        await self.connect()
        deadline = asyncio.get_running_loop().time() + timeout
        seen_paths: set[str] = set()
        async for event in self._engine.stream_chat(prompt, timeout=timeout):
            kind = str(event.get("type") or "")
            if kind == "delta":
                text = event.get("text")
                if text:
                    yield {"content": str(text)}
                continue
            if kind == "media":
                ref = event.get("ref")
                if not isinstance(ref, dict):
                    raise ProtocolGenerationError("聊天媒体引用无效")
                normalized = normalize_media_path(ref.get("path"))
                if normalized is None:
                    raise ProtocolGenerationError("聊天媒体路径不是受支持的 sandbox 路径")
                path, remote = normalized
                if (
                    not remote.startswith("workspace/")
                    or any(part in (".", "..") for part in remote.split("/"))
                    or any(char in remote for char in ("\\", "?", "#"))
                ):
                    raise ProtocolGenerationError("聊天媒体路径不是受支持的 sandbox 路径")
                if path in seen_paths:
                    continue
                item = MediaResource(
                    path=path,
                    mime=str(ref.get("mime") or ""),
                    kind=str(ref.get("kind") or ""),
                    width=ref.get("width"),
                    height=ref.get("height"),
                    duration_ms=ref.get("duration_ms"),
                    seed=ref.get("seed"),
                    raw={**ref, "path": path},
                )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ProtocolGenerationError("聊天媒体获取超时")
                try:
                    with observability.timed("chat_media_fetch"):
                        data = await asyncio.wait_for(
                            self._engine._fetch_bytes(item), timeout=remaining
                        )
                except TimeoutError as exc:
                    observability.bump("chat_media_fetch_failed")
                    raise ProtocolGenerationError("聊天媒体获取超时") from exc
                if not isinstance(data, (bytes, bytearray)) or not data:
                    observability.bump("chat_media_fetch_failed")
                    raise ProtocolGenerationError("聊天媒体获取失败：未返回非空字节")
                # File signatures take precedence over untrusted declared MIME. An
                # HTML/error body carrying image/png must not become an attachment.
                mime = refs.normalize_mime(refs.sniff_mime(data) or "")
                if mime not in refs.REFERENCE_MIME_EXT:
                    observability.bump("chat_media_fetch_failed")
                    raise ProtocolGenerationError("聊天媒体 MIME 无效或无法识别")
                with observability.timed("chat_media_store"):
                    stored = media.MediaStore().save(data, mime, refs.reference_kind(mime))
                observability.bump("chat_media_stored")
                metadata = stored.to_dict()
                metadata.update(
                    width=item.width,
                    height=item.height,
                    duration_ms=item.duration_ms,
                    seed=item.seed,
                    via="protocol",
                )
                seen_paths.add(path)
                self.chat_media.append(metadata)
                yield {"media": [metadata]}
                continue
            if kind == "done":
                return
            if kind == "refused":
                raise ProtocolRefusal(str(event.get("text") or ""))
        # 走到这里说明引擎流结束时既没给 done 也没给 refused。引擎的 `_run`
        # 在"没等到回合结束"时会自己抛错，所以本路径只可能是引擎语义变了；
        # 静默 return 会把半句话当成完整回答（上层报 finish_reason=stop），
        # 因此显式失败。
        raise ProtocolError("协议流提前结束：既没有 done 也没有 refused")

    async def stream_reply(
        self, prompt: str, *, timeout: float = 180.0
    ) -> AsyncIterator[str]:
        """字符串增量版流（`pool.stream_reply` 的既有契约）。

        为什么必须实现：`MUSE_REASONING` 默认关闭时，**非流式**聊天走
        `pool.collect_reply_events` → `pool.collect_reply` → `pool.stream_reply`
        → `driver.stream_reply`。缺这个方法，协议驱动上所有非流式请求都会
        AttributeError，而 `app.py` 不在本轮可改范围。实现就是事件接口的投影。
        """
        async for event in self.stream_reply_events(prompt, timeout=timeout):
            text = event.get("content")
            if text:
                yield text

    async def collect_reply(self, prompt: str, *, timeout: float = 180.0) -> str:
        """Collect text through the same attachment delivery path as streaming."""
        parts: list[str] = []
        async for event in self.stream_reply_events(prompt, timeout=timeout):
            if event.get("content"):
                parts.append(event["content"])
        return "".join(parts)

    # ------------------------------------------------------------ 额度
    async def quota(self) -> dict[str, Any]:
        """会话侧额度（无 UI 可读）。

        协议侧**拿不到** UI 的每周用量百分比：muse.ai 没有额度 REST 接口，那个
        百分比只存在于设置面板文本里（`quota.py` 的解析规则就是为它写的），
        纯协议读不到。所以 `weekly_used_pct` 恒为 None、`weekly_reset` 恒为空串，
        这一格**不编造**。调用方要展示周用量时必须判 `weekly_used_pct is None`
        （保留旧缓存），不能把 None 当 0% 渲染。

        `found=True` 的语义是"拿到了会话侧的 vm_state / 预计到期时间"，
        **不是**"拿到了周用量"；`source="protocol"` 让上层能区分数据来源。

        引擎读 `/api/session` 失败（401 一类）会抛 `ProtocolGenerationError`，
        此处原样冒泡：拿不到就不返回，绝不回一份空额度冒充成功。
        """
        await self.connect()
        info = await self._engine.quota()
        return {
            "raw": "protocol:/api/session",
            "found": True,
            "vm_state": info.get("vm_state"),
            "estimated_expiry_at": info.get("estimated_expiry_at"),
            "weekly_reset": "",
            "weekly_used_pct": None,
            "source": "protocol",
        }

    # ------------------------------------------------------------ 生成
    async def generate(
        self,
        prompt: str,
        *,
        expect: str = "image",
        timeout: float = 240.0,
        reference_paths: list[str] | None = None,
        deadline: float | None = None,
    ) -> dict:
        """一次完整生成：引擎事件流 → `/fs/read` 取件 → 落盘 → 浏览器同形 dict。

        真机证据（2026-11-11 05:5x，muse1）：提示词 → `delta.presentation`
        的 `image_path=sandbox://workspace/imagine_media/…webp` → 引擎分页读回
        **181,726 B** WebP（嗅探 `image/webp`）并给出 25/40/50/100 进度。所以
        本方法不是"接线占位"，而是被真机钉过的路径；签名与 `MuseDriver.generate`
        对齐（`deadline` 只接收不重复扣表：协议引擎用 `timeout` 表达总预算）。

        失败语义：`ProtocolGenerationError` 系（拒绝/额度/超时/模态不符）原样
        冒泡，由 `pool.generate_media` 决定是否回退浏览器；本类不吞、不转换。
        """
        del deadline  # 浏览器独有的总预算参数；协议侧统一用 timeout
        await self.connect()
        if expect not in ("image", "video"):
            raise ProtocolGenerationError(f"不支持的生成类型：{expect}")
        if not (prompt or "").strip():
            raise ProtocolGenerationError("生成提示词为空")

        generator = (
            self._engine.generate_video if expect == "video" else self._engine.generate_image
        )
        result = await generator(prompt, references=reference_paths, timeout=timeout)
        data = bytes(result.data or b"")
        if not data:
            raise ProtocolGenerationError(f"协议生成没有返回字节（expect={expect}）")

        mime = str(result.mime or "") or refs.sniff_mime(data) or "application/octet-stream"
        ext = refs.REFERENCE_MIME_EXT.get(mime) or (
            ".mp4" if expect == "video" else ".png"
        )
        kind = str(result.kind or "") or refs.reference_kind(mime)

        directory = _media_dir()
        os.makedirs(directory, exist_ok=True)
        filename = f"{os.urandom(16).hex()}{ext}"
        path = os.path.join(directory, filename)
        with open(path, "wb") as handle:
            handle.write(data)

        return {
            "path": path,
            "filename": filename,
            "size": len(data),
            "ext": ext,
            "mime": mime,
            "kind": kind,
            "via": "protocol",
            "w": result.width,
            "h": result.height,
            # `GenerationResult` 里带着上游元数据（duration_ms/seed/source_prompt），
            # 早期驱动层只透 w/h，视频时长与种子在调用方眼里直接消失。真实成本为零，
            # 就补上；缺失字段保持 None，不伪造。
            "duration_ms": result.duration_ms,
            "seed": result.seed,
            "source_prompt": result.source_prompt,
        }
