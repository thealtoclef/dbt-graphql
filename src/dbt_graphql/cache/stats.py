"""Process-local hit/miss/coalesce counters for the result cache.

Exists for two reasons:
1. Tests can assert exact behavior (this hit was a real TTL hit, that one
   was a singleflight wake) without scraping OTel.
2. Operators get a quick window into cache effectiveness without a metrics
   pipeline.

Reset between tests via ``stats.reset()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Counter:
    hit: int = 0
    miss: int = 0
    # A wake from cache.lock that found a populated key on re-check.
    coalesced: int = 0


@dataclass
class CacheStats:
    result: _Counter = field(default_factory=_Counter)

    def reset(self) -> None:
        self.result = _Counter()


stats = CacheStats()
