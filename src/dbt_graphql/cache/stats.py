"""Process-local hit/miss/coalesce counters per layer.

Exists for two reasons:
1. Tests can assert exact behavior (this hit was a real TTL hit, that one
   was a singleflight wake) without scraping OTel.
2. Operators get a quick window into cache effectiveness without a metrics
   pipeline. Wired into ``/metrics`` by the API layer when present.

Reset between tests via ``stats.reset()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Counter:
    hit: int = 0
    miss: int = 0
    # L3-only: a wake from cache.lock that found a populated key on re-check.
    coalesced: int = 0


@dataclass
class CacheStats:
    parsed_doc: _Counter = field(default_factory=_Counter)
    compiled_plan: _Counter = field(default_factory=_Counter)
    result: _Counter = field(default_factory=_Counter)

    def reset(self) -> None:
        self.parsed_doc = _Counter()
        self.compiled_plan = _Counter()
        self.result = _Counter()


stats = CacheStats()
