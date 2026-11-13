r"""账号选号评分器 —— 纯函数，不碰网络、不碰数据库、不碰全局状态。

为什么要换掉取模轮转
--------------------
`pool.py` 现在的 `_rotate()` 是取模轮转：不看账号死活，坏号也照发，账号越多越糟。
参考 sub2api（LGPL-3.0）的调度**设计**（不是它的代码），这里实现同一套思路的最小可用版：

  1. 每个候选算一个 score（多因子加权，全部归一化到「本批候选内相对比较」）；
  2. 取 TopK，在 TopK 内按权重加权随机打散，避免单一最高分账号长期垄断；
  3. TTFT 没有样品时给中性值 0.5 —— 给 0 会把新号误判成最差，给 1 会误判成最好。

设计约束（都是刻意的）
----------------------
* **纯函数**：`rank()` 只吃一个候选列表 + 配置，吐一个有序列表。没有 I/O，
  所以能在 CI 里用固定输入对拍，也不会把凭据带进测试。
* **相对归一化**：所有 (x-min)/(max-min)。账号从 1 个涨到 100 个都不用调参；
  全相等时（max==min）走中性分支，不会除零。
* **权重可关**：任何一项权重设 0 就完全不影响结果，方便逐项灰度。
* **可注入随机源**：默认用 `random.Random(seed)`，测试传固定 seed 得到稳定顺序。

为什么不用「纯贪心取最高分」
----------------------------
贪心会把所有请求压到同一台，它一抖全线受影响。加权随机保证高分账号**大概率**被选，
但不是永远被选——这是可用性换来的少量「次优」，值得。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# 默认权重。error_rate 给最高（2.0）：一个账号开始报错，比它慢一点要严重得多。
DEFAULT_WEIGHTS: dict[str, float] = {
    "priority": 1.0,
    "load": 1.0,
    "queue": 0.5,
    "error_rate": 2.0,
    "ttft": 1.0,
    "reset": 0.5,
    "quota": 1.0,
    "sticky_bonus": 1.5,
}

# TTFT 无样品时的中性因子。不取 0（会被当成最差）也不取 1（会被当成最好）。
NEUTRAL_TTFT_FACTOR = 0.5


def clamp01(value: float) -> float:
    """夹到 [0,1]；NaN 归 0。

    必须挡 NaN：EWMA 里一旦混进 NaN，后面的比较全部失效（NaN 与任何数比较都是
    False），排序会退化成原顺序且不报错——这种静默失效最难查。
    """
    if value is None or math.isnan(value):
        return 0.0
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return float(value)


def norm(value: float, low: float, high: float) -> float:
    """把 value 线性映射到 [0,1]，再夹紧。

    high == low 时无法归一化，返回 0.0（调用方通常再取 `1 - norm(...)`）。
    """
    if high <= low:
        return 0.0
    return clamp01((value - low) / (high - low))


@dataclass
class Candidate:
    """一条选号候选。字段都是「当前时刻的快照」，由调用方从 store/state 读出。"""

    name: str
    priority: int = 0
    load_rate: float = 0.0          # 0..1，当前在跑请求数/账号并发上限
    waiting: int = 0                # 排队数
    error_rate: float = 0.0         # EWMA 错误率 0..1
    ttft_ms: float = 0.0            # EWMA 首字节毫秒
    has_ttft: bool = False          # 有没有样品。无样品走中性因子
    reset_remaining: float | None = None   # 配额窗口剩余秒数；None = 无窗口
    quota_headroom: float = 1.0     # 剩余额度比例 0..1
    sticky: bool = False            # 命中了粘性绑定

    def is_healthy(self, max_error_rate: float) -> bool:
        return self.error_rate <= max_error_rate


@dataclass
class ScoreContext:
    """一批候选共用的归一化基准。由 `rank()` 自己算，不在外部预置。"""

    min_priority: int = 0
    max_priority: int = 0
    max_waiting: int = 0
    min_ttft: float = 0.0
    max_ttft: float = 0.0
    has_ttft_sample: bool = False
    min_reset: float = 0.0
    max_reset: float = 0.0
    has_reset_sample: bool = False


def build_context(candidates: list[Candidate]) -> ScoreContext:
    """扫一遍算归一化基准。空列表返回默认值，调用方拿到空 context 也不会崩。"""
    ctx = ScoreContext()
    if not candidates:
        return ctx
    prios = [c.priority for c in candidates]
    ctx.min_priority, ctx.max_priority = min(prios), max(prios)
    ctx.max_waiting = max((c.waiting for c in candidates), default=0)

    ttfts = [c.ttft_ms for c in candidates if c.has_ttft]
    if ttfts:
        ctx.min_ttft, ctx.max_ttft = min(ttfts), max(ttfts)
        ctx.has_ttft_sample = True

    resets = [c.reset_remaining for c in candidates if c.reset_remaining is not None]
    if resets:
        ctx.min_reset, ctx.max_reset = min(resets), max(resets)
        ctx.has_reset_sample = True
    return ctx


def score_of(c: Candidate, ctx: ScoreContext, weights: dict[str, float] | None = None) -> float:
    """单条候选的分数。权重为 0 的项跳过，保证「关掉某一项」时结果与不写这一项一致。"""
    w = weights or DEFAULT_WEIGHTS

    total = 0.0

    # 优先级：数值越小越优先（沿用 sub2api 的语义：Priority 小者先选）
    if w.get("priority", 0.0):
        f = 1.0 - norm(c.priority, ctx.min_priority, ctx.max_priority)
        total += w["priority"] * f

    if w.get("load", 0.0):
        total += w["load"] * (1.0 - clamp01(c.load_rate))

    if w.get("queue", 0.0):
        denominator = max(1, ctx.max_waiting)
        total += w["queue"] * (1.0 - clamp01(c.waiting / denominator))

    if w.get("error_rate", 0.0):
        total += w["error_rate"] * (1.0 - clamp01(c.error_rate))

    if w.get("ttft", 0.0):
        if c.has_ttft and ctx.has_ttft_sample:
            f = 1.0 - norm(c.ttft_ms, ctx.min_ttft, ctx.max_ttft)
        else:
            f = NEUTRAL_TTFT_FACTOR
        total += w["ttft"] * f

    if w.get("reset", 0.0):
        f = 0.0
        if c.reset_remaining is not None and ctx.has_reset_sample:
            if ctx.max_reset > ctx.min_reset:
                f = 1.0 - norm(c.reset_remaining, ctx.min_reset, ctx.max_reset)
            else:
                # 所有有窗口的账号剩余时间相同：一律满分，让它们优于无窗口账号。
                f = 1.0
        total += w["reset"] * f

    if w.get("quota", 0.0):
        total += w["quota"] * clamp01(c.quota_headroom)

    if w.get("sticky_bonus", 0.0) and c.sticky:
        total += w["sticky_bonus"]

    return total


def _weighted_shuffle(
    ranked: list[tuple[float, Candidate]], rng: random.Random
) -> list[tuple[float, Candidate]]:
    """按 (score - min_score + 1) 作权重轮盘抽取，抽出即移除。

    +1.0 是把分数平移进正区间：不做这一步，分数全为负时权重计算会失去意义，
    而分数全为正时最低分账号又会被完全饿死。
    """
    pool = list(ranked)
    if len(pool) <= 1:
        return pool
    min_score = min(s for s, _ in pool)
    weights = []
    for s, _ in pool:
        w = (s - min_score) + 1.0
        if math.isnan(w) or math.isinf(w) or w <= 0:
            w = 1.0
        weights.append(w)

    out: list[tuple[float, Candidate]] = []
    while pool:
        total = sum(weights)
        if total <= 0:
            idx = rng.randrange(len(pool))
        else:
            pick = rng.random() * total
            acc = 0.0
            idx = len(pool) - 1
            for i, wgt in enumerate(weights):
                acc += wgt
                if pick <= acc:
                    idx = i
                    break
        out.append(pool.pop(idx))
        weights.pop(idx)
    return out


def rank(
    candidates: list[Candidate],
    *,
    weights: dict[str, float] | None = None,
    top_k: int = 3,
    max_error_rate: float = 0.9,
    rng: random.Random | None = None,
) -> list[Candidate]:
    """选号主入口：过滤 → 评分 → TopK → 加权随机。

    `max_error_rate`：错误率超过这个值的账号**直接排除**，不参与评分。
    这是硬闸门，不是软惩罚——一个已经 90% 失败的号，给多低的分都不该再被派到。

    返回有序候选列表。空列表表示无号可派，调用方按现有 `NoAccountAvailable` 处理。
    """
    usable = [c for c in candidates if c.is_healthy(max_error_rate)]
    if not usable:
        return []
    if len(usable) == 1:
        return usable[:]

    ctx = build_context(usable)
    scored = [(score_of(c, ctx, weights), c) for c in usable]

    k = max(1, min(int(top_k), len(scored)))
    # 先按分数全序排，取前 k 作为打散池。
    scored.sort(key=lambda item: (-item[0], item[1].priority, item[1].name))
    pool = scored[:k]

    generator = rng or random.Random()
    ordered = _weighted_shuffle(pool, generator)
    # TopK 之外的账号按分数顺序接在后面：万一 TopK 全失败，还能继续试。
    tail = scored[k:]
    return [c for _, c in ordered] + [c for _, c in tail]


def explain(candidates: list[Candidate], ctx: ScoreContext | None = None) -> list[str]:
    """把每个候选的分数摊开成人读的行。doctor / 诊断端点用，不参与选号。

    存在的理由：调度出问题时要能回答「为什么选了这个号」，只给一个最终分数
    等于什么都没说。
    """
    if not candidates:
        return []
    ctx = ctx or build_context(candidates)
    rows = []
    for c in candidates:
        s = score_of(c, ctx)
        rows.append(
            f"{c.name}: score={s:.3f} "
            f"prio={c.priority} load={c.load_rate:.2f} queue={c.waiting} "
            f"err={c.error_rate:.2f} "
            f"ttft={(format(c.ttft_ms, '.0f') + 'ms') if c.has_ttft else 'n/a'} "
            f"quota={c.quota_headroom:.2f}"
            f"{' sticky' if c.sticky else ''}"
        )
    return rows
