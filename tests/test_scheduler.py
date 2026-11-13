"""选号评分器测试：全部离线、固定随机种子、不碰网络与数据库。

覆盖的是「会静默错」的地方，不是覆盖率数字：
  * 排序对不对（健康号必须排在坏号前）；
  * 硬闸门（错误率超阈值的号一个都不许进结果）；
  * 归一化边界（单账号、全相等、无 TTFT 样品、max==min）不能除零也不能抛；
  * 加权随机真的会打散（同分不会永远同一个号）；
  * 权重关掉某一项后，结果与不写该项一致；
  * NaN 输入不能污染排序。
"""

from __future__ import annotations

import random

import pytest

from muse_bridge.scheduler import (
    DEFAULT_WEIGHTS,
    NEUTRAL_TTFT_FACTOR,
    Candidate,
    build_context,
    clamp01,
    explain,
    norm,
    rank,
    score_of,
)


def only(*keys: str) -> dict[str, float]:
    """只保留指定权重项、其余置 0。测试用：字典推导式里不能混 ** 展开，所以单独写。"""
    return {k: (DEFAULT_WEIGHTS[k] if k in keys else 0.0) for k in DEFAULT_WEIGHTS}


def _c(name: str, **kw) -> Candidate:
    return Candidate(name=name, **kw)


# ---------------------------------------------------------------- 基础函数


def test_clamp01_边界与_nan():
    assert clamp01(None) == 0.0
    assert clamp01(float("nan")) == 0.0
    assert clamp01(-5) == 0.0
    assert clamp01(0.25) == 0.25
    assert clamp01(5) == 1.0


def test_norm_相等区间不除零():
    assert norm(5, 5, 5) == 0.0
    assert norm(0, 10, 5) == 0.0          # high <= low
    assert norm(5, 0, 10) == 0.5
    assert norm(20, 0, 10) == 1.0         # 越界夹紧


# ---------------------------------------------------------------- 评分


def test_健康号分数高于坏号():
    good = _c("good", error_rate=0.0, load_rate=0.1, ttft_ms=300, has_ttft=True)
    bad = _c("bad", error_rate=0.8, load_rate=0.9, ttft_ms=3000, has_ttft=True)
    ctx = build_context([good, bad])
    assert score_of(good, ctx) > score_of(bad, ctx)


def test_无ttft样品取中性值而不是最高或最低():
    """新账号没有 TTFT 样品。给 1.0 会让所有新号被瞬间打满，给 0 会让新号永远选不上。"""
    ctx = build_context([
        _c("fast", ttft_ms=100, has_ttft=True),
        _c("slow", ttft_ms=5000, has_ttft=True),
        _c("new", has_ttft=False),
    ])
    solo_weights = only("ttft")
    new_score = score_of(_c("new", has_ttft=False), ctx, solo_weights)
    fast_score = score_of(_c("fast", ttft_ms=100, has_ttft=True), ctx, solo_weights)
    slow_score = score_of(_c("slow", ttft_ms=5000, has_ttft=True), ctx, solo_weights)
    assert slow_score < new_score < fast_score
    assert new_score == pytest.approx(NEUTRAL_TTFT_FACTOR * DEFAULT_WEIGHTS["ttft"])


def test_优先级数值小者优先():
    ctx = build_context([_c("a", priority=0), _c("b", priority=10)])
    solo = only("priority")
    assert score_of(_c("a", priority=0), ctx, solo) > score_of(_c("b", priority=10), ctx, solo)


def test_配额余量直接进分数():
    ctx = build_context([_c("a", quota_headroom=1.0), _c("b", quota_headroom=0.0)])
    solo = only("quota")
    assert score_of(_c("a", quota_headroom=1.0), ctx, solo) > score_of(
        _c("b", quota_headroom=0.0), ctx, solo
    )


def test_粘性加分只在命中时生效():
    ctx = build_context([_c("a"), _c("b")])
    solo = only("sticky_bonus")
    assert score_of(_c("a", sticky=True), ctx, solo) > score_of(_c("a", sticky=False), ctx, solo)


