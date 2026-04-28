"""End-to-end result-cache integration against PostgreSQL and MySQL.

These tests boot the real Starlette + Ariadne app via ``TestClient``,
parametrized across both warehouse adapters via ``serve_adapter_env``.
They prove the cache key actually delivers the two properties operators
care about: (a) tenant isolation, and (b) practical cache hits for the
same query from the same tenant — including across token refresh.

The trick used to count "did the warehouse actually run?" is a
``DatabaseManager.execute`` wrapper that increments a per-app counter.
This is *not* a mock — the real ``execute`` is still invoked for misses;
we only count the call to detect cache effectiveness.
"""

from __future__ import annotations


import jwt as pyjwt
import pytest
import pytest_asyncio
from cashews import cache
from starlette.testclient import TestClient

from dbt_graphql.serve.app import create_app
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEntry,
    TablePolicy,
    Effect,
)
from dbt_graphql.cache import CacheConfig, stats
from dbt_graphql.cache.setup import close_cache

from .conftest import JWT_TEST_SECRET, make_test_jwt_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(payload: dict) -> dict:
    return {
        "Authorization": (
            f"Bearer {pyjwt.encode(payload, JWT_TEST_SECRET, algorithm='HS256')}"
        )
    }


def _gql(client, query, headers=None):
    resp = client.post("/graphql", json={"query": query}, headers=headers or {})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cache_config(ttl=60) -> CacheConfig:
    return CacheConfig(
        url="mem://?size=1000",
        ttl=ttl,
    )


@pytest_asyncio.fixture
async def _cleanup_cache():
    """Module fixture: nuke cashews state before AND after each test.

    Cashews' ``cache`` is a process-wide singleton; without this teardown
    state from one parametrization (postgres) leaks into the next (mysql)
    and turns real misses into phantom hits.
    """
    await cache.clear()
    stats.reset()
    yield
    await cache.clear()
    await close_cache()
    stats.reset()


@pytest.fixture
def cached_client(serve_adapter_env, _cleanup_cache):
    """Factory: returns (client, exec_count_dict).

    ``exec_count_dict["n"]`` rises every time DatabaseManager.execute is
    called — letting tests assert "the warehouse was hit N times" while
    still talking to a real DB on misses.
    """

    def _make(
        cache_cfg: CacheConfig | None = None,
        access_policy: AccessPolicy | None = None,
    ):
        app = create_app(
            registry=serve_adapter_env["registry"],
            db_url=serve_adapter_env["db_url"],
            access_policy=access_policy,
            cache_config=cache_cfg if cache_cfg is not None else _cache_config(),
            jwt_config=make_test_jwt_config(),
            security_enabled=True,
        )
        counter = {"n": 0}
        # We retrieve the live DatabaseManager from the app's resolver
        # context. The Starlette routes mount the GraphQL ASGI; the db
        # lives on the closure of the context_value. Easier path: wrap
        # right before TestClient enters the lifespan, by patching the
        # registry's db reference. Cleanest is to monkeypatch
        # DatabaseManager.execute on the *instance* the app holds.
        from dbt_graphql.compiler.connection import DatabaseManager

        original_execute = DatabaseManager.execute

        async def counting_execute(self, query):
            counter["n"] += 1
            return await original_execute(self, query)

        DatabaseManager.execute = counting_execute  # type: ignore[method-assign]
        client = TestClient(app, raise_server_exceptions=True)

        def _restore():
            DatabaseManager.execute = original_execute  # type: ignore[method-assign]

        client._restore_execute = _restore  # type: ignore[attr-defined]
        return client, counter

    yield _make


# ---------------------------------------------------------------------------
# End-to-end cache hit tests
# ---------------------------------------------------------------------------


