"""Pydantic model for the cache config block.

A single flat block — there is one cache and one set of knobs.
``lock_safety_timeout_s`` is the auto-release on the singleflight lock,
not the entry TTL.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import defaults


class CacheConfig(BaseModel):
    enabled: bool = True
    url: str = defaults.CACHE_DEFAULT_URL
    default_ttl_s: int = defaults.CACHE_DEFAULT_TTL_S
    # Per-table TTL override. ``0`` = realtime + minimal coalescing window
    # (we still acquire the singleflight lock to coalesce concurrent misses,
    # but persist for ~1s only).
    per_table_ttl_s: dict[str, int] = Field(default_factory=dict)
    lock_safety_timeout_s: int = defaults.CACHE_LOCK_SAFETY_TIMEOUT_S