def test_权重为0的项完全不影响结果():
    """关掉某项必须等于「不写这一项」，否则灰度时无法判断差异来源。"""
    ctx = build_context([_c("a", error_rate=0.9), _c("b", error_rate=0.0)])
    only_error = only("error_rate")
    no_weights = {k: 0.0 for k in DEFAULT_WEIGHTS}
    assert score_of(_c("a", error_rate=0.9), ctx, only_error) < score_of(
        _c("b", error_rate=0.0), ctx, only_error
    )
    assert score_of(_c("a", error_rate=0.9), ctx, no_weights) == 0.0


# ---------------------------------------------------------------- 排序主入口


def test_rank_把健康号排在坏号前面():
    pool = [
        _c("sick", error_rate=0.6, load_rate=0.95, ttft_ms=9000, has_ttft=True),
        _c("good", error_rate=0.0, load_rate=0.05, ttft_ms=200, has_ttft=True),
        _c("mid", error_rate=0.2, load_rate=0.4, ttft_ms=1200, has_ttft=True),
    ]
    out = rank(pool, top_k=2, max_error_rate=0.9, rng=random.Random(1234))
    # 加权随机会打散 TopK，不保证第一位是最高分；但高分号必须稳定进 TopK 池，
    # 最差的必须被甩到 TopK 之外（它在 tail 里，作为后备仍保留）。
    assert "good" in {c.name for c in out[:2]}
    assert out[-1].name == "sick"
    assert len(out) == 3


def test_rank_硬闸门排除高错误率账号():
    pool = [
        _c("healthy", error_rate=0.1),
        _c("dead", error_rate=0.95),
    ]
    out = rank(pool, top_k=3, max_error_rate=0.9)
    assert [c.name for c in out] == ["healthy"]


def test_rank_全部不合格时返回空():
    assert rank([_c("a", error_rate=0.99)], max_error_rate=0.5) == []
    assert rank([], max_error_rate=0.5) == []


def test_rank_单账号直接返回():
    out = rank([_c("only")], top_k=3)
    assert len(out) == 1 and out[0].name == "only"


def test_rank_加权随机真的会打散():
    """三个同分账号，固定种子序列下不能永远是第一个。"""
    pool = [_c("a"), _c("b"), _c("c")]
    firsts = {rank(pool, top_k=3, rng=random.Random(seed))[0].name for seed in range(40)}
    assert len(firsts) >= 2, f"加权随机失效，40 次只出现过 {firsts}"


def test_rank_同种子结果稳定():
    pool = [_c("a", load_rate=0.1), _c("b", load_rate=0.5), _c("c", load_rate=0.9)]
    a = [c.name for c in rank(pool, top_k=3, rng=random.Random(7))]
    b = [c.name for c in rank(pool, top_k=3, rng=random.Random(7))]
    assert a == b


def test_rank_topk之外的账号接在后面():
    """TopK 只影响打散范围，不能把其余账号丢掉——否则 TopK 全挂就没有后备。"""
    pool = [_c(f"a{i}", load_rate=i / 10) for i in range(6)]
    out = rank(pool, top_k=2, rng=random.Random(3))
    assert len(out) == 6
    assert {c.name for c in out} == {f"a{i}" for i in range(6)}


def test_rank_nan错误率账号被排除而不是污染排序():
    """NaN 与任何值比较都是 False，会静默毁掉排序。这里让它被 is_healthy 挡掉：
    错误率算不出来，就当作可疑号，宁可少一个候选也不让排序静默失效。"""
    pool = [_c("nan", error_rate=float("nan")), _c("ok")]
    out = rank(pool, top_k=2, rng=random.Random(1))
    assert [c.name for c in out] == ["ok"]


# ---------------------------------------------------------------- 诊断


def test_explain_每行都带账号名():
    pool = [_c("a", ttft_ms=100, has_ttft=True), _c("b")]
    rows = explain(pool)
    assert len(rows) == 2
    assert rows[0].startswith("a: score=")
    assert "ttft=100ms" in rows[0]
    assert "ttft=n/a" in rows[1]


def test_explain_空列表不炸():
    assert explain([]) == []


def test_全相等候选不抛异常():
    """max==min 是归一化的经典除零点，必须走中性分支。"""
    pool = [_c("a", load_rate=0.5, ttft_ms=500, has_ttft=True),
            _c("b", load_rate=0.5, ttft_ms=500, has_ttft=True)]
    ctx = build_context(pool)
    assert ctx.max_ttft == ctx.min_ttft
    out = rank(pool, top_k=2, rng=random.Random(9))
    assert len(out) == 2
