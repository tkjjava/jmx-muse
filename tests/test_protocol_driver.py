"""protocol_driver 与 pool 派单选择的自测：全离线，不联网、不开浏览器。

为什么这样钉：

* 「协议优先、浏览器兜底」的选择是纯分支逻辑，用假驱动就能把两条路径都走一遍。
  真机验证由 Lead 单独做（`MUSE_PROTOCOL_DRIVER=1` +
  `MUSE_PROTOCOL_PROXY=http://127.0.0.1:18000`），这里一条网线都不碰。
* 回退路径最容易写错的是**锁**：抢到锁之后协议连不上 → 回退浏览器 → 若此时
  漏放锁，那个账号就永久停在「忙」，只能重启进程。所以这里既断言驱动类型，
  也断言锁在「交接」与「释放」两个时刻的真实状态。
* 额度这一格刻意钉住 `weekly_used_pct is None`：协议侧拿不到 UI 的每周百分比，
  编一个 0% 出来会让上层把「没读到」当成「没用过」。
"""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace

import pytest

from muse_bridge import env, pool
from muse_bridge.driver import BrowserStatus, BrowserUnavailable, MuseDriver
from muse_bridge.protocol_client import ProtocolAuthError, ProtocolError
from muse_bridge.protocol_driver import DriverStatus, ProtocolDriver, ProtocolRefusal
from muse_bridge.protocol_engine import (
    GenerationResult,
    ProtocolGenerationError,
    ProtocolQuotaExhausted,
)
from muse_bridge.state import AccountState
from muse_bridge.store import AccountStore


# ---------------------------------------------------------------- 假件
class FakeClient:
    """只实现 `ProtocolDriver` 会用到的那几件：connect / close / request + 只读属性。

    `request` 的签名与真实 `HatchProtocolClient.request` 一致（`timeout` 在 `*`
    之后）：如果被测代码按位置传 timeout，这里会直接 TypeError，测试就红了——
    这正是要钉住的签名差异。
    """

    ws_url = "wss://hatch.metaaivm.com/v1/noise?vm_id=vm-1"
    vm_state = "RUNNING"
    wake_ok = True

    def __init__(self) -> None:
        self.connect_calls = 0
        self.close_calls = 0
        self.requests: list[tuple] = []
        self.response_status: int | None = 200
        self.connect_error: BaseException | None = None

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def close(self) -> None:
        self.close_calls += 1

    async def request(self, verb, path, body=None, *, timeout: float = 120.0):
        self.requests.append((verb, path, body, timeout))
        return SimpleNamespace(status=self.response_status)


class FakeEngine:
    """按 `ProtocolEngine.stream_chat` 的事件形状吐一串预置事件。

    生成路径（`generate_image` / `generate_video`）用可配置的返回值或异常模拟：
    `ProtocolDriver.generate` 只负责"调引擎、落盘、翻译 dict"，行为全在这里钉。
    """

    def __init__(
        self,
        events: list[dict],
        *,
        quota=None,
        generation: GenerationResult | None = None,
        generation_error: BaseException | None = None,
    ) -> None:
        self.events = list(events)
        self.quota_info = quota or {
            "renewable": True,
            "vm_state": "RUNNING",
            "estimated_expiry_at": 1_760_000_000,
        }
        self.stream_calls: list[tuple[str, float]] = []
        self.collect_calls: list[tuple[str, float]] = []
        self.quota_calls = 0
        self.generation_calls: list[tuple] = []
        self.generation_result = generation or GenerationResult(
            data=b"\x89PNG\r\n\x1a\nfake-image-bytes",
            mime="image/png",
            kind="image",
        )
        self.generation_error = generation_error

    async def stream_chat(self, prompt: str, *, timeout: float = 300.0):
        self.stream_calls.append((prompt, timeout))
        for event in self.events:
            yield event

    async def collect_chat(self, prompt: str, *, timeout: float = 300.0):
        self.collect_calls.append((prompt, timeout))
        texts = [str(e.get("text") or "") for e in self.events if e["type"] == "delta"]
        return "".join(texts), []

    async def generate_image(self, prompt, *, references=None, timeout=600.0, on_progress=None):
        self.generation_calls.append(("image", prompt, list(references or []), timeout))
        return self._generate_result()

    async def generate_video(self, prompt, *, references=None, timeout=600.0, on_progress=None):
        self.generation_calls.append(("video", prompt, list(references or []), timeout))
        return self._generate_result()

    def _generate_result(self) -> GenerationResult:
        if self.generation_error is not None:
            raise self.generation_error
        return self.generation_result

    async def quota(self):
        self.quota_calls += 1
        return dict(self.quota_info)


