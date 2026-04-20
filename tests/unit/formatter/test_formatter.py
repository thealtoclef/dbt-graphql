"""Tests for GraphQL db.graphql generation."""

import json
from pathlib import Path

from dbt_graphql import extract_project, format_graphql


DUCKDB_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = DUCKDB_DIR / "catalog.json"
MANIFEST = DUCKDB_DIR / "manifest.json"


def _make_project(**kwargs):
    return extract_project(CATALOG, MANIFEST, **kwargs)


class TestDbGraphQL:
    def test_has_types(self):
        project = _make_project()
        gj = format_graphql(project)
        assert "type customers" in gj.db_graphql
        assert "type orders" in gj.db_graphql

    def test_has_relation_directive(self):
        project = _make_project()
        gj = format_graphql(project)
        assert "@relation(type: customers, field: customer_id)" in gj.db_graphql

    def test_required_fields_have_bang(self):
        project = _make_project()
        gj = format_graphql(project)
        assert "Int!" in gj.db_graphql

    def test_all_models_present(self):
        project = _make_project()
        gj = format_graphql(project)
        for model in project.models:
            assert f"type {model.name}" in gj.db_graphql

    def test_non_public_schema_directive(self):
        project = _make_project()
        gj = format_graphql(project)
        if any(m.schema_ and m.schema_ != "public" for m in project.models):
            assert "@schema(name:" in gj.db_graphql

    def test_exclude_patterns(self):
        project = _make_project(exclude_patterns=[r"^stg_"])
        gj = format_graphql(project)
        assert "type stg_orders" not in gj.db_graphql
        assert "type customers" in gj.db_graphql

    def test_starts_with_type(self):
        project = _make_project()
        gj = format_graphql(project)
        first_line = gj.db_graphql.splitlines()[0]
        assert first_line.startswith("type ")


class TestTypeMapping:
    def test_standard_scalar_type_names(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])

        c = ColumnInfo(name="id", type="INTEGER", not_null=True)
        line = _column_line(m, c, rel_map={})
        assert "id: Int!" in line
        assert '@sql(type: "INTEGER")' in line

        c = ColumnInfo(name="name", type="VARCHAR(255)", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert "name: String" in line
        assert '@sql(type: "VARCHAR", size: "255")' in line

    def test_multiword_types(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])

        c = ColumnInfo(name="ts", type="TIMESTAMP WITH TIME ZONE", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert "ts: String" in line
        assert '@sql(type: "TIMESTAMP WITH TIME ZONE")' in line

    def test_array_type(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])

        c = ColumnInfo(name="tags", type="TEXT[]", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert "tags: [String]" in line
        assert '@sql(type: "TEXT")' in line

    def test_bigquery_array(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])

        c = ColumnInfo(name="items", type="ARRAY<STRING>", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert "items: [String]" in line
        assert '@sql(type: "STRING")' in line

    def test_empty_type_falls_back_to_string(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])
        c = ColumnInfo(name="x", type="", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert "x: String" in line
        assert '@sql(type: "")' in line


class TestParseSqlType:
    def test_simple_type(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        assert _parse_sql_type("INTEGER") == ("INTEGER", "", False)

    def test_type_with_size(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        assert _parse_sql_type("VARCHAR(255)") == ("VARCHAR", "255", False)

    def test_numeric_with_precision_scale(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        assert _parse_sql_type("NUMERIC(10,2)") == ("NUMERIC", "10,2", False)

    def test_double_precision(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        assert _parse_sql_type("DOUBLE PRECISION") == ("DOUBLE PRECISION", "", False)

    def test_timestamp_with_time_zone(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        assert _parse_sql_type("TIMESTAMP WITH TIME ZONE") == (
            "TIMESTAMP WITH TIME ZONE",
            "",
            False,
        )

    def test_postgres_array(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        base, size, is_array = _parse_sql_type("TEXT[]")
        assert base == "TEXT"
        assert is_array is True
        assert size == ""

    def test_bigquery_array(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        base, size, is_array = _parse_sql_type("ARRAY<STRING>")
        assert base == "STRING"
        assert is_array is True

    def test_empty_string(self):
        from dbt_graphql.formatter.graphql import _parse_sql_type

        assert _parse_sql_type("") == ("", "", False)


class TestColumnDirectives:
    def test_id_directive_on_primary_key(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(
            name="t", database="db", schema_="public", columns=[], primary_keys=["id"]
        )
        c = ColumnInfo(name="id", type="INTEGER", not_null=True)
        line = _column_line(m, c, rel_map={})
        assert "@id" in line

    def test_composite_pk_parts_do_not_get_id_directive(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(
            name="t",
            database="db",
            schema_="public",
            columns=[],
            primary_keys=["order_id", "item_id"],
        )
        for col_name in ("order_id", "item_id"):
            c = ColumnInfo(name=col_name, type="INTEGER", not_null=True)
            line = _column_line(m, c, rel_map={})
            assert "@id" not in line, (
                f"composite PK column {col_name} should not get @id"
            )

    def test_unique_directive(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])
        c = ColumnInfo(name="email", type="VARCHAR", not_null=False, unique=True)
        line = _column_line(m, c, rel_map={})
        assert "@unique" in line

    def test_pk_column_does_not_get_unique_directive(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(
            name="t", database="db", schema_="public", columns=[], primary_keys=["id"]
        )
        c = ColumnInfo(name="id", type="INTEGER", not_null=True, unique=True)
        line = _column_line(m, c, rel_map={})
        assert "@id" in line
        assert "@unique" not in line

    def test_sql_directive_preserves_size(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])
        c = ColumnInfo(name="price", type="NUMERIC(10,2)", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert '@sql(type: "NUMERIC", size: "10,2")' in line

    def test_relation_directive(self):
        from dbt_graphql.formatter.graphql import _column_line
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="orders", database="db", schema_="public", columns=[])
        c = ColumnInfo(name="customer_id", type="INTEGER", not_null=True)
        rel_map = {("orders", "customer_id"): ("customers", "customer_id")}
        line = _column_line(m, c, rel_map=rel_map)
        assert "@relation(type: customers, field: customer_id)" in line


class TestNoRelationships:
    def test_no_relationships_still_works(self, tmp_path):
        data = json.loads(MANIFEST.read_text())
        keys_to_remove = [
            k for k in data["nodes"] if k.startswith("test.") and "relationships" in k
        ]
        for k in keys_to_remove:
            del data["nodes"][k]

        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(data))

        project = extract_project(CATALOG, manifest_path)
        assert len(project.relationships) == 0

        gj = format_graphql(project)
        assert "@relation" not in gj.db_graphql
