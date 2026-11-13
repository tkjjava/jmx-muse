"""Bounded process-local scheduling feedback; no credentials or request text.

Independent capability buckets prevent long video jobs from skewing chat latency.
Monotonic expiry gives recovered accounts a fresh start after 30 idle minutes.
Only completed operations produce success/failure samples; cancellation is neutral.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class HealthSample:
    error_rate: float = 0.0
    ttft_ms: float = 0.0
    has_ttft: bool = False
    samples: int = 0
    updated_at: float = 0.0


class HealthTracker:
    def __init__(self, *, capacity: int = 1024, ttl: float = 1800.0, alpha: float = 0.3):
        if capacity < 1 or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("capacity and ttl must be positive")
        if not math.isfinite(alpha) or not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self.capacity = capacity
        self.ttl = ttl
        self.alpha = alpha
        self._items: OrderedDict[tuple[str, str], HealthSample] = OrderedDict()

    def clear(self) -> None:
        self._items.clear()

    def forget(self, account: str) -> None:
        for key in list(self._items):
            if key[0] == account:
                del self._items[key]

    def snapshot(self, account: str, capability: str = "chat") -> HealthSample:
        key = (account, capability)
        item = self._items.get(key)
        if item is None:
            return HealthSample()
        if time.monotonic() - item.updated_at >= self.ttl:
            del self._items[key]
            return HealthSample()
        return HealthSample(**vars(item))

    def report(self, account: str, *, success: bool, capability: str = "chat",
               ttft_ms: float | None = None) -> None:
        item = self.snapshot(account, capability)
        target = 0.0 if success else 1.0
        item.error_rate = self.alpha * target + (1 - self.alpha) * item.error_rate
        if ttft_ms is not None and math.isfinite(ttft_ms) and ttft_ms >= 0:
            item.ttft_ms = (self.alpha * ttft_ms + (1 - self.alpha) * item.ttft_ms
                            if item.has_ttft else ttft_ms)
            item.has_ttft = True
        item.samples += 1
        item.updated_at = time.monotonic()
        key = (account, capability)
        self._items[key] = item
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
