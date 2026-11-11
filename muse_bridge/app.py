"""FastAPI 应用：OpenAI 兼容接口 + CDP 连接授权台。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    __author__,
    __mark__,
    __version__,
    env,
    gen_params,
    jobs,
    media,
    observability,
    pool,
    protocol_hub,
)
from .canvas_api import STATIC_DIR
from .canvas_api import router as canvas_router
from .driver import CDP_URL, BrowserUnavailable, MuseDriver
from .env import env_positive_float
from .protocol_client import ProtocolAuthError, ProtocolError, ProtocolRateLimited
from .protocol_driver import ProtocolDriver, ProtocolRefusal
from .protocol_engine import ProtocolGenerationError
from .refs import ReferenceError, fetch_public
from .session_http import SessionError, renew_session
from .session_http import wake_vm as http_wake_vm
from .state import AccountState, after_probe
from .store import AccountStore, ConfigError

REPLY_TIMEOUT = env_positive_float("MUSE_REPLY_TIMEOUT", "180")


def _protocol_failure(exc: BaseException) -> tuple[int, str, AccountState | None]:
    """协议驱动的异常 → `(HTTP 状态, 错误码, 记账状态)`；状态为 None 表示不用动账号。

    为什么不能让它裸奔：协议驱动是 0.4.1 新接进派单路径的，异常类型
    （ProtocolError / ProtocolGenerationError）不是浏览器那条线的
    `TimeoutError` / `BrowserUnavailable`，SSE 分支的既有 catch 接不住，
    异常会从 `event_stream()` 逸出——客户端看到的是断开而不是
    finish_reason=length。2026-11-11 接入前由离线测试抓到，这里显式映射：
    * 拒绝（模型不肯答）：502，**不改账号状态**（账号没问题，换号也没用）；
    * 认证失败：401 + SESSION_EXPIRED（终态，必须重新登录）；
    * 限流/额度：429 + RATE_LIMITED（可重试）；
    * 超时/其余：504 或 502 + RATE_LIMITED（可重试，冷却后自动回池）。
    """
    if isinstance(exc, ProtocolRefusal):
        return 502, "protocol_refusal", None
    if isinstance(exc, ProtocolAuthError):
        return 401, "session_expired", AccountState.SESSION_EXPIRED
    if isinstance(exc, ProtocolRateLimited):
        return 429, "rate_limited", AccountState.RATE_LIMITED
    if isinstance(exc, ProtocolGenerationError):
        lowered = str(exc).lower()
        if "额度" in str(exc) or "quota" in lowered:
            return 429, "quota_exhausted", AccountState.RATE_LIMITED
        return 504, "protocol_timeout", AccountState.RATE_LIMITED
    return 502, "protocol_error", AccountState.RATE_LIMITED


def _driver_label(result) -> str:
    """本次请求实际用的驱动，随响应头 `X-Muse-Driver` 返回。

    为什么需要它：`MUSE_PROTOCOL_DRIVER=1` 时派单可能因协议连接失败而回退浏览器，
    只看 HTTP 200 分不出到底走的哪条路。压测/排障时一眼就能看出回退率，
    不必去翻只有 uvicorn access log 的 bridge 日志。
    """
    return "protocol" if isinstance(result.driver, ProtocolDriver) else "browser"

# 幂等键 → 任务 id。进程内缓存：任务本身也只活在本进程，
# 重启后两者一起失效，不会出现"键还在、任务没了"的悬空绑定。
import threading  # noqa: E402

_idempotency: dict[str, str] = {}
_idem_lock = threading.Lock()


def allowed_keys() -> set[str]:
    raw = os.environ.get("MUSE_BRIDGE_API_KEYS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


async def require_key(authorization: str | None = Header(default=None)) -> None:
    keys = allowed_keys()
    if not keys:
        return
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token not in keys:
        raise HTTPException(status_code=401, detail="无效的 API 密钥")


def admin_keys() -> set[str]:
    """管理面密钥。`MUSE_BRIDGE_ADMIN_KEYS` 非空时**只**认它。

    为什么单独一把钥匙：上游多个中转（czg86389-hub / bynne2602 / bitgacon 三份
    实现同款）把 /v1 与 /admin 共用一把 key，于是任何拿到普通调用 key 的客户端
    都能删号、换配置、读额度——权限坍缩。设了 ADMIN_KEYS 后，管理端点不再接受
    调用密钥；没设时退回 API_KEYS（不破坏既有部署），但这条降级是可审计的：
    `/healthz` 的 `admin_key_separated` 字段会如实标 false。
    """
    raw = os.environ.get("MUSE_BRIDGE_ADMIN_KEYS", "")
    keys = {item.strip() for item in raw.split(",") if item.strip()}
    return keys or allowed_keys()


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    keys = admin_keys()
    if not keys:
        return
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if token not in keys:
        raise HTTPException(status_code=401, detail="无效的管理密钥")


app = FastAPI(title="muse-bridge", version=__version__)


@app.exception_handler(RequestValidationError)
async def _explain_multipart_edits(request: Request, exc: RequestValidationError):
    """`/v1/images/edits` 收到 multipart 时给一条能看懂的 400。

    这个端点只收 JSON（见端点自己的 docstring）。FastAPI 的默认行为是把
    multipart 请求当成"JSON 解析失败"回 422 + 一串 Pydantic 路径，调用方
    只会看到 `body -> prompt: Field required`，猜不到"换成 JSON 就能用"。
    这里只截这一种情况，其它 422 原样交给 FastAPI 的默认处理器。
    """
    if request.url.path == "/v1/images/edits" and request.headers.get(
        "content-type", ""
    ).startswith("multipart/"):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": (
                        "本端点不支持 multipart/form-data：请改用 JSON 并把参考图"
                        "放进 images 字段（data URL / http(s) 地址 / 服务端本地路径）"
                    ),
                    "type": "invalid_request_error",
                    "code": "multipart_not_supported",
                }
            },
        )
    return await request_validation_exception_handler(request, exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：起暖池清扫与媒体清扫；退出时收掉它们、关池、关数据库。

    暖池的连接是进程级长活对象，不在这里收就会留下没人管的 WS：进程退出后
    服务端要等自己的空闲超时才清，期间同一个账号的旧会话还可能抢事件路由。
    清扫任务同理——它是个 `while True`，不取消会跟着事件循环一起悬着。

    媒体清扫（2026-11-11 补）：`media.MediaStore.sweep()` 本来就写好了，但全仓
    只有测试调它，生产路径**没有任何东西触发**，于是 `data/media/` 只增不减。
    它不依赖开关（暖池是可选的，媒体是必用的），所以无条件起。

    开关关着时不建暖池任务：`sweep_forever` 空转没意义，而 `evict_idle()`
    在池为空时本来就什么都不做。
    """
    sweeper: asyncio.Task | None = None
    media_sweeper: asyncio.Task | None = None
    if protocol_hub.enabled():
        sweeper = asyncio.create_task(
            protocol_hub.sweep_forever(), name="muse-warm-pool-sweeper"
        )
    media_sweeper = asyncio.create_task(
        media.sweep_forever(), name="muse-media-sweeper"
    )
    try:
        yield
    finally:
        for task in (sweeper, media_sweeper):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        with suppress(Exception):
            await protocol_hub.close_all()
        if _store is not None:
            _store.close()


