"""Integration tests for the GraphQL HTTP server (Starlette + Ariadne).

Starts the real Starlette app via TestClient against PostgreSQL and MySQL
databases populated by the jaffle-shop dbt project, then makes real HTTP
GraphQL requests to verify the full request path — including access policy.
"""

from __future__ import annotations

import pytest
import jwt as pyjwt
from starlette.testclient import TestClient

from dbt_graphql.serve.app import create_app
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEntry,
    TablePolicy,
    Effect,
)
from dbt_graphql.config import GraphQLConfig

from .conftest import JWT_TEST_SECRET, make_test_jwt_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jwt(payload: dict) -> str:
    return pyjwt.encode(payload, JWT_TEST_SECRET, algorithm="HS256")


def _bearer(payload: dict) -> dict:
    return {"Authorization": f"Bearer {_jwt(payload)}"}


def _gql(client, query: str, headers: dict | None = None) -> dict:
    resp = client.post("/graphql", json={"query": query}, headers=headers or {})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


def _gql_error(client, query: str, headers: dict | None = None) -> dict:
    """Expect a GraphQL error; return the first error dict.

    Ariadne returns HTTP 400 for resolver-raised GraphQLErrors
    when the test client uses raise_server_exceptions=True.
    """
    resp = client.post("/graphql", json={"query": query}, headers=headers or {})
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "errors" in body and body["errors"], body
    return body["errors"][0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(serve_adapter_env):
    app = create_app(
        registry=serve_adapter_env["registry"],
        db_url=serve_adapter_env["db_url"],
        jwt_config=make_test_jwt_config(),
        security_enabled=True,
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def client_with_tiny_limits(serve_adapter_env):
    """Client with very small query limits (depth=2, fields=3) for testing guards."""
    app = create_app(
        registry=serve_adapter_env["registry"],
        db_url=serve_adapter_env["db_url"],
        jwt_config=make_test_jwt_config(),
        security_enabled=True,
        graphql_config=GraphQLConfig(
            query_max_depth=3, query_max_fields=3, query_max_limit=10000
        ),
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
            json={
                "query": (
                    "{ customers { nodes { customer_id first_name last_name } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        rows = data["data"]["customers"]["nodes"]
        assert len(rows) > 0
        assert "customer_id" in rows[0]
        assert "first_name" in rows[0]

    def test_query_all_orders(self, client):
        resp = client.post(
            "/graphql",
            json={"query": ("{ orders { nodes { order_id status } } }")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert len(data["data"]["orders"]["nodes"]) > 0

    def test_query_with_first(self, client):
        resp = client.post(
            "/graphql",
            json={"query": ("{ customers(first: 1) { nodes { customer_id } } }")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert len(data["data"]["customers"]["nodes"]) == 1

    def test_query_selected_fields_only(self, client):
        resp = client.post(
            "/graphql",
            json={"query": ("{ customers { nodes { first_name } } }")},
        )
        assert resp.status_code == 200
        data = resp.json()
        rows = data["data"]["customers"]["nodes"]
        assert all("first_name" in r for r in rows)
        assert all("customer_id" not in r for r in rows)

    def test_invalid_graphql_syntax_returns_error(self, client):
        resp = client.post("/graphql", json={"query": "{ not valid graphql {{{"})
        # Ariadne rejects parse-level failures at the HTTP layer with 400.
        assert resp.status_code == 400
        body = resp.json()
        assert "errors" in body and body["errors"]
        # The error must mention the syntax problem — not a silent empty response.
        assert (
            "Syntax" in body["errors"][0]["message"]
            or "syntax" in body["errors"][0]["message"]
        )

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
        assert "customersWhere" in type_names
        assert "ordersWhere" in type_names
        assert "customersOrderBy" in type_names
        assert "ordersOrderBy" in type_names

    def test_where_filter_end_to_end(self, client):
        resp = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(where: { customer_id: { _eq: 1 } }) "
                    "{ nodes { customer_id first_name } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        rows = data["data"]["customers"]["nodes"]
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    def test_where_in_filter(self, client):
        rows = _gql(
            client,
            "{ customers(where: { customer_id: { _in: [1, 2] } }) "
            "{ nodes { customer_id } } }",
        )["customers"]["nodes"]
        assert {r["customer_id"] for r in rows} == {1, 2}

    def test_where_logical_combinators(self, client):
        rows = _gql(
            client,
            "{ customers(where: { _or: ["
            "{ customer_id: { _eq: 1 } }, { customer_id: { _eq: 2 } }] }) "
            "{ nodes { customer_id } } }",
        )["customers"]["nodes"]
        assert {r["customer_id"] for r in rows} == {1, 2}

    def test_where_is_null(self, client):
        # _is_null: false on the PK should match every row.
        rows = _gql(
            client,
            "{ customers(where: { customer_id: { _is_null: false } }) "
            "{ nodes { customer_id } } }",
        )["customers"]["nodes"]
        assert len(rows) > 0

    def test_order_by_desc(self, client):
        rows = _gql(
            client,
            "{ customers(order_by: { customer_id: desc }, first: 3) "
            "{ nodes { customer_id } } }",
        )["customers"]["nodes"]
        ids = [r["customer_id"] for r in rows]
        assert ids == sorted(ids, reverse=True)

    def test_inline_aggregates_count(self, client):
        body = _gql(
            client,
            "{ customers { nodes { _aggregate { count } } } }",
        )
        assert isinstance(body["customers"]["nodes"][0]["_aggregate"]["count"], int)
        assert body["customers"]["nodes"][0]["_aggregate"]["count"] > 0

    def test_inline_aggregate_batched_single_round_trip(self, client):
        # All four fields must come back populated from one envelope —
        # they share a single DB round-trip via the batching future.
        body = _gql(
            client,
            "{ customers { nodes { _aggregate { count } } } "
            "orders(first: 1) { nodes { order_id } } }",
        )
        assert body["customers"]["nodes"][0]["_aggregate"]["count"] > 0
        assert len(body["orders"]["nodes"]) == 1
        assert "order_id" in body["orders"]["nodes"][0]

    def test_where_filter_no_match_returns_empty(self, client):
        resp = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(where: { customer_id: { _eq: 9999 } }) "
                    "{ nodes { customer_id } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        assert data["data"]["customers"]["nodes"] == []


class TestQueryGuardsHTTP:
    """Query guard limits (depth + field count) on the HTTP /graphql endpoint."""

    def test_query_within_limits_succeeds(self, client_with_tiny_limits):
        # customers → nodes → 2 leaves: depth=3, leaves=2 — fits depth=3, fields=3.
        resp = client_with_tiny_limits.post(
            "/graphql",
            json={"query": ("{ customers { nodes { customer_id first_name } } }")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")

    def test_query_exceeding_depth_returns_400(self, client_with_tiny_limits):
        # customers → nodes → orders → customer → customer_id: depth=4, exceeds limit of 3.
        resp = client_with_tiny_limits.post(
            "/graphql",
            json={
                "query": (
                    "{ customers { nodes { orders { customer { customer_id } } } } }"
                )
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "errors" in body and body["errors"]
        err = body["errors"][0]
        assert "depth" in err["message"].lower()
        assert "exceeds" in err["message"].lower()
        assert err["extensions"]["code"] == "MAX_DEPTH_EXCEEDED"

    def test_query_exceeding_field_count_returns_400(self, client_with_tiny_limits):
        # 5 leaf fields exceeds limit of 3.
        resp = client_with_tiny_limits.post(
            "/graphql",
            json={"query": ("{ customers { nodes { c1 c2 c3 c4 c5 } } }")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "errors" in body and body["errors"]
        err = body["errors"][0]
        assert "fields" in err["message"].lower()
        assert err["extensions"]["code"] == "MAX_FIELDS_EXCEEDED"

    def test_introspection_query_not_limited_by_depth(self, client_with_tiny_limits):
        # __schema introspection is excluded from depth counting
        resp = client_with_tiny_limits.post(
            "/graphql",
            json={"query": "{ __schema { types { name } } }"},
        )
        # depth 0 (excluded) should pass max_depth=3
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")


class TestAuthHTTP:
    """Bearer-token verification against the live mounted GraphQL app."""

    def test_invalid_signature_returns_401(self, client):
        bad = pyjwt.encode({"sub": "u"}, "wrong-secret", algorithm="HS256")
        resp = client.post(
            "/graphql",
            json={"query": ("{ customers { nodes { customer_id } } }")},
            headers={"Authorization": f"Bearer {bad}"},
        )
        assert resp.status_code == 401
        www = resp.headers["WWW-Authenticate"]
        assert www.startswith("Bearer ")
        assert 'error="invalid_token"' in www

    def test_garbage_token_returns_401(self, client):
        resp = client.post(
            "/graphql",
            json={"query": ("{ customers { nodes { customer_id } } }")},
            headers={"Authorization": "Bearer not.a.jwt"},
        )
        assert resp.status_code == 401
        assert 'error="invalid_token"' in resp.headers["WWW-Authenticate"]

    def test_missing_token_treated_as_anonymous(self, client):
        """No Authorization header → reaches resolvers as anonymous (200)."""
        resp = client.post(
            "/graphql",
            json={"query": ("{ customers { nodes { customer_id } } }")},
        )
        assert resp.status_code == 200

    def test_non_bearer_scheme_treated_as_anonymous(self, client):
        resp = client.post(
            "/graphql",
            json={"query": ("{ customers { nodes { customer_id } } }")},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Policy-aware fixtures
# ---------------------------------------------------------------------------

_ALL_CUST = "{ customers { nodes { customer_id first_name last_name } } }"


@pytest.fixture
def policy_client(serve_adapter_env):
    """Factory fixture: policy_client(policy) returns a TestClient with that policy."""

    def _make(policy: AccessPolicy | None = None):
        app = create_app(
            registry=serve_adapter_env["registry"],
            db_url=serve_adapter_env["db_url"],
            access_policy=policy,
            jwt_config=make_test_jwt_config(),
            security_enabled=True,
        )
        return TestClient(app, raise_server_exceptions=True)

    return _make


# ---------------------------------------------------------------------------
# Policy integration tests (PostgreSQL + MySQL)
# ---------------------------------------------------------------------------


def _full_access_policy(**overrides) -> AccessPolicy:
    """Baseline policy granting full access to customers + orders.

    Tests that want to assert a narrower policy for one table can pass
    ``customers=...`` or ``orders=...`` to override the default entry.
    """
    tables = {
        "customers": overrides.get(
            "customers",
            TablePolicy(column_level=ColumnLevelPolicy(include_all=True)),
        ),
        "orders": overrides.get(
            "orders",
            TablePolicy(column_level=ColumnLevelPolicy(include_all=True)),
        ),
    }
    return AccessPolicy(
        policies=[
            PolicyEntry(effect=Effect.ALLOW, name="all", when="True", tables=tables)
        ]
    )


class TestPolicyHTTP:
    """Full-chain policy tests: JWT → middleware → policy engine → SQL → response."""

    def test_no_policy_returns_all_columns(self, policy_client):
        """When access.yml is not configured at all, no enforcement runs."""
        with policy_client(None) as c:
            rows = _gql(c, _ALL_CUST)["customers"]["nodes"]
        assert len(rows) > 0
        assert all(
            r["first_name"] is not None and r["last_name"] is not None for r in rows
        )

    def test_include_all_allows_every_column(self, policy_client):
        with policy_client(_full_access_policy()) as c:
            rows = _gql(c, _ALL_CUST)["customers"]["nodes"]
        assert len(rows) > 0
        assert all(r["first_name"] is not None for r in rows)

    def test_excludes_strict_rejects_excluded_column(self, policy_client):
        policy = _full_access_policy(
            customers=TablePolicy(
                column_level=ColumnLevelPolicy(
                    include_all=True, excludes=["first_name", "last_name"]
                )
            )
        )
        with policy_client(policy) as c:
            err = _gql_error(c, _ALL_CUST)
        ext = err["extensions"]
        assert ext["code"] == "FORBIDDEN_COLUMN"
        assert ext["table"] == "customers"
        assert set(ext["columns"]) == {"first_name", "last_name"}

    def test_excludes_allowed_when_query_omits_them(self, policy_client):
        policy = _full_access_policy(
            customers=TablePolicy(
                column_level=ColumnLevelPolicy(
                    include_all=True, excludes=["first_name", "last_name"]
                )
            )
        )
        with policy_client(policy) as c:
            rows = _gql(
                c,
                "{ customers { nodes { customer_id } } }",
            )["customers"]["nodes"]
        assert len(rows) > 0
        assert all(r["customer_id"] is not None for r in rows)

    def test_includes_strict_rejects_unlisted_column(self, policy_client):
        policy = _full_access_policy(
            customers=TablePolicy(
                column_level=ColumnLevelPolicy(includes=["customer_id"])
            )
        )
        with policy_client(policy) as c:
            err = _gql_error(c, _ALL_CUST)
        ext = err["extensions"]
        assert ext["code"] == "FORBIDDEN_COLUMN"
        assert ext["table"] == "customers"
        assert set(ext["columns"]) == {"first_name", "last_name"}

    def test_null_mask_returns_null(self, policy_client):
        policy = _full_access_policy(
            customers=TablePolicy(
                column_level=ColumnLevelPolicy(
                    include_all=True, mask={"last_name": None}
                )
            )
        )
        with policy_client(policy) as c:
            rows = _gql(c, _ALL_CUST)["customers"]["nodes"]
        assert all("last_name" in r for r in rows)
        assert all(r["last_name"] is None for r in rows)
        assert all(r["first_name"] is not None for r in rows)

    def test_row_filter_restricts_rows(self, policy_client):
        policy = _full_access_policy(
            customers=TablePolicy(
                column_level=ColumnLevelPolicy(include_all=True),
                row_filter={"customer_id": {"_eq": {"jwt": "claims.cust_id"}}},
            )
        )
        with policy_client(policy) as c:
            rows = _gql(
                c, _ALL_CUST, headers=_bearer({"sub": "u1", "claims": {"cust_id": 1}})
            )["customers"]["nodes"]
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    def test_jwt_group_gates_column_restriction(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="analyst",
                    when="'analysts' in jwt.groups",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(
                                includes=["customer_id", "first_name", "last_name"]
                            )
                        )
                    },
                ),
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="finance",
                    when="'finance' in jwt.groups",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True)
                        )
                    },
                ),
            ]
        )
        with policy_client(policy) as c:
            # analyst — listed columns OK
            rows = _gql(
                c, _ALL_CUST, headers=_bearer({"sub": "u1", "groups": ["analysts"]})
            )["customers"]["nodes"]
            assert all(r["first_name"] is not None for r in rows)

            # finance — broader policy allows the same query
            rows = _gql(
                c, _ALL_CUST, headers=_bearer({"sub": "u2", "groups": ["finance"]})
            )["customers"]["nodes"]
            assert all(r["first_name"] is not None for r in rows)

    def test_anon_has_own_policy(self, policy_client):
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="anon",
                    when="jwt.sub == None",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(includes=["customer_id"])
                        )
                    },
                ),
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="auth",
                    when="jwt.sub != None",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True)
                        )
                    },
                ),
            ]
        )
        with policy_client(policy) as c:
            # Anonymous can see customer_id only
            rows = _gql(
                c,
                "{ customers { nodes { customer_id } } }",
            )["customers"]["nodes"]
            assert all(r["customer_id"] is not None for r in rows)

            # Authenticated user gets the broader policy
            rows = _gql(c, _ALL_CUST, headers=_bearer({"sub": "u1", "groups": []}))[
                "customers"
            ]["nodes"]
            assert all(r["first_name"] is not None for r in rows)

    def test_default_deny_table_without_policy_returns_forbidden(self, policy_client):
        """Querying a table the active policies do not cover → FORBIDDEN_TABLE."""
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="orders_only",
                    when="True",
                    tables={
                        "orders": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True)
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            err = _gql_error(c, _ALL_CUST)
        ext = err["extensions"]
        assert ext["code"] == "FORBIDDEN_TABLE"
        assert ext["table"] == "customers"

    def test_default_deny_when_no_clause_matches(self, policy_client):
        """Even if the table is listed somewhere, deny when no when-clause fires."""
        policy = AccessPolicy(
            policies=[
                PolicyEntry(
                    effect=Effect.ALLOW,
                    name="analyst",
                    when="'analysts' in jwt.groups",
                    tables={
                        "customers": TablePolicy(
                            column_level=ColumnLevelPolicy(include_all=True)
                        )
                    },
                )
            ]
        )
        with policy_client(policy) as c:
            err = _gql_error(
                c, _ALL_CUST, headers=_bearer({"sub": "u1", "groups": ["guest"]})
            )
        assert err["extensions"]["code"] == "FORBIDDEN_TABLE"

    def test_row_filter_on_orders(self, policy_client):
        policy = _full_access_policy(
            orders=TablePolicy(
                column_level=ColumnLevelPolicy(include_all=True),
                row_filter={"customer_id": {"_eq": {"jwt": "claims.cust_id"}}},
            )
        )
        with policy_client(policy) as c:
            rows = _gql(
                c,
                "{ orders { nodes { order_id status } } }",
                headers=_bearer({"sub": "u1", "claims": {"cust_id": 1}}),
            )["orders"]["nodes"]
        assert len(rows) > 0