def make_driver(
    events: list[dict] | None = None,
    *,
    client: FakeClient | None = None,
    quota: dict | None = None,
    engine: FakeEngine | None = None,
) -> tuple[ProtocolDriver, FakeClient, FakeEngine]:
    """造一个换掉 client/engine 的 ProtocolDriver：构造期不联网（connect 是懒的）。"""
    driver = ProtocolDriver("测试号", {"hatch_sess": "cookie-value"})
    fake_client = client or FakeClient()
    fake_engine = engine or FakeEngine(events or [], quota=quota)
    driver._client = fake_client
    driver._engine = fake_engine
    return driver, fake_client, fake_engine


class FakeProtocolDriver:
    """假协议驱动：只钉住「被选到了」，不碰任何网络。

    `status_error` 是类级开关，用来模拟"协议连不上"（连接失败只有 ProtocolError）。
    """

    instances: list[FakeProtocolDriver] = []
    status_error: BaseException | None = None

    def __init__(self, account, cookies, *, proxy=None, model="hatch-web") -> None:
        self.account = account
        self.cookies = dict(cookies)
        self.proxy = proxy
        self.model = model
        self.closed = False
        FakeProtocolDriver.instances.append(self)

    async def status(self) -> DriverStatus:
        if FakeProtocolDriver.status_error is not None:
            raise FakeProtocolDriver.status_error
        return DriverStatus(
            connected=True,
            logged_in=True,
            url="wss://hatch.metaaivm.com/v1/noise",
            note="protocol: vm_state=RUNNING",
        )

    async def wake_vm(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class FakeBrowserDriver(MuseDriver):
    """真 `MuseDriver` 的子类：不碰 CDP，但 `isinstance(x, MuseDriver)` 仍成立。

    为什么要子类而不是另造一个假类：回退测试的断言是"最终驱动是浏览器驱动"，
    用无关的假类只能证明"换成了另一个对象"；子类让这条断言真的钉在类型上。
    """

    instances: list[FakeBrowserDriver] = []

    def __init__(self, cdp_url: str = "") -> None:
        # 刻意不调 super().__init__：下面覆写了所有会碰 playwright 的方法。
        self.cdp_url = cdp_url
        self.closed = False
        FakeBrowserDriver.instances.append(self)

    async def status(self) -> BrowserStatus:
        return BrowserStatus(connected=True, logged_in=True, url="https://muse.ai/")

    async def wake_vm(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_shared_state():
    """清掉池级锁表与假件计数：这些是模块级/类级状态，不清会跨用例串。"""
    pool._locks.clear()
    pool._cursor["index"] = 0
    FakeProtocolDriver.instances.clear()
    FakeProtocolDriver.status_error = None
    FakeBrowserDriver.instances.clear()
    yield
    pool._locks.clear()
    pool._cursor["index"] = 0


@pytest.fixture
def bridge_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())


@pytest.fixture
def store(bridge_key, tmp_path, monkeypatch):
    """真 AccountStore（含 cookie 加密往返）：选择逻辑要连 `store.cookies()` 一起钉。"""
    monkeypatch.setenv("MUSE_BRIDGE_DB", str(tmp_path / "muse.db"))
    instance = AccountStore()
    yield instance
    instance.close()


def _add(store: AccountStore, name: str, *, cookies: dict | None = None) -> None:
    profile = {AccountStore.COOKIES_KEY: cookies} if cookies else None
    store.upsert(
        name,
        cdp_url="http://127.0.0.1:9222",
        state=AccountState.READY,
        profile=profile,
        vm_ready=True,
    )


# ---------------------------------------------------------------- ProtocolDriver
@pytest.mark.asyncio
async def test_stream_events_maps_delta_to_content_and_stops_after_done():
    """`{"type":"delta","text":"你好"}` → `{"content":"你好"}`，done 之后不再往外吐。"""
    driver, _client, engine = make_driver(
        [
            {"type": "delta", "text": "你好"},
            {"type": "delta", "text": ""},  # 空增量不该造出空事件
            {"type": "done", "text": "你好"},
            {"type": "delta", "text": "不该出现"},  # done 之后的内容必须被截断
        ]
    )

    events = []
    async for event in driver.stream_reply_events("讲讲天气", timeout=7.5):
        events.append(event)

    assert events == [{"content": "你好"}]
    assert engine.stream_calls == [("讲讲天气", 7.5)]


@pytest.mark.asyncio
async def test_stream_events_refusal_raises_with_reason():
    """`refused` 必须抛异常并带上原因——静默结束会被上层当成空回答。"""
    driver, _client, _engine = make_driver(
        [{"type": "refused", "text": "额度不足，稍后再试"}]
    )

    seen: list[dict] = []
    with pytest.raises(ProtocolRefusal) as excinfo:
        async for event in driver.stream_reply_events("hi", timeout=5.0):
            seen.append(event)

    assert "额度不足" in str(excinfo.value)
    assert excinfo.value.reason == str(excinfo.value)
    assert seen == [], "拒绝不该先吐出半截正文再抛错"


@pytest.mark.asyncio
async def test_stream_reply_is_string_projection_of_events():
    """字符串接口（非流式路径在用）逐段等于事件接口的正文。"""
    driver, _client, _engine = make_driver(
        [
            {"type": "delta", "text": "你"},
            {"type": "delta", "text": "好"},
            {"type": "done", "text": "你好"},
        ]
    )

    parts = [part async for part in driver.stream_reply("hi", timeout=3.0)]
    assert parts == ["你", "好"]
    assert "".join(parts) == "你好"


@pytest.mark.asyncio
async def test_stream_events_done_closes_retained_engine_generator(monkeypatch):
    """done returns before source exhaustion; cleanup cannot depend on source GC."""
    driver, _client, engine = make_driver()
    finalized = []
    reached_after_done = []

    async def source():
        try:
            yield {"type": "delta", "text": "正文"}
            yield {"type": "done"}
            reached_after_done.append(True)
        finally:
            finalized.append(True)

    retained = source()
    calls = []

    def stream_chat(prompt, *, timeout):
        calls.append((prompt, timeout))
        return retained

    monkeypatch.setattr(engine, "stream_chat", stream_chat)
    try:
        assert [event async for event in driver.stream_reply_events("hi", timeout=4)] == [
            {"content": "正文"}
        ]
        assert finalized == [True]
        assert reached_after_done == []
        assert retained.ag_frame is None
        assert calls == [("hi", 4)]
    finally:
        await retained.aclose()


@pytest.mark.asyncio
async def test_stream_events_early_close_closes_retained_engine_generator(monkeypatch):
    driver, _client, engine = make_driver()
    finalized = []

    async def source():
        try:
            yield {"type": "delta", "text": "第一段"}
            yield {"type": "delta", "text": "后续"}
        finally:
            finalized.append(True)

    retained = source()
    monkeypatch.setattr(engine, "stream_chat", lambda *a, **kw: retained)
    stream = driver.stream_reply_events("hi")
    try:
        assert await anext(stream) == {"content": "第一段"}
        assert finalized == []
        await stream.aclose()
        assert finalized == [True]
        assert retained.ag_frame is None
    finally:
        await stream.aclose()
        await retained.aclose()


@pytest.mark.asyncio
async def test_stream_events_cancellation_closes_retained_engine_generator(monkeypatch):
    driver, _client, engine = make_driver()
    waiting = asyncio.Event()
    finalized = []

    async def source():
        try:
            waiting.set()
            await asyncio.Event().wait()
            yield {"type": "done"}
        finally:
            finalized.append(True)

    retained = source()
    monkeypatch.setattr(engine, "stream_chat", lambda *a, **kw: retained)
    stream = driver.stream_reply_events("hi")
    pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finalized == [True]
        assert retained.ag_frame is None
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await retained.aclose()


@pytest.mark.asyncio
async def test_string_projection_early_close_closes_retained_events(monkeypatch):
    """Retain the event generator separately so GC cannot mask missing aclose."""
    driver, _client, _engine = make_driver()
    finalized = []

    async def source():
        try:
            yield {"content": "第一段"}
            yield {"content": "后续"}
        finally:
            finalized.append(True)

    retained = source()
    monkeypatch.setattr(driver, "stream_reply_events", lambda *a, **kw: retained)
    stream = driver.stream_reply("hi")
    try:
        assert await anext(stream) == "第一段"
        assert finalized == []
        await stream.aclose()
        assert finalized == [True]
        assert retained.ag_frame is None
    finally:
        await stream.aclose()
        await retained.aclose()


@pytest.mark.asyncio
async def test_string_projection_cancellation_closes_retained_events(monkeypatch):
    driver, _client, _engine = make_driver()
    waiting = asyncio.Event()
    finalized = []

    async def source():
        try:
            yield {"content": "第一段"}
            waiting.set()
            await asyncio.Event().wait()
            yield {"content": "后续"}
        finally:
            finalized.append(True)

    retained = source()
    monkeypatch.setattr(driver, "stream_reply_events", lambda *a, **kw: retained)
    stream = driver.stream_reply("hi")
    pending = None
    try:
        assert await anext(stream) == "第一段"
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finalized == [True]
        assert retained.ag_frame is None
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await retained.aclose()


@pytest.mark.asyncio
async def test_collect_reply_returns_engine_text():
    driver, _client, engine = make_driver(
        [{"type": "delta", "text": "正文"}, {"type": "done"}]
    )

    text = await driver.collect_reply("hi", timeout=9.0)

    assert text == "正文"
    # 同步收集与流式共用事件路径，媒体取件不会被旧的 collect_chat 绕过。
    assert engine.stream_calls == [("hi", 9.0)]
    assert engine.collect_calls == []


@pytest.mark.asyncio
async def test_quota_does_not_invent_weekly_percentage():
    """协议侧拿不到 UI 的周用量：`weekly_used_pct` 必须如实是 None。"""
    driver, _client, engine = make_driver(quota={"vm_state": "RUNNING"})

    quota = await driver.quota()

    assert quota["found"] is True
    assert quota["weekly_used_pct"] is None, "不许编造周用量百分比"
    assert quota["weekly_reset"] == ""
    assert quota["raw"] == "protocol:/api/session"
    assert quota["source"] == "protocol"
    assert quota["vm_state"] == "RUNNING"
    assert set(quota) == {
        "raw",
        "found",
        "vm_state",
        "estimated_expiry_at",
        "weekly_reset",
        "weekly_used_pct",
        "source",
    }
    assert engine.quota_calls == 1


@pytest.mark.asyncio
async def test_status_reports_protocol_note_and_connects_once():
    driver, client, _engine = make_driver()

    first = await driver.status()
    second = await driver.status()

    assert first.connected is True
    assert first.logged_in is True
    assert first.url == FakeClient.ws_url
    assert first.note == "protocol: vm_state=RUNNING"
    assert client.connect_calls == 1, "并发派单不能重复建连"
    assert second.note == first.note


@pytest.mark.asyncio
async def test_status_does_not_swallow_protocol_error():
    """connect 失败必须冒出去（pool 据此回退浏览器），不能在本层变成 logged_in=False。"""
    driver, client, _engine = make_driver()
    client.connect_error = ProtocolAuthError("cookie 已失效")

    with pytest.raises(ProtocolError):
        await driver.status()


@pytest.mark.asyncio
async def test_wake_vm_reports_confirmed_wake_only():
    driver, client, _engine = make_driver()

    assert await driver.wake_vm() is True
    client.wake_ok = False
    assert await driver.wake_vm() is False, "没确认过唤醒就不能声称唤醒成功"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "expected"),
    [(200, True), (401, False), (500, False), (None, False)],
)
async def test_touch_session_is_a_probe_not_a_state_writer(http_status, expected):
    """401 的语义交给上层；这里只回答"探针是否成功"，且 timeout 必须按关键字传。"""
    driver, client, _engine = make_driver()
    client.response_status = http_status

    ok = await driver.touch_session()

    assert ok is expected
    assert client.requests == [("GET", "/api/session", None, 20.0)]


