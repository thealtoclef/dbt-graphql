"""Unit tests for the structured row-filter DSL."""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from dbt_graphql.graphql.auth import JWTPayload
from dbt_graphql.graphql.row_filter import (
    RowFilterError,
    compile_row_filter,
    validate_row_filter,
)


def _ctx(**claims) -> JWTPayload:
    return JWTPayload({"claims": claims})


def _render(clause) -> tuple[str, dict]:
    """Compile a SQLAlchemy clause to (sql_text, bind_params) for assertions."""
    compiled = clause.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    return str(compiled), dict(compiled.params)


# ---------------------------------------------------------------------------
# validate_row_filter — load-time structural checks
# ---------------------------------------------------------------------------


_TABLE_COLUMNS = {"id", "org_id", "owner_id", "is_public", "status", "created_at"}


def test_validate_simple_eq_ok():
    validate_row_filter(
        {"org_id": {"_eq": {"jwt": "claims.org_id"}}},
        allowed_columns=_TABLE_COLUMNS,
    )


def test_validate_unknown_column_rejected():
    with pytest.raises(RowFilterError, match="unknown column 'orgg_id'"):
        validate_row_filter(
            {"orgg_id": {"_eq": 1}}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_unknown_logical_operator_rejected():
    with pytest.raises(RowFilterError, match="unknown logical operator"):
        validate_row_filter(
            {"_xor": [{"id": {"_eq": 1}}]}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_unknown_comparison_operator_rejected():
    with pytest.raises(RowFilterError, match="unknown comparison operator"):
        validate_row_filter(
            {"id": {"_like": "%foo"}}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_in_requires_non_empty_list():
    with pytest.raises(RowFilterError, match="non-empty list"):
        validate_row_filter(
            {"status": {"_in": []}}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_is_null_requires_bool():
    with pytest.raises(RowFilterError, match="expected a bool"):
        validate_row_filter(
            {"owner_id": {"_is_null": "yes"}}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_jwt_ref_rejects_extra_keys():
    with pytest.raises(RowFilterError, match=r"\{ jwt: '<dotted.path>' \}"):
        validate_row_filter(
            {"id": {"_eq": {"jwt": "sub", "extra": "no"}}},
            allowed_columns=_TABLE_COLUMNS,
        )


def test_validate_nested_logical_walks_recursively():
    validate_row_filter(
        {
            "_and": [
                {"org_id": {"_eq": {"jwt": "claims.org_id"}}},
                {
                    "_or": [
                        {"is_public": {"_eq": True}},
                        {"owner_id": {"_eq": {"jwt": "sub"}}},
                    ]
                },
            ]
        },
        allowed_columns=_TABLE_COLUMNS,
    )


def test_validate_not_walks_subtree():
    with pytest.raises(RowFilterError, match="unknown column 'bogus'"):
        validate_row_filter(
            {"_not": {"bogus": {"_eq": 1}}}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_empty_root_rejected():
    with pytest.raises(RowFilterError, match="empty"):
        validate_row_filter({}, allowed_columns=_TABLE_COLUMNS)


def test_validate_mixed_logical_and_column_keys_rejected():
    """A node mixing `_and` with a column key would silently drop the column
    branch at compile time — reject at load time."""
    with pytest.raises(RowFilterError, match="cannot mix logical operators"):
        validate_row_filter(
            {"_and": [{"id": {"_eq": 1}}], "org_id": {"_eq": 2}},
            allowed_columns=_TABLE_COLUMNS,
        )


def test_validate_multiple_logical_operators_at_one_node_rejected():
    with pytest.raises(RowFilterError, match="only one logical operator"):
        validate_row_filter(
            {"_and": [{"id": {"_eq": 1}}], "_or": [{"id": {"_eq": 2}}]},
            allowed_columns=_TABLE_COLUMNS,
        )


def test_validate_in_rejects_null_element():
    """SQL `IN (NULL)` never matches anything — reject at load time and tell
    the operator to use `_is_null` instead."""
    with pytest.raises(RowFilterError, match="NULL is not a valid"):
        validate_row_filter(
            {"status": {"_in": ["active", None]}}, allowed_columns=_TABLE_COLUMNS
        )


def test_validate_two_operators_in_one_comparison_rejected():
    with pytest.raises(RowFilterError, match="exactly one operator"):
        validate_row_filter(
            {"id": {"_eq": 1, "_lt": 5}}, allowed_columns=_TABLE_COLUMNS
        )


# ---------------------------------------------------------------------------
# compile_row_filter — emits SQLAlchemy ColumnElement
# ---------------------------------------------------------------------------


def test_compile_eq_with_jwt_ref():
    sql, params = _render(
        compile_row_filter(
            {"org_id": {"_eq": {"jwt": "claims.org_id"}}},
            _ctx(org_id=7),
        )
    )
    assert "org_id =" in sql
    assert params == {"p_0": 7}


def test_compile_eq_with_literal():
    sql, params = _render(
        compile_row_filter({"is_public": {"_eq": True}}, _ctx())
    )
    assert "is_public =" in sql
    assert params == {"p_0": True}


def test_compile_in_binds_each_element():
    """List claims do NOT collapse to a single bind param. Each element gets
    its own placeholder so the SQL is structurally correct on every dialect."""
    clause = compile_row_filter(
        {"status": {"_in": [{"jwt": "claims.role"}, "active", "pending"]}},
        _ctx(role="admin"),
    )
    sql, params = _render(clause)
    assert "status IN" in sql
    assert set(params.values()) == {"admin", "active", "pending"}
    assert len(params) == 3


def test_compile_is_null_emits_sql_keyword():
    sql, params = _render(
        compile_row_filter({"owner_id": {"_is_null": True}}, _ctx())
    )
    assert sql == "owner_id IS NULL"
    assert params == {}


def test_compile_is_not_null():
    sql, _ = _render(
        compile_row_filter({"owner_id": {"_is_null": False}}, _ctx())
    )
    assert sql == "owner_id IS NOT NULL"


def test_compile_and_or_not_tree():
    clause = compile_row_filter(
        {
            "_and": [
                {"org_id": {"_eq": {"jwt": "claims.org_id"}}},
                {
                    "_or": [
                        {"is_public": {"_eq": True}},
                        {"owner_id": {"_eq": {"jwt": "sub"}}},
                    ]
                },
                {"_not": {"status": {"_eq": "deleted"}}},
            ]
        },
        JWTPayload({"sub": "u1", "claims": {"org_id": 7}}),
    )
    sql, params = _render(clause)
    assert "org_id =" in sql
    assert " AND " in sql
    assert " OR " in sql
    # SA collapses NOT (col = X) into a != predicate; semantics preserved.
    assert "status != %(p_3)s" in sql or "NOT (status = %(p_3)s)" in sql
    assert set(params.values()) == {7, True, "u1", "deleted"}


def test_compile_missing_jwt_path_resolves_to_null():
    """Missing claim becomes a SQL NULL bind value (default-deny semantics)."""
    _sql, params = _render(
        compile_row_filter({"org_id": {"_eq": {"jwt": "claims.org_id"}}}, _ctx())
    )
    assert params == {"p_0": None}


def test_compile_jwt_value_is_bound_not_interpolated():
    """SQL injection regression: a malicious claim value must reach the
    parameter dict, never the rendered SQL string."""
    sql, params = _render(
        compile_row_filter(
            {"owner_id": {"_eq": {"jwt": "sub"}}},
            JWTPayload({"sub": "x'; DROP TABLE orders; --"}),
        )
    )
    assert "DROP TABLE" not in sql
    assert params == {"p_0": "x'; DROP TABLE orders; --"}


def test_compile_lt_lte_gt_gte():
    clause = compile_row_filter(
        {"_and": [
            {"id": {"_lt": 100}},
            {"id": {"_lte": 200}},
            {"id": {"_gt": 0}},
            {"id": {"_gte": 1}},
        ]},
        _ctx(),
    )
    sql, params = _render(clause)
    assert "id <" in sql and "id <=" in sql and "id >" in sql and "id >=" in sql
    assert set(params.values()) == {100, 200, 0, 1}


def test_compile_ne():
    sql, params = _render(
        compile_row_filter({"status": {"_ne": "deleted"}}, _ctx())
    )
    assert "status !=" in sql
    assert params == {"p_0": "deleted"}
