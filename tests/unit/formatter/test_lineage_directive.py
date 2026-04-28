"""Tests for the ``@lineage`` directive (type + field level).

The directive surfaces dbt-derived lineage in the GraphQL SDL so the same
schema text is the source of truth for ``--output``, the ``_sdl`` field,
and the MCP ``describe_tables`` tool.
"""

from pathlib import Path

from dbt_graphql.formatter.graphql import build_registry, build_source_doc
from dbt_graphql.formatter.schema import parse_db_graphql
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


def test_type_level_lineage_emitted():
    _, doc = _setup()
    sdl = render_sdl(doc)
    assert '@lineage(sources: ["stg_customers", "stg_orders", "stg_payments"])' in sdl


def test_field_level_lineage_emitted_with_type():
    _, doc = _setup()
    sdl = render_sdl(doc)
    assert (
        '@lineage(source: "stg_customers", column: "customer_id", type: pass_through)'
        in sdl
    )
    assert (
        '@lineage(source: "stg_orders", column: "order_date", type: transformation)'
        in sdl
    )


def test_lineage_roundtrips_through_parse_db_graphql():
    _, doc = _setup()
    sdl = render_sdl(doc)
    _, reparsed = parse_db_graphql(sdl)
    customers = reparsed["customers"]
    assert customers.lineage_sources == [
        "stg_customers",
        "stg_orders",
        "stg_payments",
    ]
    cust_id = next(c for c in customers.columns if c.name == "customer_id")
    assert len(cust_id.lineage) == 1
    ref = cust_id.lineage[0]
    assert ref.source == "stg_customers"
    assert ref.column == "customer_id"
    assert ref.type == "pass_through"


def test_effective_document_strips_denied_lineage_sources():
    """A caller who can only see ``customers`` and ``stg_customers`` must
    not see lineage refs to the other (denied) upstream models."""
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
                    "stg_customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True)
                    ),
                },
            )
        ]
    )
    eff = effective_registry(registry, JWTPayload({}), PolicyEngine(policy))
    pruned = effective_document(doc, eff)
    sdl = render_sdl(pruned)
    customers_block = sdl.split("type customers")[1].split("\n}")[0]
    # type-level: hidden sources removed; visible source kept.
    assert "stg_orders" not in customers_block
    assert "stg_payments" not in customers_block
    assert '"stg_customers"' in customers_block
    # field-level: no @lineage(source: "stg_orders" | "stg_payments", ...).
    assert '@lineage(source: "stg_orders"' not in sdl
    assert '@lineage(source: "stg_payments"' not in sdl
    # field-level lineage to the visible upstream survives.
    assert '@lineage(source: "stg_customers"' in sdl


def test_effective_document_strips_lineage_for_hidden_upstream_column():
    """When an upstream column is excluded by policy, field-level
    ``@lineage`` directives that point at it must be dropped — even if
    the upstream model itself is still visible."""
    registry, doc = _setup()
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust-no-id",
                effect=Effect.ALLOW,
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True)
                    ),
                    "stg_customers": TablePolicy(
                        column_level=ColumnLevelPolicy(excludes=["customer_id"])
                    ),
                },
            )
        ]
    )
    eff = effective_registry(registry, JWTPayload({}), PolicyEngine(policy))
    pruned = effective_document(doc, eff)
    sdl = render_sdl(pruned)
    # The directive referencing the hidden upstream column is gone.
    assert '@lineage(source: "stg_customers", column: "customer_id"' not in sdl
