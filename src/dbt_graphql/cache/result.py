"""Result cache + singleflight (one mechanism, one key namespace).

Combines:
- TTL-based result cache keyed by rendered SQL + bound params
- ``cache.lock`` for singleflight: concurrent misses on the same key
  serialize through one warehouse roundtrip

Per-table TTL semantics:
- ``ttl > 0``  : cache for that many seconds
- ``ttl == 0`` : "realtime + minimal coalescing window". We still acquire
  the lock (so a burst still coalesces), but persist for ~1s only —
  enough that the 99 callers waiting on the lock all get the same result.

When multiple tables are touched (e.g., a join via correlated subqueries),
TTL = ``min(per-table-TTLs, default_ttl_s)``. The strictest table wins —
correctness over cache-hit-rate.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Iterable

from cashews import cache
from loguru import logger
from sqlalchemy.sql import ClauseElement

from .config import ResultConfig
from .keys import hash_sql
from .stats import stats


def resolve_ttl(table_names: Iterable[str], cfg: ResultConfig) -> int:
    """Compute the effective TTL for a set of tables. See module docstring."""
    per_table = [
        cfg.per_table_ttl_s[t] for t in table_names if t in cfg.per_table_ttl_s
    ]
    if not per_table:
        return cfg.default_ttl_s
    return min(per_table + [cfg.default_ttl_s])


async def execute_with_cache(
    stmt: ClauseElement,
    *,
    dialect_name: str,
    table_names: Iterable[str],
    runner: Callable[[ClauseElement], Awaitable[list[dict]]],
    cfg: ResultConfig,
) -> list[dict]:
    """Cached wrapper over ``runner(stmt)``.

    ``runner`` is the only thing that talks to the warehouse. We never call
    it from inside the fast path; only on a miss, holding the singleflight
    lock.
    """
    key = hash_sql(stmt, dialect_name)
    ttl = resolve_ttl(table_names, cfg)

    # Fast path — TTL hit. Steady state.
    cached = await cache.get(key)
    if cached is not None:
        stats.result.hit += 1
        return cached

    # Slow path — coalesce concurrent misses through a lock. The lock's
    # ``expire`` is the *safety timeout* (auto-release on lock-holder crash);
    # it is unrelated to the result TTL.
    async with cache.lock(
        f"lock:{key}", expire=cfg.lock_safety_timeout_s
    ):
        # Re-check inside the lock: another caller may have populated while
        # we were waiting. Discriminate hit (TTL) from coalesced (singleflight)
        # for operator-facing observability.
        cached = await cache.get(key)
        if cached is not None:
            stats.result.coalesced += 1
            return cached

        stats.result.miss += 1
        result = await runner(stmt)
        # TTL=0 → micro-window so the lock-waiters wake to populated cache.
        # TTL=N → operator-set freshness window.
        effective_ttl = 1 if ttl == 0 else ttl
        await cache.set(key, result, expire=effective_ttl)
        logger.debug("cache.result MISS key={} stored ttl={}s", key, effective_ttl)
        return result
