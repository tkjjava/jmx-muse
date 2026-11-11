r"""muse_bridge.session_http 的离线测试：不连浏览器、不发真网络（httpx.MockTransport）。

断言编号对齐本轮逆向规格（证据来自 work/museapi-video/engine.py 与上游测试）：

  U-RMH-01  /api/session 必须带六个硬请求头，Cookie 只拼非空值      engine.py:543-551
  U-RMH-02  不跟随重定向：3xx 判"状态未确认"而不是跟进去             engine.py:554
  U-RMH-03  HTTP 401 → 会话真失效（可判终态）                        engine.py:564-565
  U-RMH-04  HTTP 403 → 出口受限，**不得**判 cookie 失效              engine.py:566-569
  U-RMH-05  HTTP 429 → 限流，可重试                                  app.py:237-244
  U-RMH-06  200 但非 JSON / 非对象 / status!=assigned → 未确认        engine.py:570-575
  U-RMH-07  Set-Cookie 回读 value 与 expires，且合并不覆盖            engine.py:576-580
  U-RMH-08  wake 的 HTTP 200 只是"受理"，必须回读 vm_state==RUNNING   engine.py:595-606
  U-RMH-09  vm_state==DISABLED 不发 wake                             engine.py:585
  U-RMH-10  wake body = {vm_id, retry_count:0, connect_attempt_id:uuid4}  engine.py:588-592
  U-RMH-11  异常消息不得回显带凭据的请求内容                          engine.py:555-556
  U-RMH-12  vm_state 大小写不稳，归一化并保留原值                     engine.py:582/605
  U-RMH-13  hatch_vml 无真值时按 now+48h 估算，且不拿 30d 设备令牌冒充  store.py:27-31
  U-RMH-14  assigned 但缺 endpoint_url → 判不可连                     本轮规格 §session
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from muse_bridge import session_http as sh
from muse_bridge.session_http import (
    EgressBlocked,
    RateLimited,
    RenewResult,
    SessionExpired,
    SessionUnconfirmed,
)

NOW = 1_900_000_000
GOOD_BODY = {
    "status": "assigned",
    "vm_id": "vm-fixture",
    "vm_state": "RUNNING",
    "endpoint_url": "https://vm-fixture.muse.ai",
}
COOKIES = {"hatch_sess": "sess-value", "hatch_vml": "vml-value", "empty_one": ""}


class Recorder:
    """记录每个请求，按顺序返回预设响应；也用来断言"根本没发出第二个请求"。"""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self._responses.pop(0) if self._responses else httpx.Response(
            200, json=dict(GOOD_BODY)
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), follow_redirects=False
        )

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def resp(body: object = GOOD_BODY, *, status: int = 200, headers: dict | None = None):
    if isinstance(body, str):
        return httpx.Response(status, text=body, headers=headers or {})
    return httpx.Response(status, json=body, headers=headers or {})


def session_of(**kw) -> RenewResult:
    payload = {**GOOD_BODY, **{"vm_state": kw.pop("vm_state_raw", "RUNNING")}, **kw}
    return sh._parse_session_payload(
        httpx.Response(200, json=payload), cookies=dict(COOKIES), what="fixture"
    )


# ---------------------------------------------------------------- renew_session


async def test_renew_sends_six_hard_headers_and_skips_empty_cookies():
    """U-RMH-01：六个硬头一个都不能少，空值 cookie 不进 Cookie 头。"""
    rec = Recorder(resp())
    async with rec.client() as client:
        result = await sh.renew_session(COOKIES, client=client)
    request = rec.requests[0]
    for name, value in (
        ("origin", "https://muse.ai"),
        ("referer", "https://muse.ai/thread/new"),
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-dest", "empty"),
    ):
        assert request.headers[name] == value, name
    assert "Chrome/135" in request.headers["user-agent"]
    cookie = request.headers["cookie"]
    assert "hatch_sess=sess-value" in cookie and "hatch_vml=vml-value" in cookie
    assert "empty_one" not in cookie
    assert result.connectable and result.assigned


async def test_renew_uses_session_path_and_vm_state_live_param():
    """本轮规格要求 GET /api/session?vm_state=live。"""
    rec = Recorder(resp())
    async with rec.client() as client:
        await sh.renew_session(COOKIES, client=client)
    assert rec.requests[0].url.path == "/api/session"
    assert rec.requests[0].url.params.get("vm_state") == "live"


async def test_renew_respects_custom_site():
    rec = Recorder(resp())
    async with rec.client() as client:
        await sh.renew_session(COOKIES, site="https://muse.example/", client=client)
    request = rec.requests[0]
    assert str(request.url).startswith("https://muse.example/api/session")
    assert request.headers["referer"] == "https://muse.example/thread/new"


async def test_renew_never_follows_redirect():
    """U-RMH-02：302 必须停下来报错，不能跟进跳转目标（跟进=把会话判断交给别人的站点）。"""
    rec = Recorder(
        httpx.Response(302, headers={"location": "https://login.example/continue"})
    )
    async with rec.client() as client:
        with pytest.raises(SessionUnconfirmed) as caught:
            await sh.renew_session(COOKIES, client=client)
    assert "重定向" in str(caught.value)
    assert len(rec.requests) == 1, "跟随了跳转"


@pytest.mark.parametrize(
    ("status", "expected", "code"),
    [
        (401, SessionExpired, 401),
        (403, EgressBlocked, 403),
        (429, RateLimited, 429),
        (500, SessionUnconfirmed, 502),
        (503, SessionUnconfirmed, 502),
    ],
)
async def test_renew_status_mapping(status, expected, code):
    """U-RMH-03/04/05 + 其它非 200 一律"未确认"。"""
    rec = Recorder(httpx.Response(status, text="nope"))
    async with rec.client() as client:
        with pytest.raises(expected) as caught:
            await sh.renew_session(COOKIES, client=client)
    assert caught.value.status_code == code
    assert f"HTTP {status}" in str(caught.value)


async def test_403_is_not_treated_as_expired():
    """U-RMH-04 的核心：出口被拒时把账号判成 cookie 失效会误杀一批好号。"""
    rec = Recorder(httpx.Response(403, text="blocked"))
    async with rec.client() as client:
        with pytest.raises(EgressBlocked) as caught:
            await sh.renew_session(COOKIES, client=client)
    assert not isinstance(caught.value, SessionExpired)
    assert "不能据此判定 cookie 失效" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        "<html>not json</html>",
        "[]",
        {"status": "unassigned"},
        {"status": ""},
        {},
    ],
)
async def test_renew_requires_json_object_and_assigned(body):
    """U-RMH-06：200 不等于有效会话。非 JSON / 非对象 / 非 assigned 都不能放行。"""
    async with Recorder(resp(body)).client() as client:
        with pytest.raises(SessionUnconfirmed):
            await sh.renew_session(COOKIES, client=client)


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [("retryable", True), ("manual", False), ("", False), (None, False)],
)
async def test_unavailable_vm_resolution_issue_carries_retryable_flag(kind, retryable):
    """VM 重启窗口（status=unavailable）：只有 kind=retryable 才允许退避重试。

    依据是 muse-cli docs/PROTOCOL.md 的 live 记录（2026-11-11 §8.10 采用）。
    其它 unavailable/manual 仍按未确认失败，不能把"号坏了"伪装成"正在重启"。
    """
    body = {"status": "unavailable"}
    if kind is not None:
        body["vm_resolution_issue"] = {"kind": kind}
    async with Recorder(resp(body)).client() as client:
        with pytest.raises(SessionUnconfirmed) as caught:
            await sh.renew_session(COOKIES, client=client)
    assert caught.value.retryable is retryable
    assert caught.value.vm_issue == (kind or None)
    assert "unavailable" in str(caught.value)
    assert not isinstance(caught.value, SessionExpired), "不能据此判定 cookie 失效"


async def test_renew_reads_back_set_cookie_values_and_expires():
    """U-RMH-07：Max-Age 折算成绝对时间戳，上游没下发的条目原样保留。"""
    headers = {"set-cookie": "hatch_vml=vml-new; Path=/; Max-Age=172800"}
    rec = Recorder(resp(headers=headers))
    async with rec.client() as client:
        result = await sh.renew_session(COOKIES, client=client)
    assert result.cookies["hatch_vml"] == "vml-new"
    assert result.cookies["hatch_sess"] == "sess-value", "合并而不是覆盖"
    assert result.expires["hatch_vml"] > 0
    assert "empty_one" not in result.cookies


def test_parse_set_cookie_supports_expires_maxage_and_session_cookie():
    """U-RMH-07 的纯函数部分：Max-Age 优先于 Expires；没过期信息记 -1。"""
    values, expiries = sh.parse_set_cookie(
        [
            "hatch_sess=abc; Path=/; Max-Age=3600",
            "hatch_vml=def; Path=/; Expires=Wed, 09 Jul 2031 10:00:00 GMT",
            "wd=1; Path=/",
            "garbage-without-equals",
        ],
        now=NOW,
    )
    assert values == {"hatch_sess": "abc", "hatch_vml": "def", "wd": "1"}
    assert expiries["hatch_sess"] == NOW + 3600
    assert expiries["wd"] == -1
    assert expiries["hatch_vml"] > NOW


async def test_renew_raises_before_any_request_without_cookies():
    """没 cookie 就别浪费一次 RTT。"""
    rec = Recorder(resp())
    async with rec.client() as client:
        with pytest.raises(SessionExpired):
            await sh.renew_session({"hatch_sess": ""}, client=client)
    assert rec.requests == []


async def test_transport_error_does_not_leak_credentials():
    """U-RMH-11：异常串会进日志，绝不能带上 cookie 值。"""
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to https://x:y@muse.ai")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(SessionUnconfirmed) as caught:
        await sh.renew_session({"hatch_sess": "super-secret-value"}, client=client)
    message = str(caught.value)
    assert "super-secret-value" not in message
    assert "ConnectError" in message, "只保留异常类型名"


# ---------------------------------------------------------------- wake_vm


async def test_wake_body_shape_and_uuid_per_call():
    """U-RMH-10：body 三个字段名固定，connect_attempt_id 是唯一的 uuid4。"""
    rec = Recorder(resp({"status": "assigned", "vm_state": "RUNNING"}))
    async with rec.client() as client:
        await sh.wake_vm(session_of(vm_state="SUSPENDED"), client=client)
    body = json.loads(rec.requests[0].content.decode())
    assert body["vm_id"] == "vm-fixture"
    assert body["retry_count"] == 0
    assert uuid.UUID(body["connect_attempt_id"]).version == 4
    assert rec.requests[0].method == "POST"
    assert rec.requests[0].url.path == "/api/hatch/vm/wake"


async def test_wake_confirms_by_reading_session_again():
    """U-RMH-08：wake 之后必须再打一次 /api/session 才算确认。"""
    rec = Recorder(resp({"status": "assigned", "vm_state": "RUNNING"}), resp())
    async with rec.client() as client:
        result = await sh.wake_vm(session_of(vm_state="SUSPENDED"), client=client)
    assert rec.paths == ["/api/hatch/vm/wake", "/api/session"]
    assert result.accepted and result.wake_ok
    assert result.vm_state == "RUNNING"


async def test_wake_200_alone_is_not_success():
    """U-RMH-08 的反面：这是最容易写错的一条——受理 ≠ 起来。"""
    rec = Recorder(
        httpx.Response(200, json={"accepted": True}),
        resp({"status": "assigned", "vm_state": "SUSPENDED"}),
    )
    async with rec.client() as client:
        result = await sh.wake_vm(session_of(vm_state="SUSPENDED"), client=client)
    assert result.accepted is True
    assert result.wake_ok is False
    assert "受理" in result.reason and "RUNNING" in result.reason
    assert result.status_code == 502


async def test_wake_failed_verification_does_not_raise_but_reports_not_ok():
    """确认那次 GET 失败只影响 wake_ok，不把已受理的 wake 升级成异常。"""
    rec = Recorder(
        httpx.Response(200, json={}), resp({"status": "assigned"}, status=503)
    )
    async with rec.client() as client:
        result = await sh.wake_vm(session_of(vm_state="SUSPENDED"), client=client)
    assert result.accepted and not result.wake_ok
    assert "HTTP 503" in (result.vm_state_raw or "")


async def test_wake_rejects_500_as_not_accepted():
    """非 200（且非 401/403/429）走 accepted=False 分支，不抛异常、不发确认请求。"""
    rec = Recorder(httpx.Response(500, text="boom"))
    async with rec.client() as client:
        result = await sh.wake_vm(session_of(vm_state="SUSPENDED"), client=client)
    assert result.accepted is False and result.wake_ok is False
    assert rec.paths == ["/api/hatch/vm/wake"]


async def test_wake_raises_on_expired_cookie():
    rec = Recorder(httpx.Response(401, text="nope"))
    async with rec.client() as client:
        with pytest.raises(SessionExpired):
            await sh.wake_vm(session_of(vm_state="SUSPENDED"), client=client)


async def test_wake_skipped_when_vm_disabled():
    """U-RMH-09：DISABLED 的 VM 发 wake 也不会起来，白烧一次请求。"""
    rec = Recorder()
    async with rec.client() as client:
        result = await sh.wake_vm(session_of(vm_state_raw="DISABLED"), client=client)
    assert result.requested is False and result.wake_ok is False
    assert rec.requests == [], "DISABLED 不该发 wake"


async def test_wake_skipped_without_vm_id():
    rec = Recorder()
    async with rec.client() as client:
        result = await sh.wake_vm(session_of(vm_id=None), client=client)
    assert result.requested is False and "vm_id" in result.reason
    assert rec.requests == []


async def test_wake_verify_tries_polls_until_running():
    """verify_tries>1 才做多次确认；默认值是 1，与上游一致，不擅自放大。"""
    rec = Recorder(
        httpx.Response(200, json={}),
        resp({"status": "assigned", "vm_state": "STARTING"}),
        resp({"status": "assigned", "vm_state": "RUNNING"}),
    )
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with rec.client() as client:
        result = await sh.wake_vm(
            session_of(vm_state="SUSPENDED"),
            client=client,
            verify_tries=3,
            sleep=fake_sleep,
        )
    assert result.wake_ok and slept == [2.0], "第二次确认前才等待"
    assert rec.paths.count("/api/session") == 2


# ---------------------------------------------------------------- vm_state / 寿命


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("running", "RUNNING"),
        (" Running ", "RUNNING"),
        ("v4-mapped", "V4_MAPPED"),
        ("", None),
        (None, None),
        ("SUSPENDED", "SUSPENDED"),
    ],
)
def test_normalize_vm_state_is_case_and_space_stable(raw, want):
    """U-RMH-12：上游大小写不稳，归一化后才能当判据。"""
    assert sh.normalize_vm_state(raw) == want


def test_renew_result_keeps_raw_state_and_connectable_needs_all_four():
    """U-RMH-14：assigned 但缺 endpoint_url 必须判不可连。"""
    ok = session_of()
    assert ok.connectable and ok.reason == ""
    assert ok.vm_state_raw == "RUNNING"
    no_endpoint = session_of(endpoint_url=None)
    assert no_endpoint.assigned and not no_endpoint.connectable
    assert "endpoint_url" in no_endpoint.reason
    for field_name in ("vm_id", "vm_state"):
        broken = session_of(**{field_name: None})
        assert not broken.connectable and field_name in broken.reason


def test_account_expiry_estimates_vml_and_ignores_device_token():
    """U-RMH-13：没有 hatch_vml 真值就按 48h 估，且亮着 estimated 标记。"""
    expires = {"hatch_native_auth_device": NOW + 30 * 86400}
    result = sh.account_expiry(expires, now=NOW)
    assert result.estimated is True and result.source == "estimated_vml_ttl"
    assert result.expires_at == NOW + 48 * 3600
    assert result.expires_at != expires["hatch_native_auth_device"], "30d 设备令牌冒充了寿命"


def test_account_expiry_prefers_real_vml_value():
    real = NOW + 3600
    result = sh.account_expiry({"hatch_vml": real}, now=NOW)
    assert (result.estimated, result.expires_at, result.source) == (False, real, "cookie")


@pytest.mark.parametrize("bad", [None, 0, -1, "abc", "", {}, 1_900_000_000.5])
def test_estimate_is_never_in_the_past(bad):
    """U-RMH-13：估算值不得早于 now；脏值也不能让函数抛。"""
    result = sh.normalize_cookie_expires("hatch_vml", {"hatch_vml": bad}, now=NOW)
    assert result.expires_at is not None and result.expires_at > NOW
    if bad is not None and not isinstance(bad, int | float) or (
        isinstance(bad, float) and int(bad) == bad
    ):
        assert result.estimated is True


def test_unknown_cookie_without_expiry_is_not_lifetime():
    result = sh.normalize_cookie_expires("hatch_sess", {}, now=NOW)
    assert result.expires_at is None and result.estimated is False


def test_injection_expires_avoids_chromium_drop_window():
    """engine.py:636-640 的坑：expires 落在 now+1h 内会被 Chromium 静默丢弃。"""
    assert sh.injection_expires(NOW - 10, now=NOW) == NOW + 7 * 86400
    assert sh.injection_expires(None, now=NOW) == NOW + 7 * 86400
    assert sh.injection_expires(NOW + 600, now=NOW) == NOW + 7 * 86400
    far = NOW + 30 * 86400
    assert sh.injection_expires(far, now=NOW) == float(far)


def test_defaults_match_upstream_timeouts():
    """上游取值：session 12s / wake 8s（engine.py:554、engine.py:592）。"""
    assert sh.SESSION_TIMEOUT_SECONDS == 12
    assert sh.WAKE_TIMEOUT_SECONDS == 8
    assert sh.VML_TTL_SECONDS == 48 * 3600
    assert "hatch_native_auth_device" in sh.NON_LIFETIME_COOKIES


async def test_injected_client_is_not_closed_by_the_call_target():
    """调用方传进来的 client 不许被关：那是它的连接池。"""
    rec = Recorder(resp())
    client = rec.client()
    await sh.renew_session(COOKIES, client=client)
    assert not client.is_closed
    await sh.renew_session(COOKIES, client=client)
    assert len(rec.requests) == 2
    await client.aclose()


def test_remaining_helper_is_usable_for_ui():
    result = sh.account_expiry({"hatch_vml": NOW + 100}, now=NOW)
    assert result.remaining(now=NOW) == 100
    assert result.known is True
    assert sh.CookieExpiry("hatch_vml", None, False, "unknown").remaining(now=NOW) is None


def test_module_uses_env_helpers_not_raw_int():
    """四个超时/上限常量必须走 env.py，坏值要点名报错。"""
    from muse_bridge.env import EnvError

    with pytest.raises(EnvError):
        sh.env_positive_int("MUSE_SESSION_TIMEOUT_BAD", "abc")
    assert sh.env_positive_int("MUSE_SESSION_TIMEOUT", "12") == 12
    assert time.time() > 0  # 只是确认导入没被优化掉
