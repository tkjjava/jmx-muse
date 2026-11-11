"""截断语义：话没说完不能报 finish_reason=stop。"""

from __future__ import annotations

from muse_bridge import pool


class _TruncatingResult:
    ok = True
    account = "trunc"
    driver = None
    _lock = None
    verdict = ""
    note = ""
    released = False

    async def release(self):
        self.released = True


async def _truncated_events(result, prompt, **kwargs):
    yield {"content": "前半段"}
    raise pool.ReplyTimeout(account="trunc")


def _client(monkeypatch):
    """在 monkeypatch 生效之后再建客户端：TestClient 的生命周期不跨 fixture 边界。"""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from muse_bridge import app as app_module

    if not __import__("os").environ.get("MUSE_BRIDGE_KEY") and not __import__("os").environ.get(
        "HARK_BRIDGE_KEY"
    ):
        monkeypatch.setenv("MUSE_BRIDGE_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(app_module, "require_key", lambda: None)

    async def _dispatch(*args, **kwargs):
        return _TruncatingResult()

    monkeypatch.setattr(app_module.pool, "dispatch", _dispatch)
    monkeypatch.setattr(pool, "stream_reply_events", _truncated_events)
    return TestClient(app_module.app)


def test_truncated_stream_reports_length_not_stop(monkeypatch):
    client = _client(monkeypatch)
    body = {
        "model": "m",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    with client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert response.status_code == 200, response.read()
        raw = "".join(response.iter_text())
    assert '"finish_reason": "length"' in raw, raw
    assert "前半段" in raw, "截断前已产出的内容必须照常交付"
