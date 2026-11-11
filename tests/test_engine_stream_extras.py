"""`ProtocolEngine` 给 `read_stream` 传可选关键字时的签名探测契约。

这条契约的意义：`self.client` 是鸭子契约，真实现接受 `idle_timeout`，离线/第三方
假件往往不接受。探测写错的后果不是"少了优化"，而是"打开开关后所有生成都
TypeError"——所以它必须有独立测试，而不是只靠真机试。
"""

from __future__ import annotations

from muse_bridge import env
from muse_bridge.protocol_engine import (
    ProtocolEngine,
    accepts_kwarg,
    stream_idle_timeout,
)
from tests.test_protocol_engine import FakeEngineClient, make_engine


def test_accepts_kwarg_true_for_explicit_parameter():
    def fn(*, idle_timeout=None):
        return idle_timeout

    assert accepts_kwarg(fn, "idle_timeout") is True


def test_accepts_kwarg_true_for_var_keyword():
    def fn(**kwargs):
        return kwargs

    assert accepts_kwarg(fn, "anything") is True


def test_accepts_kwarg_false_for_narrow_signature():
    def fn(*, node_id=None, timeout=None):
        return node_id, timeout

    assert accepts_kwarg(fn, "idle_timeout") is False


def test_accepts_kwarg_false_for_non_callable():
    assert accepts_kwarg(None, "idle_timeout") is False
    assert accepts_kwarg(42, "idle_timeout") is False


def test_stream_idle_timeout_zero_means_disabled(monkeypatch):
    monkeypatch.setattr(env, "PROTOCOL_STREAM_IDLE_TIMEOUT", 0.0)
    assert stream_idle_timeout() is None


def test_stream_idle_timeout_negative_means_disabled(monkeypatch):
    monkeypatch.setattr(env, "PROTOCOL_STREAM_IDLE_TIMEOUT", -5.0)
    assert stream_idle_timeout() is None


def test_stream_idle_timeout_positive_passes_through(monkeypatch):
    monkeypatch.setattr(env, "PROTOCOL_STREAM_IDLE_TIMEOUT", 45.0)
    assert stream_idle_timeout() == 45.0


def test_stream_extras_empty_when_switch_off(monkeypatch):
    monkeypatch.setattr(env, "PROTOCOL_STREAM_IDLE_TIMEOUT", 0.0)
    engine, _clock = make_engine(FakeEngineClient())
    assert engine._stream_extras() == {}


def test_stream_extras_skips_narrow_client(monkeypatch):
    """开着开关但客户端签名窄：不传，不能让调用炸掉。"""
    monkeypatch.setattr(env, "PROTOCOL_STREAM_IDLE_TIMEOUT", 30.0)
    engine, _clock = make_engine(FakeEngineClient())
    assert accepts_kwarg(engine.client.read_stream, "idle_timeout") is False
    assert engine._stream_extras() == {}


def test_stream_extras_passes_when_supported(monkeypatch):
    monkeypatch.setattr(env, "PROTOCOL_STREAM_IDLE_TIMEOUT", 30.0)

    class WideClient(FakeEngineClient):
        def read_stream(self, prompt=None, references=None, *, node_id=None, timeout=None, idle_timeout=None):
            return self._empty()

    engine = ProtocolEngine(WideClient())
    assert engine._stream_extras() == {"idle_timeout": 30.0}
