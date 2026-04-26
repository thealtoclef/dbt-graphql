"""End-to-end cache integration against PostgreSQL and MySQL.

These tests boot the real Starlette + Ariadne app via ``TestClient``,
parametrized across both warehouse adapters via ``serve_adapter_env``.
They prove the three cache layers compose correctly — same query twice
hits L1+L2+L3 from the cache and skips the warehouse — without mocking
the database.

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

from dbt_graphql.api.app import create_app
from dbt_graphql.api.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEntry,
    TablePolicy,
)
from dbt_graphql.cache import CacheConfig, stats
from dbt_graphql.cache.config import CacheBackendConfig, L3Config
from dbt_graphql.cache.setup import close_cache

pytest.importorskip("ariadne", reason="ariadne required for serve tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(payload: dict) -> dict:
    return {"Authorization": f"Bearer {pyjwt.encode(payload, 's', algorithm='HS256')}"}


def _gql(client, query, headers=None):
    resp = client.post("/graphql", json={"query": query}, headers=headers or {})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cache_config(per_table=None, ttl=60) -> CacheConfig:
    return CacheConfig(
        backends=[CacheBackendConfig(url="mem://?size=1000")],
        result=L3Config(default_ttl_s=ttl, per_table_ttl_s=per_table or {}),
    )


@pytest_asyncio.fixture
async def _cleanup_cache():
    """Module fixture: nuke cashews state before AND after each test.

    Cashews' ``cache`` is a process-wide singleton; without this teardown
    state from one parametrization (postgres) leaks into the next (mysql)
    and turns real misses into phantom hits.
    """
    try:
        await cache.clear()
    except Exception:
        pass
    stats.reset()
    yield
    try:
        await cache.clear()
    except Exception:
        pass
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
            db_graphql_path=serve_adapter_env["db_graphql_path"],
            db_url=serve_adapter_env["db_url"],
            access_policy=access_policy,
            cache_config=cache_cfg if cache_cfg is not None else _cache_config(),
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
    """Full request path: HTTP → parse (L1) → compile (L2) → execute (L3) → DB."""

    def test_repeat_query_hits_result_cache(self, cached_client):
        """Second identical query → no warehouse call."""
        client, counter = cached_client()
        try:
            with client as c:
                rows1 = _gql(c, "{ customers { customer_id first_name } }")[
                    "customers"
                ]
                first = counter["n"]
                rows2 = _gql(c, "{ customers { customer_id first_name } }")[
                    "customers"
                ]
                second = counter["n"]
            # First request: compile + execute → counter went up.
            # Second request: full L3 hit → counter unchanged.
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
        """Two queries with different bound where-values → different L3 keys."""
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

    def test_parse_cache_hit_metric(self, cached_client):
        """Repeated query string → L1 hit recorded in stats."""
        client, _ = cached_client()
        try:
            with client as c:
                _gql(c, "{ customers { customer_id } }")
                _gql(c, "{ customers { customer_id } }")
            assert stats.parsed_doc.hit >= 1
        finally:
            client._restore_execute()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tenant isolation under policy
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Two users with row-filtered policies must NEVER see each other's rows.

    This is the cache-correctness invariant: even though both queries have
    the same GraphQL shape, the bound row-filter values differ → L3 SQL
    keys differ → no cache cross-contamination.
    """

    def _row_filtered_policy(self) -> AccessPolicy:
        return AccessPolicy(
            policies=[
                PolicyEntry(
                    name="self",
                    when="True",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True),
                            row_level="customer_id = {{ jwt.claims.cust_id }}",
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


# Note on burst-protection coverage:
# The 100→1 singleflight invariant is asserted definitively in
# ``tests/unit/cache/test_result.py::TestSingleflight``. Replicating the
# same assertion through TestClient is awkward (the sync TestClient
# serializes posts) and adds no signal beyond the unit test, so we stop
# at "the layers wire up against a real warehouse without regressions".
