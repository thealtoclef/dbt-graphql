"""Pydantic model for the cache config block.

A single flat block — there is one cache and one set of knobs.
``lock_safety_timeout`` is the auto-release on the singleflight lock,
not the entry TTL. All time fields are in seconds (cashews/Redis convention).
"""

from __future__ import annotations

from pydantic import BaseModel

from .. import defaults


class CacheConfig(BaseModel):
    url: str = defaults.CACHE_DEFAULT_URL
    ttl: int = defaults.CACHE_TTL
    lock_safety_timeout: int = defaults.CACHE_LOCK_SAFETY_TIMEOUT
