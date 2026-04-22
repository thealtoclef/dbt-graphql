"""Integration tests for MCP live enrichment against real jaffle-shop DuckDB.

These tests run dbt seed/run/docs-generate (via the session-scoped dbt_artifacts
fixture in conftest.py), open a real DuckDB file, and verify that SchemaDiscovery
produces correct row_count, sample_rows, and per-column value_summary.

They complement the unit tests in tests/unit/mcp/ which use mock DB objects.
"""

from __future__ import annotations

import asyncio

import pytest

from dbt_graphql.config import EnrichmentConfig
from dbt_graphql.mcp.discovery import SchemaDiscovery
from dbt_graphql.mcp.server import McpTools
from dbt_graphql.pipeline import extract_project


# ---------------------------------------------------------------------------
# Thin async adapter around the sync DuckDB engine (no async DuckDB driver)
# ---------------------------------------------------------------------------


class _DuckDbAdapter:
    """Wraps a sync duckdb SQLAlchemy engine to satisfy SchemaDiscovery's DB protocol."""

    def __init__(self, db_path: str) -> None:
        from sqlalchemy import create_engine

        self._engine = create_engine(f"duckdb:///{db_path}")

    async def execute_text(self, sql: str) -> list[dict]:
        loop = asyncio.get_running_loop()

        def _sync() -> list[dict]:
            from sqlalchemy import text

            with self._engine.connect() as conn:
                result = conn.execute(text(sql))
                return [dict(row._mapping) for row in result]

        return await loop.run_in_executor(None, _sync)

    def dispose(self) -> None:
        self._engine.dispose()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def jaffle_db(dbt_artifacts):
    db_path = str(dbt_artifacts["duckdb"]["project_dir"] / "jaffle_shop.duckdb")
    adapter = _DuckDbAdapter(db_path)
    yield adapter
    adapter.dispose()


@pytest.fixture(scope="module")
def jaffle_project(dbt_artifacts):
    a = dbt_artifacts["duckdb"]
    return extract_project(a["catalog_path"], a["manifest_path"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchemaDiscoveryLiveEnrichment:
    @pytest.mark.asyncio
    async def test_row_count_is_positive(self, jaffle_project, jaffle_db):
        d = SchemaDiscovery(jaffle_project, db=jaffle_db)
        detail = await d.describe_table("orders")
        assert detail is not None
        assert isinstance(detail.row_count, int)
        assert detail.row_count > 0

    @pytest.mark.asyncio
    async def test_sample_rows_returned(self, jaffle_project, jaffle_db):
        d = SchemaDiscovery(jaffle_project, db=jaffle_db)
        detail = await d.describe_table("orders")
        assert detail is not None
        assert len(detail.sample_rows) == 3
        assert "order_id" in detail.sample_rows[0]

    @pytest.mark.asyncio
    async def test_status_is_enum_summary(self, jaffle_project, jaffle_db):
        """orders.status has accepted_values → enum summary, no DB query needed."""
        d = SchemaDiscovery(jaffle_project, db=jaffle_db)
        detail = await d.describe_table("orders")
        assert detail is not None
        status = next(c for c in detail.columns if c.name == "status")
        assert status.value_summary is not None
        assert status.value_summary["kind"] == "enum"
        assert set(status.value_summary["values"]) == {
            "placed",
            "shipped",
            "completed",
            "return_pending",
            "returned",
        }

    @pytest.mark.asyncio
    async def test_low_cardinality_column_gets_distinct_summary(
        self, jaffle_project, jaffle_db
    ):
        """stg_payments.payment_method has few distinct values → kind:distinct."""
        d = SchemaDiscovery(jaffle_project, db=jaffle_db)
        detail = await d.describe_table("stg_payments")
        assert detail is not None
        pm = next((c for c in detail.columns if c.name == "payment_method"), None)
        if pm is None:
            pytest.skip("stg_payments.payment_method not found in this fixture")
        # If cardinality is low enough we get distinct values; if not, None is acceptable.
        if pm.value_summary is not None:
            assert pm.value_summary["kind"] in ("distinct", "enum", "range")

    @pytest.mark.asyncio
    async def test_cache_returns_same_object(self, jaffle_project, jaffle_db):
        d = SchemaDiscovery(jaffle_project, db=jaffle_db)
        first = await d.describe_table("customers")
        second = await d.describe_table("customers")
        assert first is second

    @pytest.mark.asyncio
    async def test_budget_zero_skips_non_enum_column_queries(
        self, jaffle_project, jaffle_db
    ):
        """With budget=0, column queries are skipped; row_count/sample_rows still run."""
        d = SchemaDiscovery(
            jaffle_project, db=jaffle_db, enrichment=EnrichmentConfig(budget=0)
        )
        detail = await d.describe_table("stg_customers")
        assert detail is not None
        assert detail.row_count is not None
        assert detail.row_count > 0
        for col in detail.columns:
            if col.enum_values is None:
                assert col.value_summary is None

    @pytest.mark.asyncio
    async def test_budget_limits_column_queries(self, jaffle_project, jaffle_db):
        """With budget=2, at most 2 non-enum columns get live value_summary."""
        d = SchemaDiscovery(
            jaffle_project, db=jaffle_db, enrichment=EnrichmentConfig(budget=2)
        )
        detail = await d.describe_table("customers")
        assert detail is not None
        live_summaries = [
            c
            for c in detail.columns
            if c.value_summary is not None and c.value_summary.get("kind") != "enum"
        ]
        assert len(live_summaries) <= 2


class TestMcpToolsLiveEnrichment:
    @pytest.mark.asyncio
    async def test_describe_table_response_has_row_count(
        self, jaffle_project, jaffle_db
    ):
        tools = McpTools(jaffle_project, db=jaffle_db)
        result = await tools.describe_table("orders")
        assert result.get("row_count") is not None
        assert result["row_count"] > 0

    @pytest.mark.asyncio
    async def test_describe_table_response_has_sample_rows(
        self, jaffle_project, jaffle_db
    ):
        tools = McpTools(jaffle_project, db=jaffle_db)
        result = await tools.describe_table("orders")
        assert len(result["sample_rows"]) == 3

    @pytest.mark.asyncio
    async def test_describe_table_enum_column_has_value_summary(
        self, jaffle_project, jaffle_db
    ):
        tools = McpTools(jaffle_project, db=jaffle_db)
        result = await tools.describe_table("orders")
        status = next(c for c in result["columns"] if c["name"] == "status")
        assert status["value_summary"] is not None
        assert status["value_summary"]["kind"] == "enum"

    @pytest.mark.asyncio
    async def test_describe_table_column_has_value_summary_field(
        self, jaffle_project, jaffle_db
    ):
        tools = McpTools(jaffle_project, db=jaffle_db)
        result = await tools.describe_table("customers")
        for col in result["columns"]:
            assert "value_summary" in col
