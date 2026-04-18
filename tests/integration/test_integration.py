"""End-to-end integration tests.

For duckdb adapter the test:
1. Copies jaffle-shop into a temp directory
2. Runs dbt seed/run/docs-generate to produce catalog.json + manifest.json
3. Runs extract_project → format_graphql → parse_db_graphql → compile_query
4. Executes the compiled SQL against the real database
5. Asserts results
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from dbt_mdl.graphql.compiler import compile_query
from dbt_mdl.graphql.schema import TableRegistry, parse_db_graphql
from dbt_mdl.graphql.formatter import format_graphql
from dbt_mdl.pipeline import extract_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _field_node(name, selections=None):
    class FN:
        def __init__(self, n, sels=None):
            self.name = type("N", (), {"value": n})()
            self.selection_set = None
            if sels is not None:
                self.selection_set = type("SS", (), {"selections": sels})()
    return FN(name, selections)


def _relation_field_node(col_name, child_names):
    children = [_field_node(n) for n in child_names]
    return type("FN", (), {
        "name": type("N", (), {"value": col_name})(),
        "selection_set": type("SS", (), {"selections": children})(),
    })()


def _build_registry(catalog_path: Path, manifest_path: Path) -> TableRegistry:
    """Full pipeline: artifacts → ProjectInfo → db.graphql → TableRegistry."""
    project = extract_project(catalog_path, manifest_path)
    result = format_graphql(project)
    _, registry = parse_db_graphql(result.db_graphql)
    return registry


# ---------------------------------------------------------------------------
# E2E tests (duckdb only)
# ---------------------------------------------------------------------------


class TestE2E:
    """End-to-end tests against duckdb."""

    @pytest_asyncio.fixture
    async def registry(self, dbt_artifacts):
        artifacts = dbt_artifacts["duckdb"]
        return _build_registry(
            artifacts["catalog_path"], artifacts["manifest_path"],
        )

    @pytest.mark.asyncio
    async def test_select_customers(self, dbt_artifacts, db_connection, registry):
        fn = _field_node("customers", [
            _field_node("customer_id"),
            _field_node("first_name"),
            _field_node("last_name"),
        ])
        rows = await db_connection["duckdb"].execute(
            compile_query(registry["customers"], [fn], registry)
        )
        assert len(rows) > 0
        assert rows[0]["customer_id"] is not None
        assert "first_name" in rows[0]

    @pytest.mark.asyncio
    async def test_where_filter(self, dbt_artifacts, db_connection, registry):
        fn = _field_node("customers", [
            _field_node("customer_id"),
            _field_node("first_name"),
        ])
        stmt = compile_query(
            registry["customers"], [fn], registry, where={"customer_id": 1},
        )
        rows = await db_connection["duckdb"].execute(stmt)
        assert len(rows) == 1
        assert rows[0]["customer_id"] == 1

    @pytest.mark.asyncio
    async def test_limit(self, dbt_artifacts, db_connection, registry):
        fn = _field_node("customers", [_field_node("customer_id")])
        rows = await db_connection["duckdb"].execute(
            compile_query(registry["customers"], [fn], registry, limit=1)
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_orders_with_relation(self, dbt_artifacts, db_connection, registry):
        fn = _field_node("orders", [
            _field_node("order_id"),
            _field_node("status"),
            _relation_field_node("customer_id", ["customer_id", "first_name"]),
        ])
        rows = await db_connection["duckdb"].execute(
            compile_query(registry["orders"], [fn], registry)
        )
        assert len(rows) > 0
        assert rows[0]["order_id"] is not None
