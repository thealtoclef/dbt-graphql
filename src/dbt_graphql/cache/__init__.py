"""Result cache + singleflight, sitting between the resolver and the warehouse.

The public API is intentionally tiny:

- ``setup_cache(cfg)`` / ``close_cache()``  — lifespan hooks
- ``execute_with_cache(...)``               — the resolver-side wrapper
- ``CacheStats`` / ``stats``                — observability counters

Parse and compiled-plan caching were considered and rejected: parse is
~µs per request and compile is ~ms — both are dwarfed by the warehouse
roundtrip, and the compiled-plan cross-tenant correctness story (which
JWT claims could the policy possibly read?) was fragile. Only the
warehouse-roundtrip cache earns its keep.
"""

from __future__ import annotations

from .config import CacheBackendConfig, CacheConfig, ResultConfig
from .setup import close_cache, setup_cache
from .stats import CacheStats, stats

__all__ = [
    "CacheBackendConfig",
    "CacheConfig",
    "CacheStats",
    "ResultConfig",
    "close_cache",
    "setup_cache",
    "stats",
]
