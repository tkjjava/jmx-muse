"""参考素材超上限被截断时，必须留下计数。

这是 C3 审计里的一条"契约与可观测性不一致"：`app._references_of` 超出
`MUSE_REFERENCE_MAX`（默认 10）会**静默截断**，而 `refs.py` 那条
`MUSE_MAX_REFERENCE_FILES` 超限会**报 400**。两者的默认值都是 10，所以"传 12 个"
永远走截断、400 那条基本走不到。

本轮**不改契约**（改成报错属于对外行为变更，要拍板），只把静默变可数：
新增 `references_truncated` 计数。本文件钉住三点：

1. 未超上限：不计数，原样返回；
2. 超上限：计数 +1，返回前 `limit` 个，顺序不变（保序是调用方可依赖的行为）；
3. 上限来自 `env.REFERENCE_MAX`，读的是**配置值**而不是写死的 10。
"""

from __future__ import annotations

import pytest

from muse_bridge import app as app_mod
from muse_bridge import env, observability


class _Request:
    """只需 `_references_of` 用到的字段；用轻量替身免去构造 pydantic 模型。"""

    def __init__(self, **kw) -> None:
        self.images = kw.get("images")
        self.videos = kw.get("videos")
        self.audios = kw.get("audios")
        self.files = kw.get("files")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    observability.reset()
    yield
    observability.reset()


def counters() -> dict:
    return observability.snapshot()["counters"]


def test_under_limit_returns_as_is_and_does_not_count(monkeypatch) -> None:
    monkeypatch.setattr(env, "REFERENCE_MAX", 10)
    request = _Request(images=["a.png", "b.png"], files=["c.pdf"])
    assert app_mod._references_of(request) == ["a.png", "b.png", "c.pdf"]
    assert "references_truncated" not in counters()


def test_over_limit_truncates_keeps_order_and_counts(monkeypatch) -> None:
    monkeypatch.setattr(env, "REFERENCE_MAX", 3)
    request = _Request(images=["a", "b", "c", "d"], videos=["e"])
    refs = app_mod._references_of(request)
    assert refs == ["a", "b", "c"], "必须保序取前 limit 个"
    assert counters()["references_truncated"] == 1


def test_limit_comes_from_env_not_hardcoded(monkeypatch) -> None:
    monkeypatch.setattr(env, "REFERENCE_MAX", 1)
    assert app_mod._references_of(_Request(files=["x", "y"])) == ["x"]
    # 换成 2：同一份输入不再触发截断
    observability.reset()
    monkeypatch.setattr(env, "REFERENCE_MAX", 2)
    assert app_mod._references_of(_Request(files=["x", "y"])) == ["x", "y"]
    assert "references_truncated" not in counters()
