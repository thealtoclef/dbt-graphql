"""Integration tests for the GraphQL HTTP server (Starlette + Ariadne).

Starts the real Starlette app via TestClient against PostgreSQL and MySQL
databases populated by the jaffle-shop dbt project, then makes real HTTP
GraphQL requests to verify the full request path — including access policy.
"""

from __future__ import annotations

import pytest
import jwt as pyjwt
from starlette.testclient import TestClient

from dbt_graphql.api.app import create_app
from dbt_graphql.api.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEntry,
    TablePolicy,
)

pytest.importorskip("ariadne", reason="ariadne required for serve tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    return pyjwt.encode(payload, "test-secret", algorithm="HS256")


def _bearer(payload: dict) -> dict:
    return {"Authorization": f"Bearer {_jwt(payload)}"}


def _gql(client, query: str, headers: dict | None = None) -> dict:
    resp = client.post("/graphql", json={"query": query}, headers=headers or {})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(serve_adapter_env):
    app = create_app(
        db_graphql_path=serve_adapter_env["db_graphql_path"],
        db_url=serve_adapter_env["db_url"],
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraphQLHTTP:
    def test_query_all_customers(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ customers { customer_id first_name last_name } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        rows = data["data"]["customers"]
        assert len(rows) > 0
        assert "customer_id" in rows[0]
        assert "first_name" in rows[0]

    def test_query_all_orders(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ orders { order_id status } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert len(data["data"]["orders"]) > 0

    def test_query_with_limit(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ customers(limit: 1) { customer_id } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert len(data["data"]["customers"]) == 1

    def test_query_selected_fields_only(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ customers { first_name } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        rows = data["data"]["customers"]
        assert all("first_name" in r for r in rows)
        assert all("customer_id" not in r for r in rows)

    def test_invalid_graphql_syntax_returns_error(self, client):
        resp = client.post("/graphql", json={"query": "{ not valid graphql {{{"})
        assert resp.status_code in (400, 200)
        data = resp.json()
        if resp.status_code == 200:
            assert "errors" in data

    def test_introspection_type_names(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ __schema { types { name } } }"},
        )
        assert resp.status_code == 200
        type_names = {t["name"] for t in resp.json()["data"]["__schema"]["types"]}
        assert "customers" in type_names
        assert "orders" in type_names

    def test_schema_exposes_where_input_types(self, client):
        resp = client.post(
            "/graphql",
            json={"query": "{ __schema { types { name } } }"},
        )
        assert resp.status_code == 200
        type_names = {t["name"] for t in resp.json()["data"]["__schema"]["types"]}
        assert "customersWhereInput" in type_names
        assert "ordersWhereInput" in type_names

    def test_where_filter_end_to_end(self, client):
        resp = client.post(
            "/graphql",
            json={
                "query": "{ customers(where: { customer_id: 1 }) { customer_id first_name } }"
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        rows = data["data"]["customers"]
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    def test_where_filter_no_match_returns_empty(self, client):
        resp = client.post(
            "/graphql",
            json={
                "query": "{ customers(where: { customer_id: 9999 }) { customer_id } }"
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert data["data"]["customers"] == []


# ---------------------------------------------------------------------------
# Policy-aware fixtures
# ---------------------------------------------------------------------------

_ALL_CUST = "{ customers { customer_id first_name last_name } }"


@pytest.fixture
def policy_client(serve_adapter_env):
    """Factory fixture: policy_client(policy) returns a TestClient with that policy."""

    def _make(policy: AccessPolicy | None = None):
        app = create_app(
            db_graphql_path=serve_adapter_env["db_graphql_path"],
            db_url=serve_adapter_env["db_url"],
            access_policy=policy,
        )
        return TestClient(app, raise_server_exceptions=True)

    return _make


# ---------------------------------------------------------------------------
# Policy integration tests (PostgreSQL + MySQL)
# ---------------------------------------------------------------------------


class TestPolicyHTTP:
    """Full-chain policy tests: JWT → middleware → policy engine → SQL → response."""

    def test_no_policy_returns_all_columns(self, policy_client):
        with policy_client(None) as c:
            rows = _gql(c, _ALL_CUST)["customers"]
        assert len(rows) > 0
        assert all(
            r["first_name"] is not None and r["last_name"] is not None for r in rows
        )

    def test_includes_strips_unlisted_columns(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="limited",
                    when="True",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(includes=["customer_id"])
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            rows = _gql(c, _ALL_CUST)["customers"]
        assert len(rows) > 0
        for r in rows:
            assert r["customer_id"] is not None
            assert r["first_name"] is None
            assert r["last_name"] is None

    def test_excludes_removes_pii_columns(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="analyst",
                    when="True",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(
                                include_all=True, excludes=["first_name", "last_name"]
                            )
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            rows = _gql(c, _ALL_CUST)["customers"]
        assert all(r["first_name"] is None for r in rows)
        assert all(r["last_name"] is None for r in rows)
        assert all(r["customer_id"] is not None for r in rows)

    def test_null_mask_returns_null(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="analyst",
                    when="True",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(
                                include_all=True, mask={"last_name": None}
                            )
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            rows = _gql(c, _ALL_CUST)["customers"]
        assert all("last_name" in r for r in rows)
        assert all(r["last_name"] is None for r in rows)
        assert all(r["first_name"] is not None for r in rows)

    def test_row_filter_restricts_rows(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="scoped",
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
        with policy_client(policy) as c:
            rows = _gql(
                c, _ALL_CUST, headers=_bearer({"sub": "u1", "claims": {"cust_id": 1}})
            )["customers"]
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    def test_jwt_group_gates_column_restriction(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="analyst",
                    when="'analysts' in jwt.groups",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(includes=["customer_id"])
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            # analyst — restricted to customer_id
            rows = _gql(
                c, _ALL_CUST, headers=_bearer({"sub": "u1", "groups": ["analysts"]})
            )["customers"]
            assert all(r["first_name"] is None and r["last_name"] is None for r in rows)

            # finance — no matching policy → unrestricted
            rows = _gql(
                c, _ALL_CUST, headers=_bearer({"sub": "u2", "groups": ["finance"]})
            )["customers"]
            assert all(r["first_name"] is not None for r in rows)

    def test_no_jwt_anon_policy_applies(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="anon",
                    when="jwt.sub == None",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(includes=["customer_id"])
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            # no Authorization header → anon policy
            rows = _gql(c, _ALL_CUST)["customers"]
            assert all(r["first_name"] is None for r in rows)

            # authenticated user → no matching policy → unrestricted
            rows = _gql(c, _ALL_CUST, headers=_bearer({"sub": "u1", "groups": []}))[
                "customers"
            ]
            assert all(r["first_name"] is not None for r in rows)

    def test_when_fires_but_table_absent_is_unrestricted(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="orders_only",
                    when="True",
                    tables={
                        "orders": TablePolicy(
                            column_level=ColumnLevelPolicy(includes=["order_id"])
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            # customers not covered → all columns returned
            rows = _gql(c, _ALL_CUST)["customers"]
            assert all(r["first_name"] is not None for r in rows)

    def test_row_filter_on_orders(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    name="scoped",
                    when="True",
                    tables={
                        "orders": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True),
                            row_level="customer_id = {{ jwt.claims.cust_id }}",
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            rows = _gql(
                c,
                "{ orders { order_id status } }",
                headers=_bearer({"sub": "u1", "claims": {"cust_id": 1}}),
            )["orders"]
        assert len(rows) > 0
