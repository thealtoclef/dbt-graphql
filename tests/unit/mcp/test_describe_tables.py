"""Tests for the MCP ``describe_tables`` tool."""

from pathlib import Path

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


def test_empty_list_returns_empty_sdl():
    tools = _tools()
    sdl = tools.describe_tables([])
    # No directive declarations or types — silent skip on empty input.
    assert "type customers " not in sdl
    assert "type orders " not in sdl


def test_unknown_name_silently_skipped():
    tools = _tools()
    sdl = tools.describe_tables(["nope_does_not_exist"])
    assert "nope_does_not_exist" not in sdl
    assert "type customers " not in sdl


def test_unauthorized_and_unknown_are_indistinguishable():
    """A table the caller's policy denies must produce the same output
    shape as a table that genuinely does not exist — both are silently
    skipped so the response cannot leak existence."""
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
    denied = tools.describe_tables(["orders"])
    unknown = tools.describe_tables(["nonexistent_xyz"])
    assert "type orders " not in denied
    assert "nonexistent_xyz" not in unknown


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
