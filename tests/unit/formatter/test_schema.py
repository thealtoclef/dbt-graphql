"""Tests for the SDL parser (schema.py)."""

from dbt_graphql.formatter.schema import parse_db_graphql


SDL = """\
type customers @table(database: mydb, schema: main, name: customers) {
  customer_id: Integer! @column(type: "INTEGER") @unique
  first_name: Varchar @column(type: "VARCHAR")
  last_name: Varchar @column(type: "VARCHAR")
}

type orders @table(database: mydb, schema: main, name: orders) {
  order_id: Integer! @column(type: "INTEGER") @id
  customer_id: Integer! @column(type: "INTEGER") @relation(type: customers, field: customer_id)
  order_date: Date @column(type: "DATE")
  status: Varchar @column(type: "VARCHAR")
  tags: [Text] @column(type: "TEXT[]")
  amount: Varchar @column(type: "VARCHAR", size: "255")
}

type payments @table(database: mydb, schema: main, name: payments) {
  payment_id: Integer! @column(type: "INTEGER")
  secret: Text @column(type: "TEXT")
}
"""


def _parse():
    return parse_db_graphql(SDL)


class TestTableParsing:
    def test_all_tables_present(self):
        info, _ = _parse()
        names = [t.name for t in info.tables]
        assert names == ["customers", "orders", "payments"]

    def test_database_directive(self):
        info, _ = _parse()
        assert info.tables[0].database == "mydb"

    def test_schema_directive(self):
        info, _ = _parse()
        assert info.tables[0].schema == "main"

    def test_table_directive(self):
        info, _ = _parse()
        assert info.tables[0].table == "customers"

    def test_table_defaults_to_name(self):
        """If @table name arg is absent, table name defaults to the type name."""
        sdl = "type foo @table(database: db, schema: public) { id: Integer }"
        info, _ = parse_db_graphql(sdl)
        assert info.tables[0].table == "foo"


class TestColumnParsing:
    def test_not_null(self):
        info, _ = _parse()
        col = next(c for c in info.tables[0].columns if c.name == "customer_id")
        assert col.not_null is True

    def test_nullable(self):
        info, _ = _parse()
        col = next(c for c in info.tables[0].columns if c.name == "first_name")
        assert col.not_null is False

    def test_gql_type(self):
        info, _ = _parse()
        col = next(c for c in info.tables[0].columns if c.name == "customer_id")
        assert col.gql_type == "Integer"

    def test_array_type(self):
        info, _ = _parse()
        col = next(c for c in info.tables[1].columns if c.name == "tags")
        assert col.is_array is True
        assert col.gql_type == "Text"

    def test_sql_type_directive(self):
        info, _ = _parse()
        col = next(c for c in info.tables[1].columns if c.name == "amount")
        assert col.sql_type == "VARCHAR"
        assert col.sql_size == "255"


class TestDirectives:
    def test_id_directive(self):
        info, _ = _parse()
        col = next(c for c in info.tables[1].columns if c.name == "order_id")
        assert col.is_pk is True

    def test_unique_directive(self):
        info, _ = _parse()
        col = next(c for c in info.tables[0].columns if c.name == "customer_id")
        assert col.is_unique is True

    def test_relation_directive(self):
        info, _ = _parse()
        col = next(c for c in info.tables[1].columns if c.name == "customer_id")
        assert col.relation is not None
        assert col.relation.target_model == "customers"
        assert col.relation.target_column == "customer_id"


class TestExtendedRelationDirective:
    """Parse @relation with origin, confidence, composite fields, etc."""

    SDL_EXTENDED = """\
    type orders @table(database: db, schema: main, name: orders) {
      customer_id: Integer! @column(type: "INTEGER") @relation(type: customers, field: customer_id, origin: constraint, confidence: declared, name: "order customer", description: "FK to customers")
    }
    """

    def test_origin_parsed(self):
        info, _ = parse_db_graphql(self.SDL_EXTENDED)
        col = info.tables[0].columns[0]
        assert col.relation.origin == "constraint"

    def test_confidence_parsed(self):
        info, _ = parse_db_graphql(self.SDL_EXTENDED)
        col = info.tables[0].columns[0]
        assert col.relation.confidence == "declared"

    def test_business_name_parsed(self):
        info, _ = parse_db_graphql(self.SDL_EXTENDED)
        col = info.tables[0].columns[0]
        assert col.relation.business_name == "order customer"

    def test_description_parsed(self):
        info, _ = parse_db_graphql(self.SDL_EXTENDED)
        col = info.tables[0].columns[0]
        assert col.relation.description == "FK to customers"


class TestCompositeRelationDirective:
    """Parse @relation with fields/toFields for composite FKs."""

    SDL_COMPOSITE = """\
    type order_items @table(database: db, schema: main, name: order_items) {
      order_id: Integer! @column(type: "INTEGER") @relation(type: orders, fields: [tenant_id, order_id], toFields: [tenant_id, id])
    }
    """

    def test_from_columns_parsed(self):
        info, _ = parse_db_graphql(self.SDL_COMPOSITE)
        col = info.tables[0].columns[0]
        assert col.relation.from_columns == ["tenant_id", "order_id"]

    def test_to_columns_parsed(self):
        info, _ = parse_db_graphql(self.SDL_COMPOSITE)
        col = info.tables[0].columns[0]
        assert col.relation.to_columns == ["tenant_id", "id"]


class TestReverseRelationDirective:
    """Parse @reverseRelation fields into TableDef.reverse_relations."""

    SDL_REVERSE = """\
    type customers @table(database: db, schema: main, name: customers) {
      customer_id: Integer! @column(type: "INTEGER") @id
      orders: [orders] @reverseRelation(from: orders, via: "customer_id")
    }
    """

    def test_reverse_relation_parsed(self):
        info, _ = parse_db_graphql(self.SDL_REVERSE)
        assert len(info.tables[0].reverse_relations) == 1
        rev = info.tables[0].reverse_relations[0]
        assert rev.from_model == "orders"
        assert rev.via_column == "customer_id"


class TestRegistry:
    def test_get_existing(self):
        _, reg = _parse()
        assert reg.get("customers") is not None
        assert reg["customers"].name == "customers"

    def test_get_missing(self):
        _, reg = _parse()
        assert reg.get("nonexistent") is None

    def test_contains(self):
        _, reg = _parse()
        assert "orders" in reg
        assert "missing" not in reg

    def test_len(self):
        _, reg = _parse()
        assert len(reg) == 3

    def test_iter(self):
        _, reg = _parse()
        names = [t.name for t in reg]
        assert names == ["customers", "orders", "payments"]