app.router.lifespan_context = lifespan

_store: AccountStore | None = None


def store() -> AccountStore:
    global _store
    if _store is None:
        _store = AccountStore()
    return _store


# ---- OpenAI 兼容 ------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None


class ChatRequest(BaseModel):
    model: str = pool.DEFAULT_MODEL
    messages: list[ChatMessage]
    stream: bool = False
    # 点名派单：只用该账号；空则整池轮转（既有行为不变）
    account: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    # OpenAI's usage control for streams. A gateway in front of this bridge bills token
    # quota from the reported usage, and a stream that never reports it is billed as zero
    # — so this is honoured, not ignored. {"include_usage": true} adds a final usage chunk.
    stream_options: dict | None = None

    def wants_stream_usage(self) -> bool:
        return bool(isinstance(self.stream_options, dict) and self.stream_options.get("include_usage"))


def _prompt_of(messages: list[ChatMessage]) -> str:
    """压成一段文本。muse 是单轮接口，历史以标记形式拼入。"""
    parts: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        if not content:
            continue
        if message.role == "system":
            parts.append(f"[系统指令] {content}")
        elif message.role == "assistant":
            parts.append(f"[助手] {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts).strip()


# ---- token 估算 ---------------------------------------------------------
# 汉字在主流 BPE 分词器里基本一字一 token（o200k 常为 1-1.5），按 4 字符/token 会把
# 中文用量少算 6-10 倍：2000 字的回答只报 ~500 token，挂在计量用的网关后面等于中文
# 流量近乎免费。拉丁文本仍按 4 字符/token，与英文常见密度一致，所以英文路径不变。

_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 日文假名
    (0x3400, 0x4DBF),   # 扩展 A
    (0x4E00, 0x9FFF),   # 基本汉字
    (0xAC00, 0xD7AF),   # 谚文
    (0xF900, 0xFAFF),   # 兼容汉字
    (0xFF00, 0xFFEF),   # 全角形式
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _estimate_tokens(text: str) -> int:
    """CJK 一字一 token，其余按四字符一 token（向下取整）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + (len(text) - cjk) // 4


def _usage_of(prompt: str, completion: str) -> dict:
    """Token estimate shared by the streaming and non-streaming paths.

    The upstream reports no token counts, so this is a heuristic — see _estimate_tokens.
    It exists because a gateway bills token quota from it: report nothing and the call
    costs the customer nothing, and a flat 4-chars-per-token under-bills Chinese ~10x.
    total is the sum of the two parts, so it always equals what the client sees.
    """
    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(completion)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


@app.exception_handler(ConfigError)
async def _config_error(_request: Request, exc: ConfigError) -> JSONResponse:
    """配置错误（缺密钥/密钥损坏）是服务侧缺钥匙，不是客户端的锅。

    500 会把 store.py 里写给人看的修正指引吞成一句 Internal Server Error；
    503 + 原文案让 /console 之外的第一入口也能看到怎么修。
    """
    return JSONResponse(
        status_code=503,
        content={"error": str(exc), "type": "config_error"},
    )


@app.get("/v1/models")
async def list_models(_: None = Depends(require_key)) -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "muse-bridge",
                "upstream": pool.UPSTREAM_MODEL,
            }
            for model in sorted(pool.MODEL_ALIASES | {"muse-image", "muse-video"})
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
):
    """OpenAI 兼容补全。

    不标注返回类型：JSONResponse | StreamingResponse 联合类型会被
    FastAPI 当成 Pydantic 响应模型解析并报错。
    """
    prompt = _prompt_of(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="消息内容为空")

    pinned = (request.account or x_account or "").strip() or None
    try:
        result = (
            await pool.dispatch(store(), account=pinned)
            if pinned
            else await pool.dispatch(store())
        )
    except pool.AllAccountsBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except pool.NoAccountAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except BrowserUnavailable as exc:
        # dispatch 阶段就会抛：`_pick_muse_page` 新建标签页后 goto 失败
        # （出口断网 / ERR_CONNECTION_CLOSED）以前是裸的 playwright Error，
        # 一路冒到 ASGI 变成 500——用户拿不到任何可读信息，而协议回退的根因
        # 早已落进日志。这里必须接住并给可操作的文案。
        raise HTTPException(
            status_code=503,
            detail={"error": "browser_unavailable", "note": str(exc)},
        ) from exc

    if not result.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "error": result.verdict,
                "note": result.note,
                "type": "upstream_error",
            },
        )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = request.model or pool.DEFAULT_MODEL

    async def finish() -> None:
        """收尾：释放账号锁并断开 CDP（不关用户的 Chrome）。

        锁必须覆盖整个请求（含流式输出），所以释放发生在这里。

        用 shield 保护：客户端中途断开时当前任务处于 cancelled 状态，
        直接 await 会被立即取消，锁就漏了——那个账号从此永久不可用。
        """
        await asyncio.shield(result.release())

    if not request.stream:
        try:
            reasoning, text = await pool.collect_reply_events(
                result, prompt, timeout=REPLY_TIMEOUT
            )
        except (ProtocolError, ProtocolGenerationError) as exc:
            status, code, state = _protocol_failure(exc)
            if state is not None:
                store().mark_failure(result.account, state=state, error=str(exc))
            raise HTTPException(
                status_code=status, detail={"error": code, "note": str(exc)}
            ) from exc
        except BrowserUnavailable as exc:
            # 通道掉线（页面还在「正在连接…」/WS 断了）不是限流：记成可重试的
            # browser_unavailable，回 503，冷却期满自动回池。2026-11-11 真机
            # 关代理复测过：旧代码把它当 timeout，账号被误标 rate_limited。
            store().mark_failure(
                result.account, state=AccountState.BROWSER_UNAVAILABLE, error=str(exc)
            )
            raise HTTPException(
                status_code=503,
                detail={"error": "browser_unavailable", "note": str(exc)},
            ) from exc
        except TimeoutError as exc:
            store().mark_failure(
                result.account, state=AccountState.RATE_LIMITED, error=str(exc)
            )
            raise HTTPException(
                status_code=504, detail={"error": "timeout", "note": str(exc)}
            ) from exc
        finally:
            await finish()

        return JSONResponse(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text,
                            # 非流式也带思考（OpenAI/DeepSeek 语义）；仅有值时出现
                            **({"reasoning_content": reasoning} if reasoning else {}),
                            **(
                                {"media": _chat_media_urls(result.driver.chat_media)}
                                if getattr(result.driver, "chat_media", None)
                                else {}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                # 思考计入用量（与流式 streamed_parts 含 reasoning 对齐）
                "usage": _usage_of(prompt, reasoning + text),
            },
            headers={"X-Muse-Driver": _driver_label(result)},
        )

    async def event_stream() -> AsyncIterator[str]:
        def chunk(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"



        streamed_parts: list[str] = []
        truncated = False
        yield chunk(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                ],
            }
        )
        try:
            async for event in pool.stream_reply_events(result, prompt, timeout=REPLY_TIMEOUT):
                reasoning = event.get("reasoning")
                content = event.get("content")
                if event.get("media"):
                    yield chunk(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"media": _chat_media_urls(event["media"])},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                if reasoning:
                    # 思考是真实输出，计入同一份用量估算；relay 按字节透传该字段。
                    streamed_parts.append(reasoning)
                    yield chunk(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"reasoning_content": reasoning},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                if not content:
                    continue
                streamed_parts.append(content)
                yield chunk(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        except pool.ReplyTimeout:
            # 到点截断：已产出的部分照常交付，但收尾必须区别于正常完成，
            # 否则下游会把半句话当成完整回复（stop = 模型自己说完了）。
            truncated = True
        except (ProtocolError, ProtocolGenerationError) as exc:
            # 流已经开始了，改不了 HTTP 状态码：按"未说完"收尾 + 记可重试状态
            # （拒绝类不动账号，见 _protocol_failure）。
            _, _code, state = _protocol_failure(exc)
            if state is not None:
                store().mark_failure(result.account, state=state, error=str(exc))
            truncated = True
        except BrowserUnavailable as exc:
            # 通道掉线：流已经开始，不能再改 HTTP 状态码，只能按"未说完"收尾，
            # 并把账号记成可重试的 browser_unavailable（不是 rate_limited）。
            store().mark_failure(
                result.account, state=AccountState.BROWSER_UNAVAILABLE, error=str(exc)
            )
            truncated = True
        except TimeoutError:
            truncated = True
        finally:
            # 包含 CancelledError（客户端断开）的情况——
            # finish 内部用 shield 保护，确保锁一定释放
            await finish()

        yield chunk(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length" if truncated else "stop"}],
            }
        )
        if request.wants_stream_usage():
            # OpenAI's shape for the final usage frame: no choices, usage alongside.
            yield chunk(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": _usage_of(prompt, "".join(streamed_parts)),
                }
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
            "X-Muse-Driver": _driver_label(result),
        },
    )


# ---- 账号管理 ---------------------------------------------------------


class AttachRequest(BaseModel):
    account: str
    label: str = ""
    cdp_url: str = ""


@app.post("/admin/attach")
async def attach(request: AttachRequest, _key: None = Depends(require_admin)) -> dict:
    """连接 CDP、检查登录态、入库。不触发任何登录或注册。"""
    cdp = request.cdp_url.strip() or CDP_URL
    driver = MuseDriver(cdp)
    # status() 自己把连接失败折成 connected=False，这里不需要 try/finally
    status = await driver.status()

    if not status.connected:
        await driver.close()
        raise HTTPException(
            status_code=503,
            detail={"error": "browser_unavailable", "note": status.note},
        )

    if not status.logged_in:
        await driver.close()
        return {
            "ok": False,
            "error": "not_logged_in",
            "url": status.url,
            "note": "在这个 Chrome 里打开 muse.ai 并登录后重试",
        }

    now = int(time.time())
    state = after_probe(logged_in=True, now=now, vm_ready=True)
    store().upsert(
        request.account,
        cdp_url=cdp,
        state=state,
        label=request.label,
        profile={"site": status.url},
        vm_ready=True,
    )
    await driver.close()
    return {
        "ok": True,
        "account": request.account,
        "state": state.value,
        "cdp_url": cdp,
        "url": status.url,
    }


@app.get("/admin/accounts")
async def list_accounts(_: None = Depends(require_admin)) -> dict:
    return {"accounts": store().redacted()}


@app.delete("/admin/accounts/{account}")
async def delete_account(account: str, _key: None = Depends(require_admin)) -> dict:
    deleted = store().delete(account)
    pool.forget_account(account)
    return {"deleted": deleted}


@app.post("/admin/accounts/{account}/refresh")
async def refresh_session(account: str, _key: None = Depends(require_admin)) -> dict:
    """续期一次（hatch_vml 48h / hatch_sess 30d）。

    两条路径与 keepalive / wake 同口径：**导入过 cookie 就走纯 HTTP**
    （`session_http.renew_session`，不需要 CDP、不需要浏览器窗口在跑）；
    没有 cookie 才退回 CDP 浏览器点击。为什么这条也要收口：早期 refresh 是
    唯一还无条件构造 `MuseDriver` 的运维端点，浏览器没开时无法续期，与
    "协议优先"的部署前提矛盾（实测 accounts_dispatchable 掉到 0 时，
    keepalive 能复活、refresh 却 503）。
    """
    record = store().get(account)
    if record is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    cookies = store().cookies(account)
    if cookies:
        renewal = await _renew_http(cookies)
        # 服务端回的 Set-Cookie 是新的一代：必须落回去，否则下一轮还用旧串。
        store().save_cookies(account, renewal.cookies)
        store().touch_refresh(account)
        if not renewal.assigned:
            return {
                "account": account,
                "ok": False,
                "refreshed": False,
                "via": "http",
                "detail": renewal.reason,
            }
        store().mark_ready(
            account, vm_ready=bool(record.vm_ready), state=AccountState.READY
        )
        return {
            "account": account,
            "ok": True,
            "refreshed": True,
            "via": "http",
            "vm_state": renewal.status,
        }

    driver = MuseDriver(record.cdp_url)
    try:
        ok = await driver.touch_session()
    except BrowserUnavailable as exc:
        # 与 /admin/attach 同语义：CDP 拿不到是 503，不是裸 500
        raise HTTPException(
            status_code=503,
            detail={"error": "browser_unavailable", "note": str(exc)},
        ) from exc
    finally:
        await driver.close()
    if ok:
        store().touch_refresh(account)
        store().mark_ready(account, vm_ready=True, state=AccountState.READY)
    return {"ok": ok, "refreshed": ok, "via": "browser"}


@app.post("/admin/accounts/{account}/wake")
async def wake_vm(account: str, _key: None = Depends(require_admin)) -> dict:
    """唤醒一次 VM。导入过 cookie 的账号走纯 HTTP（不启动浏览器）。

    为什么要改成与 keepalive 同一条路径：真机 2026-11-11 复测时这个端点
    走浏览器点击，页面正停在「Muse 正在连接…」（出口被墙、WS 起不来），
    于是返回 `{"ok": false}`——一个不区分"点了但 VM 没醒"和"根本点不动"
    的结果，把真实原因藏掉了。纯 HTTP 的 `hatch/vm/wake` 只需要 cookie，
    并且会逐段报告 refreshed / woken / vm_state，排障信息完整得多。
    """
    record = store().get(account)
    if record is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    cookies = store().cookies(account)
    if cookies:
        result = await _http_keepalive(account, cookies, record)
        return {
            "ok": bool(result.get("woken")),
            "refreshed": result.get("refreshed"),
            "woken": result.get("woken"),
            "via": "http",
            "vm_state": result.get("vm_state"),
            "detail": result.get("detail"),
        }
    driver = MuseDriver(record.cdp_url)
    try:
        ok = await driver.wake_vm()
    except BrowserUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "browser_unavailable", "note": str(exc)},
        ) from exc
    finally:
        await driver.close()
    return {"ok": ok, "via": "browser"}


@app.get("/admin/observability")
async def admin_observability(_: None = Depends(require_admin)) -> dict:
    """分阶段计数与耗时。**不放公开 healthz**：内部失败率不该对匿名探针可见。

    这里只给计数与秒数，不含 cookie / Bearer key / 提示词正文（见
    `observability` 模块头的边界说明）。
    """
    return observability.snapshot()


@app.get("/healthz")
async def healthz() -> dict:
    accounts = store().list_all()
    dispatchable = store().dispatchable()
    return {
        "status": "ok",
        "version": __version__,
        "author": __author__,
        "mark": __mark__,
        "accounts_total": len(accounts),
        "accounts_dispatchable": len(dispatchable),
        "accounts_busy": len(pool.busy_accounts()),
        "cdp_url": CDP_URL,
        # 管理面是否与调用面分钥匙：false 表示 /admin 仍接受普通调用 key（降级可审计）。
        "admin_key_separated": bool(os.environ.get("MUSE_BRIDGE_ADMIN_KEYS", "").strip()),
        "public_base": public_base(),
        # 聊天/生成走哪条驱动：protocol（纯协议，需 MUSE_PROTOCOL_DRIVER=1 + 该账号
        # 已导入 cookie + MUSE_PROTOCOL_PROXY 可通）/ browser。注意这里只反映**开关**，
        # 不反映单个账号有没有 cookie：没 cookie 的账号 dispatch 时仍会落到浏览器。
        # 生成正常路径同协议；仅"可重试类"协议失败会一次性回退浏览器（见 pool）。
        "chat_driver": "protocol" if pool._protocol_driver_enabled() else "browser",
        "protocol_proxy_configured": bool(
            os.environ.get("MUSE_PROTOCOL_PROXY", "").strip()
        ),
        # 暖池可观测性：开关、当前持有可用会话的账号、各账号引用数。
        # 为什么放进 healthz：暖池的失败形态是"悄悄没命中"，从响应时间上看不出来，
        # 只有把 `warm_accounts` 摊开才能现场判断"到底复用上了没有"。
        #
        # 注意这里**只是池内状态快照，不是命中率**：`refs` 反映此刻谁持有租用，
        # 命中/未命中次数在 `/admin/observability`（公开面不该暴露失败率）。
        # `leased_accounts` 是按 refs 派生的真实量，省得读的人自己加。
        "warm_pool": _warm_pool_snapshot(),
    }


def _warm_pool_snapshot() -> dict:
    info = protocol_hub.stats()
    refs = dict(info.get("refs") or {})
    return {
        **info,
        "leased_accounts": sum(1 for value in refs.values() if value > 0),
    }


# ---- 授权台 -----------------------------------------------------------

CONSOLE_HTML = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>muse-bridge 授权台</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:760px;
      margin:40px auto;padding:0 20px;line-height:1.6;color:#1a1a1a}
 h1{font-size:20px;font-weight:600}
 label{display:block;margin:14px 0 6px;font-size:13px;color:#555}
 input{width:100%;padding:8px 10px;border:1px solid #d0d0d0;
      border-radius:6px;font-size:14px;box-sizing:border-box}
 button{margin-top:16px;padding:9px 18px;background:#1a1a1a;color:#fff;
      border:0;border-radius:6px;font-size:14px;cursor:pointer}
 pre{background:#f5f5f5;padding:12px;border-radius:6px;font-size:12px;
      overflow:auto;white-space:pre-wrap}
 .hint{font-size:12px;color:#777;margin-top:6px}
 .step{background:#fafafa;border-left:3px solid #ddd;padding:10px 14px;
      margin:12px 0;font-size:13px}
 .sign{margin-top:32px;padding-top:14px;border-top:1px solid #eee;
      font-size:12px;color:#999;text-align:center}
 .sign b{color:#666;font-weight:600}
</style>
<h1>muse-bridge 授权台</h1>

<div class="step">
 <b>前置条件</b><br>
 1. 启动带调试端口的 Chrome：<code>--remote-debugging-port=9222</code><br>
 2. 在这个 Chrome 里打开 <code>muse.ai</code> 并登录<br>
 3. 保持该标签页打开
</div>

<p class="hint">本工具不登录、不注册、不伪造任何签名。它只连接你已登录的浏览器。</p>

<label>账号标签</label>
<input id="account" placeholder="muse-main">
<label>显示名（可选）</label>
<input id="label" placeholder="主号">
<label>CDP 端点（留空用默认）</label>
<input id="cdp" placeholder="http://127.0.0.1:9222">
<label>API 密钥（服务端配了 MUSE_BRIDGE_API_KEYS 时必填）</label>
<input id="key" placeholder="留空 = 服务端未启用鉴权">

<button id="go">连接并入库</button>
<pre id="out">等待操作</pre>

<div class="sign">muse-bridge · <b>君莫笑</b><br><b>永恒之兰</b></div>

<script>
const keyBox = document.getElementById('key');
keyBox.value = localStorage.getItem('muse_bridge_key') || '';
document.getElementById('go').onclick = async () => {
  const out = document.getElementById('out');
  out.textContent = '连接中…';
  const body = {
    account: document.getElementById('account').value.trim(),
    label: document.getElementById('label').value.trim(),
    cdp_url: document.getElementById('cdp').value.trim()
  };
  if (!body.account) { out.textContent = '账号标签必填'; return; }
  const key = keyBox.value.trim();
  localStorage.setItem('muse_bridge_key', key);
  try {
    const r = await fetch('/admin/attach', {
      method: 'POST',
      headers: {'Content-Type': 'application/json',
                ...(key ? {'Authorization': 'Bearer ' + key} : {})},
      body: JSON.stringify(body)
    });
    if (r.status === 401) {
      out.textContent = '401：服务端启用了 API 密钥，请在上方填入正确密钥'; return;
    }
    out.textContent = JSON.stringify(await r.json(), null, 2);
  } catch (e) {
    out.textContent = '请求失败: ' + e;
  }
};
</script>
</html>
"""


@app.get("/console", response_class=HTMLResponse)
async def console() -> str:
    return CONSOLE_HTML


# ---- 生成管线（图像 / 视频 / 任务 / 媒体下载）--------------------------


def _collect_references(messages: list[ChatMessage]) -> list[str]:
    """把 OpenAI 多模态 content 里的 image_url 收集成本地文件路径。

    之前 _prompt_of 只取 text、静默丢 image_url：客户端按多模态发的请求
    被当成纯文本处理，图片凭空消失且没有任何报错——比 4xx 更糟。现在
    data: URL 解码成临时文件、http(s) 走 refs.fetch_public_ref（自带 SSRF
    防护）； muse 聊天通道本身不吃图，所以这条只服务生成端点。
    """
    import base64 as _b64

    paths: list[str] = []
    upload_dir = media.DEFAULT_MEDIA_DIR
    os.makedirs(upload_dir, exist_ok=True)
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            url = str((item.get("image_url") or {}).get("url") or "")
            if not url:
                continue
            if url.startswith("data:"):
                head, _, payload = url.partition(",")
                mime = head.removeprefix("data:").partition(";")[0] or "image/png"
                blob = _b64.b64decode(payload)
            else:
                blob, mime = fetch_public(url)
            ext = media.ext_from_mime(mime) if hasattr(media, "ext_from_mime") else ".png"
            fd, tmp_path = tempfile.mkstemp(
                prefix="ref_", suffix=ext or ".png", dir=upload_dir
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
            paths.append(tmp_path)
    return paths


def _error(status: int, message: str, err_type: str, code: str | None = None):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def _normalize_state(state: str) -> str:
    """读侧归一：succeeded/success/done 都是口语化的 completed（上游 1499-1500）。"""
    lowered = (state or "").lower()
    if lowered in ("succeeded", "success", "done"):
        return jobs.JobStatus.COMPLETED.value
    return lowered if lowered in jobs.STATUS_VALUES else jobs.JobStatus.UNKNOWN.value


def public_base() -> str:
    """成品 URL 的对外前缀。空 = 相对路径（客户端自己拼 host）。

    反代/公网部署时相对 URL 会让客户端把 127.0.0.1 当媒体主机；设
    `MUSE_PUBLIC_BASE=https://api.example.com` 后响应里的 url 可直接下载。
    """
    return os.environ.get("MUSE_PUBLIC_BASE", "").strip().rstrip("/")


def _absolute(url: str) -> str:
    """把 bridge 自己的相对路径拼成对外地址（已是绝对地址则原样返回）。"""
    base = public_base()
    if not base or not url.startswith("/"):
        return url
    return f"{base}{url}"


def _chat_media_urls(items: list[dict]) -> list[dict]:
    """只在响应边界补对外前缀；下载仍使用既有 Bearer 鉴权。"""
    return [
        {**item, "url": _absolute(str(item.get("url") or ""))}
        for item in items
    ]


def _job_response(job: jobs.Job) -> dict:
    payload = job.to_dict()
    # 用 `job.state` 做读侧归一，而不是 `payload["state"]`——`Job.to_dict()` 的
    # 键叫 status，取 "state" 永远是 None，会把每条任务的对外状态都写成 unknown。
    payload["status"] = _normalize_state(job.state)
    token = str(payload.get("media_token") or "")
    if token:
        payload["url"] = _absolute(f"/v1/media/{token}")
        payload["result"] = {"url": payload["url"]}
        # 补 mime/size/kind（2026-11-11 补）。之前任务响应只有 token 和 url：
        # 客户端不知道成品是图还是视频、多大，要显示预览就得先下载一遍再说，
        # 视频尤其浪费。`ref()` 只读文件头 32 字节，代价可以忽略。
        #
        # 刻意**不**在这里算 sha256：那要整读文件，而轮询会重复调用本函数，
        # 视频大文件会被读成几十上百次。完整性校验留在下载侧做。
        if payload["status"] == "completed":
            try:
                ref = media.MediaStore().ref(token)
            except media.MediaError:
                # 文件已被清理（TTL）或不存在：不带元数据，不要因为补字段而报错。
                pass
            else:
                payload["mime"] = ref.mime
                payload["size"] = ref.size
                payload["kind"] = ref.kind
    return payload


class _GenerationRequest(BaseModel):
    """生成请求的公共形状。字段名对齐 OpenAI images API + 上游 VideoRequest。"""

    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str = "1:1"
    response_format: str = "url"
    images: list[str] = []
    videos: list[str] = []
    audios: list[str] = []
    files: list[str] = []
    extra: str | None = None
    timeout: float | None = None
    account: str | None = None
    # /v1/images/generations 专有：true 走任务模型（立即返回 task）
    async_mode: bool = False


def _references_of(
    request: _GenerationRequest, messages: list[ChatMessage] | None = None
) -> list[str]:
    refs_list = [*(request.images or []), *(request.videos or []),
                 *(request.audios or []), *(request.files or [])]
    if messages:
        refs_list.extend(_collect_references(messages))
    return refs_list[:env.REFERENCE_MAX]


async def _run_generation(
    kind: str,
    request: _GenerationRequest,
    *,
    messages: list[ChatMessage] | None = None,
    x_account: str | None = None,
):
    if not env.GENERATION_ENABLED:
        return _error(503, "生成能力已关闭（MUSE_GENERATION_ENABLED=0）", "service_unavailable")
    references = _references_of(request, messages)
    pinned = request.account or x_account or None
    try:
        payload = await pool.generate_media(
            store(),
            kind=kind,
            prompt=request.prompt,
            references=references,
            timeout=request.timeout,
            account=pinned,
        )
    except pool.NoAccountAvailable as exc:
        return _error(503, str(exc), "no_account_available")
    except pool.AllAccountsBusy as exc:
        return _error(429, str(exc), "all_accounts_busy")
    except pool.GenerationQuotaExhausted as exc:
        return _error(402, str(exc), "quota_exhausted", "quota_exhausted")
    except pool.GenerationRefused as exc:
        return _error(422, str(exc), "model_refusal")
    except pool.ReplyTimeout as exc:
        return _error(504, str(exc), "generation_timeout")
    except pool.GenerationError as exc:
        return _error(400, str(exc), "invalid_request_error")
    except ReferenceError as exc:
        return _error(400, str(exc), "invalid_request_error")
    return payload


@app.post("/v1/images/generations")
async def images_generations(
    request: _GenerationRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
):
    """OpenAI 图像端点。async_mode=true 时立即返回任务（语义同 /v1/images/tasks）。"""
    if not request.prompt.strip():
        return _error(400, "提示词为空", "invalid_request_error")
    if request.async_mode:
        return _submit_task("image", request, x_account=x_account)
    payload = await _run_generation("image", request, x_account=x_account)
    if isinstance(payload, JSONResponse):
        return payload
    media_info = payload.get("media") or {}
    data_item: dict = {"url": payload.get("url")}
    if request.response_format == "b64_json":
        token = str(media_info.get("token") or "")
        if token:
            data_item["b64_json"] = base64.b64encode(
                media.png_deliverable(media.MediaStore().read(token))
            ).decode()
    if media_info.get("width") or media_info.get("w"):
        data_item["width"] = media_info.get("width") or media_info.get("w")
    if media_info.get("height") or media_info.get("h"):
        data_item["height"] = media_info.get("height") or media_info.get("h")
    return {
        "created": int(payload.get("created_at") or 0),
        "data": [data_item],
        "task_id": payload.get("id"),
    }


def _submit_task(kind: str, request: _GenerationRequest, *, x_account: str | None = None):
    """入队并立即返回任务形状。任务实际执行交给后台推进。"""
    import asyncio as _asyncio

    if not request.prompt.strip():
        return _error(400, "提示词为空", "invalid_request_error")
    if not env.GENERATION_ENABLED:
        return _error(503, "生成能力已关闭（MUSE_GENERATION_ENABLED=0）", "service_unavailable")
    tasks = jobs.TaskStore(store())
    try:
        references = _references_of(request)
        job = tasks.create(
            kind,
            request.prompt,
            params={"references": references},
            account=request.account or x_account or "",
            ttl_seconds=env.TASK_TTL_SECONDS,
        )
    except jobs.JobValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    except jobs.JobQueueFull as exc:
        return _error(429, str(exc), "queue_full")

    async def _runner() -> None:
        # 复用外层刚建的那条任务行：旧实现让 generate_media 又建一条，于是客户端
        # 轮询的那条永远停在 queued，真正干活的那条在任务表里没人看。
        try:
            await pool.generate_media(
                store(),
                kind=kind,
                prompt=request.prompt,
                references=references,
                timeout=request.timeout,
                account=request.account or x_account or None,
                job_id=job.id,
            )
        except Exception as exc:  # noqa: BLE001 - 后台循环不能死在异常上
            # 正常失败路径 generate_media 已把任务写成终态；这里兜住"它还没来得及
            # 更新任务表就抛了"的早期失败（例如生成开关关闭、参数非法），否则客户端
            # 会永远看到 queued 而以为只是排队。
            with suppress(Exception):
                jobs.TaskStore(store()).fail(job.id, f"generation: {exc}")

    _asyncio.get_running_loop().create_task(_runner())
    payload = _job_response(job)
    payload.setdefault("progress", 0)
    payload.setdefault("task_id", payload.get("id"))
    return payload


@app.post("/v1/images/tasks")
async def images_tasks_create(
    request: _GenerationRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """异步图像任务。`Idempotency-Key` 相同则**原样重放**已有任务。

    顺序很关键：先查键再建任务。旧实现是"先建任务、后查键、冲突就 409"，于是
    重复请求会白建一条永远没人轮询的任务（还给队列加了一份负载），而客户端
    只看到 409 —— 既不是幂等，也泄漏任务。
    """

    if idempotency_key:
        with _idem_lock:
            prior = _idempotency.get(idempotency_key)
            if prior is not None:
                job = jobs.TaskStore(store()).get(prior) if prior else None
                if job is None:
                    return _error(
                        409,
                        f"Idempotency-Key 已绑定任务 {prior}，但它已不在任务表里",
                        "idempotency_conflict",
                    )
                return _job_response(job)
            # 占位：并发重复请求在第一条建完之前就要拿到同一个结论，不能各建一条。
            _idempotency[idempotency_key] = ""

    payload = _submit_task("image", request, x_account=x_account)
    task_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
    if idempotency_key:
        with _idem_lock:
            if task_id:
                _idempotency[idempotency_key] = task_id
            else:  # 提交失败（参数校验 400 等）：释放占位，下一次可以重试
                _idempotency.pop(idempotency_key, None)
    return payload


@app.get("/v1/images/tasks/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
@app.get("/v1/videos/{task_id}")
async def get_task(
    task_id: str,
    _: None = Depends(require_key),
):
    job = jobs.TaskStore(store()).get(task_id)
    if job is None:
        return _error(404, f"任务不存在：{task_id}", "not_found")
    return _job_response(job)


@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def videos_create(
    request: _GenerationRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
):
    """视频端点。**总是异步**：上游 600s 的生成不可能挂在 HTTP 请求里等。"""
    return _submit_task("video", request, x_account=x_account)


@app.head("/v1/media/{token}")
@app.get("/v1/media/{token}")
async def media_download(
    token: str,
    request: Request,
    range_header: str | None = Header(default=None, alias="range"),
    _: None = Depends(require_key),
):
    """成品下载。必须带 key：上游 `/v1/media/{name}` 无鉴权是已确认的公开缺陷。

    形状判定、Range 解析、响应头全部交给 media.py（那里是唯一事实来源），
    这里只做 HTTP 映射：非法 token 与不存在都是同一句 404，不给探测信号。

    **HEAD 必须支持**：播放器、CDN 回源、下载管理器普遍先发 HEAD 探测
    `Content-Length` / `Accept-Ranges` 再决定要不要续传。只挂 GET 会让它们
    拿到 405 然后放弃——成品明明在，客户端却播不了。HEAD 只回头不回体，
    而且**不读文件内容**（长度来自元数据），所以对几十 MB 的视频也是零成本。
    """
    head_only = request.method == "HEAD"
    ms = media.MediaStore()
    try:
        ref = ms.ref(token)
    except media.MediaError as exc:
        observability.bump("media_download_failed")
        if head_only:
            # HEAD 也要如实回状态码：探测方靠它判断"这份成品还在不在"。
            return Response(
                content=b"",
                status_code=exc.status_code,
                headers={"Content-Type": "application/json"},
            )
        return _error(exc.status_code, str(exc), "not_found" if exc.status_code == 404 else "media_error")

    headers = media.serve_headers(size=ref.size, filename=ref.filename, mime=ref.mime)
    span = media.parse_range(range_header, ref.size) if range_header else None
    if range_header and span is None and media.is_unsatisfiable_range(range_header, ref.size):
        # 可解析但不可满足（起点越界 / 起止倒置 / suffix 为 0）：必须 416。
        # 与"不可解析"分开判：后者按 RFC 忽略 Range 发 200 整文件。
        # 混成一个分支，要么把垃圾头判成 416，要么把越界判成 200——后者正是
        # 2026-11-11 活网验收抓到的 bug：`bytes=999999999-` 被 clamp 成末尾
        # 1 字节的 206，播放器当成有效数据。
        observability.bump("media_download_failed")
        headers["Content-Range"] = f"bytes */{ref.size}"
        headers["Content-Length"] = "0"
        return Response(
            content=b"",
            status_code=416,
            headers=headers,
            media_type="application/octet-stream",
        )
    if head_only:
        # 只回头：206 的 Content-Range/Content-Length 用区间算出来的值，
        # 与 GET 保持一致，否则探测方看到的长度和实测长度会对不上。
        if span is not None:
            first, last = span
            headers["Content-Range"] = f"bytes {first}-{last}/{ref.size}"
            headers["Content-Length"] = str(last - first + 1)
            return Response(content=b"", status_code=206, headers=headers)
        return Response(content=b"", status_code=200, headers=headers)
    try:
        with observability.timed("media_download"):
            if span is None:
                # 无 Range，或 Range 不可解析（多区间/非 bytes=）：
                # 按 RFC 忽略这个头，发整文件。
                return Response(content=ms.read(ref.token), headers=headers)
            data, first, last, total = ms.read_range(ref.token, span[0], span[1])
    except media.MediaRangeError:
        observability.bump("media_download_failed")
        headers["Content-Range"] = f"bytes */{ref.size}"
        headers["Content-Length"] = "0"
        return Response(
            content=b"",
            status_code=416,
            headers=headers,
            media_type="application/octet-stream",
        )
    headers["Content-Range"] = f"bytes {first}-{last}/{total}"
    headers["Content-Length"] = str(len(data))
    return Response(content=data, status_code=206, headers=headers)


@app.delete("/v1/media/{token}")
async def media_delete(token: str, _: None = Depends(require_key)) -> dict:
    """删掉一份成品。与上游"交付后由服务端定期清理"不同，这里给客户端一个
    主动回收的口子：`/v1/media` 带鉴权，删除也带鉴权，越权无从谈起。"""
    return {"deleted": media.MediaStore().delete(token)}


# ---- 图片编辑 ---------------------------------------------------------


@app.post("/v1/images/edits")
async def images_edits(
    request: _GenerationRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
):
    """OpenAI 图像编辑端点（**JSON 变体**）。

    OpenAI 官方形状是 `multipart/form-data`（`image[]` + `prompt`）。本服务没有
    装 `python-multipart`，也不打算为这一个端点多一个运行时依赖，所以只收 JSON：
    把参考图放进 `images`（data URL / http(s) 地址 / 服务端本地路径）。用
    multipart 的客户端会拿到一条点名原因的 400，而不是一个看不懂的 422——
    422 不会告诉调用方"换个格式就能用"。
    """
    if not request.prompt.strip():
        return _error(400, "提示词为空", "invalid_request_error")
    if not request.images:
        return _error(
            400,
            "images 为空：编辑必须至少带 1 张参考图（本端点只接受 JSON 的 images 字段，"
            "不支持 multipart/form-data）",
            "invalid_request_error",
        )
    payload = await _run_generation("image", request, x_account=x_account)
    if isinstance(payload, JSONResponse):
        return payload
    media_info = payload.get("media") or {}
    item: dict = {"url": _absolute(str(payload.get("url") or ""))}
    if media_info.get("width") or media_info.get("w"):
        item["width"] = media_info.get("width") or media_info.get("w")
    if media_info.get("height") or media_info.get("h"):
        item["height"] = media_info.get("height") or media_info.get("h")
    if request.response_format == "b64_json":
        token = str(media_info.get("token") or "")
        if token:
            item["b64_json"] = base64.b64encode(
                media.png_deliverable(media.MediaStore().read(token))
            ).decode()
    return {
        "created": int(payload.get("created_at") or 0),
        "data": [item],
        "task_id": payload.get("id"),
    }


# ---- fal.ai 队列兼容 ---------------------------------------------------


#: fal 队列的对外状态词。这里刻意只做映射，不发明新状态。
_FAL_STATUS = {
    jobs.JobStatus.QUEUED.value: "IN_QUEUE",
    jobs.JobStatus.PROCESSING.value: "IN_PROGRESS",
    jobs.JobStatus.COMPLETED.value: "COMPLETED",
    jobs.JobStatus.FAILED.value: "ERROR",
    jobs.JobStatus.CANCELLED.value: "ERROR",
    jobs.JobStatus.UNKNOWN.value: "ERROR",
}

_FAL_MODELS = (
    {"id": "muse-image", "kind": "image", "display_name": "muse 图像"},
    {"id": "muse-video", "kind": "video", "display_name": "muse 视频"},
)


class _FalRequest(BaseModel):
    """fal 队列风格入参。只映射我们真有的字段，其余的忽略（不假装支持）。"""

    prompt: str
    image_url: str | None = None
    video_url: str | None = None
    duration: int | None = None
    aspect_ratio: str | None = None
    seed: int | None = None
    account: str | None = None
    timeout: float | None = None

    def as_generation(self, kind: str) -> _GenerationRequest:
        """把 fal 形状翻成本仓的生成请求。

        时长/比例只走 `gen_params`：白名单与措辞在那里有唯一出处，本文件不再
        自己拼 `--duration`（那是 midjourney 风格的二手语法，muse 侧零解析证据）。
        `seed` 在 `gen_params.HYPOTHESIS_FIELDS` 里，传了会被显式拒绝——拒绝比
        悄悄忽略诚实；调用方据此知道"这个字段我们没证实"。
        """
        raw: dict[str, object] = {"model": f"muse-{kind}"}
        if self.duration is not None:
            raw["duration"] = self.duration
        if self.aspect_ratio:
            raw["aspect_ratio"] = self.aspect_ratio
        if self.seed is not None:
            raw["seed"] = self.seed
        params = gen_params.normalize_params(raw)
        clauses = gen_params.render_prompt_clauses(params)
        prompt = f"{self.prompt}，{clauses}" if clauses else self.prompt
        return _GenerationRequest(
            prompt=prompt,
            model=f"muse-{kind}",
            size=self.aspect_ratio or "1:1",
            images=[self.image_url] if self.image_url else [],
            videos=[self.video_url] if self.video_url else [],
            timeout=self.timeout,
            account=self.account,
        )


@app.get("/v1/fal/models")
async def fal_models(_: None = Depends(require_key)) -> dict:
    return {"models": [dict(item) for item in _FAL_MODELS]}


async def _fal_submit(kind: str, request: _FalRequest, *, x_account: str | None):
    """入队并回 fal 形状。fal 的语义是"提交即刻返回 request_id，再轮询"，
    与 `/v1/videos`「总是异步」一致；不额外造一条同步路径。"""
    if not request.prompt.strip():
        return _error(400, "prompt 为空", "invalid_request_error")
    try:
        generation = request.as_generation(kind)
    except ValueError as exc:
        # 参数不在白名单（含未被证实的 seed/…）：400 把人话原因带回去，
        # 别让它变成后台任务里的一条 generation_failed。
        return _error(400, str(exc), "invalid_request_error")
    payload = _submit_task(kind, generation, x_account=x_account)
    if isinstance(payload, JSONResponse):
        return payload
    request_id = str(payload.get("task_id") or payload.get("id") or "")
    return {
        "request_id": request_id,
        "status": _FAL_STATUS.get(str(payload.get("status") or ""), "IN_QUEUE"),
        "response_url": f"/v1/fal/muse/requests/{request_id}",
        "status_url": f"/v1/fal/muse/requests/{request_id}/status",
    }


@app.post("/v1/fal/muse/image")
async def fal_muse_image(
    request: _FalRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
):
    return await _fal_submit("image", request, x_account=x_account)


@app.post("/v1/fal/muse/video")
async def fal_muse_video(
    request: _FalRequest,
    _: None = Depends(require_key),
    x_account: str | None = Header(default=None, alias="x-account"),
):
    return await _fal_submit("video", request, x_account=x_account)


@app.get("/v1/fal/muse/requests/{request_id}/status")
async def fal_request_status(request_id: str, _: None = Depends(require_key)):
    job = jobs.TaskStore(store()).get(request_id)
    if job is None:
        return _error(404, f"请求不存在：{request_id}", "not_found")
    return {
        "request_id": request_id,
        "status": _FAL_STATUS.get(job.status, "ERROR"),
        "queue_position": 1 if job.status == jobs.JobStatus.QUEUED.value else 0,
        "progress": job.progress,
    }


@app.get("/v1/fal/muse/requests/{request_id}")
async def fal_request_result(request_id: str, _: None = Depends(require_key)):
    """取结果。没跑完回 202（fal 客户端据此继续轮询），跑完回成品 URL。"""
    job = jobs.TaskStore(store()).get(request_id)
    if job is None:
        return _error(404, f"请求不存在：{request_id}", "not_found")
    if not job.terminal:
        return JSONResponse(
            status_code=202,
            content={
                "request_id": request_id,
                "status": _FAL_STATUS.get(job.status, "IN_PROGRESS"),
                "progress": job.progress,
            },
        )
    if job.status != jobs.JobStatus.COMPLETED.value:
        return _error(
            500,
            f"生成失败：{job.error or '未知原因'}",
            "generation_failed",
            code=job.failure_class,
        )
    payload = _job_response(job)
    url = payload.get("url") or ""
    if job.kind == jobs.JobKind.VIDEO.value:
        body: dict = {"video": {"url": url}, "request_id": request_id}
    else:
        body = {"images": [{"url": url}], "request_id": request_id}
    body["status"] = "COMPLETED"
    body["task"] = payload
    return body


# ---- 运维面：任务与额度 ------------------------------------------------


@app.get("/admin/tasks")
async def admin_list_tasks(
    _: None = Depends(require_admin),
    kind: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> dict:
    """任务清单。`limit` 夹在 1..200：管理面翻页不需要一次拉全库。"""
    tasks = jobs.TaskStore(store())
    rows = tasks.list(
        kind=kind,
        state=state,
        limit=max(1, min(int(limit), 200)),
    )
    return {
        "tasks": [_job_response(job) for job in rows],
        "queued": tasks.queued_count(),
    }


@app.get("/admin/tasks/{task_id}")
async def admin_get_task(task_id: str, _: None = Depends(require_admin)):
    job = jobs.TaskStore(store()).get(task_id)
    if job is None:
        return _error(404, f"任务不存在：{task_id}", "not_found")
    return _job_response(job)


@app.post("/admin/tasks/clear")
async def admin_clear_tasks(_: None = Depends(require_admin), keep: int = 0) -> dict:
    """清空**已结束**的任务。`keep` 保留最新 N 条（对齐上游 clear_tasks(keep)）。"""
    removed = jobs.TaskStore(store()).clear(keep=keep)
    return {"removed": removed, "kept": max(0, int(keep))}


@app.get("/admin/accounts/{account}/quota")
async def admin_account_quota(account: str, _: None = Depends(require_admin)) -> dict:
    """读额度面板（muse.ai 没有额度 REST 接口，必须浏览器读文本）。"""
    record = store().get(account)
    if record is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    driver = MuseDriver(record.cdp_url)
    try:
        parsed = await driver.quota()
    except BrowserUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "browser_unavailable", "note": str(exc)},
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={"error": "quota_unavailable", "note": str(exc)},
        ) from exc
    finally:
        await driver.close()
    return {"account": account, "quota": parsed}


class _CookieImport(BaseModel):
    """导入的 cookie。`hatch_sess` 是必须的那条，其余按原样带上。"""

    cookies: dict[str, str]


@app.post("/admin/accounts/{account}/cookies")
async def admin_import_cookies(
    account: str, request: _CookieImport, _: None = Depends(require_admin)
) -> dict:
    """导入账号 cookie（纯协议/无浏览器路径的凭据）。

    落库走加密列；响应**只回 cookie 名字**，不回声值——响应体会进日志与
    浏览器缓存，那里不该出现会话凭据。
    """
    names = sorted(
        str(name).strip()
        for name, value in request.cookies.items()
        if str(name).strip() and str(value).strip()
    )
    if not names:
        return _error(
            400,
            "cookies 里没有有效键值对：至少需要一条有值的 hatch_sess",
            "invalid_request_error",
        )
    if not store().save_cookies(account, request.cookies):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"account": account, "stored": names, "count": len(names)}


async def _renew_http(cookies: dict[str, str]):
    """纯 HTTP 续期一步：把 `SessionError` 映射成与浏览器路径一致的状态码。

    401 会话死（人工重登）/ 403 出口受限（查代理）/ 429 限流 / 502 未确认。
    抽出来是为了 refresh / keepalive 两份实现不会在各写一套错误映射。
    """
    try:
        return await renew_session(cookies)
    except SessionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": "session_error", "note": str(exc)},
        ) from exc


async def _http_keepalive(account: str, cookies: dict[str, str], record) -> dict:
    """纯 HTTP 续期 + 唤醒：有导入的 cookie 时完全不碰浏览器。

    这是"协议优先"的第一块实际落地——`session_http` 的两个动作只需要 cookie，
    不需要 CDP。任一阶段失败都按 `SessionError.status_code` 映射（401 会话死 /
    403 出口受限 / 429 限流 / 502 未确认），与浏览器路径的状态码语义一致。
    """
    renewal = await _renew_http(cookies)
    # 服务端回的 Set-Cookie 是新的一代：必须落回去，否则下一轮还用旧串。
    store().save_cookies(account, renewal.cookies)
    store().touch_refresh(account)
    if not renewal.assigned:
        return {
            "account": account,
            "ok": False,
            "refreshed": False,
            "woken": False,
            "via": "http",
            "detail": renewal.reason,
        }
    try:
        wake = await http_wake_vm(renewal)
    except SessionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": "session_error", "note": str(exc)},
        ) from exc
    if wake.wake_ok:
        store().mark_ready(account, vm_ready=True, state=AccountState.READY)
    return {
        "account": account,
        "ok": bool(renewal.assigned and wake.wake_ok),
        "refreshed": True,
        "woken": bool(wake.wake_ok),
        "via": "http",
        "vm_state": wake.vm_state,
    }