class TestCacheEndToEnd:
    """Full request path: HTTP → resolver → execute_with_cache → DB."""

    def test_repeat_query_hits_result_cache(self, cached_client):
        """Second identical query → no warehouse call."""
        client, counter = cached_client()
        try:
            with client as c:
                rows1 = _gql(c, "{ customers { customer_id first_name } }")["customers"]
                first = counter["n"]
                rows2 = _gql(c, "{ customers { customer_id first_name } }")["customers"]
                second = counter["n"]
            assert first >= 1
            assert second == first
            assert rows1 == rows2
            assert stats.result.hit >= 1
        finally:
            client._restore_execute()  # type: ignore[attr-defined]

    def test_distinct_queries_independent(self, cached_client):
        """Different queries → both run, no spurious cache collisions."""
        client, counter = cached_client()
        try:
            with client as c:
                _gql(c, "{ customers { customer_id } }")
                after_first = counter["n"]
                _gql(c, "{ orders { order_id } }")
                after_second = counter["n"]
            assert after_second > after_first  # second query hit the warehouse
        finally:
            client._restore_execute()  # type: ignore[attr-defined]

    def test_different_where_does_not_collide(self, cached_client):
        """Two queries with different bound where-values → different keys."""
        client, counter = cached_client()
        try:
            with client as c:
                _gql(c, "{ customers(where: {customer_id: 1}) { customer_id } }")
                first = counter["n"]
                _gql(c, "{ customers(where: {customer_id: 2}) { customer_id } }")
                second = counter["n"]
            assert second > first
        finally:
            client._restore_execute()  # type: ignore[attr-defined]

    def test_different_field_order_does_not_collide(self, cached_client):
        """``{ id name }`` vs ``{ name id }`` → different SELECT column order
        → different SQL → independent cache entries.

        Documents the operator-relevant edge: clients sending the same
        logical query with different field orders will see lower hit
        rates. Worth pinning so we don't accidentally collapse them."""
        client, counter = cached_client()
        try:
            with client as c:
                _gql(c, "{ customers { customer_id first_name } }")
                first = counter["n"]
                _gql(c, "{ customers { first_name customer_id } }")
                second = counter["n"]
            assert second > first
        finally:
            client._restore_execute()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tenant isolation under policy
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Two users with row-filtered policies must NEVER see each other's rows.

    This is the cache-correctness invariant: even though both queries have
    the same GraphQL shape, the bound row-filter values differ → SQL
    keys differ → no cache cross-contamination.
    """

    def _row_filtered_policy(self) -> AccessPolicy:
        return AccessPolicy(
            policies=[
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="self",
                    when="True",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True),
                            row_filter={
                                "customer_id": {"_eq": {"jwt": "claims.cust_id"}}
                            },
                        )
                    },
                )
            ]
        )

    def test_users_with_different_row_filters_isolated(self, cached_client):
        client, _ = cached_client(access_policy=self._row_filtered_policy())
        try:
            with client as c:
                rows_a = _gql(
                    c,
                    "{ customers { customer_id } }",
                    headers=_bearer({"sub": "a", "claims": {"cust_id": 1}}),
                )["customers"]
                rows_b = _gql(
                    c,
                    "{ customers { customer_id } }",
                    headers=_bearer({"sub": "b", "claims": {"cust_id": 2}}),
                )["customers"]
            # Each user sees only their own row — never the other's.
            assert all(r["customer_id"] == 1 for r in rows_a)
            assert all(r["customer_id"] == 2 for r in rows_b)
            # And the responses must NOT be identical (they would be if
            # one user accidentally got the other's cached entry).
            assert rows_a != rows_b
        finally:
            client._restore_execute()  # type: ignore[attr-defined]

    def test_same_user_repeat_hits_cache(self, cached_client):
        """Same user, same query, twice → second one served from cache."""
        client, counter = cached_client(access_policy=self._row_filtered_policy())
        try:
            with client as c:
                _gql(
                    c,
                    "{ customers { customer_id } }",
                    headers=_bearer({"sub": "a", "claims": {"cust_id": 1}}),
                )
                first = counter["n"]
                _gql(
                    c,
                    "{ customers { customer_id } }",
                    headers=_bearer({"sub": "a", "claims": {"cust_id": 1}}),
                )
                second = counter["n"]
            assert second == first
        finally:
            client._restore_execute()  # type: ignore[attr-defined]

    def test_token_refresh_same_claims_still_hits_cache(self, cached_client):
        """Fresh JWT (different ``iat`` / ``exp``) with the same policy-
        relevant claims must still hit the cache.

        This is the practical-hit case the operator cares about: clients
        rotate tokens constantly, but the cache key derives from rendered
        SQL — and the rendered SQL only embeds ``cust_id`` (the claim the
        policy reads). Other claim drift must not invalidate the cache."""
        import time as _time

        now = int(_time.time())
        client, counter = cached_client(access_policy=self._row_filtered_policy())
        try:
            with client as c:
                _gql(
                    c,
                    "{ customers { customer_id } }",
                    headers=_bearer(
                        {
                            "sub": "a",
                            "claims": {"cust_id": 1},
                            "iat": now - 10,
                            "exp": now + 600,
                        }
                    ),
                )
                first = counter["n"]
                _gql(
                    c,
                    "{ customers { customer_id } }",
                    headers=_bearer(
                        {
                            "sub": "a",
                            "claims": {"cust_id": 1},
                            "iat": now,
                            "exp": now + 1200,
                        }
                    ),
                )
                second = counter["n"]
            assert second == first  # warehouse not called the second time
            assert stats.result.hit >= 1
        finally:
            client._restore_execute()  # type: ignore[attr-defined]


# Note on burst-protection coverage:
# The 100→1 singleflight invariant is asserted definitively in
# ``tests/unit/cache/test_result.py::TestSingleflight``. Replicating the
# same assertion through TestClient is awkward (the sync TestClient
# serializes posts) and adds no signal beyond the unit test, so we stop
# at "the layers wire up against a real warehouse without regressions".
