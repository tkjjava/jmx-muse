"""上游兼容层边界：状态归一、进度里程碑、续期三策略、媒体交付拒绝无鉴权路径。

全部离线：不联网、不启浏览器、不碰真实时钟（renew 相关的时间全部显式注入）。
每条断言旁边写上游证据行号，改规则时能立刻判断「是上游变了还是我们写错了」。

对拍关系（不是重复测试）：
  * `normalize_state` 六个规范值与 `jobs.public_status` 必须一致；
  * `is_media_token` 与 `media.TOKEN_RE` 必须一致；
  * 三条 `renew_policy` 分别对应 muse2api `jobs_keepalive.py:186-217`
    的两个判据与「未知兜底」分支。
"""

from __future__ import annotations

import uuid

import pytest

from muse_bridge import jobs as jobs_mod
from muse_bridge import media as media_mod
from muse_bridge import upstream_quirks as uq
from muse_bridge.upstream_quirks import (
    CANCELLED,
    COMPLETED,
    FAILED,
    LIVE_STATES,
    MEDIA_SERVE_BRIDGE,
    MEDIA_SERVE_UPSTREAM,
    MEDIA_UPSTREAM_TTL_SECONDS,
    MILESTONE_AGENT_GREW,
    MILESTONE_GENERATING,
    MILESTONE_STOP_LIFECYCLE,
    PROCESSING,
    PROCESSING_FLOOR,
    PROGRESS_MILESTONES,
    PUBLIC_STATES,
    QUEUED,
    RENEW_BACKOFF,
    RENEW_CONTINUE,
    RENEW_DEFER,
    RENEW_EGRESS_BLOCKED,
    RENEW_FAIL,
    RENEW_NOW,
    RENEW_PROBE_UNKNOWN,
    RENEW_PROXY_AUTH,
    RENEW_RELOGIN,
    RENEW_RETRY,
    RENEW_STAGE_LEASE,
    RENEW_STAGE_SESSION,
    RENEW_STAGE_WAKE,
    RENEW_WINDOW_SECONDS,
    TERMINAL_STATES,
    UNKNOWN,
    MediaPolicy,
    MediaPolicyError,
    advance_progress,
    classify_renew_response,
    is_media_token,
    media_url_policy,
    milestone_for,
    normalize_progress,
    normalize_state,
    progress_for_state,
    renew_policy,
    renew_stages,
)

T0 = 1_800_000_000
TOKEN = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------- 里程碑


def test_progress_milestones_are_the_upstream_tuple():
    """上游只报这八个值，顺序固定（czg app.py:1281-1505 + engine.py:1255-1258）。"""
    assert PROGRESS_MILESTONES == (0, 5, 10, 15, 20, 25, 40, 100)


def test_progress_milestones_are_strictly_increasing():
    values = list(PROGRESS_MILESTONES)
    assert values[0] == 0
    assert values[-1] == 100
    assert all(
        later > earlier for earlier, later in zip(values, values[1:], strict=False)
    )


def test_progress_milestones_stay_within_bounds():
    assert all(0 <= value <= 100 for value in PROGRESS_MILESTONES)
    assert len(set(PROGRESS_MILESTONES)) == len(PROGRESS_MILESTONES)


# ---------------------------------------------------------------- 状态归一


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 读侧归一：succeeded/success/done → completed（czg app.py:1499-1500）
        ("succeeded", COMPLETED),
        ("success", COMPLETED),
        ("done", COMPLETED),
        ("Succeeded", COMPLETED),
        ("  DONE  ", COMPLETED),
        # 运行中写法：in_progress / running（relaykit/dto/openai_video.go:11）
        ("in_progress", PROCESSING),
        ("in-progress", PROCESSING),
        ("IN PROGRESS", PROCESSING),
        ("running", PROCESSING),
        # 排队写法（midjourney TaskStatus.java:10-18；museapi-video db.js:166）
        ("pending", QUEUED),
        ("submitted", QUEUED),
        ("not_start", QUEUED),
        # 失败/取消（TaskStatus.java:22；museapi-video db.js:35 美式拼写）
        ("failure", FAILED),
        ("canceled", CANCELLED),
        ("cancelled", CANCELLED),
        ("aborted", CANCELLED),
    ],
)
def test_normalize_state_aliases(raw, expected):
    assert normalize_state(raw) == expected