# ---------------------------------------------------------------------------
# Pagination Tests
# ---------------------------------------------------------------------------


class TestPagination:
    """Integration tests for cursor-based pagination via the HTTP endpoint."""

    def test_first_returns_nodes_and_page_info(self, client):
        """Basic first query with order_by returns nodes array and pageInfo."""
        resp = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(first: 5, order_by: { customer_id: asc }) { nodes { customer_id } "
                    "pageInfo { hasNextPage endCursor } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["customers"]
        assert "nodes" in result
        assert "pageInfo" in result
        assert isinstance(result["nodes"], list)
        assert len(result["nodes"]) <= 5
        assert "hasNextPage" in result["pageInfo"]
        assert "endCursor" in result["pageInfo"]

    def test_first_with_order_by(self, client):
        """order_by enables cursor pagination and controls sort order."""
        resp = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(first: 3, order_by: { customer_id: asc }) "
                    "{ nodes { customer_id } "
                    "pageInfo { hasNextPage endCursor } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["customers"]
        assert len(result["nodes"]) == 3
        # With order_by we get proper cursors
        assert result["pageInfo"]["endCursor"] is not None
        assert result["pageInfo"]["hasNextPage"] is not None

    def test_has_next_page_true_when_more_rows(self, client):
        """When first=2 and more rows exist, hasNextPage is true."""
        resp = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(first: 2, order_by: { customer_id: asc }) "
                    "{ nodes { customer_id } "
                    "pageInfo { hasNextPage endCursor } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["customers"]
        # With more than 2 customers, hasNextPage should be True
        # (The jaffle shop has 5 customers by default)
        assert result["pageInfo"]["hasNextPage"] is True

    def test_has_next_page_false_at_end(self, client):
        """When first=100 and fewer rows exist, hasNextPage is false."""
        resp = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(first: 100, order_by: { customer_id: asc }) "
                    "{ nodes { customer_id } "
                    "pageInfo { hasNextPage endCursor } } }"
                )
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        result = data["data"]["customers"]
        assert result["pageInfo"]["hasNextPage"] is False

    def test_pagination_with_cursors(self, client):
        """Fetch first page, then next page using endCursor as after."""
        # First page
        resp1 = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(first: 2, order_by: { customer_id: asc }) "
                    "{ nodes { customer_id } "
                    "pageInfo { hasNextPage endCursor } } }"
                )
            },
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert "errors" not in data1, data1.get("errors")
        first_page = data1["data"]["customers"]
        first_ids = [n["customer_id"] for n in first_page["nodes"]]
        end_cursor = first_page["pageInfo"]["endCursor"]

        # Second page using endCursor as 'after'
        resp2 = client.post(
            "/graphql",
            json={
                "query": (
                    '{ customers(first: 2, after: "%s", order_by: { customer_id: asc }) '
                    "{ nodes { customer_id } "
                    "pageInfo { hasNextPage endCursor } } }" % end_cursor
                )
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert "errors" not in data2, data2.get("errors")
        second_page = data2["data"]["customers"]
        second_ids = [n["customer_id"] for n in second_page["nodes"]]

        # No overlap between pages
        assert len(set(first_ids) & set(second_ids)) == 0

    def test_query_default_limit(self, client):
        """When first is not specified, query_default_limit is applied."""
        resp = client.post(
            "/graphql",
            json={"query": "{ customers { nodes { customer_id } } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        # Should use default limit (100) and return up to 100 rows
        assert len(data["data"]["customers"]["nodes"]) <= 100

    def test_first_capped_at_max_limit(self, client_with_tiny_limits):
        """first=5000 with query_max_limit=10000 is allowed (below max)."""
        resp = client_with_tiny_limits.post(
            "/graphql",
            json={"query": "{ customers(first: 5000) { nodes { customer_id } } }"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "errors" not in data, data.get("errors")
        # Should be capped to 3 (query_max_fields for tiny limits)
        # But more importantly it should not error
        assert len(data["data"]["customers"]["nodes"]) <= 100

    def test_after_without_order_by_returns_error(self, client):
        """Using 'after' without 'order_by' raises an error."""
        # First get a valid cursor with order_by
        resp1 = client.post(
            "/graphql",
            json={
                "query": (
                    "{ customers(first: 1, order_by: { customer_id: asc }) "
                    "{ nodes { customer_id } pageInfo { endCursor } } }"
                )
            },
        )
        cursor = resp1.json()["data"]["customers"]["pageInfo"]["endCursor"]

        # Now try to use it without order_by
        # GraphQLError with raise_server_exceptions=True → HTTP 400
        resp2 = client.post(
            "/graphql",
            json={
                "query": (
                    '{ customers(after: "%s") '
                    "{ nodes { customer_id } pageInfo { endCursor } } }" % cursor
                )
            },
        )
        assert resp2.status_code == 400
        body = resp2.json()
        assert "errors" in body
        err = body["errors"][0]
        assert err["extensions"]["code"] == "CURSOR_REQUIRES_ORDER_BY"
