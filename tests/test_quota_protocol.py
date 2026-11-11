"""quota_protocol 的离线自测：假客户端，不触网、不碰 cookie、不开浏览器。

为什么这样钉（对应 `docs/PROTOCOL_QUOTA.md` 的结论）：

* **不许编造周百分比**：哪怕 payload 里出现形似 `weekly_percent` 的键，也只能
  原样留在 `raw`，`weekly_percent` 必须还是 None。把"没读到"渲染成 0% 会让
  上层把"额度未知"当成"额度充足"（协议驱动那一格已经按同一原则钉过）。
* **错误映射要能区分处置动作**：401 是 cookie 失效（换号）、404 route-not-found
  是"内层根本没这条路由"（别重试）、其余 4xx/5xx 原样报状态码。
* **签名差异要钉住**：真实 `HatchProtocolClient.request` 的 `timeout` 是
  keyword-only，假客户端照抄；被测代码若按位置传，这里直接 TypeError。
"""

from __future__ import annotations

import pytest

from muse_bridge.quota_protocol import (
    KNOWN_SESSION_FIELDS,
    QUOTA_CANDIDATE_PATHS,
    REGISTRY_METHOD_COUNT,
    WEEKLY_QUOTA_NOTE,
    QuotaNoAccount,
    QuotaProtocolError,
    QuotaRouteNotFound,
    QuotaSessionUnauthorized,
    is_route_not_found,
    parse_session_payload,
    read_quota,
    read_session_quota,
)

# ---------------------------------------------------------------- 假件


class FakeResponse:
    """`ProtocolResponse` 的最小替身：status + headers + json()。"""

    def __init__(self, payload, *, status=200, headers=None):
        self.status = status
        self.headers = list(headers or [])
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    """`request(verb, path, body=None, *, timeout=...)`，签名与真实客户端一致。"""

    def __init__(self, response=None, *, error=None):
        self.response = response if response is not None else FakeResponse({})
        self.error = error
        self.calls = []

    async def request(self, verb, path, body=None, *, timeout=120.0):
        self.calls.append((verb, path, body, timeout))
        if self.error is not None:
            raise self.error
        return self.response


LEAD_404 = {
    "ok": False,
    "error": {"code": "not_found", "message": "route not found"},
}

FULL_SESSION = {
    "status": "assigned",
    "vm_id": "vm-123",
    "vm_name": "standard",
    "vm_type": "hatch_vm",
    "vm_state": "RUNNING",
    "endpoint_url": "https://vm-123.example.invalid",
    "cvm_eligibility": {"state": "ELIGIBLE", "observedAtMs": 1_760_000_000_000},
    "expires_at": 1_762_000_000,
}


# ---------------------------------------------------------------- 无账号/无 cookie
async def test_no_client_is_no_account():
    with pytest.raises(QuotaNoAccount):
        await read_session_quota(None)


async def test_client_without_request_is_no_account():
    with pytest.raises(QuotaNoAccount):
        # object() 上取不到 request，等价于"没有可用的协议客户端"
        await read_session_quota(object())


async def test_client_exception_wraps_into_protocol_error():
    client = FakeClient(error=RuntimeError("no cookie"))

    with pytest.raises(QuotaProtocolError) as caught:
        await read_session_quota(client)

    assert not isinstance(caught.value, QuotaNoAccount)
    assert "RuntimeError" in str(caught.value)
    assert caught.value.__cause__ is not None, "原始异常必须挂在 __cause__ 上"


# ---------------------------------------------------------------- session 字段缺失
@pytest.mark.parametrize("payload", [{}, {"status": "none"}, {"status": "assigned"}])
def test_missing_fields_become_none(payload):
    parsed = parse_session_payload(payload)

    assert parsed["vm_state"] is None
    assert parsed["vm_id"] is None
    assert parsed["cvm_eligibility"] is None
    assert parsed["expires_at"] is None
    assert parsed["unknown_fields"] == []


async def test_empty_session_keeps_quota_fields_none():
    client = FakeClient(FakeResponse({"result": {}}))

    info = await read_session_quota(client)

    assert info["found"] is False
    assert info["weekly_percent"] is None
    assert info["reset_at"] is None
    assert info["plan"] is None
    assert info["quota_readable"] is False
    assert info["renewable"] is False


# ---------------------------------------------------------------- 正常解析
async def test_full_session_parses_known_fields():
    client = FakeClient(
        FakeResponse(
            {"result": FULL_SESSION},
            headers=[("x-hatch-session-refreshed", "1")],
        )
    )

    info = await read_session_quota(client)

    assert client.calls == [("GET", "/api/session", None, 20.0)]
    assert info["found"] is True
    assert info["status"] == "assigned"
    assert info["vm_state"] == "RUNNING"
    assert info["vm_type"] == "hatch_vm"
    assert info["vm_id"] == "vm-123"
    assert info["endpoint_url"] == "https://vm-123.example.invalid"
    assert info["cvm_eligibility"] == "ELIGIBLE"
    assert info["expires_at"] == 1_762_000_000
    assert info["session_header_refreshed"] is True
    assert info["renewable"] is True
    assert info["source"] == "protocol:/api/session"
    # 额度面：读不到就写清楚，绝不编一个数出来。
    assert info["weekly_percent"] is None
    assert info["weekly_used_pct"] is None
    assert info["reset_at"] is None
    assert info["plan"] is None
    assert info["quota_readable"] is False
    assert info["note"] == WEEKLY_QUOTA_NOTE