@pytest.mark.parametrize("state", sorted(PUBLIC_STATES))
def test_normalize_state_keeps_canonical_values(state):
    assert normalize_state(state) == state


@pytest.mark.parametrize("raw", [None, "", "   ", 0, 42, {"state": "completed"}, ["done"]])
def test_normalize_state_unknown_for_non_strings(raw):
    """非字符串一律 unknown：把 dict 转成 str 再查表会撞上 repr 的偶然匹配。"""
    assert normalize_state(raw) == UNKNOWN


@pytest.mark.parametrize("raw", ["partial", "unknown", "weird", "succeed", "finish"])
def test_normalize_state_dat_not_guess_processing(raw):
    """认不出 → unknown，绝不猜成 processing（jobs.py:22-25 同一条理由）。"""
    assert normalize_state(raw) == UNKNOWN


@pytest.mark.parametrize("state", sorted(PUBLIC_STATES))
def test_normalize_state_agrees_with_public_status(state):
    """六个规范值与 jobs.public_status 对拍（jobs.py:139-142）。"""
    assert normalize_state(state) == jobs_mod.public_status(state)


def test_normalize_state_terminal_and_live_partition_public_states():
    assert TERMINAL_STATES | LIVE_STATES == PUBLIC_STATES
    assert not TERMINAL_STATES & LIVE_STATES
    assert normalize_state("completed") in TERMINAL_STATES
    assert normalize_state("processing") in LIVE_STATES


# ---------------------------------------------------------------- 进度解析


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (40, 40),
        (100, 100),
        (40.0, 40),
        ("40", 40),
        (" 40 ", 40),
        ("40%", 40),
        # 上游 new-api 收到的就是 "100%" 字符串（openai_video.go:33-36）
        ("100%", 100),
        ("100 %", 100),
    ],
)
def test_normalize_progress_accepts(raw, expected):
    assert normalize_progress(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        True,
        False,
        "",
        "   ",
        "abc",
        "40.5",  # 小数不是上游里程碑；new-api 的 Atoi 会静默变 0
        "40%%",
        40.5,
        -1,
        101,
        1e3,
        float("nan"),
        float("inf"),
    ],
)
def test_normalize_progress_rejects_dirty_values(raw):
    assert normalize_progress(raw) is None


@pytest.mark.parametrize(
    ("current", "incoming", "expected"),
    [
        (40, 20, 40),  # 回退保持
        (20, 40, 40),  # 前进
        (40, 40, 40),  # 不动
        (0, 5, 5),
        (100, 40, 100),  # 100 之后不再回退
        ("40", "60", 60),
        (None, 40, 40),
        (40, None, 40),
    ],
)
def test_advance_progress_is_monotonic(current, incoming, expected):
    assert advance_progress(current, incoming) == expected


def test_advance_progress_with_two_garbage_values_is_zero():
    assert advance_progress(None, "abc") == 0
    assert advance_progress("abc", "xyz") == 0


@pytest.mark.parametrize(
    ("state", "reported", "expected"),
    [
        (COMPLETED, 20, 100),
        (COMPLETED, None, 100),
        ("succeeded", 0, 100),  # 别名也要补到 100
        (PROCESSING, 5, PROCESSING_FLOOR),
        (PROCESSING, None, PROCESSING_FLOOR),
        (PROCESSING, 40, 40),
        (QUEUED, 0, 0),
        (QUEUED, 10, 10),  # video 受理时上游就报 10（czg app.py:1488）
        (FAILED, 40, 40),  # 失败不补 100（czg 只在 completed 分支补）
        (CANCELLED, 15, 15),
        (UNKNOWN, None, 0),
    ],
)
def test_progress_for_state(state, reported, expected):
    assert progress_for_state(state, reported) == expected


@pytest.mark.parametrize("state", sorted(PUBLIC_STATES | {UNKNOWN}))
@pytest.mark.parametrize("reported", [None, 0, "40%", 100, 101, "abc"])
def test_progress_for_state_never_emits_out_of_range(state, reported):
    value = progress_for_state(state, reported)
    assert isinstance(value, int)
    assert 0 <= value <= 100


# ---------------------------------------------------------------- 里程碑推导


