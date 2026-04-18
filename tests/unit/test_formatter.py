"""Tests for GraphQL db.graphql generation."""

import json
from pathlib import Path

from dbt_mdl import extract_project, format_graphql


DUCKDB_DIR = Path(__file__).parent.parent / "fixtures" / "dbt-artifacts"
CATALOG = DUCKDB_DIR / "catalog.json"
MANIFEST = DUCKDB_DIR / "manifest.json"


def _make_project(**kwargs):
    return extract_project(CATALOG, MANIFEST, **kwargs)


class TestDbGraphQL:
    def test_has_dbinfo_header(self):
        project = _make_project()
        gj = format_graphql(project)
        first_line = gj.db_graphql.splitlines()[0]
        assert first_line.startswith("# dbinfo:")
        assert "duckdb" in first_line

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
        assert "Integer!" in gj.db_graphql or "BigInt!" in gj.db_graphql

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

    def test_duckdb_preserved(self):
        project = _make_project()
        gj = format_graphql(project)
        first_line = gj.db_graphql.splitlines()[0]
        assert "duckdb" in first_line


class TestTypeMapping:
    def test_sql_to_gql_known_types(self):
        from dbt_mdl.graphql.formatter import _sql_to_gql_type

        assert _sql_to_gql_type("INTEGER") == ("Integer", "")
        assert _sql_to_gql_type("BIGINT") == ("BigInt", "")
        assert _sql_to_gql_type("SMALLINT") == ("SmallInt", "")
        assert _sql_to_gql_type("VARCHAR") == ("Varchar", "")
        assert _sql_to_gql_type("TEXT") == ("Text", "")
        assert _sql_to_gql_type("BOOLEAN") == ("Boolean", "")
        assert _sql_to_gql_type("DATE") == ("Date", "")
        assert _sql_to_gql_type("TIMESTAMP") == ("Timestamp", "")
        assert _sql_to_gql_type("JSONB") == ("Jsonb", "")
        assert _sql_to_gql_type("UUID") == ("Uuid", "")

    def test_size_is_extracted(self):
        from dbt_mdl.graphql.formatter import _sql_to_gql_type

        assert _sql_to_gql_type("VARCHAR(255)") == ("Varchar", "255")
        assert _sql_to_gql_type("NUMERIC(10,2)") == ("Numeric", "10,2")

    def test_multiword_types(self):
        from dbt_mdl.graphql.formatter import _sql_to_gql_type

        assert _sql_to_gql_type("TIMESTAMP WITH TIME ZONE") == (
            "TimestampWithTimeZone",
            "",
        )
        assert _sql_to_gql_type("CHARACTER VARYING") == ("CharacterVarying", "")
        assert _sql_to_gql_type("DOUBLE PRECISION") == ("DoublePrecision", "")

    def test_bigquery_aliases(self):
        from dbt_mdl.graphql.formatter import _sql_to_gql_type

        assert _sql_to_gql_type("INT64") == ("BigInt", "")
        assert _sql_to_gql_type("FLOAT64") == ("DoublePrecision", "")

    def test_array_detection(self):
        from dbt_mdl.graphql.formatter import _parse_sql_type

        assert _parse_sql_type("INTEGER[]") == ("integer", "", True)
        assert _parse_sql_type("TEXT[]") == ("text", "", True)
        assert _parse_sql_type("ARRAY<STRING>") == ("string", "", True)

    def test_array_renders_as_list(self):
        from dbt_mdl.graphql.formatter import _column_line
        from dbt_mdl.ir.models import ColumnInfo, ModelInfo

        m = ModelInfo(name="t", database="db", schema_="public", columns=[])
        c = ColumnInfo(name="tags", type="TEXT[]", not_null=False)
        line = _column_line(m, c, rel_map={})
        assert "[Text]" in line


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
