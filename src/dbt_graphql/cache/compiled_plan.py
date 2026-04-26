"""L2 — compiled-plan cache.

Skips ``compile_query`` (GraphQL-AST → SQLAlchemy ``Select`` + policy enforcement)
on repeated identical-shaped requests. Cache stores the SQLAlchemy ``Select``
object directly (mem backend, no serialization); Redis-backed L2 would require
pickling which is not supported by SQLAlchemy in general — hence L2 is
documented as **always in-memory** in the plan.

Cache key includes the full JWT signature: see ``keys.jwt_signature`` for
the rationale (nested-table policy resolution prevents up-front claim-path
enumeration).

The compiled ``Select`` already embeds policy decisions (column allow-list,
mask expressions, row-filter SQL with bound params) at the time it was built.
A cache hit therefore re-uses the *exact* policy that the cache holder
saw — meaning policy hot-reload (Sec-K, future) must invalidate L2.
"""

from __future__ import annotations

from typing import Any

from cashews import cache
from sqlalchemy.sql import Select

from .keys import compiled_plan_key, jwt_signature
from .stats import stats


async def compile_with_cache(
    *,
    field_node,
    table_name: str,
    where: dict[str, Any] | None,
    limit: int | None,
    offset: int | None,
    dialect: str,
    jwt_payload: Any,
    compiler,
) -> Select:
    """Cache the result of ``compiler()``.

    ``compiler`` is a zero-arg callable that returns the ``Select``. We pass
    it as a callback (rather than the args) so the cache layer never has to
    know about ``compile_query``'s signature; it's just a memoized thunk.
    """
    sig = jwt_signature(jwt_payload)
    key = compiled_plan_key(
        field_node=field_node,
        table_name=table_name,
        where=where,
        limit=limit,
        offset=offset,
        dialect=dialect,
        jwt_sig=sig,
    )

    hit = await cache.get(key)
    if hit is not None:
        stats.compiled_plan.hit += 1
        return hit

    stats.compiled_plan.miss += 1
    stmt = compiler()
    await cache.set(key, stmt)
    return stmt
