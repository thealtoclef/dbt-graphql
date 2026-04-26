"""Cache layer: L1 parsed-doc, L2 compiled-plan, L3 result + singleflight.

See `docs/plans/sec-j-caching.md` for the design rationale. The public API
is intentionally tiny:

- ``setup_cache(cfg)`` / ``close_cache()``  — lifespan hooks
- ``parse_sync_cached(query)``              — L1 wrapper (sync, used by Ariadne)
- ``compile_with_cache(...)``               — L2 wrapper
- ``execute_with_cache(...)``               — L3 wrapper
- ``CacheStats`` / ``stats``                — observability counters
"""

from __future__ import annotations

from .config import CacheBackendConfig, CacheConfig, L1Config, L2Config, L3Config
from .setup import close_cache, setup_cache
from .stats import CacheStats, stats

__all__ = [
    "CacheBackendConfig",
    "CacheConfig",
    "CacheStats",
    "L1Config",
    "L2Config",
    "L3Config",
    "close_cache",
    "setup_cache",
    "stats",
]
