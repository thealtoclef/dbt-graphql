"""Tests for GraphQL db.graphql generation."""

import json
from pathlib import Path

from dbt_graphql import extract_project, format_graphql


ARTIFACTS_DIR = (
    next(p for p in Path(__file__).parents if p.name == "tests")
    / "fixtures"
    / "dbt-artifacts"
)
CATALOG = ARTIFACTS_DIR / "catalog.json"
MANIFEST = ARTIFACTS_DIR / "manifest.json"


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
        assert "@relation(type: customers, fromField:" in gj.db_graphql
        assert "toField:" in gj.db_graphql
        assert "origin:" in gj.db_graphql
        assert "cardinality:" in gj.db_graphql

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
            assert "@table(" in gj.db_graphql

    def test_exclude_patterns(self):
        project = _make_project(exclude_patterns=[r"^stg_"])
        gj = format_graphql(project)
        assert "type stg_orders" not in gj.db_graphql
        assert "type customers" in gj.db_graphql

    def test_starts_without_directive_declarations(self):
        project = _make_project()
        gj = format_graphql(project)
        # First non-empty line is either a description block or a type.
        first_line = next(ln for ln in gj.db_graphql.splitlines() if ln.strip())
        assert not first_line.lstrip().startswith("directive ")

    def test_no_directive_declarations(self):
        project = _make_project()
        gj = format_graphql(project)
        assert "directive @" not in gj.db_graphql


def _sdl_col(
    name: str,
    sql_type: str,
    *,
    not_null: bool = False,
    is_pk: bool = False,
    is_unique: bool = False,
    relation=None,
) -> str:
    from dbt_graphql.graphql.sdl.generator import (
        _column_to_sdl,
        _parse_sql_type,
        _sql_to_gql_scalar,
    )
    from dbt_graphql.schema.models import ColumnDef

    base, size, is_array = _parse_sql_type(sql_type)
    col = ColumnDef(
        name=name,
        gql_type=_sql_to_gql_scalar(base),
        is_array=is_array,
        not_null=not_null,
        is_pk=is_pk,
        is_unique=is_unique,
        sql_type=base,
        sql_size=size,
        relation=relation,
    )
    return _column_to_sdl(col)


class TestTypeMapping:
    def test_standard_scalar_type_names(self):
        line = _sdl_col("id", "INTEGER", not_null=True)
        assert "id: Int!" in line
        assert '@column(type: "INTEGER")' in line

        line = _sdl_col("name", "VARCHAR(255)")
        assert "name: String" in line
        assert '@column(type: "VARCHAR", size: "255")' in line

    def test_multiword_types(self):
        line = _sdl_col("ts", "TIMESTAMP WITH TIME ZONE")
        assert "ts: String" in line
        assert '@column(type: "TIMESTAMP WITH TIME ZONE")' in line

    def test_array_type(self):
        line = _sdl_col("tags", "TEXT[]")
        assert "tags: [String]" in line
        assert '@column(type: "TEXT")' in line

    def test_bigquery_array(self):
        line = _sdl_col("items", "ARRAY<STRING>")
        assert "items: [String]" in line
        assert '@column(type: "STRING")' in line

    def test_empty_type_falls_back_to_string(self):
        line = _sdl_col("x", "")
        assert "x: String" in line
        assert '@column(type: "")' in line


