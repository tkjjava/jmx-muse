"""协议侧额度/用量读取：先把"不可得"钉住，再如实返回读到的会话字段。

结论（2026-11-11 静态取证 + Lead 真机只读探针）：

1. **Noise 内层没有额度路由**。418 条网关方法白名单里没有
   `usage` / `quota` / `billing` / `plan` / `credit` 命名的 RPC；两条
   `subscription.*` 只负责回跳与兑换（路径 `/subscription/callback`、
   `/subscription/redeem-complimentary`），且语料里**没有调用点**；
   `wallet.get` 是支付方式目录（`payment_methods`/`card_last4`），不含用量。
2. **真机只读探针**：`GET /api/usage`、`/api/quota`、`/api/billing/usage`、
   `/api/me/usage`、`/api/session` 全部返回
   `404 {"ok":false,"error":{"code":"not_found","message":"route not found"}}`，
   与对照路径 `/api/zzz-not-a-route` 同形（证据 Level：真机实测）。
3. **UI 面板确实存在**，但它的组件不在已下载的 220 个 chunk 里：额度文案
   `{percent}% used`（词表 id `UJXDx`）在 46 份本地化词表里各有定义，
   而 `hk:"UJXDx"` 的代码引用**为 0**——说明渲染该面板的 chunk 未覆盖，
   其取数端点无从静态确定。
4. 前端 agent 状态枚举里有 `out_of_credits`（渲染成 "Out of usage"），
   即"额度是否耗尽"这一布尔信号在 UI 侧存在；但它由哪条事件/字段下发，
   在语料里定位不到（负证据，见 `docs/PROTOCOL_QUOTA.md`）。
5. **注意**：连 `GET /api/session` 在 Noise 内层也是 404 `route not found`——
   它不是 418 路由表里的条目，真正能读它的地方是 `session_http`（Noise **外层**
   HTTP + cookie）。所以 `read_session_quota()` 的内层读取路径在真机目前只会抛
   `QuotaRouteNotFound`；保留它有两个理由：(a) 把字段解析规则离线钉死，
   (b) 若将来服务端放开内层路由，接口不变。**不要**把它当"现成可用的额度源"。

因此本模块**不猜**周百分比：`weekly_percent` / `reset_at` / `plan` 恒为
``None``，调用方必须把 None 当"协议侧不可得"，绝不能渲染成 0%。能如实读出的
是 `/api/session` 的会话字段（`vm_state` 等，真机内层读不到，见第 5 条），
以及未知字段的透明透传。

用法（注入式，离线可测）：:

    from muse_bridge.quota_protocol import read_quota

    info = await read_quota(client)          # client.request 走 HTTP-over-Noise
    info["quota_readable"]                  # False：协议侧没有额度面
    info["session"]["vm_state"]             # "RUNNING" 之类，读到就如实给
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 明确写进返回值的负证据：不要把它当"还没实现"，它是本轮的结论。
WEEKLY_QUOTA_NOTE = (
    "每周用量百分比在协议侧不可得：Noise 内层无额度路由（418 条白名单里没有），"
    "前端额度面板文本的取数端点未在已下载 chunk 中定位；weekly_percent 恒为 None。"
)

#: Lead 2026-11-11 真机只读探针（GET-only、无额度消耗）实测全部
#: `404 route not found` 的候选路径。留在这里是为了让复跑命令和文档同源。
QUOTA_CANDIDATE_PATHS = (
    "/api/usage",
    "/api/quota",
    "/api/billing/usage",
    "/api/me/usage",
)

#: 网关方法白名单条目数（`3qzcufyl9d94_.js` 注册表去重后 418 条）。
REGISTRY_METHOD_COUNT = 418

#: `/api/session` 里前端实际消费过的字段（超出的键一律进 `unknown_fields`）。
KNOWN_SESSION_FIELDS = frozenset(
    {
        "status",
        "vm_id",
        "vm_name",
        "vm_type",
        "vm_state",
        "endpoint_url",
        "cvm_eligibility",
        "expires_at",
        "estimated_expiry_at",
    }
)

#: 404 时内层网关认的两种"路由不存在"错误码（真机实测形如 not_found）。
ROUTE_NOT_FOUND_CODES = frozenset({"not_found", "route_not_found"})

SESSION_PATH = "/api/session"
SESSION_TIMEOUT_SECONDS = 20.0
REFRESHED_HEADER = "x-hatch-session-refreshed"


class QuotaProtocolError(RuntimeError):
    """额度读取失败的基类：拿不到就说拿不到，不回一份假额度。"""


class QuotaNoAccount(QuotaProtocolError):
    """没有可用的账号/会话（没有注入 client，也就没有 cookie）。"""


class QuotaRouteNotFound(QuotaProtocolError):
    """内层路由不存在（真机 GET 额度候选路径的 404 就是这个形状）。"""


class QuotaSessionUnauthorized(QuotaProtocolError):
    """`/api/session` 401：cookie 已失效，该换号/重登，不是额度问题。"""


def _int_or_none(value: Any) -> int | None:
    """只接受真正的整数（bool 不算，浮点不截断），其余一律 None。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _header_value(headers: Any, name: str) -> str:
    """同时兼容 `list[tuple[str, str]]`（真实 ProtocolResponse）与 Mapping。"""
    wanted = name.lower()
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return ""
    for key, value in list(headers or ()):
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _payload_of(body: Any) -> dict[str, Any]:
    """内层响应可能把 JSON 包在 `result` 里，也可能直接是对象。"""
    if isinstance(body, Mapping):
        result = body.get("result")
        if isinstance(result, Mapping):
            return dict(result)
        return dict(body)
    return {}


