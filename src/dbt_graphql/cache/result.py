"""Result cache + singleflight (one mechanism, one key namespace).

Combines:
- TTL-based result cache keyed by rendered SQL + bound params
- ``cache.lock`` for singleflight: concurrent misses on the same key
  serialize through one warehouse roundtrip

TTL semantics:
- ``ttl > 0``  : cache for that many seconds
- ``ttl == 0`` : "realtime + minimal coalescing window". We still acquire
  the lock (so a burst still coalesces), but persist for ~1s only —
  enough that the 99 callers waiting on the lock all get the same result.

Note on the lock key: we use ``f"{key}:lock"``, not ``f"lock:{key}"``,
so that the lock inherits the same key prefix as its data entry. Any
prefix-routed multi-backend setup that puts ``sql:`` keys on Redis
will then put the corresponding locks on Redis too — without that,
the singleflight lock lives on a different backend than the data
and cluster-wide coalescing silently breaks.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from cashews import cache
from cashews.wrapper import Cache
from loguru import logger
from opentelemetry import metrics
from sqlalchemy.sql import ClauseElement

from ..config import CacheConfig
from .keys import hash_sql
from .stats import stats

_meter = metrics.get_meter(__name__)
_result_outcomes = _meter.create_counter(
    "cache.result", description="Result-cache outcomes by attribute"
)


async def execute_with_cache(
    stmt: ClauseElement,
    *,
    dialect_name: str,
    runner: Callable[[ClauseElement], Awaitable[list[dict]]],
    cfg: CacheConfig,
) -> list[dict]:
    """Cached wrapper over ``runner(stmt)``.

    ``runner`` is the only thing that talks to the warehouse. We never call
    it from inside the fast path; only on a miss, holding the singleflight
    lock.
    """
    return await _execute_with(
        cache, stmt, dialect_name=dialect_name, runner=runner, cfg=cfg
    )


async def _execute_with(
    cache_obj: Cache,
    stmt: ClauseElement,
    *,
    dialect_name: str,
    runner: Callable[[ClauseElement], Awaitable[list[dict]]],
    cfg: CacheConfig,
) -> list[dict]:
    """Singleflight + TTL-cache pipeline against an arbitrary cashews ``Cache``."""
    key = hash_sql(stmt, dialect_name)
    ttl = cfg.ttl

    cached = await cache_obj.get(key)
    if cached is not None:
        stats.result.hit += 1
        _result_outcomes.add(1, {"outcome": "hit"})
        return cached

    async with cache_obj.lock(f"{key}:lock", expire=cfg.lock_safety_timeout):
        cached = await cache_obj.get(key)
        if cached is not None:
            stats.result.coalesced += 1
            _result_outcomes.add(1, {"outcome": "coalesced"})
            return cached

        stats.result.miss += 1
        _result_outcomes.add(1, {"outcome": "miss"})
        result = await runner(stmt)
        effective_ttl = 1 if ttl == 0 else ttl
        await cache_obj.set(key, result, expire=effective_ttl)
        logger.debug("cache.result MISS key={} stored ttl={}s", key, effective_ttl)
        return result
