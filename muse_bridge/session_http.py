r"""不开浏览器维持 hatch_vml 的唯一路径。

为什么需要这个模块（以及为什么它和 driver.py 的假设相反）：

    driver.py:378-432 的 `wake_vm()` / `touch_session()` 走的是"页面内 fetch"，
    注释写着"不自己构造请求——那样拿不到前端生成的签名"。上游 muse2api 的实现
    证伪了这一点：engine.py:534-624 的 `renew_session_http()` 从头到尾只有
    普通 HTTP，没有任何签名头，而它是上游主链路（app.py:350、app.py:769）
    和整个后台保活（app.py:2346）实际在用的那条路。

    区别只在于**带不带 cookie**：页面内 fetch 靠浏览器 jar，外部构造就必须
    自己把 hatch_sess 拼进 Cookie 头。补齐这点之后，续期与唤醒都不需要浏览器。

为什么这件事必须能做：

  * hatch_vml 约 48 小时到期，而且**不因使用而续期**（store.py:27-31 的实测记录，
    上游 engine.py:655-658 也确认生成后读回来的 expires 一动不动）。
  * 于是"没人派单就没有浏览器、没有浏览器就没人续期、没有续期账号就死"是个死循环。
    /api/session 是唯一一个能在零浏览器成本下把 48 小时时钟往前拨的动作。

三条从上游踩坑里学来的硬规则，改代码前先读完注释：

  1. `wake` 返回 200 **只代表请求被受理**，不代表 VM 起来了。必须再打一次
     /api/session 看 vm_state==RUNNING 才算唤醒成功（engine.py:596-606）。
     把 200 当成功，表现为"报了唤醒成功然后第一条请求超时"。
  2. HTTP 403 **不是** cookie 失效，多半是出口 IP / 地区被拒。按 cookie 失效处理
     会把一批好号标记成终态、逼运营重新登录（engine.py:566-569）。
  3. 判断异常时绝不把请求内容拼进消息：代理 URL 里带凭据，异常串会进日志
     （engine.py:555-556 特意 `from None`）。这里只保留类型名。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from .env import SESSION_PROXY, env_positive_int

SITE = os.environ.get("MUSE_SITE", "https://muse.ai").rstrip("/")

# 上游取值：engine.py:554（session 12s）、engine.py:592（wake 8s）
SESSION_TIMEOUT_SECONDS = env_positive_int("MUSE_SESSION_TIMEOUT", "12")
WAKE_TIMEOUT_SECONDS = env_positive_int("MUSE_WAKE_TIMEOUT", "8")

# 决定账号生死的那条 cookie，以及它的实测寿命（store.py:27-31、state.py:18）
VML_COOKIE = "hatch_vml"
VML_TTL_SECONDS = 48 * 3600

# 这些 cookie 的有效期**不能**当成账号寿命：hatch_native_auth_device 是 30 天的
# 设备令牌，不决定生死，拿它估算会让面板显示"还剩 28 天"而账号其实明天就废。
NON_LIFETIME_COOKIES = frozenset({"hatch_native_auth_device", "hatch_sess"})

# 浏览器注入用不了的时刻（engine.py:636-640：expires <= now+3600 会被静默丢弃）
INJECTION_MIN_LIVE_SECONDS = 3600
INJECTION_FALLBACK_SECONDS = 7 * 86400

# 与上游的差异：engine.py:553 打的是裸 /api/session，不带 query。
# 这里按本轮规格带上 vm_state=live；muse 若忽略未知参数则无副作用，
# 真出问题把它设成空 dict 即可回退到上游形态（见 tests 里的说明）。
SESSION_PARAMS: dict[str, str] = {"vm_state": "live"}

# 上游实测的必要请求头（engine.py:543-551）。缺 Origin/Referer 时网关会把
# 请求当成跨站脚本；Sec-Fetch-* 是 Chrome 自己加的，缺了更容易命中风控。
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

RUNNING = "RUNNING"
DISABLED = "DISABLED"


# ---------------------------------------------------------------- 异常分层
# status_code 是给 app.py 直接映射用的，避免调用方再用 isinstance 链重写一遍。


class SessionError(RuntimeError):
    """会话/VM 操作失败。子类各自对应一个 HTTP 状态码。"""

    status_code = 502


class SessionExpired(SessionError):
    """401：cookie 真的失效了，必须人工重新登录。账号可判终态。"""

    status_code = 401


class EgressBlocked(SessionError):
    """403：出口 IP / 地区被拒。**不得**据此判定 cookie 失效。"""

    status_code = 403


class RateLimited(SessionError):
    """429：被限流，冷却后重试即可，不是坏号。"""

    status_code = 429


class SessionUnconfirmed(SessionError):
    """请求没成功 / 返回不是 JSON / status 不是 assigned：会话状态未知，保留原判定。

    `retryable` 只对一种情况为真：`status=unavailable` 且
    `vm_resolution_issue.kind=retryable`（VM 正在重启/重新调度）。
    此时调用方可以短退避重试，**不能**据此判定 cookie 失效（muse-cli
    `docs/PROTOCOL.md` 的 live 记录；本仓 2026-11-11 §8.10 采用）。
    """

    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        vm_issue: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.vm_issue = vm_issue


# ---------------------------------------------------------------- 工具


def normalize_vm_state(value: str | None) -> str | None:
    """上游大小写与空白不稳定（见 U-RMH-12）。归一化成大写常量形态，原值另存。"""
    if value is None:
        return None
    text = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    return text or None


def build_headers(cookies: Mapping[str, str], site: str) -> dict[str, str]:
    """拼六个硬头 + Cookie。空值 cookie 直接丢掉，否则会留下 `hatch_sess=;` 这种尾巴。"""
    jar = "; ".join(f"{name}={value}" for name, value in cookies.items() if value)
    return {
        "User-Agent": USER_AGENT,
        "Origin": site,
        "Referer": f"{site}/thread/new",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept": "application/json",
        "Cookie": jar,
    }


def parse_set_cookie(
    header_values: list[str], *, now: int | None = None
) -> tuple[dict[str, str], dict[str, int]]:
    """从 Set-Cookie 头读回 name -> value 与 name -> 过期时间戳（Unix 秒）。

    为什么要自己解析：httpx 的 Cookie jar 需要 request/response 成对注册，
    而我们只关心"上游重新下发了哪几条、什么时候到期"，手动解析既能单测也好读。
    支持 Expires（HTTP-date）与 Max-Age（秒），Max-Age 优先——语义上更新。
    没有过期信息的 cookie 返回 -1（会话 cookie），调用方自己决定怎么处理。
    """
    current = int(now if now is not None else time.time())
    values: dict[str, str] = {}
    expiries: dict[str, int] = {}
    for header in header_values or []:
        parts = str(header).split(";")
        if not parts or "=" not in parts[0]:
            continue
        name, _, raw = parts[0].partition("=")
        name = name.strip()
        if not name:
            continue
        values[name] = raw.strip().strip('"')
        max_age: int | None = None
        expires: int | None = None
        for attr in parts[1:]:
            key, _, attr_value = attr.partition("=")
            key = key.strip().lower()
            if key == "max-age":
                try:
                    max_age = int(attr_value.strip())
                except ValueError:
                    max_age = None
            elif key == "expires" and expires is None:
                try:
                    expires = int(parsedate_to_datetime(attr_value.strip()).timestamp())
                except (TypeError, ValueError):
                    expires = None
        if max_age is not None:
            expiries[name] = current + max_age
        elif expires is not None:
            expiries[name] = expires
        else:
            expiries[name] = -1
    return values, expiries


@dataclass(frozen=True)
class CookieExpiry:
    """一条 cookie 的有效期结论。estimated=True 表示这个数是推算的，不是读到的。"""

    name: str
    expires_at: int | None
    estimated: bool
    source: str

    @property
    def known(self) -> bool:
        return self.expires_at is not None

    def remaining(self, now: int | None = None) -> int | None:
        if self.expires_at is None:
            return None
        return self.expires_at - int(now if now is not None else time.time())


def normalize_cookie_expires(
    name: str, expires: Mapping[str, object] | None, *, now: int | None = None
) -> CookieExpiry:
    """账号寿命只认 hatch_vml 的真值；取不到就按 48h 估，并把 estimated 亮出来。

    刻意不去读 hatch_native_auth_device 的 30 天（上游踩过：拿它当寿命，面板显示
    还有二十几天，实际账号第二天就 401）。估算值也永不早于 now——把过去的时间
    当"还剩 -3 天"显示出来毫无意义，只会被误读成配置坏了。
    """
    current = int(now if now is not None else time.time())
    raw = (expires or {}).get(name)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0
    else:
        # 浮点与布尔都是脏数据：cookie 解析层（parse_set_cookie）只会产出整数字符串。
        # 带小数的时间戳意味着上游某处类型已经坏了——采信它会得到一个"恰好等于 now"
        # 的过期时刻（U-RMH-13 的回归就是这个形状），宁可回退估算。
        if isinstance(raw, (float, bool)):
            value = 0
    if value > 0:
        return CookieExpiry(name, value, False, "cookie")
    if name == VML_COOKIE:
        return CookieExpiry(name, current + VML_TTL_SECONDS, True, "estimated_vml_ttl")
    return CookieExpiry(name, None, False, "unknown")


def account_expiry(
    expires: Mapping[str, object] | None, *, now: int | None = None
) -> CookieExpiry:
    """整个账号的失效时间。只有 hatch_vml 说了算，其余一律不参与寿命判定。"""
    return normalize_cookie_expires(VML_COOKIE, expires, now=now)


def injection_expires(expires_at: int | None, *, now: int | None = None) -> float:
    """给浏览器注入时要用的 expires：太短会被 Chromium 丢弃，所以抬到 +7d。

    engine.py:636-640 的实测坑——expires 落在 now+3600 以内时 `Network.setCookie`
    看起来成功、cookie 却不在 jar 里，症状是"导入即失效"。这里显式实现成纯函数，
    好让 driver 那边（不归我改）以后能直接复用同一套判定。
    """
    current = float(now if now is not None else time.time())
    if expires_at and expires_at > current + INJECTION_MIN_LIVE_SECONDS:
        return float(expires_at)
    return current + INJECTION_FALLBACK_SECONDS


# ---------------------------------------------------------------- 结果对象


@dataclass(frozen=True)
class RenewResult:
    """一次 /api/session 的结果。`connectable` 才是"能用"，不是 `assigned`。"""

    status: str
    vm_id: str | None
    vm_state: str | None
    vm_state_raw: str | None
    endpoint_url: str | None
    cookies: Mapping[str, str]
    expires: Mapping[str, int]
    http_status: int
    raw: Mapping[str, object] = field(repr=False, default_factory=dict)

    @property
    def assigned(self) -> bool:
        return self.status == "assigned"

    @property
    def vm_running(self) -> bool:
        return self.vm_state == RUNNING

    @property
    def connectable(self) -> bool:
        """四个条件缺一不可：status/vm_id/vm_state/endpoint_url。

        缺 endpoint_url 意味着网关还没把 VM 的接入点发下来，此时即便
        vm_state 写着 RUNNING，直连也会 404 —— 判不可连比"看起来能连然后超时"好。
        """
        return bool(self.assigned and self.vm_id and self.vm_state and self.endpoint_url)

    @property
    def reason(self) -> str:
        if not self.assigned:
            return f"会话状态未确认（status={self.status or '空'}）"
        missing = [
            label
            for label, present in (
                ("vm_id", self.vm_id),
                ("vm_state", self.vm_state),
                ("endpoint_url", self.endpoint_url),
            )
            if not present
        ]
        if missing:
            return "/api/session 已 assigned 但缺字段：" + "、".join(missing)
        return ""


@dataclass(frozen=True)
class WakeResult:
    """一次 wake 的结果。accepted 与 ok 是两回事，混用是上游最容易犯的错。"""

    requested: bool
    accepted: bool
    wake_ok: bool
    vm_state: str | None
    vm_state_raw: str | None
    session: RenewResult | None
    http_status: int | None
    reason: str

    @property
    def status_code(self) -> int:
        return 200 if self.wake_ok else 502


# ---------------------------------------------------------------- 传输层


def _raise_auth_status(response: httpx.Response, *, what: str) -> None:
    """只抛会改变"这个号还能不能用"结论的三类状态码。

    401/403/429 的处置动作完全不同（重新登录 / 换出口 / 等一会儿），必须分开；
    其它状态码不该把一次操作升级成异常。分类依据见模块头三条硬规则的第 2 条。
    """
    code = response.status_code
    if code == 401:
        raise SessionExpired(
            f"{what} 认证失败（HTTP 401）：cookie 已失效，请在官网重新登录后导入"
        )
    if code == 403:
        raise EgressBlocked(
            f"{what} 被拒绝（HTTP 403）：多半是服务器出口 IP／地区受限，"
            "不能据此判定 cookie 失效，请先换出口再试"
        )
    if code == 429:
        raise RateLimited(f"{what} 被限流（HTTP 429）：稍后重试，账号本身没有失效")


def _raise_for_status(response: httpx.Response, *, what: str) -> None:
    """/api/session 用：先分认证类，剩下只要不是 200 就一律"状态未确认"。"""
    _raise_auth_status(response, what=what)
    code = response.status_code
    if code in (301, 302, 303, 307, 308):
        raise SessionUnconfirmed(
            f"{what} 返回了重定向（HTTP {code}）：本客户端不跟随跳转，"
            "通常说明出口被网关劫持到了登录／验证页"
        )
    if code != 200:
        raise SessionUnconfirmed(
            f"{what} 未成功（HTTP {code}）：上游状态未知，保留账号原有判定，请稍后重试"
        )


def _merge_cookies(
    current: Mapping[str, str],
    renewed: Mapping[str, str],
    expiries: Mapping[str, int],
) -> tuple[dict[str, str], dict[str, int]]:
    """合并而不是覆盖上游没下发的条目；expires 也只合并正值与会话标记，别把旧记录清空。"""
    cookies = {name: value for name, value in current.items() if value}
    cookies.update({name: value for name, value in renewed.items() if value})
    expires = {name: int(value) for name, value in (expiries or {}).items()}
    return cookies, expires


def _parse_session_payload(
    response: httpx.Response, *, cookies: Mapping[str, str], what: str
) -> RenewResult:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SessionUnconfirmed(
            f"{what} 返回 200 但不是 JSON：会话状态未确认，不要当成有效"
        ) from exc
    if not isinstance(payload, dict):
        raise SessionUnconfirmed(
            f"{what} 返回的 JSON 不是对象（{type(payload).__name__}）：会话状态未确认"
        )
    status = str(payload.get("status") or "")
    if status != "assigned":
        issue = payload.get("vm_resolution_issue")
        kind = str(issue.get("kind") or "") if isinstance(issue, dict) else ""
        if status == "unavailable":
            # VM 重启/重新调度窗口：muse-cli 记录到 200 + status=unavailable +
            # vm_resolution_issue.kind=retryable，可退避重试；这既不是 cookie
            # 失效，也不是"号坏了"。把 kind 原样带给上层，重试策略由调用方定。
            raise SessionUnconfirmed(
                f"{what} 返回 status=unavailable"
                f"（vm_resolution_issue.kind={kind or '空'}）："
                "VM 正在重启/重新调度，可短退避重试",
                retryable=kind == "retryable",
                vm_issue=kind or None,
            )
        raise SessionUnconfirmed(
            f"{what} 未返回 assigned 会话（status={status or '空'}）："
            "请在官网检查账号／工作区状态"
        )
    renewed, expiries = parse_set_cookie(response.headers.get_list("set-cookie"))
    merged_cookies, merged_expires = _merge_cookies(cookies, renewed, expiries)
    raw_state = payload.get("vm_state")
    return RenewResult(
        status=status,
        vm_id=payload.get("vm_id") or None,
        vm_state=normalize_vm_state(raw_state if isinstance(raw_state, str) else None),
        vm_state_raw=raw_state if isinstance(raw_state, str) else None,
        endpoint_url=payload.get("endpoint_url") or None,
        cookies=merged_cookies,
        expires=merged_expires,
        http_status=response.status_code,
        raw=payload,
    )


class _OwnClient:
    """没传 client 就临时建一个；传了就不许关——那是调用方的连接池。

    `proxy` 只在自建时生效：注入了 client 就说明调用方已经决定走它的出口
    （协议客户端那条路自带代理），这里再套一层只会打架。
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        timeout: float,
        proxy: str | None = None,
    ) -> None:
        self._injected = client
        self._timeout = timeout
        self._proxy = proxy
        self._own: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._injected is not None:
            return self._injected
        kwargs: dict[str, object] = {
            "timeout": self._timeout,
            "follow_redirects": False,
            "trust_env": False,
        }
        if self._proxy:
            # httpx>=0.26 的 `proxy=` 只接受 URL 字符串；空值必须整个省掉，
            # 传 `proxy=None` 在旧版本上是另一种语义。
            kwargs["proxy"] = self._proxy
        self._own = httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]
        return self._own

    async def __aexit__(self, *exc_info: object) -> None:
        if self._own is not None:
            await self._own.aclose()