@pytest.mark.asyncio
async def test_close_is_idempotent():
    driver, client, _engine = make_driver()

    await driver.close()
    await driver.close()  # 第二次不该抛

    assert client.close_calls == 2


@pytest.mark.asyncio
async def test_generate_delegates_to_engine_and_returns_driver_shaped_dict(tmp_path, monkeypatch):
    """协议生成的真机链路已验收（2026-11-11）：驱动只做"调引擎 → 落盘 → 翻译"。

    这里钉住翻译的每一格，特别是 `via="protocol"`（上层一眼看出没走浏览器）
    与 `path` 指向真实存在的文件（pool 下一步会 open 它）。
    """
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    driver, client, engine = make_driver()
    payload = b"RIFF\x00\x00\x00\x00WEBPfake"
    engine.generation_result = GenerationResult(
        data=payload, mime="image/webp", kind="image", width=64, height=64
    )

    result = await driver.generate(
        "生成一只银色狐狸", expect="image", timeout=5.0, reference_paths=[]
    )

    assert engine.generation_calls == [("image", "生成一只银色狐狸", [], 5.0)]
    assert client.connect_calls == 1, "生成前必须连上（connect 幂等）"
    assert result["via"] == "protocol"
    assert result["mime"] == "image/webp"
    assert result["kind"] == "image"
    assert result["size"] == len(payload)
    assert result["ext"] == ".webp"
    assert result["w"] == 64 and result["h"] == 64
    assert os.path.dirname(result["path"]) == str(tmp_path)
    with open(result["path"], "rb") as handle:
        assert handle.read() == payload
    await driver.close()