def is_route_not_found(status: Any, body: Any) -> bool:
    """真机 404 形状：`{"ok":false,"error":{"code":"not_found",...}}`。"""
    if status != 404 or not isinstance(body, Mapping):
        return False
    error = body.get("error")
    if not isinstance(error, Mapping):
        return False
    code = str(error.get("code") or "").strip().lower()
    if code in ROUTE_NOT_FOUND_CODES:
        return True
    message = str(error.get("message") or "").strip().lower()
    return "route not found" in message


def parse_session_payload(payload: Any) -> dict[str, Any]:
    """把 `/api/session` 的 payload 拆成"已知字段 + 透传未知字段"。

    **不做事后猜测**：payload 里即便出现形似 `weekly_percent` 的键，也不会被
    映射进额度字段，只会原样留在 `raw` 与 `unknown_fields` 里——要接线必须先
    取证（见 `docs/PROTOCOL_QUOTA.md` 的"未证实项"）。
    """
    if not isinstance(payload, Mapping):
        return {
            "status": None,
            "vm_id": None,
            "vm_name": None,
            "vm_type": None,
            "vm_state": None,
            "endpoint_url": None,
            "cvm_eligibility": None,
            "expires_at": None,
            "estimated_expiry_at": None,
            "unknown_fields": [],
            "raw": {},
        }
    data = dict(payload)
    eligibility = data.get("cvm_eligibility")
    if isinstance(eligibility, Mapping):
        eligibility_state = eligibility.get("state")
        cvm_eligibility: str | None = (
            str(eligibility_state) if eligibility_state is not None else None
        )
    else:
        cvm_eligibility = None
    expires_at = _int_or_none(data.get("expires_at"))
    if expires_at is None:
        expires_at = _int_or_none(data.get("estimated_expiry_at"))
    return {
        "status": data.get("status"),
        "vm_id": data.get("vm_id"),
        "vm_name": data.get("vm_name"),
        "vm_type": data.get("vm_type"),
        "vm_state": data.get("vm_state"),
        "endpoint_url": data.get("endpoint_url"),
        "cvm_eligibility": cvm_eligibility,
        # expires_at 优先取 payload 的显式字段；`estimated_expiry_at` 只是同一个
        # 语义的候选键名，取到哪个都如实返回，取不到就是 None（不推断 TTL）。
        "expires_at": expires_at,
        "estimated_expiry_at": _int_or_none(data.get("estimated_expiry_at")),
        "unknown_fields": sorted(
            key for key in data if str(key) not in KNOWN_SESSION_FIELDS
        ),
        "raw": data,
    }