@pytest.mark.parametrize(
    ("stop_seen", "agent_grew", "expected"),
    [
        (False, False, MILESTONE_GENERATING),
        (True, False, MILESTONE_STOP_LIFECYCLE),
        (False, True, MILESTONE_AGENT_GREW),
        # 上游原式里 agent 增长覆盖 Stop 分支（engine.py:1257-1258）
        (True, True, MILESTONE_AGENT_GREW),
    ],
)
def test_milestone_for(stop_seen, agent_grew, expected):
    assert milestone_for(stop_seen=stop_seen, agent_grew=agent_grew) == expected


@pytest.mark.parametrize("stop_seen", [False, True])
@pytest.mark.parametrize("agent_grew", [False, True])
def test_milestone_for_only_returns_milestones(stop_seen, agent_grew):
    assert milestone_for(stop_seen=stop_seen, agent_grew=agent_grew) in (
        PROGRESS_MILESTONES
    )


# ---------------------------------------------------------------- 续期策略


def test_renew_window_matches_upstream():
    """12 小时窗口，来自 jobs_keepalive.py:186-217 的 expiring_ids(within_sec=12*3600)。"""
    assert RENEW_WINDOW_SECONDS == 12 * 3600


@pytest.mark.parametrize(
    ("expires_at", "last_renew_at", "expected"),
    [
        # 到期已知：窗口外 defer、窗口边界/内部/已过期都续
        (T0 + RENEW_WINDOW_SECONDS + 1, None, RENEW_DEFER),
        (T0 + RENEW_WINDOW_SECONDS, None, RENEW_NOW),
        (T0 + 60, None, RENEW_NOW),
        (T0 - 1, None, RENEW_NOW),
        # 到期未知：靠 last_renew_at 兜底（jobs_keepalive.py:207-216）
        (None, None, RENEW_PROBE_UNKNOWN),
        (None, T0 - 60, RENEW_DEFER),
        (None, T0 - RENEW_WINDOW_SECONDS, RENEW_PROBE_UNKNOWN),
        (None, T0 - RENEW_WINDOW_SECONDS - 1, RENEW_PROBE_UNKNOWN),
        # 解析不了的 expires_at 当未知，绝不能当「还很久」
        ("not-a-date", None, RENEW_PROBE_UNKNOWN),
        ("", None, RENEW_PROBE_UNKNOWN),
        # 字符串形态的真实时间戳要能解析（上游 JSON 里就是数字/字符串混用）
        (str(T0 + 60), None, RENEW_NOW),
    ],
)
def test_renew_policy_three_strategies(expires_at, last_renew_at, expected):
    assert (
        renew_policy(expires_at=expires_at, last_renew_at=last_renew_at, now=T0)
        == expected
    )


def test_renew_policy_boundary_is_inclusive():
    """刚好 12 小时整时选择「续」：晚一秒可能撞上 48h 硬过期，代价不对称。"""
    assert renew_policy(expires_at=T0 + RENEW_WINDOW_SECONDS, now=T0) == RENEW_NOW
    assert (
        renew_policy(expires_at=T0 + RENEW_WINDOW_SECONDS + 1, now=T0) == RENEW_DEFER
    )


def test_renew_policy_window_is_configurable():
    # 还剩 2 小时：按 12h 窗口 → 续；按 1h 窗口 → 不续
    assert renew_policy(expires_at=T0 + 7200, now=T0) == RENEW_NOW
    assert (
        renew_policy(expires_at=T0 + 7200, now=T0, window_seconds=3600)
        == RENEW_DEFER
    )
    assert (
        renew_policy(expires_at=T0 + 7200, now=T0, window_seconds=7200) == RENEW_NOW
    )


@pytest.mark.parametrize("window", [0, -1, -RENEW_WINDOW_SECONDS])
def test_renew_policy_rejects_non_positive_window(window):
    """窗口 <= 0 等于每轮都续，比不续更糟（风控 + 代理账单）。"""
    with pytest.raises(ValueError):
        renew_policy(expires_at=T0 + 60, now=T0, window_seconds=window)


@pytest.mark.parametrize("window", [True, 12.0, "12h"])
def test_renew_policy_rejects_non_integer_window(window):
    with pytest.raises(TypeError):
        renew_policy(expires_at=T0 + 60, now=T0, window_seconds=window)