@pytest.mark.asyncio
async def test_generate_expect_video_uses_video_engine(tmp_path, monkeypatch):
    """expect=video 必须打到 `generate_video`：模态走错会把静帧当视频交付。"""
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    driver, _client, engine = make_driver()
    engine.generation_result = GenerationResult(
        data=b"fake-mp4-bytes", mime="video/mp4", kind="video"
    )

    result = await driver.generate("生成一段视频", expect="video", timeout=6.0)

    assert engine.generation_calls[0][0] == "video"
    assert result["kind"] == "video"
    assert result["ext"] == ".mp4"
    await driver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expect", ["text", "", "IMAGE"])
async def test_generate_rejects_unknown_expect(expect):
    driver, _client, engine = make_driver()

    with pytest.raises(ProtocolGenerationError) as excinfo:
        await driver.generate("画一只猫", expect=expect, timeout=1.0)

    assert "不支持的生成类型" in str(excinfo.value)
    assert engine.generation_calls == [], "参数非法时不该调用引擎"
    await driver.close()


@pytest.mark.asyncio
async def test_generate_propagates_engine_errors_and_never_fakes_bytes(
    tmp_path, monkeypatch
):
    """引擎报错原样冒泡；即使引擎"成功"但字节为空也必须失败，不能交付空文件。"""
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    driver, _client, engine = make_driver()
    engine.generation_error = ProtocolQuotaExhausted("额度不足")

    with pytest.raises(ProtocolQuotaExhausted):
        await driver.generate("画一只猫", expect="image", timeout=1.0)
    await driver.close()

    driver2, _client2, engine2 = make_driver()
    engine2.generation_result = GenerationResult(data=b"", mime="", kind="")
    with pytest.raises(ProtocolGenerationError):
        await driver2.generate("画一只猫", expect="image", timeout=1.0)
    await driver2.close()


