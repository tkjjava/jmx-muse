"""FastAPI 应用：OpenAI 兼容接口 + CDP 连接授权台。"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __author__, __mark__, __version__, pool
from .driver import CDP_URL, MuseDriver
from .state import AccountState, after_probe
from .store import AccountStore

REPLY_TIMEOUT = float(os.environ.get("MUSE_REPLY_TIMEOUT", "180"))


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


app = FastAPI(title="muse-bridge", version=__version__)

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
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


def _prompt_of(messages: list[ChatMessage]) -> str:
    """压成一段文本。muse 是单轮接口，历史以标记形式拼入。"""
    parts: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict)
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
            for model in sorted(pool.MODEL_ALIASES)
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, _: None = Depends(require_key)):
    """OpenAI 兼容补全。

    不标注返回类型：JSONResponse | StreamingResponse 联合类型会被
    FastAPI 当成 Pydantic 响应模型解析并报错。
    """
    prompt = _prompt_of(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="消息内容为空")

    try:
        result = await pool.dispatch(store())
    except pool.NoAccountAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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
        """收尾：关闭驱动连接（不关用户的 Chrome）。"""
        if result.driver is not None:
            await result.driver.close()

    if not request.stream:
        try:
            text = await pool.collect_reply(result, prompt, timeout=REPLY_TIMEOUT)
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
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(prompt) // 4,
                    "completion_tokens": len(text) // 4,
                    "total_tokens": (len(prompt) + len(text)) // 4,
                },
            }
        )

    async def event_stream() -> AsyncIterator[str]:
        def chunk(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

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
            async for delta in pool.stream_reply(
                result, prompt, timeout=REPLY_TIMEOUT
            ):
                if not delta:
                    continue
                yield chunk(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        except TimeoutError:
            pass
        finally:
            await finish()

        yield chunk(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


# ---- 账号管理 ---------------------------------------------------------


class AttachRequest(BaseModel):
    account: str
    label: str = ""
    cdp_url: str = ""


@app.post("/admin/attach")
async def attach(request: AttachRequest) -> dict:
    """连接 CDP、检查登录态、入库。不触发任何登录或注册。"""
    cdp = request.cdp_url.strip() or CDP_URL
    driver = MuseDriver(cdp)
    try:
        status = await driver.status()
    finally:
        # 只断开，不关用户的 Chrome
        pass

    if not status.connected:
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
async def list_accounts(_: None = Depends(require_key)) -> dict:
    return {"accounts": store().redacted()}


@app.delete("/admin/accounts/{account}")
async def delete_account(account: str) -> dict:
    return {"deleted": store().delete(account)}


@app.post("/admin/accounts/{account}/refresh")
async def refresh_session(account: str) -> dict:
    """打一次 /api/session 续期（hatch_vml 48h / hatch_sess 30d）。"""
    record = store().get(account)
    if record is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    driver = MuseDriver(record.cdp_url)
    try:
        ok = await driver.touch_session()
    finally:
        await driver.close()
    if ok:
        store().touch_refresh(account)
        store().mark_ready(account, vm_ready=True, state=AccountState.READY)
    return {"ok": ok, "refreshed": ok}


@app.post("/admin/accounts/{account}/wake")
async def wake_vm(account: str) -> dict:
    record = store().get(account)
    if record is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    driver = MuseDriver(record.cdp_url)
    try:
        ok = await driver.wake_vm()
    finally:
        await driver.close()
    return {"ok": ok}


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
        "cdp_url": CDP_URL,
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

<button id="go">连接并入库</button>
<pre id="out">等待操作</pre>

<div class="sign">muse-bridge · <b>君莫笑</b><br><b>永恒之兰</b></div>

<script>
document.getElementById('go').onclick = async () => {
  const out = document.getElementById('out');
  out.textContent = '连接中…';
  const body = {
    account: document.getElementById('account').value.trim(),
    label: document.getElementById('label').value.trim(),
    cdp_url: document.getElementById('cdp').value.trim()
  };
  if (!body.account) { out.textContent = '账号标签必填'; return; }
  try {
    const r = await fetch('/admin/attach', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
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