async def _request_session(client: Any, timeout: float) -> Any:
    """发一次 `GET /api/session`。`timeout` 必须是关键字（真实客户端就是这样）。"""
    requester = getattr(client, "request", None)
    if not callable(requester):
        raise QuotaNoAccount(
            "没有可用的协议客户端（无 client 即无 cookie），/api/session 无法发起"
        )
    try:
        return await requester("GET", SESSION_PATH, None, timeout=timeout)
    except QuotaProtocolError:
        raise
    except Exception as exc:
        raise QuotaProtocolError(
            "协议请求失败（会话/连接层）："
            f"{type(exc).__name__}；没有 cookie 或没连上 Noise 都会走这条路径"
        ) from exc


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        raise QuotaProtocolError("/api/session 没有返回状态码（响应未结束或流未收全）")
    return int(status)


def _response_json(response: Any) -> Any:
    reader = getattr(response, "json", None)
    if not callable(reader):
        raise QuotaProtocolError("/api/session 的响应没有 json()，无法解析")
    try:
        return reader()
    except (ValueError, UnicodeDecodeError) as exc:
        raise QuotaProtocolError("/api/session 的响应不是 JSON") from exc


async def read_session_quota(
    client: Any, *, timeout: float = SESSION_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """读 `/api/session` 的已知会话字段，并显式标注额度面不可得。

    失败一律抛 `QuotaProtocolError` 系：401 → 会话失效，404 route-not-found →
    内层没有这条路由，其余 4xx/5xx → 原样报状态码。绝不返回空额度冒充成功。

    真机现状：这条内层路由本身就是 404（见模块文档第 5 条），所以在真机上它
    通常抛 `QuotaRouteNotFound`；能读到字段的只有离线假客户端或用外层 HTTP
    （`session_http`）另走一遍。
    """
    response = await _request_session(client, timeout)
    status = _response_status(response)
    headers = getattr(response, "headers", None)
    if status == 401:
        raise QuotaSessionUnauthorized(
            "/api/session HTTP 401：cookie 已失效，该换号/重登（不是额度问题）"
        )
    if status == 404:
        body = _response_json(response)
        if is_route_not_found(404, body):
            raise QuotaRouteNotFound(
                "/api/session 在内层返回 404 route not found（与 /api/usage 等候选同形）"
            )
        raise QuotaProtocolError("/api/session HTTP 404")
    if status >= 400:
        raise QuotaProtocolError(f"/api/session HTTP {status}")

    parsed = parse_session_payload(_payload_of(_response_json(response)))
    refreshed = _header_value(headers, REFRESHED_HEADER).strip() == "1"
    assigned = str(parsed["status"] or "") == "assigned"
    parsed.update(
        {
            "renewable": bool(refreshed or assigned),
            "session_header_refreshed": refreshed,
            "found": bool(parsed["raw"]),
            "source": f"protocol:{SESSION_PATH}",
            "plan": None,
            "weekly_percent": None,
            "weekly_used_pct": None,
            "reset_at": None,
            "quota_readable": False,
            "note": WEEKLY_QUOTA_NOTE,
        }
    )
    return parsed


async def read_quota(
    client: Any, *, timeout: float = SESSION_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """额度门面：固定返回 `{plan, weekly_percent, reset_at, raw}` + 诚实尾巴。

    `plan` / `weekly_percent` / `reset_at` 恒为 ``None``（协议侧不可得），
    `raw` 是 `/api/session` 的原始 payload；`session` 是解析后的已知字段。
    """
    info = await read_session_quota(client, timeout=timeout)
    return {
        "plan": info["plan"],
        "weekly_percent": info["weekly_percent"],
        "reset_at": info["reset_at"],
        "raw": info["raw"],
        "session": {key: value for key, value in info.items() if key != "raw"},
        "source": info["source"],
        "found": info["found"],
        "quota_readable": info["quota_readable"],
        "note": info["note"],
    }
