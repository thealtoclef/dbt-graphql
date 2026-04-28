"""Tests for ``graphql.effective.effective_registry``."""

from pathlib import Path

from dbt_graphql.formatter.graphql import build_registry
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


def _registry():
    return build_registry(extract_project(CATALOG, MANIFEST))


def test_no_policy_engine_returns_registry_unchanged():
    reg = _registry()
    out = effective_registry(reg, JWTPayload({}), None)
    assert out is reg


def test_denied_tables_dropped():
    reg = _registry()
    # Allow only "customers" — every other table is default-denied.
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="customers-only",
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
    pe = PolicyEngine(policy)
    out = effective_registry(reg, JWTPayload({}), pe)
    names = [t.name for t in out]
    assert names == ["customers"]


def test_blocked_columns_dropped_and_masked_flagged():
    reg = _registry()
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
    pe = PolicyEngine(policy)
    out = effective_registry(reg, JWTPayload({}), pe)
    cust = next(t for t in out if t.name == "customers")
    col_names = [c.name for c in cust.columns]
    assert col_names == ["customer_id", "first_name"]
    masked = {c.name: c.masked for c in cust.columns}
    assert masked == {"customer_id": False, "first_name": True}


def test_input_registry_not_mutated():
    reg = _registry()
    before = sorted(c.name for c in reg["customers"].columns)
    policy = AccessPolicy(
        policies=[
            PolicyEntry(
                name="cust",
                effect=Effect.ALLOW,
                when="True",
                tables={
                    "customers": TablePolicy(
                        column_level=ColumnLevelPolicy(includes=["customer_id"]),
                    ),
                },
            )
        ]
    )
    pe = PolicyEngine(policy)
    effective_registry(reg, JWTPayload({}), pe)
    after = sorted(c.name for c in reg["customers"].columns)
    assert before == after
