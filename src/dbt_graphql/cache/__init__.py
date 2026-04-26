"""Result cache + singleflight, sitting between the resolver and the warehouse.

The public API is intentionally tiny:

- ``setup_cache(cfg)`` / ``close_cache()``  — lifespan hooks
- ``execute_with_cache(...)``               — the resolver-side wrapper
- ``CacheStats`` / ``stats``                — observability counters

Parse-cache (L1) and compiled-plan-cache (L2) were considered and rejected:
parse is ~µs per request and compile is ~ms — both are dwarfed by the
warehouse roundtrip (and the cross-tenant correctness story for L2 was
fragile). Only the result cache earns its keep.
"""

from __future__ import annotations

from .config import CacheBackendConfig, CacheConfig, L3Config
from .setup import close_cache, setup_cache
from .stats import CacheStats, stats

__all__ = [
    "CacheBackendConfig",
    "CacheConfig",
    "CacheStats",
    "L3Config",
    "close_cache",
    "setup_cache",
    "stats",
]