# ---------------------------------------------------------------- 派单选择
@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", " On "])
def test_switch_accepts_only_truthy_values(monkeypatch, raw):
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", raw)
    assert pool._protocol_driver_enabled() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "2", "enable", "  "])
def test_switch_treats_everything_else_as_off(monkeypatch, raw):
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", raw)
    assert pool._protocol_driver_enabled() is False


@pytest.mark.parametrize("raw", ["1", "0", "On", " true ", "off", "2"])
def test_pool_switch_and_env_flag_share_one_semantics(monkeypatch, raw):
    """两处判定必须一致：pool 现读环境变量，env 只是同语义的模块级快照。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", raw)
    assert pool._protocol_driver_enabled() is env.env_flag("MUSE_PROTOCOL_DRIVER", "0")


def test_capability_defaults_to_chat():
    """既有调用点不传 capability，默认值必须仍是聊天（行为不变）。"""
    default = inspect.signature(pool.dispatch).parameters["capability"].default
    assert default == "chat"


@pytest.mark.asyncio
async def test_dispatch_uses_protocol_driver_for_chat(store, monkeypatch):
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setenv("MUSE_PROTOCOL_PROXY", "http://127.0.0.1:18000")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    _add(store, "协议号", cookies={"hatch_sess": "s3cr3t"})

    result = await pool.dispatch(store)

    try:
        assert result.ok is True
        assert isinstance(result.driver, FakeProtocolDriver)
        made = FakeProtocolDriver.instances[-1]
        assert made.account == "协议号"
        assert made.cookies == {"hatch_sess": "s3cr3t"}
        assert made.proxy == "http://127.0.0.1:18000", "代理必须从环境变量透传"
        assert FakeBrowserDriver.instances == [], "有 cookie 且开关打开时不该建浏览器驱动"
        assert result.note == "", "成功走协议不该带任何回退提示"
        lock = result._lock
        assert lock is not None and lock.locked(), "锁必须交给调用方"
    finally:
        await result.release()

    assert lock is not None and not lock.locked()
    assert made.closed is True


@pytest.mark.asyncio
async def test_dispatch_without_cookies_keeps_browser(store, monkeypatch):
    """开关打开但该账号没导入 cookie：建协议客户端只会白烧一次握手，直接用浏览器。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    _add(store, "无饼干号")

    result = await pool.dispatch(store)

    try:
        assert isinstance(result.driver, MuseDriver)
        assert FakeProtocolDriver.instances == []
    finally:
        await result.release()


