"""Integration tests for MCP live enrichment across PostgreSQL and MySQL.

Verifies that SchemaDiscovery produces correct row_count, sample_rows, and
per-column value_summary against real databases populated by jaffle-shop dbt.
"""

from __future__ import annotations

import pytest

from dbt_graphql.config import EnrichmentConfig
from dbt_graphql.mcp.discovery import SchemaDiscovery
from dbt_graphql.mcp.server import McpTools


class TestSchemaDiscoveryLiveEnrichment:
    @pytest.mark.asyncio
    async def test_row_count_is_positive(self, adapter_env):
        d = SchemaDiscovery(adapter_env.project, db=adapter_env.db)
        detail = await d.describe_table("orders")
        assert detail is not None
        assert isinstance(detail.row_count, int)
        assert detail.row_count > 0

    @pytest.mark.asyncio
    async def test_sample_rows_returned(self, adapter_env):
        d = SchemaDiscovery(adapter_env.project, db=adapter_env.db)
        detail = await d.describe_table("orders")
        assert detail is not None
        assert len(detail.sample_rows) == 3
        assert "order_id" in detail.sample_rows[0]

    @pytest.mark.asyncio
    async def test_status_is_enum_summary(self, adapter_env):
        d = SchemaDiscovery(adapter_env.project, db=adapter_env.db)
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
    async def test_low_cardinality_column_gets_distinct_summary(self, adapter_env):
        d = SchemaDiscovery(adapter_env.project, db=adapter_env.db)
        detail = await d.describe_table("stg_payments")
        assert detail is not None
        pm = next((c for c in detail.columns if c.name == "payment_method"), None)
        if pm is None:
            pytest.skip("stg_payments.payment_method not found in this fixture")
        assert pm.value_summary is not None, (
            "payment_method has low cardinality and must receive a value_summary"
        )
        assert pm.value_summary["kind"] in ("distinct", "enum")
        assert len(pm.value_summary["values"]) > 0

    @pytest.mark.asyncio
    async def test_cache_returns_same_object(self, adapter_env):
        d = SchemaDiscovery(adapter_env.project, db=adapter_env.db)
        first = await d.describe_table("customers")
        second = await d.describe_table("customers")
        assert first is second

    @pytest.mark.asyncio
    async def test_budget_zero_skips_non_enum_column_queries(self, adapter_env):
        d = SchemaDiscovery(
            adapter_env.project,
            db=adapter_env.db,
            enrichment=EnrichmentConfig(budget=0),
        )
        detail = await d.describe_table("stg_customers")
        assert detail is not None
        assert detail.row_count is not None
        assert detail.row_count > 0
        for col in detail.columns:
            if col.enum_values is None:
                assert col.value_summary is None

    @pytest.mark.asyncio
    async def test_budget_limits_column_queries(self, adapter_env):
        d = SchemaDiscovery(
            adapter_env.project,
            db=adapter_env.db,
            enrichment=EnrichmentConfig(budget=2),
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
    async def test_describe_table_response_has_row_count(self, adapter_env):
        tools = McpTools(adapter_env.project, db=adapter_env.db)
        result = await tools.describe_table("orders")
        assert result.get("row_count") is not None
        assert result["row_count"] > 0

    @pytest.mark.asyncio
    async def test_describe_table_response_has_sample_rows(self, adapter_env):
        tools = McpTools(adapter_env.project, db=adapter_env.db)
        result = await tools.describe_table("orders")
        assert len(result["sample_rows"]) == 3

    @pytest.mark.asyncio
    async def test_describe_table_enum_column_has_value_summary(self, adapter_env):
        tools = McpTools(adapter_env.project, db=adapter_env.db)
        result = await tools.describe_table("orders")
        status = next(c for c in result["columns"] if c["name"] == "status")
        assert status["value_summary"] is not None
        assert status["value_summary"]["kind"] == "enum"

    @pytest.mark.asyncio
    async def test_describe_table_column_has_value_summary_field(self, adapter_env):
        tools = McpTools(adapter_env.project, db=adapter_env.db)
        result = await tools.describe_table("customers")
        for col in result["columns"]:
            assert "value_summary" in col
