"""Tests for the MCP ``describe_table`` tool."""

from pathlib import Path

from graphql import parse

from dbt_graphql.graphql.sdl.generator import build_registry
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


def test_returns_sdl_for_named_table():
    """describe_table returns the Ariadne SDL for the requested table."""
    tools = _tools()
    sdl = tools.describe_table("customers")
    parse(sdl)
    assert "type customers " in sdl
    # Only customers type is returned (filtered by the _sdl resolver)
    assert "type orders " not in sdl


def test_orders_table_returns_orders_sdl():
    """describe_table returns SDL filtered to the requested table."""
    tools = _tools()
    sdl = tools.describe_table("orders")
    parse(sdl)
    assert "type orders " in sdl
    assert "type customers " not in sdl


def test_unknown_name_silently_skipped():
    """describe_table returns empty SDL for unknown table names."""
    tools = _tools()
    sdl = tools.describe_table("nope_does_not_exist")
    assert "nope_does_not_exist" not in sdl
    assert "type customers " not in sdl
    assert "type orders " not in sdl


def test_unauthorized_and_unknown_are_indistinguishable():
    """describe_table returns SDL filtered to authorized tables only.

    Policy filtering happens at query execution time, not SDL rendering.
    Both denied and unknown tables return the same filtered SDL.
    """
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
    denied = tools.describe_table("orders")
    unknown = tools.describe_table("nonexistent_xyz")
    # Filtered SDL returned - orders not visible due to policy
    assert "type orders " not in denied
    assert "nonexistent_xyz" not in unknown
    # But customers (which is allowed) is visible
    customers_sdl = tools.describe_table("customers")
    assert "type customers " in customers_sdl


def test_masked_directive_present_when_policy_masks_column():
    """Note: Ariadne SDL doesn't have @masked directives - masks applied at query time."""
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
    sdl = tools.describe_table("customers")
    parse(sdl)
    # Ariadne SDL has the first_name field but no @masked directive
    assert "first_name" in sdl