@pytest.mark.asyncio
async def test_generation_capability_uses_protocol_when_enabled(store, monkeypatch):
    """生成与聊天同一次开关：真机验收后，开关打开 + 有 cookie 就该走协议。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    _add(store, "生成号", cookies={"hatch_sess": "s3cr3t"})

    result = await pool.dispatch(store, capability="generation")

    try:
        assert isinstance(result.driver, FakeProtocolDriver), "生成路径应先走协议"
        assert FakeBrowserDriver.instances == [], "有 cookie 时不该同时建浏览器驱动"
    finally:
        await result.release()


@pytest.mark.asyncio
async def test_generation_browser_capability_is_always_browser(store, monkeypatch):
    """`generation-browser` 是回退专用取值：即使开着协议也必须落到浏览器。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    _add(store, "回退生成号", cookies={"hatch_sess": "s3cr3t"})

    result = await pool.dispatch(store, capability="generation-browser")

    try:
        assert isinstance(result.driver, MuseDriver)
        assert FakeProtocolDriver.instances == [], "回退路径必须避开协议驱动"
    finally:
        await result.release()


@pytest.mark.asyncio
async def test_generation_without_cookies_keeps_browser(store, monkeypatch):
    """生成同样受"没有 cookie 就没有 hatch_sess"约束：直接浏览器，不白烧握手。"""
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    _add(store, "无饼干生成号")

    result = await pool.dispatch(store, capability="generation")

    try:
        assert isinstance(result.driver, MuseDriver)
        assert FakeProtocolDriver.instances == []
    finally:
        await result.release()


def test_generation_fallback_allowed_only_for_retryable_protocol_errors():
    """回退浏览器只对"可换号重试"的协议错误开放：拒绝/额度不换链路、不重烧。"""
    protocol = ProtocolDriver("号", {"hatch_sess": "x"})  # 懒连接，构造不联网
    browser = FakeBrowserDriver()
    allowed = pool.DispatchResult(ok=True, account="号", driver=protocol)
    denied = pool.DispatchResult(ok=True, account="号", driver=browser)

    assert pool._generation_fallback_allowed(allowed, ProtocolGenerationError("超时")) is True
    assert pool._generation_fallback_allowed(allowed, ProtocolQuotaExhausted("额度不足")) is False
    assert pool._generation_fallback_allowed(denied, ProtocolGenerationError("超时")) is False
    assert pool._generation_fallback_allowed(allowed, TimeoutError("总超时")) is False


@pytest.mark.asyncio
async def test_protocol_status_error_falls_back_to_browser_and_hands_off_lock(
    store, monkeypatch, caplog
):
    """协议连不上 → 回退浏览器，回退原因进 note，锁仍被正常交接。

    回退原因还必须有**计数器 + 日志**（2026-11-11 补）：note 只活在返回值里，
    调用方拿到的是一个 504/503；没有这两样，根因（WS 被重置）在现场完全不可见。
    """
    from muse_bridge import observability

    observability.reset()
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    FakeProtocolDriver.status_error = ProtocolError("Noise WebSocket 连接失败（代理不通）")
    _add(store, "回退号", cookies={"hatch_sess": "s3cr3t"})

    with caplog.at_level("WARNING", logger="muse_bridge.pool"):
        result = await pool.dispatch(store)

    try:
        assert result.ok is True
        assert isinstance(result.driver, MuseDriver), "回退目标必须是真的浏览器驱动类型"
        assert "protocol_fallback" in result.note
        assert "ProtocolError" in result.note
        assert "Noise WebSocket 连接失败" in result.note
        assert FakeProtocolDriver.instances[-1].closed is True, "回退前必须关掉协议连接"
        assert observability.snapshot()["counters"]["protocol_fallback"] == 1
        assert "Noise WebSocket 连接失败" in caplog.text, "根因必须落一行日志"
        assert "s3cr3t" not in caplog.text, "日志不得带出 cookie 值"
        lock = result._lock
        assert lock is not None and lock.locked()
    finally:
        await result.release()
        observability.reset()

    assert lock is not None and not lock.locked()
    assert pool.busy_accounts() == []


@pytest.mark.asyncio
async def test_switch_off_keeps_browser_even_with_cookies(store, monkeypatch):
    """默认行为不变：没配开关（或配成 0）时，有 cookie 也走浏览器。"""
    monkeypatch.delenv("MUSE_PROTOCOL_DRIVER", raising=False)
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FakeBrowserDriver)
    _add(store, "默认号", cookies={"hatch_sess": "s3cr3t"})

    result = await pool.dispatch(store)

    try:
        assert isinstance(result.driver, MuseDriver)
        assert FakeProtocolDriver.instances == []
    finally:
        await result.release()