@pytest.mark.parametrize(
    ("assigned", "wake_vm", "expected"),
    [
        # 已分配：renew → wake（engine.py:666-687）
        (True, True, (RENEW_STAGE_SESSION, RENEW_STAGE_WAKE)),
        # 已分配但不唤醒：只有 renew
        (True, False, (RENEW_STAGE_SESSION,)),
        # 未分配：renew → lease → 再 renew（engine.py:608-650）
        (False, True, (RENEW_STAGE_SESSION, RENEW_STAGE_LEASE, RENEW_STAGE_SESSION)),
        (False, False, (RENEW_STAGE_SESSION, RENEW_STAGE_LEASE, RENEW_STAGE_SESSION)),
    ],
)
def test_renew_stages_follow_upstream_order(assigned, wake_vm, expected):
    assert renew_stages(assigned=assigned, wake_vm=wake_vm) == expected


def test_renew_stages_never_wake_without_assignment():
    """没有 vm_id 可唤醒；上游在未分配时直接关掉 wake_vm（engine.py:661-662）。"""
    stages = renew_stages(assigned=False, wake_vm=True)
    assert RENEW_STAGE_WAKE not in stages
    assert stages.index(RENEW_STAGE_LEASE) < len(stages)


# ---------------------------------------------------------------- 响应分类


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, RENEW_CONTINUE),
        (401, RENEW_RELOGIN),  # cookie 真失效（engine.py:564-565）
        (403, RENEW_EGRESS_BLOCKED),  # 出口/地区被拒，不是 cookie 失效（:566-568）
        (407, RENEW_PROXY_AUTH),  # 代理认证失败（:562-563）
        (429, RENEW_BACKOFF),  # 限流，冷却后重试（session_http.py:102-106）
        (500, RENEW_RETRY),
        (503, RENEW_RETRY),
        (400, RENEW_FAIL),
        (404, RENEW_FAIL),
        (418, RENEW_FAIL),
    ],
)
def test_classify_renew_response(status, expected):
    assert classify_renew_response(status) == expected


def test_403_is_never_treated_as_cookie_invalidation():
    """这是上游踩过的最贵的坑：403 判成失效会把一批好号逼去重新登录。"""
    result = classify_renew_response(403)
    assert result == RENEW_EGRESS_BLOCKED
    assert result != RENEW_RELOGIN


def test_classify_renew_response_accepts_numeric_strings():
    assert classify_renew_response("401") == RENEW_RELOGIN
    assert classify_renew_response(" 429 ") == RENEW_BACKOFF


@pytest.mark.parametrize("status", ["abc", "", None, "200 OK"])
def test_classify_renew_response_rejects_garbage(status):
    with pytest.raises(ValueError):
        classify_renew_response(status)


def test_classify_renew_response_rejects_bool():
    with pytest.raises(TypeError):
        classify_renew_response(True)


# ---------------------------------------------------------------- 媒体 token


def test_media_token_accepts_uuid4_hex_shape():
    """真实成品文件名就是 uuid4().hex（czg engine.py:1884/1908）。"""
    assert is_media_token(uuid.uuid4().hex)
    assert is_media_token(TOKEN)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a" * 31,
        "a" * 33,
        "A" * 32,  # 大写不是 uuid4().hex 的形态
        "g" * 32,
        "../" + "a" * 29,
        "a" * 16 + "/" + "a" * 15,
        "a" * 16 + "\\" + "a" * 15,
        "a" * 16 + ".." + "a" * 14,
        "../../etc/passwd",
        None,
        123,
        b"a" * 32,
    ],
)
def test_media_token_rejects_anything_else(token):
    assert not is_media_token(token)


def test_media_token_agrees_with_media_module():
    """与 media.py:66 的 TOKEN_RE 同一事实源，避免两处规则漂移。"""
    for token in (TOKEN, uuid.uuid4().hex, "A" * 32, "a" * 31, "../x"):
        expected = bool(media_mod.TOKEN_RE.fullmatch(token))
        assert is_media_token(token) == expected


# ---------------------------------------------------------------- 媒体交付策略


def test_media_policy_serves_bridge_with_key_by_default():
    policy = media_url_policy(TOKEN)
    assert isinstance(policy, MediaPolicy)
    assert policy.url == f"/v1/media/{TOKEN}"
    assert policy.require_key is True
    assert policy.allow_range is True
    assert policy.serve_via == MEDIA_SERVE_BRIDGE
    assert policy.serve_via != MEDIA_SERVE_UPSTREAM
    assert policy.ttl_seconds == MEDIA_UPSTREAM_TTL_SECONDS


