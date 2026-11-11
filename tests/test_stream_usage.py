"""Streaming usage reporting for muse-bridge (same gateway-billing fix as hark-bridge).

A relay bills token quota from reported usage and has no byte fallback: a stream that
never reports usage is billed as zero. OpenAI signals the want with
`stream_options: {include_usage: true}` — the stream must then append a final usage chunk,
and must not when it isn't asked (the old shape stays byte-for-byte identical).

Hits the real ASGI app with the pool faked, so the asserted frames are what a caller gets.
"""

from __future__ import annotations

import json

import httpx
import pytest

from muse_bridge import app as app_module
from muse_bridge import pool


def _sse_data(text: str) -> list[dict | str]:
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        payload = block.removeprefix("data:").strip()
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


@pytest.fixture
def patched_pool(monkeypatch):
    deltas = ["今天 ", "天气", "不错"]

    async def fake_dispatch(store):
        return pool.DispatchResult(ok=True, account="muse-main", driver=None)

    async def fake_stream(result, prompt, timeout=180.0):
        for d in deltas:
            yield d

    async def fake_collect(result, prompt, timeout=180.0):
        return "".join(deltas)

    monkeypatch.setattr(app_module, "store", lambda: None)
    monkeypatch.setattr(pool, "dispatch", fake_dispatch)
    monkeypatch.setattr(pool, "stream_reply", fake_stream)
    monkeypatch.setattr(pool, "collect_reply", fake_collect)
    return "".join(deltas)


@pytest.mark.asyncio
async def test_stream_with_include_usage_appends_usage_frame(patched_pool):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "你好"}],
                  "stream": True, "stream_options": {"include_usage": True}},
        )
        assert r.status_code == 200
    frames = _sse_data(r.text)
    assert frames[-1] == "[DONE]"
    usage = frames[-2]["usage"]
    assert frames[-2]["choices"] == []
    # The property this guards is billing, not the arithmetic: a Chinese answer must not be
    # compressed to a quarter of its length. 6 of these 7 characters are Han.
    assert usage["completion_tokens"] >= 6, usage
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_estimator_ascii_is_unchanged_quarter_rule():
    """Latin keeps the historical 4-chars-per-token, so the common path does not move."""
    assert app_module._estimate_tokens("a" * 200) == 50
    assert app_module._estimate_tokens("") == 0
    assert app_module._estimate_tokens("abc") == 0      # floor, not round up


def test_estimator_counts_han_one_per_char():
    assert app_module._estimate_tokens("中" * 200) == 200
    assert app_module._estimate_tokens("中文abc") == 2   # 2 Han + 3 latin//4 == 2
    assert app_module._estimate_tokens("。，！？") == 4   # CJK punctuation counts


def test_usage_total_is_the_sum_of_its_parts():
    # A previous version divided the concatenated length, so total could differ from
    # prompt + completion by 1 and the client could not reconcile its own numbers.
    u = app_module._usage_of("你好" * 3, "答案" * 5)
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"]


# ---- golden vectors: the agreed token contract, pinned against silent drift ----------
# Identical to hark-bridge's table on purpose: both bridges carry their own copy of this
# estimator and were shown to drift in other ways during the same session. Changing
# billing means editing this table in BOTH projects deliberately — never one by accident.
#
# The three "outside the ranges" rows are recorded as-is: U+33FF / U+A000 and emoji are
# NOT one-token-per-char today, so widening _CJK_RANGES must come back and change them —
# exactly the scrutiny a billing change deserves.
TOKEN_CONTRACT = [
    ("", 0),
    ("abc", 0),                    # latin floors, never rounds up
    ("abcd", 1),
    ("a" * 200, 50),
    ("中" * 200, 200),             # basic han: one token per char
    ("\u3400" * 10, 10),           # CJK extension A
    ("。" * 5, 5),                 # CJK punctuation
    ("あ" * 5, 5),                  # kana
    ("한" * 5, 5),                  # hangul
    ("\uF900" * 5, 5),             # compatibility ideographs
    ("\uFF21" * 5, 5),             # fullwidth forms
    ("\u33FF" * 5, 1),             # outside the ranges -> latin rule
    ("\uA000" * 5, 1),             # outside the ranges -> latin rule
    ("\U0001F600" * 4, 1),         # emoji outside the ranges
    ("     ", 1),                  # whitespace is latin density
    ("中文abc", 2),                 # 2 han + 3 latin//4
]


@pytest.mark.parametrize("text,expected", TOKEN_CONTRACT)
def test_estimator_matches_the_token_contract(text, expected):
    assert app_module._estimate_tokens(text) == expected


def test_usage_envelope_matches_the_token_contract():
    assert app_module._usage_of("你好世界" * 10, "答案在此" * 30) == {
        "prompt_tokens": 40,
        "completion_tokens": 120,
        "total_tokens": 160,
    }


@pytest.mark.asyncio
async def test_stream_without_include_usage_is_unchanged(patched_pool):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "你好"}], "stream": True},
        )
    frames = _sse_data(r.text)
    assert frames[-1] == "[DONE]"
    assert "usage" not in frames[-2]
    assert frames[-2]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_nonstream_usage_matches_the_streamed_total(patched_pool):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ns = await client.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "你好"}]},
        )
        st = await client.post(
            "/v1/chat/completions",
            json={"model": "muse-1", "messages": [{"role": "user", "content": "你好"}],
                  "stream": True, "stream_options": {"include_usage": True}},
        )
    assert ns.json()["usage"] == _sse_data(st.text)[-2]["usage"]
