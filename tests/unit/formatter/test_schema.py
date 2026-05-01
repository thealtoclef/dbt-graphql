"""Tests for the SDL parser (schema.py)."""

from dbt_graphql.schema import parse_db_graphql


SDL = """\
type customers @table(database: mydb, schema: main, name: customers) {
  customer_id: Integer! @column(type: "INTEGER") @unique
  first_name: Varchar @column(type: "VARCHAR")
  last_name: Varchar @column(type: "VARCHAR")
}

type orders @table(database: mydb, schema: main, name: orders) {
  order_id: Integer! @column(type: "INTEGER") @id
  customer_id: Integer! @column(type: "INTEGER") @relation(type: customers, fromField: customer_id, toField: customer_id, cardinality: many_to_one, origin: data_test)
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
    """Parse @relation with origin and cardinality."""

    SDL_EXTENDED = """\
    type orders @table(database: db, schema: main, name: orders) {
      customer_id: Integer! @column(type: "INTEGER") @relation(fromField: customer_id, toField: customer_id, cardinality: many_to_one, origin: constraint)
    }
    """

    def test_origin_parsed(self):
        info, _ = parse_db_graphql(self.SDL_EXTENDED)
        col = info.tables[0].columns[0]
        assert col.relation.origin == "constraint"

    def test_cardinality_parsed(self):
        info, _ = parse_db_graphql(self.SDL_EXTENDED)
        col = info.tables[0].columns[0]
        assert col.relation.cardinality == "many_to_one"


class TestCompositeRelationDirective:
    """Parse @relation with fromField/toField for composite FKs."""

    SDL_COMPOSITE = """\
    type order_items @table(database: db, schema: main, name: order_items) {
      order_id: Integer! @column(type: "INTEGER") @relation(type: orders, fromField: [tenant_id, order_id], toField: [tenant_id, id], cardinality: many_to_one, origin: constraint)
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


class TestDescriptionRoundTrip:
    """SDL with triple-quoted descriptions parses back into description fields."""

    SDL_WITH_DESCRIPTIONS = '''\
"""
Customer accounts and contact info.
"""
type customers @table(database: db, schema: main, name: customers) {
  """The primary key."""
  customer_id: Int! @column(type: "INTEGER") @id
  """User's email address."""
  email: String @column(type: "VARCHAR")
}
'''

    def test_table_description_parsed(self):
        info, _ = parse_db_graphql(self.SDL_WITH_DESCRIPTIONS)
        assert info.tables[0].description == "Customer accounts and contact info."

    def test_column_description_parsed(self):
        info, _ = parse_db_graphql(self.SDL_WITH_DESCRIPTIONS)
        cols = {c.name: c for c in info.tables[0].columns}
        assert cols["customer_id"].description == "The primary key."
        assert cols["email"].description == "User's email address."


class TestPkDirectiveRoundTrip:
    """A column tagged with ``@id`` is recognised as PK; the underlying scalar
    is preserved so ``{T}_bool_exp`` dispatches by real type."""

    SDL_PK_DIRECTIVE = """\
type customers @table(database: db, schema: main, name: customers) {
  customer_id: Int! @column(type: "INTEGER") @id
  uuid_pk: String! @column(type: "UUID") @id
  name: String @column(type: "VARCHAR")
}
"""

    def test_id_directive_marks_pk(self):
        info, _ = parse_db_graphql(self.SDL_PK_DIRECTIVE)
        col = next(c for c in info.tables[0].columns if c.name == "customer_id")
        assert col.is_pk is True

    def test_pk_preserves_underlying_scalar(self):
        info, _ = parse_db_graphql(self.SDL_PK_DIRECTIVE)
        cols = {c.name: c for c in info.tables[0].columns}
        assert cols["customer_id"].gql_type == "Int"
        assert cols["uuid_pk"].gql_type == "String"

    def test_non_id_column_not_pk(self):
        info, _ = parse_db_graphql(self.SDL_PK_DIRECTIVE)
        col = next(c for c in info.tables[0].columns if c.name == "name")
        assert col.is_pk is False


class TestMaskedFilteredRoundTrip:
    """SDL with @masked / @filtered directives sets the corresponding flags."""

    SDL_FLAGGED = """\
type customers @table(database: db, schema: main, name: customers) @filtered {
  customer_id: Int! @column(type: "INTEGER") @id
  email: String @column(type: "VARCHAR") @masked
  name: String @column(type: "VARCHAR")
}
"""

    def test_filtered_flag_set_on_table(self):
        info, _ = parse_db_graphql(self.SDL_FLAGGED)
        assert info.tables[0].filtered is True

    def test_masked_flag_set_on_column(self):
        info, _ = parse_db_graphql(self.SDL_FLAGGED)
        col = next(c for c in info.tables[0].columns if c.name == "email")
        assert col.masked is True

    def test_unmasked_column_default_false(self):
        info, _ = parse_db_graphql(self.SDL_FLAGGED)
        col = next(c for c in info.tables[0].columns if c.name == "name")
        assert col.masked is False

    def test_filtered_default_false(self):
        sdl = 'type t @table(database: d, schema: s, name: t) { id: Int! @column(type: "INT") @id }'
        info, _ = parse_db_graphql(sdl)
        assert info.tables[0].filtered is False


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
