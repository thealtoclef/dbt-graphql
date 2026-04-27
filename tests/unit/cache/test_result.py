"""Result cache + singleflight.

The most behavior-sensitive piece. Tests pin:

1. Steady-state TTL hit (no warehouse roundtrip on repeat).
2. Burst protection: 100 concurrent identical requests → exactly 1
   warehouse call. Asserted via a counter on the runner.
3. Distinct queries do not block each other.
4. TTL semantics (incl. ``ttl=0`` = realtime + minimal coalescing).
5. TTL expiry triggers a fresh fetch.
6. Lock-holder failure (simulated): exception propagates and the lock
   releases so the next caller can succeed.

The "runner" is a callable that the cache invokes only on a miss inside
the singleflight lock. We pass our own deterministic runner instead of a
mock — counting calls is the correctness contract this layer is built on.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select

from dbt_graphql.cache import CacheConfig, stats
from dbt_graphql.cache.result import execute_with_cache

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader


def _stmt(value: str = "alice"):
    meta = MetaData()
    t = Table("u", meta, Column("id", Integer), Column("name", String))
    return select(t.c.id).where(t.c.name == value)


class CountingRunner:
    """Tracks every invocation and supports per-call latency injection."""

    def __init__(self, result=None, delay: float = 0.0):
        self.calls = 0
        self.delay = delay
        self.result = result if result is not None else [{"id": 1}]

    async def __call__(self, _stmt):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


# ---------------------------------------------------------------------------
# Steady-state behavior
# ---------------------------------------------------------------------------


class TestSteadyState:
    @pytest.mark.asyncio
    async def test_first_call_misses(self, fresh_cache):
        runner = CountingRunner(result=[{"id": 1}])
        rows = await execute_with_cache(
            _stmt(),
            dialect_name="postgresql",
            runner=runner,
            cfg=CacheConfig(ttl=60),
        )
        assert runner.calls == 1
        assert rows == [{"id": 1}]
        assert stats.result.miss == 1

    @pytest.mark.asyncio
    async def test_repeat_within_ttl_hits(self, fresh_cache):
        runner = CountingRunner()
        s = _stmt()
        cfg = CacheConfig(ttl=60)
        for _ in range(5):
            await execute_with_cache(
                s,
                dialect_name="postgresql",
                runner=runner,
                cfg=cfg,
            )
        assert runner.calls == 1
        assert stats.result.hit == 4
        assert stats.result.miss == 1

    @pytest.mark.asyncio
    async def test_distinct_bound_params_independent_entries(self, fresh_cache):
        runner = CountingRunner()
        cfg = CacheConfig(ttl=60)
        # Two distinct parameter values → two SQL hashes → two warehouse calls.
        await execute_with_cache(
            _stmt("alice"),
            dialect_name="postgresql",
            runner=runner,
            cfg=cfg,
        )
        await execute_with_cache(
            _stmt("bob"),
            dialect_name="postgresql",
            runner=runner,
            cfg=cfg,
        )
        assert runner.calls == 2


# ---------------------------------------------------------------------------
# Singleflight (the burst-protection invariant)
# ---------------------------------------------------------------------------


class TestSingleflight:
    @pytest.mark.asyncio
    async def test_concurrent_identical_coalesce_to_one(self, fresh_cache):
        """100 concurrent identical requests → exactly 1 runner invocation."""
        # ``delay`` ensures all 100 enter the lock before any of them set
        # the cache — without it the first task could finish before others
        # arrive and we'd see TTL hits instead of coalesced wakes.
        runner = CountingRunner(result=[{"id": 42}], delay=0.05)
        s = _stmt()
        cfg = CacheConfig(ttl=60)

        async def one():
            return await execute_with_cache(
                s,
                dialect_name="postgresql",
                runner=runner,
                cfg=cfg,
            )

        results = await asyncio.gather(*(one() for _ in range(100)))
        assert runner.calls == 1
        # Every caller got the same data.
        assert all(r == [{"id": 42}] for r in results)
        # Telemetry: exactly 1 miss, the rest are coalesced wakes (not TTL hits).
        assert stats.result.miss == 1
        assert stats.result.coalesced == 99
        assert stats.result.hit == 0

    @pytest.mark.asyncio
    async def test_distinct_queries_do_not_serialize(self, fresh_cache):
        """Different keys → independent locks → all run in parallel."""
        runner = CountingRunner(delay=0.05)
        cfg = CacheConfig(ttl=60)

        async def one(name):
            return await execute_with_cache(
                _stmt(name),
                dialect_name="postgresql",
                runner=runner,
                cfg=cfg,
            )

        names = [f"u{i}" for i in range(20)]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.gather(*(one(n) for n in names))
        elapsed = loop.time() - t0
        assert runner.calls == 20
        # If they had serialized, total time would be ~20 * 0.05 = 1.0s.
        # In parallel it's ~0.05s; allow generous slack to avoid flake.
        assert elapsed < 0.5, f"distinct keys serialized: took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# TTL semantics
# ---------------------------------------------------------------------------


class TestTtl:
    @pytest.mark.asyncio
    async def test_ttl_expiry_refetches(self, fresh_cache):
        runner = CountingRunner()
        s = _stmt()
        cfg = CacheConfig(ttl=1)
        await execute_with_cache(
            s,
            dialect_name="postgresql",
            runner=runner,
            cfg=cfg,
        )
        await asyncio.sleep(1.2)
        await execute_with_cache(
            s,
            dialect_name="postgresql",
            runner=runner,
            cfg=cfg,
        )
        assert runner.calls == 2

    @pytest.mark.asyncio
    async def test_ttl_zero_still_coalesces(self, fresh_cache):
        """``ttl=0`` → coalesce, but only briefly persist."""
        cfg = CacheConfig(ttl=0)
        runner = CountingRunner(delay=0.05)
        s = _stmt()

        async def one():
            return await execute_with_cache(
                s,
                dialect_name="postgresql",
                runner=runner,
                cfg=cfg,
            )

        await asyncio.gather(*(one() for _ in range(20)))
        # All 20 coalesced into one warehouse call.
        assert runner.calls == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestOTelMetrics:
    """The ``cache.result`` counter increments for hit/coalesced/miss."""

    def _collect(self, reader: InMemoryMetricReader) -> dict[str, int]:
        data = reader.get_metrics_data()
        out: dict[str, int] = {}
        if data is None:
            return out
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name != "cache.result":
                        continue
                    for dp in m.data.data_points:
                        attrs = dp.attributes or {}
                        outcome = str(attrs.get("outcome"))
                        out[outcome] = out.get(outcome, 0) + int(
                            getattr(dp, "value", 0)
                        )
        return out

    @pytest.mark.asyncio
    async def test_three_outcomes_emit(self, fresh_cache):
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        metrics.set_meter_provider(provider)

        cfg = CacheConfig(ttl=60)
        s = _stmt()

        # miss
        runner = CountingRunner(result=[{"id": 1}], delay=0.05)
        await execute_with_cache(s, dialect_name="postgresql", runner=runner, cfg=cfg)

        # hit (steady state)
        await execute_with_cache(s, dialect_name="postgresql", runner=runner, cfg=cfg)

        # Burst on a different statement: 1 wins the lock-holder path,
        # the other 9 wake to a populated entry → 1 miss + 9 coalesced.
        s2 = _stmt("burst")
        runner2 = CountingRunner(result=[{"id": 2}], delay=0.05)

        async def one():
            await execute_with_cache(
                s2, dialect_name="postgresql", runner=runner2, cfg=cfg
            )

        await asyncio.gather(*(one() for _ in range(10)))

        counts = self._collect(reader)
        assert counts.get("hit", 0) >= 1
        assert counts.get("miss", 0) >= 2  # one per statement
        assert counts.get("coalesced", 0) >= 1


class TestFailures:
    @pytest.mark.asyncio
    async def test_runner_exception_propagates_and_lock_releases(self, fresh_cache):
        cfg = CacheConfig(ttl=60)
        s = _stmt()

        class BoomFirst:
            def __init__(self):
                self.calls = 0

            async def __call__(self, _stmt):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("warehouse down")
                return [{"id": 99}]

        runner = BoomFirst()
        with pytest.raises(RuntimeError):
            await execute_with_cache(
                s,
                dialect_name="postgresql",
                runner=runner,
                cfg=cfg,
            )
        # The lock must have released (cashews releases on context exit even
        # when the body raised) so the retry can acquire it and succeed.
        rows = await execute_with_cache(
            s,
            dialect_name="postgresql",
            runner=runner,
            cfg=cfg,
        )
        assert rows == [{"id": 99}]
        assert runner.calls == 2