# ---------------------------------------------------------------- 两个动作


async def renew_session(
    cookies: Mapping[str, str],
    *,
    site: str = SITE,
    client: httpx.AsyncClient | None = None,
    timeout: float = SESSION_TIMEOUT_SECONDS,
    proxy: str | None = None,
) -> RenewResult:
    """打一次 GET /api/session，续 hatch_vml(≈48h) / hatch_sess(≈30d) 并读回状态。

    不跟随重定向（engine.py:554 的 allow_redirects=False）：跟随后可能从
    muse.ai 跳到登录 CDN，那次的 Set-Cookie 与状态码都会把"会话有效"这件事
    解释成完全不同的意思。
    """
    if not any(value for value in (cookies or {}).values()):
        raise SessionExpired("没有可用的 cookie：/api/session 需要带上 hatch_sess")
    base = site.rstrip("/")
    async with _OwnClient(client, timeout, proxy or SESSION_PROXY) as http:
        try:
            response = await http.get(
                f"{base}/api/session",
                headers=build_headers(cookies, base),
                params=SESSION_PARAMS or None,
            )
        except httpx.HTTPError as exc:
            # 消息里只留类型名：代理 URL 可能带凭据，异常串会进日志。
            raise SessionUnconfirmed(
                f"/api/session 网络请求失败（{type(exc).__name__}）："
                "请检查服务器出口网络／代理后重试"
            ) from None
    _raise_for_status(response, what="/api/session")
    return _parse_session_payload(response, cookies=cookies, what="/api/session")


