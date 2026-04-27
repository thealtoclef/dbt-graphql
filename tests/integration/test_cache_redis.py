"""Multi-replica singleflight against a real Redis.

Two cashews ``Cache`` instances against one Redis must coalesce a
concurrent burst into a single runner invocation — the cluster-wide
counterpart of the in-process singleflight invariant.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from cashews import Cache
from sqlalchemy import Column, Integer, MetaData, String, Table, select

from dbt_graphql.cache import CacheConfig, stats
from dbt_graphql.cache.result import _execute_with

pytest.importorskip("redis", reason="redis client required for cashews redis backend")


def _stmt(value: str = "alice"):
    meta = MetaData()
    t = Table("u", meta, Column("id", Integer), Column("name", String))
    return select(t.c.id).where(t.c.name == value)


@pytest_asyncio.fixture
async def two_replicas(redis_service):
    """Two independent cashews ``Cache`` instances against the same Redis."""
    a = Cache()
    b = Cache()
    a.setup(redis_service)
    b.setup(redis_service)
    # Both clients must start clean.
    await a.clear()
    stats.reset()
    yield a, b
    await a.clear()
    await a.close()
    await b.close()
    stats.reset()


class TestMultiReplicaSingleflight:
    @pytest.mark.asyncio
    async def test_burst_split_across_replicas_coalesces_to_one(self, two_replicas):
        """100 concurrent identical queries split 50/50 across two cashews
        clients on the same Redis → exactly 1 runner call."""
        a, b = two_replicas
        cfg = CacheConfig(ttl=60)
        s = _stmt()

        runner_calls = 0

        async def runner(_stmt):
            nonlocal runner_calls
            runner_calls += 1
            # Hold long enough that all 100 callers enter the lock-wait
            # path before the holder populates the cache.
            await asyncio.sleep(0.2)
            return [{"id": 1}]

        async def one(client: Cache):
            return await _execute_with(
                client, s, dialect_name="postgresql", runner=runner, cfg=cfg
            )

        tasks = []
        for i in range(100):
            tasks.append(one(a if i % 2 == 0 else b))
        results = await asyncio.gather(*tasks)

        assert runner_calls == 1, (
            f"singleflight broke across replicas: runner ran {runner_calls} times"
        )
        assert all(r == [{"id": 1}] for r in results)

    @pytest.mark.asyncio
    async def test_steady_state_hit_visible_from_other_replica(self, two_replicas):
        """A populates the cache; B's next get must see the entry."""
        a, b = two_replicas
        cfg = CacheConfig(ttl=60)
        s = _stmt("steady")

        runner_calls = 0

        async def runner(_stmt):
            nonlocal runner_calls
            runner_calls += 1
            return [{"id": 7}]

        await _execute_with(a, s, dialect_name="postgresql", runner=runner, cfg=cfg)
        await _execute_with(b, s, dialect_name="postgresql", runner=runner, cfg=cfg)

        assert runner_calls == 1
