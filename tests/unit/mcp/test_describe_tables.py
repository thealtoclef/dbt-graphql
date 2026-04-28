"""Tests for the MCP ``describe_tables`` tool."""

from pathlib import Path

import pytest
from graphql import parse

from dbt_graphql.formatter.graphql import build_registry
from dbt_graphql.graphql.app import create_graphql_subapp
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    Effect,
    PolicyEntry,
    TablePolicy,
)
from dbt_graphql.mcp.server import McpTools
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


def _tools(access_policy=None):
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    bundle = create_graphql_subapp(
        registry=registry,
        db=_FakeDB(),  # ty: ignore[invalid-argument-type]
        access_policy=access_policy,
    )
    return McpTools(
        bundle.registry,
        bundle=bundle,
        policy_engine=bundle.policy_engine,
    )


def test_returns_sdl_for_named_tables():
    tools = _tools()
    sdl = tools.describe_tables(["customers"])
    parse(sdl)
    assert "type customers " in sdl
    assert "type orders " not in sdl
    assert "@table" in sdl


def test_multiple_tables():
    tools = _tools()
    sdl = tools.describe_tables(["customers", "orders"])
    parse(sdl)
    assert "type customers " in sdl
    assert "type orders " in sdl


def test_empty_list_rejected():
    tools = _tools()
    with pytest.raises(ValueError, match="at least one table name"):
        tools.describe_tables([])


def test_unknown_name_rejected_without_leaking_existence():
    tools = _tools()
    with pytest.raises(ValueError, match="unknown or unauthorized"):
        tools.describe_tables(["nope_does_not_exist"])


def test_unauthorized_table_rejected_same_message_as_unknown():
    """A table the caller's policy denies must surface the same error
    shape as a table that genuinely does not exist — otherwise the
    error itself leaks existence."""
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust-only",
                effect=Effect.ALLOW,
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True)
                    ),
                },
            )
        ]
    )
    tools = _tools(access_policy=policy)
    with pytest.raises(ValueError, match="unknown or unauthorized") as exc_real:
        tools.describe_tables(["orders"])  # exists in registry, denied by policy
    with pytest.raises(ValueError, match="unknown or unauthorized") as exc_fake:
        tools.describe_tables(["nonexistent_xyz"])
    # The error messages should be structurally identical except for the
    # offending name — same prefix, no policy-vs-existence distinction.
    assert "unknown or unauthorized" in str(exc_real.value)
    assert "unknown or unauthorized" in str(exc_fake.value)


def test_masked_directive_present_when_policy_masks_column():
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust",
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
    tools = _tools(access_policy=policy)
    sdl = tools.describe_tables(["customers"])
    parse(sdl)
    line = next(line for line in sdl.splitlines() if "first_name" in line)
    assert "@masked" in line
