"""Pydantic models for the cache config block.

Single layer: the result cache + singleflight. ``lock_safety_timeout_s``
is the auto-release on the singleflight lock — not the entry TTL.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import defaults


class CacheBackendConfig(BaseModel):
    """One cashews backend. Maps to a single ``cache.setup(url, prefix=...)`` call.

    The ``prefix`` string routes any cache key starting with that prefix to
    this backend. Empty prefix is the catch-all backend.
    """

    url: str
    prefix: str = ""
    enabled: bool = True


class L3Config(BaseModel):
    enabled: bool = True
    default_ttl_s: int = defaults.CACHE_RESULT_DEFAULT_TTL_S
    # Per-table TTL override. ``0`` = realtime + minimal coalescing window
    # (we still acquire the singleflight lock to coalesce concurrent misses,
    # but persist for ~1s only).
    per_table_ttl_s: dict[str, int] = Field(default_factory=dict)
    lock_safety_timeout_s: int = defaults.CACHE_RESULT_LOCK_SAFETY_TIMEOUT_S


def _default_backends() -> list[CacheBackendConfig]:
    return [CacheBackendConfig(url=defaults.CACHE_BACKEND_DEFAULT_URL)]


class CacheConfig(BaseModel):
    backends: list[CacheBackendConfig] = Field(default_factory=_default_backends)
    result: L3Config = Field(default_factory=L3Config)
