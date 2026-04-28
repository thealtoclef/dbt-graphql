"""Pool exhaustion → POOL_TIMEOUT GraphQL error.

The resolver-layer half of the admission-control wiring; the HTTP-handler
half (POOL_TIMEOUT extension → 503 + Retry-After) lives in
``test_monitoring.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from graphql import GraphQLError
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

from dbt_graphql.cache import CacheConfig
from dbt_graphql.graphql.resolvers import POOL_TIMEOUT_CODE, _make_resolver
from dbt_graphql.config import PoolConfig


class _FakeRegistry:
    def __init__(self, tdef):
        self._t = tdef

    def get(self, _name):
        return self._t


def _make_info(db, registry):
    info = MagicMock()
    info.context = {
        "registry": registry,
        "db": db,
        "jwt_payload": {},
        "policy_engine": None,
        "cache_config": CacheConfig(),
    }
    info.field_nodes = []
    return info


@pytest.mark.asyncio
async def test_resolver_translates_pool_timeout_to_graphql_error(
    monkeypatch, fresh_cache
):
    """``db.execute`` raises ``TimeoutError`` → resolver raises GraphQLError
    with ``extensions.code == POOL_TIMEOUT`` and the configured retry-after."""
    del fresh_cache  # fixture configures cashews — production lifespan equivalent
    # Stub compile_query so we don't need a real registry/table def.
    monkeypatch.setattr(
        "dbt_graphql.graphql.resolvers.compile_query",
        lambda **_: MagicMock(name="stmt"),
    )

    db = MagicMock()
    db.dialect_name = "postgresql"
    db._pool = PoolConfig(
        size=1, max_overflow=0, timeout=7, recycle=1800, retry_after=3
    )

    async def boom(_stmt):
        raise SAPoolTimeoutError(
            "QueuePool limit of size 1 overflow 0 reached", None, None
        )

    db.execute = boom

    tdef = MagicMock(name="customers")
    registry = _FakeRegistry(tdef)
    info = _make_info(db, registry)

    resolver = _make_resolver("customers")

    with pytest.raises(GraphQLError) as exc_info:
        await resolver(None, info)

    err = exc_info.value
    assert err.extensions is not None
    assert err.extensions["code"] == POOL_TIMEOUT_CODE
    assert err.extensions["retry_after"] == 3
    # message is operator-readable
    assert "pool exhausted" in str(err).lower()