def test_media_policy_uses_absolute_url_when_public_base_given():
    """相对路径会被客户端解析到自己域名（czg app.py:995-1003 的教训）。"""
    policy = media_url_policy(TOKEN, public_base="https://bridge.example")
    assert policy.url == f"https://bridge.example/v1/media/{TOKEN}"


def test_media_policy_normalizes_public_base_trailing_slash():
    policy = media_url_policy(TOKEN, public_base="https://bridge.example/")
    assert policy.url == f"https://bridge.example/v1/media/{TOKEN}"
    assert "//v1/media" not in policy.url


@pytest.mark.parametrize("public_base", ["javascript:alert(1)", "//evil.example", "ftp://x"])
def test_media_policy_rejects_non_http_public_base(public_base):
    with pytest.raises(MediaPolicyError):
        media_url_policy(TOKEN, public_base=public_base)


@pytest.mark.parametrize("token", ["", "a" * 31, "A" * 32, "../etc/passwd", None, 123])
def test_media_policy_rejects_bad_tokens(token):
    with pytest.raises(MediaPolicyError):
        media_url_policy(token)


def test_media_policy_refuses_upstream_direct():
    """上游 /v1/media/{name} 无鉴权（czg app.py:1822-1829），必须拒绝。"""
    with pytest.raises(MediaPolicyError) as caught:
        media_url_policy(TOKEN, upstream_direct=True)
    assert "无鉴权" in str(caught.value) or "upstream" in str(caught.value).lower()


def test_media_policy_refuses_auth_off():
    """关掉鉴权等于把成品变成公开可枚举资源，没有回退分支。"""
    with pytest.raises(MediaPolicyError):
        media_url_policy(TOKEN, auth_required=False)


def test_media_policy_refuses_both_unsafe_options_at_once():
    with pytest.raises(MediaPolicyError):
        media_url_policy(TOKEN, upstream_direct=True, auth_required=False)


def test_media_policy_ttl_clamped_by_upstream_expiry():
    """上游直链约 2 天过期（docs/PROTOCOL.md:176）；本地保留时长不能超过它。"""
    policy = media_url_policy(
        TOKEN, upstream_expires_at=T0 + 3600, now=T0
    )
    assert policy.ttl_seconds == 3600


def test_media_policy_ttl_floor_when_upstream_already_expired():
    """上游链接已过期时仍有本地副本可交付；但保留时间至少 1 秒，不能是 0。"""
    policy = media_url_policy(TOKEN, upstream_expires_at=T0 - 100, now=T0)
    assert policy.ttl_seconds == 1


def test_media_policy_ttl_ignores_unparseable_upstream_expiry():
    policy = media_url_policy(TOKEN, upstream_expires_at="soon", now=T0)
    assert policy.ttl_seconds == MEDIA_UPSTREAM_TTL_SECONDS


@pytest.mark.parametrize("ttl", [0, -1])
def test_media_policy_rejects_non_positive_ttl(ttl):
    with pytest.raises(MediaPolicyError):
        media_url_policy(TOKEN, ttl_seconds=ttl)


@pytest.mark.parametrize("ttl", [True, 2.5, "172800"])
def test_media_policy_rejects_non_integer_ttl(ttl):
    with pytest.raises(MediaPolicyError):
        media_url_policy(TOKEN, ttl_seconds=ttl)


def test_media_policy_never_yields_upstream_service():
    """任何正常参数组合都只能走 bridge；serve_via 不是可配置项。"""
    for base in ("", "https://b.example", "http://b.example/"):
        policy = media_url_policy(TOKEN, public_base=base)
        assert policy.serve_via == MEDIA_SERVE_BRIDGE
        assert policy.require_key is True
        assert policy.allow_range is True


def test_media_policy_error_is_a_value_error():
    """调用方的参数错误：app 层可以直接按 400 映射，不必加 isinstance 链。"""
    assert issubclass(MediaPolicyError, ValueError)


def test_module_exports_only_public_helpers():
    """防止把内部实现（_timestamp 之类）挂进 __all__ 被外部当接口用。"""
    exported = set(uq.__all__)
    assert exported
    assert all(not name.startswith("_") for name in exported)
    for name in (
        "normalize_state",
        "PROGRESS_MILESTONES",
        "renew_policy",
        "media_url_policy",
    ):
        assert name in exported