async def wake_vm(
    session: RenewResult,
    *,
    site: str = SITE,
    client: httpx.AsyncClient | None = None,
    timeout: float = WAKE_TIMEOUT_SECONDS,
    verify_tries: int = 1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    proxy: str | None = None,
) -> WakeResult:
    """唤醒云端 VM。**只有再读回 vm_state==RUNNING 才算 wake_ok**。

    verify_tries 默认 1，与上游 engine.py:596-606 完全一致（受理后立即确认一次）。
    真机 VM 冷启动常常超过一个 RTT，需要更稳的调用方传 verify_tries>1 慢慢等；
    默认值不敢擅自放大，因为每次确认都是一个真实请求。
    """
    base = site.rstrip("/")
    state = session.vm_state
    if not session.vm_id:
        return WakeResult(False, False, False, state, session.vm_state_raw, session, None,
                          "/api/session 没给出 vm_id，无法唤醒")
    if state == DISABLED:
        return WakeResult(False, False, False, state, session.vm_state_raw, session, None,
                          "工作区 VM 处于 DISABLED，发 wake 也不会起来，需要人工处理")

    async with _OwnClient(client, timeout, proxy or SESSION_PROXY) as http:
        headers = build_headers(session.cookies, base)
        headers["Content-Type"] = "application/json"
        body = {
            "vm_id": session.vm_id,
            "retry_count": 0,
            "connect_attempt_id": str(uuid.uuid4()),
        }
        try:
            response = await http.post(
                f"{base}/api/hatch/vm/wake", headers=headers, json=body
            )
        except httpx.HTTPError as exc:
            raise SessionUnconfirmed(
                f"唤醒请求发送失败（{type(exc).__name__}）：未确认 VM 状态"
            ) from None
        # 只把 401/403/429 抛出去：它们改变"这个号还能不能用"的结论。
        # 其它非 200（含 500）走 accepted=False 分支——wake 没成，但不必变成异常。
        _raise_auth_status(response, what="/api/hatch/vm/wake")

        accepted = response.status_code == 200
        final_state, final_raw = state, session.vm_state_raw
        verified: RenewResult | None = session
        if not accepted:
            return WakeResult(
                True, False, False, final_state, final_raw, session,
                response.status_code,
                f"wake 未被受理（HTTP {response.status_code}）",
            )
        for attempt in range(max(1, verify_tries)):
            if attempt:
                await sleep(timeout if timeout < 5 else 2.0)
            try:
                check = await http.get(
                    f"{base}/api/session",
                    headers=build_headers(session.cookies, base),
                    params=SESSION_PARAMS or None,
                )
            except httpx.HTTPError as exc:
                verified = None
                final_state = None
                final_raw = f"验证请求失败：{type(exc).__name__}"
                break
            if check.status_code != 200:
                # 确认这一步失败不推翻"已受理"，但也绝不能报成功。
                verified = None
                final_state = None
                final_raw = f"确认 vm_state 失败（HTTP {check.status_code}）"
                continue
            try:
                verified = _parse_session_payload(
                    check, cookies=session.cookies, what="/api/session(确认)"
                )
            except SessionUnconfirmed as exc:
                verified = None
                final_state = None
                final_raw = str(exc)
                continue
            final_state, final_raw = verified.vm_state, verified.vm_state_raw
            if final_state == RUNNING:
                break
        wake_ok = bool(accepted and final_state == RUNNING)
        if wake_ok:
            reason = "VM 已 RUNNING（wake 受理并确认通过）"
        elif accepted:
            reason = (
                "wake 已被受理（HTTP 200），但 /api/session 未确认 RUNNING"
                f"（vm_state={final_raw if final_raw not in (None, '') else final_state}）："
                "这只代表还在启动中，不能报成功"
            )
        else:
            reason = "wake 未被受理"
        return WakeResult(
            True, accepted, wake_ok, final_state, final_raw, verified,
            response.status_code, reason,
        )
