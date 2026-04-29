"""End-to-end tests for the GraphQL ``_sdl: String!`` field.

Two callers, same boot, same field — different SDL slices because
their ``AccessPolicy`` views differ.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from graphql import execute, parse

from dbt_graphql.formatter.graphql import build_registry
from dbt_graphql.graphql.app import create_graphql_subapp
from dbt_graphql.graphql.auth import JWTPayload
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    Effect,
    PolicyEntry,
    TablePolicy,
)
from dbt_graphql.pipeline import extract_project


FIXTURES_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = FIXTURES_DIR / "catalog.json"
MANIFEST = FIXTURES_DIR / "manifest.json"


class _FakeDB:
    @property
    def dialect_name(self) -> str:
        return "postgresql"

    async def execute(self, stmt):
        return []


def _exec(bundle, payload: JWTPayload) -> str:
    ctx = bundle.build_context(payload)
    result = execute(bundle.schema, parse("{ _sdl }"), context_value=ctx)
    if asyncio.iscoroutine(result):
        result = asyncio.get_event_loop().run_until_complete(result)
    assert result.errors is None, result.errors
    return result.data["_sdl"]


def _bundle(access_policy=None):
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    return create_graphql_subapp(
        registry=registry,
        db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        access_policy=access_policy,
    )


def test_sdl_field_returns_full_sdl_with_no_policy():
    bundle = _bundle()
    sdl = _exec(bundle, JWTPayload({}))
    parse(sdl)
    assert "type customers " in sdl
    assert "type orders " in sdl
    assert "@table" in sdl


def test_sdl_field_reflects_caller_policy():
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust-only",
                effect=Effect.ALLOW,
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(
                            includes=["customer_id", "first_name"],
                            mask={"first_name": "NULL"},
                        ),
                    ),
                },
            )
        ]
    )
    bundle = _bundle(access_policy=policy)
    sdl = _exec(bundle, JWTPayload({}))
    parse(sdl)
    assert "type customers " in sdl
    assert "type orders " not in sdl
    masked_line = next(line for line in sdl.splitlines() if "first_name" in line)
    assert "@masked" in masked_line


def test_two_callers_same_boot_different_sdl():
    """Same bundle, two JWTs is not a feature here (policy ``when`` reads
    the JWT) — but verify the resolver does not cache cross-user."""
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="admin",
                effect=Effect.ALLOW,
                when="jwt.role == 'admin'",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True)
                    ),
                    "orders": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True)
                    ),
                },
            ),
            PolicyEntry(
                name="user",
                effect=Effect.ALLOW,
                when="jwt.role == 'user'",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True)
                    ),
                },
            ),
        ]
    )
    bundle = _bundle(access_policy=policy)
    admin_sdl = _exec(bundle, JWTPayload({"role": "admin"}))
    user_sdl = _exec(bundle, JWTPayload({"role": "user"}))
    assert "type orders " in admin_sdl
    assert "type orders " not in user_sdl
    assert "type customers " in user_sdl


def _exec_query(bundle, payload: JWTPayload, query: str):
    ctx = bundle.build_context(payload)
    result = execute(bundle.schema, parse(query), context_value=ctx)
    if asyncio.iscoroutine(result):
        result = asyncio.get_event_loop().run_until_complete(result)
    assert result.errors is None, result.errors  # ty: ignore[unresolved-attribute]
    return result.data  # ty: ignore[unresolved-attribute]


def test_sdl_with_tables_arg_filters_to_subset():
    bundle = _bundle()
    data = _exec_query(
        bundle,
        JWTPayload({}),
        '{ _sdl(tables: ["customers"]) }',
    )
    sdl = data["_sdl"]
    parse(sdl)
    assert "type customers " in sdl
    assert "type orders " not in sdl


def test_sdl_with_tables_arg_silently_skips_unknown_names():
    bundle = _bundle()
    data = _exec_query(
        bundle,
        JWTPayload({}),
        '{ _sdl(tables: ["customers", "does_not_exist"]) }',
    )
    sdl = data["_sdl"]
    parse(sdl)
    assert "type customers " in sdl
    assert "does_not_exist" not in sdl


def test_sdl_with_tables_arg_silently_skips_policy_hidden_names():
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust-only",
                effect=Effect.ALLOW,
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True),
                    ),
                },
            )
        ]
    )
    bundle = _bundle(access_policy=policy)
    data = _exec_query(
        bundle,
        JWTPayload({}),
        '{ _sdl(tables: ["customers", "orders"]) }',
    )
    sdl = data["_sdl"]
    parse(sdl)
    assert "type customers " in sdl
    assert "type orders " not in sdl


def test_sdl_with_empty_tables_arg_returns_empty_view():
    bundle = _bundle()
    data = _exec_query(bundle, JWTPayload({}), "{ _sdl(tables: []) }")
    sdl = data["_sdl"]
    assert "type customers " not in sdl
    assert "type orders " not in sdl


def test_tables_field_returns_visible_summaries():
    bundle = _bundle()
    data = _exec_query(bundle, JWTPayload({}), "{ _tables { name description } }")
    rows = data["_tables"]
    by_name = {r["name"]: r for r in rows}
    assert {"customers", "orders"} <= by_name.keys()
    # Every entry has both fields with the right shapes.
    for r in rows:
        assert isinstance(r["name"], str) and r["name"]
        assert isinstance(r["description"], str)  # may be empty


def test_tables_field_reflects_caller_policy():
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust-only",
                effect=Effect.ALLOW,
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True),
                    ),
                },
            )
        ]
    )
    bundle = _bundle(access_policy=policy)
    data = _exec_query(bundle, JWTPayload({}), "{ _tables { name } }")
    assert [r["name"] for r in data["_tables"]] == ["customers"]


def test_reserved_name_collision_rejects_tables_model():
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    # Force a collision by renaming an existing table to the reserved name.
    next(iter(registry)).name = "_tables"
    with pytest.raises(ValueError, match="_tables"):
        create_graphql_subapp(
            registry=registry,
            db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        )
