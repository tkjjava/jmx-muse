"""进程内可观测性：分阶段耗时与失败计数。

## 为什么需要它

暖池与聊天媒体的失败形态都是"看起来成功了"：复用没命中不报错、只是变慢；
附件取件失败被上层包成一条 502，从总耗时里根本看不出是卡在握手、生成、取件
还是下载。**没有一个能分开计数的东西，排障就只剩猜。**

## 边界（不要把它当监控系统）

* 纯进程内，进程重启归零——这不是持久化指标库，也没有导出协议。
  要接 Prometheus 一类的东西，应该另外做，而不是把这个 dict 硬塞成 exporter。
* 只记**计数与耗时**，不记 cookie、Bearer key、提示词正文或上游原始报文。
* 线程安全（`threading.Lock`）：FastAPI 的同步路径与工作线程都会写。
* 供 `/admin/observability` 读取，走 `require_admin`；不放进 `/healthz` 的公开面，
  因为公开面不该暴露内部失败率。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}
_TIMING_TOTAL: dict[str, float] = {}
_TIMING_COUNT: dict[str, int] = {}
_TIMING_MAX: dict[str, float] = {}

# 已登记的阶段名：拼写错误会在这里被发现，而不是悄悄多出一个没人读的键。
STAGES = (
    "warm_acquire",
    "chat_media_fetch",
    "chat_media_store",
    "media_download",
    "chat_request",
)


def bump(name: str, amount: int = 1) -> None:
    """计数 +amount。名称自由，用于失败/命中一类离散事件。"""
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + amount


def observe(stage: str, seconds: float) -> None:
    """记录一次阶段耗时（秒）。"""
    with _LOCK:
        _TIMING_COUNT[stage] = _TIMING_COUNT.get(stage, 0) + 1
        _TIMING_TOTAL[stage] = _TIMING_TOTAL.get(stage, 0.0) + seconds
        _TIMING_MAX[stage] = max(_TIMING_MAX.get(stage, 0.0), seconds)


@contextmanager
def timed(stage: str) -> Iterator[None]:
    """`with timed("chat_media_fetch"): ...` —— 异常也照样记，失败同样耗时。"""
    started = time.monotonic()
    try:
        yield
    finally:
        observe(stage, time.monotonic() - started)


def snapshot() -> dict[str, object]:
    """当前读数。计数用不到 1 秒的耗时，所以耗时统一给到毫秒。"""
    with _LOCK:
        counters = dict(_COUNTERS)
        averages = {
            stage: round(_TIMING_TOTAL[stage] / count, 6)
            for stage, count in _TIMING_COUNT.items()
            if count
        }
        maximum = {stage: round(value, 6) for stage, value in _TIMING_MAX.items()}
        counts = dict(_TIMING_COUNT)
    return {
        "counters": counters,
        "timing": {
            "count": counts,
            "average_seconds": averages,
            "max_seconds": maximum,
        },
    }


def reset() -> None:
    """清空读数。给测试与"重启前先归档"的运维手工调用，不自动调用。"""
    with _LOCK:
        _COUNTERS.clear()
        _TIMING_TOTAL.clear()
        _TIMING_COUNT.clear()
        _TIMING_MAX.clear()


__all__ = ["STAGES", "bump", "observe", "reset", "snapshot", "timed"]