class FailingBrowserDriver(FakeBrowserDriver):
    """浏览器也连不上：用来钉住"回退之后的重探仍由既有分支处理"。"""

    async def status(self) -> BrowserStatus:
        raise BrowserUnavailable("CDP 连不上 127.0.0.1:9222")


@pytest.mark.asyncio
async def test_fallback_then_browser_unavailable_is_classified(store, monkeypatch):
    """协议连不上 + 浏览器也连不上：必须记成 browser_unavailable，且不把锁漏掉。

    这条钉的是**异常处理的嵌套顺序**：若把"回退后的重探"写成 except 处理器里的
    裸调用，它抛出的 BrowserUnavailable 不会再被同级的另一个 except 接住，
    结果既不是重试分类、也不是干净失败，而是带着锁逃出一个意外异常。
    """
    monkeypatch.setenv("MUSE_PROTOCOL_DRIVER", "1")
    monkeypatch.setattr(pool, "ProtocolDriver", FakeProtocolDriver)
    monkeypatch.setattr(pool, "MuseDriver", FailingBrowserDriver)
    FakeProtocolDriver.status_error = ProtocolError("Noise 握手失败")
    _add(store, "双失败号", cookies={"hatch_sess": "s3cr3t"})

    result = await pool.dispatch(store)

    assert result.ok is False
    assert result.verdict == "all_accounts_failed"
    assert "CDP 连不上" in result.note
    record = store.get("双失败号")
    assert record is not None
    assert record.state is AccountState.BROWSER_UNAVAILABLE
    assert "CDP 连不上" in (record.last_error or "")
    assert pool.busy_accounts() == [], "失败出口也必须把锁放掉"


class ExplodingBrowserDriver(FakeBrowserDriver):
    """构造即失败：模拟 `connect()` 里新开标签页后 goto 不通。

    真实成因（2026-11-11 用失效代理实测）：CDP 连得上，但 Chrome 里没有现成的
    muse.ai 标签页，`_pick_muse_page` 就 `new_page()` + `goto(SITE)`；出口断网时
    playwright 抛 `ERR_CONNECTION_CLOSED`。修复前那是裸 Error，冲出 dispatch
    一路冒到 ASGI，用户只拿到 `500 Internal Server Error`。
    """

    def __init__(self, cdp_url: str = "") -> None:
        raise BrowserUnavailable(
            "打开的 https://muse.ai/ 页面加载失败：Page.goto: net::ERR_CONNECTION_CLOSED"
        )


@pytest.mark.asyncio
async def test_build_driver_browser_unavailable_is_classified_not_crashed(
    store, monkeypatch
):
    """**构造期**抛 BrowserUnavailable 也必须被分类，不能变成 500。

    这条钉的是"异常发生在哪一步"：`_build_driver` 在状态探测循环之前调用，
    那里的异常以前没有任何 except 接，于是单账号的浏览器故障会升级成整个
    请求的 500。正确行为与"探状态时抛"一致：记 browser_unavailable、换下一个号。
    """
    monkeypatch.delenv("MUSE_PROTOCOL_DRIVER", raising=False)
    monkeypatch.setattr(pool, "MuseDriver", ExplodingBrowserDriver)
    _add(store, "构造失败号")

    result = await pool.dispatch(store)

    assert result.ok is False
    assert result.verdict == "all_accounts_failed"
    assert "ERR_CONNECTION_CLOSED" in result.note
    record = store.get("构造失败号")
    assert record is not None
    assert record.state is AccountState.BROWSER_UNAVAILABLE
    assert "ERR_CONNECTION_CLOSED" in (record.last_error or "")
    assert pool.busy_accounts() == [], "构造期失败也必须把锁放掉"


@pytest.mark.asyncio
async def test_build_driver_failure_falls_over_to_next_account(store, monkeypatch):
    """一个号的浏览器坏了，另一个号还能派单：必须换号成功，而不是整体失败。"""
    monkeypatch.delenv("MUSE_PROTOCOL_DRIVER", raising=False)
    _add(store, "坏号")
    _add(store, "好号")

    real = MuseDriver
    built: list[str] = []

    def factory(cdp_url: str = ""):
        # 第一个号用爆炸驱动，其余用正常假件。用计数决定，避免依赖排序。
        built.append(cdp_url)
        if len(built) == 1:
            return ExplodingBrowserDriver(cdp_url)
        return FakeBrowserDriver(cdp_url)

    monkeypatch.setattr(pool, "MuseDriver", factory)

    result = await pool.dispatch(store)
    try:
        assert result.ok is True, "坏号不该拖垮整个请求"
        assert isinstance(result.driver, FakeBrowserDriver)
        assert result.account == "好号"
    finally:
        await result.release()
    assert real is not None  # 只是证明没把真类换成别的东西


