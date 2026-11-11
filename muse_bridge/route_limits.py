"""按路由的请求预算：超时 + 响应体上限。

数据来源：`work/muse2api_harvest/muse-cli/src/muse_cli/routes.json`（402 条路由，
其中 24 条带 `requestTimeoutMs`、18 条带 `maxResponseBytes`）。那份表是客户端从
服务端路由注册表抄出来的**逐路由预算**，不是猜的；本模块只抄我们真的会调用的项，
其余落默认域。

为什么需要它（`docs/DEEP_DIVE_REJECTED.md` §5）：

1. 现在 `HatchProtocolClient.request()` 固定 120 s。`/api/dyi/export` 这类长任务
   真实预算 65 min，被 120 s 掐断是**假失败**；反过来 `/chat/subscribe` 只该等 30 s。
2. 现在没有任何响应体上限：`_receiver` 会把 body 一路 `extend` 进内存。上游回一个
   超大文件（`email.attachment.get` 上限 36 MiB）就能把进程内存顶起来。上限要在
   **追加时**检查，不能读完再判断。

只对 `end_body=True` 的一次性请求生效：订阅流（`/chat/subscribe`、`/chat/stream`）
的 body 是设计上持续增长的，不能套上限。
"""

from __future__ import annotations

from dataclasses import dataclass

# 默认域：没有被路由表点名的路径走这里。
# 32 MiB 是"防炸"线而不是"业务上限"——我们实际用到的最大的单次响应是视频成品
# （实测 6.8 MB），留足余量；真的遇到更大的合法响应，再按路由显式放宽。
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RouteLimits:
    """一条路由的预算。`max_bytes=None` 表示这条路径不收响应体。"""

    timeout: float = DEFAULT_TIMEOUT
    max_bytes: int | None = DEFAULT_MAX_BYTES


# 逐路由表：键是 (verb, path)，path 可以是前缀（以 "/*" 结尾）。
# 只收我们真的会调的路径；`requestTimeoutMs` 原值换算成秒，取整不留小数。
ROUTE_LIMITS: dict[tuple[str, str], RouteLimits] = {
    # 订阅与短控制面
    ("POST", "/chat/subscribe"): RouteLimits(timeout=30.0),
    ("POST", "/chat/cancel"): RouteLimits(timeout=30.0),
    ("POST", "/client/register-capabilities"): RouteLimits(timeout=30.0),
    # 会话/工作区
    ("POST", "/api/session"): RouteLimits(timeout=45.0),
    ("POST", "/api/hatch/vm/wake"): RouteLimits(timeout=60.0),
    ("POST", "/api/hatch/stateful-sharing"): RouteLimits(timeout=30.0),
    # 媒体：读文件与缩略图各有独立预算（缩略图上游只给 600 KiB）
    ("GET", "/fs/read"): RouteLimits(timeout=120.0, max_bytes=32 * 1024 * 1024),
    # 长任务：65 min 的导出预算（原值 3,900,000 ms）
    ("POST", "/api/dyi/export"): RouteLimits(
        timeout=3900.0, max_bytes=32 * 1024 * 1024
    ),
}

# 前缀规则单独列一份，避免在字典里做"最长前缀匹配"这种容易写错的逻辑。
# 顺序即优先级，先命中先用。
PREFIX_RULES: tuple[tuple[str, str, RouteLimits], ...] = (
    # 缩略图：上游 `maxResponseBytes` 只给 600 KiB，超了就是异常响应
    ("GET", "/fs/thumbnail/", RouteLimits(timeout=30.0, max_bytes=614_400)),
    # 灵感卡媒体：上游 404 与真图都可能，单张不可能超过 8 MiB
    ("GET", "/api/idea-cards/media/", RouteLimits(timeout=60.0, max_bytes=8 * 1024 * 1024)),
    # Spaces 导出：5.3 MB 的静态 HTML（实测），留 32 MiB
    ("GET", "/spaces/", RouteLimits(timeout=180.0, max_bytes=32 * 1024 * 1024)),
    ("GET", "/peer-spaces/", RouteLimits(timeout=120.0, max_bytes=8 * 1024 * 1024)),
)


def limits_for(verb: str, path: str) -> RouteLimits:
    """按 (verb, path) 查预算；未收录的路径返回默认域。

    路径里的查询串先切掉：`/fs/read?x=1` 与 `/fs/read` 是同一条路由。
    """
    clean = (path or "").split("?", 1)[0]
    key = ((verb or "").upper(), clean)
    hit = ROUTE_LIMITS.get(key)
    if hit is not None:
        return hit
    for rule_verb, prefix, limits in PREFIX_RULES:
        if key[0] == rule_verb and clean.startswith(prefix):
            return limits
    return RouteLimits()
