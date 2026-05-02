"""End-to-end pool admission control: HTTP 503 + Retry-After under load.

Unit-level coverage:
- ``tests/unit/graphql/test_pool_timeout.py`` proves the resolver
  translates ``SAPoolTimeoutError`` → ``GraphQLError`` (POOL_TIMEOUT).
- ``tests/unit/graphql/test_monitoring.py`` proves the HTTP handler
  elevates POOL_TIMEOUT → 503 + Retry-After.

This test wires the real Starlette + Ariadne + DatabaseManager + SQLAlchemy
pool to verify the production LB-visible behavior end-to-end.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from dbt_graphql.compiler.connection import DatabaseManager
from dbt_graphql.config import PoolConfig
from dbt_graphql.serve.app import create_app

from .conftest import make_test_jwt_config


@pytest.mark.asyncio
async def test_pool_exhaustion_returns_503_with_retry_after(
    serve_adapter_env, monkeypatch
):
    """``size=1, max_overflow=0, timeout=0.1``: hold the single connection
    in one in-flight request and verify concurrent requests get HTTP 503
    with a ``Retry-After`` header."""
    pool = PoolConfig(size=1, max_overflow=0, timeout=0.1, recycle=1800, retry_after=3)
    app = create_app(
        registry=serve_adapter_env["registry"],
        db_url=serve_adapter_env["db_url"],
        jwt_config=make_test_jwt_config(),
        security_enabled=True,
        pool_config=pool,
    )

    # Hold a real pool slot for ~1s by sleeping inside the connection scope.
    # Patch via monkeypatch so pytest unwinds it deterministically even if
    # the test fails or is interrupted mid-flight.
    async def slow_execute(self, query):
        engine = self._engine
        assert engine is not None
        async with engine.connect() as conn:
            await asyncio.sleep(1.0)
            result = await conn.execute(query)
            return [dict(row._mapping) for row in result]

    monkeypatch.setattr(DatabaseManager, "execute", slow_execute)

    # Drive the lifespan on the same event loop as the requests — TestClient
    # uses a separate portal loop, which conflicts with the cashews memory
    # backend's expired-entry sweeper task on shutdown.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            timeout=10.0,
            follow_redirects=True,
        ) as ac:
            # Distinct ``first`` value per request → distinct rendered SQL →
            # distinct cache keys, so the always-on result cache + singleflight
            # can't coalesce these three calls into one DB checkout.
            async def one(i: int):
                return await ac.post(
                    "/graphql",
                    json={
                        "query": f"query {{ customers(first: {i + 1}) {{ nodes {{ customer_id }} }} }}"
                    },
                )

            responses = await asyncio.gather(*(one(i) for i in range(3)))

    statuses = sorted(r.status_code for r in responses)
    # At least one 503 — exact count depends on scheduling, but the
    # invariant is: pool-exhausted concurrent requests must elevate to
    # 503 (not 200 with a generic error, not 500).
    assert 503 in statuses, f"expected at least one 503, got {statuses}"
    # Every 503 must carry Retry-After (operator/LB contract).
    for r in responses:
        if r.status_code == 503:
            assert r.headers.get("retry-after") == "3", r.headers