def test_estimated_expiry_at_is_a_candidate_key_only():
    parsed = parse_session_payload({"estimated_expiry_at": 1_700_000_000})

    assert parsed["expires_at"] == 1_700_000_000
    assert parsed["estimated_expiry_at"] == 1_700_000_000


def test_bool_and_float_are_not_accepted_as_expiry():
    parsed = parse_session_payload({"expires_at": True, "estimated_expiry_at": 1.5e9})

    assert parsed["expires_at"] is None


# ---------------------------------------------------------------- 未知字段透传
async def test_unknown_fields_pass_through_without_guessing():
    payload = {
        "status": "assigned",
        "vm_state": "RUNNING",
        # 这些键**目前没有任何取证依据**：只能透传，不许映射成额度字段。
        "weekly_percent": 42,
        "plan_name": "pro",
        "nightly_reset_at": "2026-11-17T00:00:00Z",
    }
    client = FakeClient(FakeResponse(payload))

    info = await read_session_quota(client)

    assert info["raw"]["weekly_percent"] == 42
    assert info["raw"]["plan_name"] == "pro"
    assert info["unknown_fields"] == ["nightly_reset_at", "plan_name", "weekly_percent"]
    assert info["weekly_percent"] is None, "未取证的键绝不能映射进额度字段"
    assert info["plan"] is None
    assert info["reset_at"] is None


def test_known_fields_are_not_reported_as_unknown():
    parsed = parse_session_payload(FULL_SESSION)

    assert set(KNOWN_SESSION_FIELDS) - {"estimated_expiry_at"} <= set(parsed)
    assert parsed["unknown_fields"] == []


# ---------------------------------------------------------------- 错误映射
async def test_401_is_session_unauthorized():
    client = FakeClient(FakeResponse({"result": {}}, status=401))

    with pytest.raises(QuotaSessionUnauthorized) as caught:
        await read_session_quota(client)

    assert "401" in str(caught.value)


async def test_404_route_not_found_is_classified():
    """真机 GET /api/usage 等的 404 形状：必须归到"内层没有这条路由"。"""
    client = FakeClient(FakeResponse(LEAD_404, status=404))

    with pytest.raises(QuotaRouteNotFound) as caught:
        await read_session_quota(client)

    assert "route not found" in str(caught.value)


async def test_generic_404_is_plain_protocol_error():
    client = FakeClient(FakeResponse({"error": {"code": "forbidden"}}, status=404))

    with pytest.raises(QuotaProtocolError) as caught:
        await read_session_quota(client)

    assert not isinstance(caught.value, QuotaRouteNotFound)
    assert "404" in str(caught.value)


async def test_5xx_is_plain_protocol_error():
    client = FakeClient(FakeResponse({"result": {}}, status=503))

    with pytest.raises(QuotaProtocolError) as caught:
        await read_session_quota(client)

    assert "503" in str(caught.value)


async def test_missing_status_code_is_protocol_error():
    client = FakeClient(FakeResponse({}, status=None))

    with pytest.raises(QuotaProtocolError):
        await read_session_quota(client)


async def test_body_that_is_not_json_maps_to_protocol_error():
    client = FakeClient(FakeResponse(ValueError("not json")))

    with pytest.raises(QuotaProtocolError) as caught:
        await read_session_quota(client)

    assert "不是 JSON" in str(caught.value)


def test_route_not_found_shape():
    assert is_route_not_found(404, LEAD_404) is True
    assert is_route_not_found(404, {"error": {"message": "Route Not Found"}}) is True
    assert is_route_not_found(404, {"error": {"code": "forbidden"}}) is False
    assert is_route_not_found(200, LEAD_404) is False
    assert is_route_not_found(404, None) is False


# ---------------------------------------------------------------- 门面契约
async def test_read_quota_returns_four_quota_keys_plus_honest_tail():
    client = FakeClient(FakeResponse({"result": FULL_SESSION}))

    quota = await read_quota(client)

    assert set(quota) >= {"plan", "weekly_percent", "reset_at", "raw", "session"}
    assert quota["plan"] is None
    assert quota["weekly_percent"] is None
    assert quota["reset_at"] is None
    assert quota["raw"] == FULL_SESSION
    assert quota["source"] == "protocol:/api/session"
    assert quota["quota_readable"] is False
    assert quota["note"] == WEEKLY_QUOTA_NOTE
    assert quota["session"]["vm_state"] == "RUNNING"
    assert "raw" not in quota["session"]


async def test_timeout_is_forwarded_as_keyword():
    client = FakeClient(FakeResponse({"result": FULL_SESSION}))

    await read_session_quota(client, timeout=5.0)

    assert client.calls == [("GET", "/api/session", None, 5.0)]


# ---------------------------------------------------------------- 文档同源
def test_live_probed_candidates_and_registry_count_match_docs():
    assert QUOTA_CANDIDATE_PATHS == (
        "/api/usage",
        "/api/quota",
        "/api/billing/usage",
        "/api/me/usage",
    )
    assert REGISTRY_METHOD_COUNT == 418
    assert "不可得" in WEEKLY_QUOTA_NOTE
