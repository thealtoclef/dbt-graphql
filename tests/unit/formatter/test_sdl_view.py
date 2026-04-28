"""Tests for ``formatter.sdl_view`` — AST pruning + SDL rendering."""

from pathlib import Path

from graphql import parse

from dbt_graphql.formatter.graphql import build_registry, build_source_doc
from dbt_graphql.formatter.sdl_view import effective_document, render_sdl
from dbt_graphql.graphql.auth import JWTPayload
from dbt_graphql.graphql.effective import effective_registry
from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    Effect,
    PolicyEngine,
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


def _setup():
    project = extract_project(CATALOG, MANIFEST)
    registry = build_registry(project)
    doc = build_source_doc(registry)
    return registry, doc


def test_no_policy_returns_full_sdl():
    registry, doc = _setup()
    eff = effective_registry(registry, JWTPayload({}), None)
    out = render_sdl(effective_document(doc, eff))
    parse(out)  # must parse cleanly
    for table in registry:
        assert f"type {table.name} " in out


def test_denied_table_absent_from_output():
    registry, doc = _setup()
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
    eff = effective_registry(registry, JWTPayload({}), PolicyEngine(policy))
    out = render_sdl(effective_document(doc, eff))
    parse(out)
    assert "type customers " in out
    assert "type orders " not in out


def test_masked_directive_injected_on_masked_columns():
    registry, doc = _setup()
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
    eff = effective_registry(registry, JWTPayload({}), PolicyEngine(policy))
    out = render_sdl(effective_document(doc, eff))
    parse(out)
    # first_name carries @masked; customer_id does not.
    assert "first_name" in out
    line = next(line for line in out.splitlines() if "first_name" in line)
    assert "@masked" in line
    cust_line = next(line for line in out.splitlines() if "customer_id:" in line)
    assert "@masked" not in cust_line


def test_restrict_to_narrows_to_named_tables():
    registry, doc = _setup()
    eff = effective_registry(registry, JWTPayload({}), None)
    out = render_sdl(effective_document(doc, eff, restrict_to={"customers"}))
    assert "type customers " in out
    assert "type orders " not in out


def test_does_not_mutate_source_doc():
    registry, doc = _setup()
    eff = effective_registry(registry, JWTPayload({}), None)
    before = len(doc.definitions)
    effective_document(doc, eff, restrict_to={"customers"})
    assert len(doc.definitions) == before