@pytest.mark.asyncio
async def test_connected_false_is_not_misread_as_session_expired(store, monkeypatch):
    """`status.connected is False` 必须记成 browser_unavailable，不能记成会话过期。

    为什么这条是**真缺口**：真实 `MuseDriver.status()` 会把 `connect()` 抛出的
    `BrowserUnavailable` **吞掉**，返回 `connected=False, logged_in=False`；
    而 `pool.dispatch` 只看 `logged_in`，于是"浏览器连不上"被记成
    `SESSION_EXPIRED`。两种状态的冷却与重试语义不同，误记会让账号按"会话过期"
    处理（要求重新登录），而真正该做的是等浏览器/网络恢复。
    """
    monkeypatch.delenv("MUSE_PROTOCOL_DRIVER", raising=False)

    class DisconnectedDriver(FakeBrowserDriver):
        async def status(self) -> BrowserStatus:
            # 复刻真实 MuseDriver.status() 的行为：不抛，返回 connected=False。
            return BrowserStatus(
                connected=False,
                logged_in=False,
                note="无法连接 CDP http://127.0.0.1:9222",
            )

    monkeypatch.setattr(pool, "MuseDriver", DisconnectedDriver)
    _add(store, "断连号")

    result = await pool.dispatch(store)

    assert result.ok is False
    record = store.get("断连号")
    assert record is not None
    assert record.state is AccountState.BROWSER_UNAVAILABLE, (
        f"应为 browser_unavailable，实际 {record.state}"
    )
    assert "CDP" in (record.last_error or "")
    assert pool.busy_accounts() == []


def test_driver_label_distinguishes_protocol_from_browser(monkeypatch):
    """响应头 `X-Muse-Driver` 的判据：协议驱动 vs 浏览器（含回退后）。

    这个头是验收 `MUSE_PROTOCOL_DRIVER=1` 是否真的生效、以及统计"协议失败
    回退浏览器"比例的唯一低摩擦信号；判错会让人误以为协议全量生效（或反之），
    排查方向直接跑偏。
    """
    from types import SimpleNamespace

    from muse_bridge import app as app_module

    class _FakeProtocol:
        pass

    monkeypatch.setattr(app_module, "ProtocolDriver", _FakeProtocol)
    assert app_module._driver_label(SimpleNamespace(driver=None)) == "browser"
    assert app_module._driver_label(SimpleNamespace(driver=object())) == "browser"
    assert app_module._driver_label(SimpleNamespace(driver=_FakeProtocol())) == "protocol"


@pytest.mark.asyncio
async def test_generate_passes_engine_metadata_through_and_never_invents_it(
    tmp_path, monkeypatch
):
    """`duration_ms` / `seed` / `source_prompt` 必须原样透传；缺失保持 None/空串。

    为什么单钉这三格：上游 app 层把它们全丢掉了，调用方要看视频时长/种子/实际
    提示词只能靠猜。驱动早期也只透 w/h，等于在最后一步再丢一次。缺失时**不伪造**
    （不补 0、不拿本次 prompt 冒充 source_prompt），否则上层会把"没读到"当真实值。
    """
    monkeypatch.setenv("MUSE_MEDIA_DIR", str(tmp_path))
    driver, _client, engine = make_driver()
    engine.generation_result = GenerationResult(
        data=b"fake-mp4-bytes",
        mime="video/mp4",
        kind="video",
        duration_ms=5_000,
        seed="seed-7",
        source_prompt="一段海浪",
    )

    result = await driver.generate("一段海浪", expect="video", timeout=5.0)
    await driver.close()

    assert result["duration_ms"] == 5_000
    assert result["seed"] == "seed-7"
    assert result["source_prompt"] == "一段海浪"

    driver2, _client2, engine2 = make_driver()
    engine2.generation_result = GenerationResult(
        data=b"fake-mp4-bytes", mime="video/mp4", kind="video"
    )
    result2 = await driver2.generate("另一段海浪", expect="video", timeout=5.0)
    await driver2.close()

    assert result2["duration_ms"] is None
    assert result2["seed"] is None
    assert result2["source_prompt"] == "", "不得拿本次 prompt 冒充引擎给的原提示词"