class TestParseSqlType:
    def test_simple_type(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        assert _parse_sql_type("INTEGER") == ("INTEGER", "", False)

    def test_type_with_size(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        assert _parse_sql_type("VARCHAR(255)") == ("VARCHAR", "255", False)

    def test_numeric_with_precision_scale(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        assert _parse_sql_type("NUMERIC(10,2)") == ("NUMERIC", "10,2", False)

    def test_double_precision(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        assert _parse_sql_type("DOUBLE PRECISION") == ("DOUBLE PRECISION", "", False)

    def test_timestamp_with_time_zone(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        assert _parse_sql_type("TIMESTAMP WITH TIME ZONE") == (
            "TIMESTAMP WITH TIME ZONE",
            "",
            False,
        )

    def test_postgres_array(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        base, size, is_array = _parse_sql_type("TEXT[]")
        assert base == "TEXT"
        assert is_array is True
        assert size == ""

    def test_bigquery_array(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        base, size, is_array = _parse_sql_type("ARRAY<STRING>")
        assert base == "STRING"
        assert size == ""
        assert is_array is True

    def test_empty_string(self):
        from dbt_graphql.graphql.sdl.generator import _parse_sql_type

        assert _parse_sql_type("") == ("", "", False)


class TestColumnDirectives:
    def test_pk_keeps_native_scalar_with_id_directive(self):
        # PK columns retain their underlying scalar so ``{T}_bool_exp`` can
        # dispatch the right comparison_exp; PK signal moves to ``@id``.
        line = _sdl_col("id", "INTEGER", not_null=True, is_pk=True)
        assert "id: Int!" in line
        assert "@id" in line

    def test_non_pk_column_has_no_id_directive(self):
        for col_name in ("order_id", "item_id"):
            line = _sdl_col(col_name, "INTEGER", not_null=True, is_pk=False)
            assert ": Int" in line
            assert "@id" not in line

    def test_unique_directive(self):
        line = _sdl_col("email", "VARCHAR", is_unique=True)
        assert "@unique" in line

    def test_pk_column_does_not_get_unique_directive(self):
        line = _sdl_col("id", "INTEGER", not_null=True, is_pk=True, is_unique=False)
        assert "@id" in line
        assert "@unique" not in line

    def test_masked_directive_emitted_when_flag_set(self):
        from dbt_graphql.graphql.sdl.generator import _column_to_sdl
        from dbt_graphql.schema.models import ColumnDef

        col = ColumnDef(
            name="email", gql_type="String", sql_type="VARCHAR", masked=True
        )
        line = _column_to_sdl(col)
        assert "@masked" in line

    def test_masked_directive_absent_by_default(self):
        from dbt_graphql.graphql.sdl.generator import _column_to_sdl
        from dbt_graphql.schema.models import ColumnDef

        col = ColumnDef(name="email", gql_type="String", sql_type="VARCHAR")
        line = _column_to_sdl(col)
        assert "@masked" not in line

    def test_sql_directive_preserves_size(self):
        line = _sdl_col("price", "NUMERIC(10,2)")
        assert '@column(type: "NUMERIC", size: "10,2")' in line

    def test_relation_directive(self):
        from dbt_graphql.schema.models import RelationDef

        rel = RelationDef(
            target_model="customers",
            target_column="customer_id",
            from_columns=["customer_id"],
            to_columns=["customer_id"],
            origin="data_test",
            cardinality="many_to_one",
        )
        line = _sdl_col("customer_id", "INTEGER", not_null=True, relation=rel)
        assert (
            "@relation(type: customers, fromField: customer_id, toField: customer_id, cardinality: many_to_one, origin: data_test)"
            in line
        )


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


class TestTableDirectives:
    def test_filtered_directive_emitted_when_flag_set(self):
        from dbt_graphql.graphql.sdl.generator import _table_to_sdl
        from dbt_graphql.schema.models import TableDef

        sdl = _table_to_sdl(TableDef(name="X", filtered=True))
        assert "@filtered" in sdl

    def test_filtered_directive_absent_by_default(self):
        from dbt_graphql.graphql.sdl.generator import _table_to_sdl
        from dbt_graphql.schema.models import TableDef

        sdl = _table_to_sdl(TableDef(name="X"))
        assert "@filtered" not in sdl


class TestDescriptionEmission:
    def test_table_description_emitted_as_block(self):
        from dbt_graphql.graphql.sdl.generator import _table_to_sdl
        from dbt_graphql.schema.models import TableDef

        sdl = _table_to_sdl(TableDef(name="X", description="Customer accounts."))
        assert '"""' in sdl
        assert "Customer accounts." in sdl

    def test_column_description_emitted_as_block(self):
        from dbt_graphql.graphql.sdl.generator import _column_to_sdl, _table_to_sdl
        from dbt_graphql.schema.models import ColumnDef, TableDef

        col = ColumnDef(
            name="email",
            gql_type="String",
            sql_type="VARCHAR",
            description="User email.",
        )
        # _column_to_sdl is the field-line-only renderer; the description
        # comes from _table_to_sdl which composes the block above the field.
        sdl = _table_to_sdl(TableDef(name="X", columns=[col]))
        assert "User email." in sdl
        assert _column_to_sdl(col) in sdl


class TestRelationDirectiveMetadata:
    def test_relation_directive_has_origin(self):
        project = _make_project()
        gj = format_graphql(project)
        assert "origin:" in gj.db_graphql


class TestBuildRegistry:
    """build_registry produces the same logical schema as the SDL roundtrip."""

    def test_all_tables_present(self):
        from dbt_graphql.graphql.sdl.generator import build_registry

        project = _make_project()
        registry = build_registry(project)
        for model in project.models:
            assert model.name in registry

    def test_columns_match_model(self):
        from dbt_graphql.graphql.sdl.generator import build_registry

        project = _make_project()
        registry = build_registry(project)
        customers = registry.get("customers")
        assert customers is not None
        col_names = {c.name for c in customers.columns}
        assert "customer_id" in col_names
        assert "first_name" in col_names

    def test_pk_flag_set_for_sole_pk_model(self):
        from dbt_graphql.graphql.sdl.generator import build_registry
        from dbt_graphql.ir.models import ColumnInfo, ModelInfo, ProjectInfo

        col = ColumnInfo(name="id", type="INTEGER", not_null=True)
        model = ModelInfo(
            name="things",
            database="db",
            schema="public",
            columns=[col],
            primary_keys=["id"],
        )
        project = ProjectInfo(
            project_name="test",
            adapter_type="postgres",
            models=[model],
            relationships=[],
        )
        registry = build_registry(project)
        things = registry.get("things")
        assert things is not None
        pk_cols = [c for c in things.columns if c.is_pk]
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"

    def test_relation_wired(self):
        from dbt_graphql.graphql.sdl.generator import build_registry

        project = _make_project()
        if not project.relationships:
            return
        registry = build_registry(project)
        found_rel = False
        for table in registry:
            for col in table.columns:
                if col.relation is not None:
                    found_rel = True
                    assert col.relation.target_model
                    assert col.relation.target_column or col.relation.to_columns
        assert found_rel, "expected at least one relation to be wired"

    def test_table_database_and_schema_set(self):
        from dbt_graphql.graphql.sdl.generator import build_registry

        project = _make_project()
        registry = build_registry(project)
        for table in registry:
            assert table.database or table.schema or table.table

    def test_exclude_pattern_respected(self):
        from dbt_graphql.graphql.sdl.generator import build_registry

        project = _make_project(exclude_patterns=[r"^stg_"])
        registry = build_registry(project)
        assert registry.get("stg_orders") is None
        assert registry.get("customers") is not None

    def test_sdl_roundtrip_and_build_registry_same_tables(self):
        from dbt_graphql.graphql.sdl.generator import build_registry
        from dbt_graphql.schema import parse_db_graphql

        project = _make_project()
        gj = format_graphql(project)
        _, sdl_registry = parse_db_graphql(gj.db_graphql)
        direct_registry = build_registry(project)

        sdl_tables = {t.name for t in sdl_registry}
        direct_tables = {t.name for t in direct_registry}
        assert sdl_tables == direct_tables

    def test_sdl_roundtrip_and_build_registry_same_columns(self):
        from dbt_graphql.graphql.sdl.generator import build_registry
        from dbt_graphql.schema import parse_db_graphql

        project = _make_project()
        gj = format_graphql(project)
        _, sdl_registry = parse_db_graphql(gj.db_graphql)
        direct_registry = build_registry(project)

        for table in sdl_registry:
            direct_table = direct_registry.get(table.name)
            assert direct_table is not None, f"missing table {table.name}"
            sdl_cols = {c.name for c in table.columns}
            direct_cols = {c.name for c in direct_table.columns}
            assert sdl_cols == direct_cols, f"column mismatch in {table.name}"