@app.post("/admin/accounts/{account}/keepalive")
async def admin_keepalive(account: str, _: None = Depends(require_admin)) -> dict:
    """续期 + 唤醒一次跑完，逐段报告。

    为什么要合成一个端点：调度器只关心"这个号这轮还活不活"，分两次调
    refresh / wake 会拿到两份互相矛盾的中间态（续期成功但 VM 已睡）。这里
    串行执行、逐段给结论。

    两条路径：账号导入过 cookie 就走**纯 HTTP**（不启动浏览器）；否则走
    CDP 浏览器路径。返回值里的 `via` 说明实际用了哪条，运维据此能判断
    "浏览器没开也能续期"这个前提是否成立。
    """
    record = store().get(account)
    if record is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    cookies = store().cookies(account)
    if cookies:
        return await _http_keepalive(account, cookies, record)

    driver = MuseDriver(record.cdp_url)
    refreshed = False
    woken = False
    try:
        refreshed = await driver.touch_session()
        # 续期失败说明 cookie 已死，唤醒没有意义；成功才继续拉 VM。
        woken = await driver.wake_vm() if refreshed else False
    except BrowserUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "browser_unavailable", "note": str(exc)},
        ) from exc
    finally:
        await driver.close()
    if refreshed:
        store().touch_refresh(account)
        store().mark_ready(
            account, vm_ready=bool(woken or record.vm_ready), state=AccountState.READY
        )
    return {
        "account": account,
        "ok": bool(refreshed and woken),
        "refreshed": bool(refreshed),
        "woken": bool(woken),
        "via": "browser",
    }


# ---- 无限画布 ---------------------------------------------------------
# 顺序很关键：**先 include_router 再 mount 静态目录**。`StaticFiles(html=True)`
# 会把 `/canvas/api/spaces` 也当静态文件去查（查不到就 404），如果先 mount，
# 画布 API 会被静态托管整个盖掉，且这种坑在测试里只有真正请求 API 才会暴露。
app.include_router(canvas_router)
if STATIC_DIR.is_dir():
    # 目录由前端交付（muse_bridge/canvas/）。不存在时跳过挂载而不是让 app 起不来：
    # 前端还没写完的窗口期里，`/v1/*` 不能被画布连坐。
    app.mount(
        "/canvas",
        StaticFiles(directory=str(STATIC_DIR), html=True),
        name="canvas",
    )
