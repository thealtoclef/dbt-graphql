"""Tests for dbt constraint extraction (primary keys, foreign keys)."""

from dbt_graphql.dbt.processors.constraints import (
    _parse_fk_expression,
    _resolve_to_model,
    extract_constraints,
)
from dbt_graphql.ir.models import Cardinality, ProcessorRelationship


# ---------------------------------------------------------------------------
# _parse_fk_expression
# ---------------------------------------------------------------------------


class TestParseFkExpression:
    def test_simple_table_column(self):
        result = _parse_fk_expression("customers(customer_id)")
        assert result == ("customers", "customer_id")

    def test_schema_qualified(self):
        result = _parse_fk_expression("public.customers(customer_id)")
        assert result == ("customers", "customer_id")

    def test_catalog_schema_qualified(self):
        result = _parse_fk_expression("mydb.public.customers(customer_id)")
        assert result == ("customers", "customer_id")

    def test_strips_double_quotes(self):
        result = _parse_fk_expression('"customers"("customer_id")')
        assert result == ("customers", "customer_id")

    def test_strips_backtick_quotes(self):
        result = _parse_fk_expression("`customers`(`customer_id`)")
        assert result == ("customers", "customer_id")

    def test_whitespace_trimmed(self):
        result = _parse_fk_expression("  customers( customer_id )  ")
        assert result == ("customers", "customer_id")

    def test_invalid_no_parens_returns_none(self):
        assert _parse_fk_expression("customers") is None

    def test_invalid_empty_returns_none(self):
        assert _parse_fk_expression("") is None

    def test_invalid_only_parens_returns_none(self):
        assert _parse_fk_expression("()") is None


# ---------------------------------------------------------------------------
# extract_constraints — helpers
# ---------------------------------------------------------------------------


class _FakeNode:
    def __init__(self, constraints=None, columns=None):
        self.constraints = constraints or []
        self.columns = columns or {}


class _FakeManifest:
    def __init__(self, nodes):
        self.nodes = nodes


def _manifest_with(**nodes):
    return _FakeManifest(nodes)


# ---------------------------------------------------------------------------
# extract_constraints
# ---------------------------------------------------------------------------


class TestExtractConstraints:
    def test_empty_manifest_returns_empty(self):
        manifest = _manifest_with()
        result = extract_constraints(manifest)
        assert result.primary_keys == {}
        assert result.foreign_key_relationships == []

    def test_non_model_nodes_ignored(self):
        manifest = _manifest_with(
            **{
                "test.project.some_test": _FakeNode(
                    constraints=[{"type": "primary_key", "columns": ["id"]}]
                )
            }
        )
        result = extract_constraints(manifest)
        assert result.primary_keys == {}

    def test_model_level_primary_key(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[{"type": "primary_key", "columns": ["order_id"]}]
                )
            }
        )
        result = extract_constraints(manifest)
        assert result.primary_keys[uid] == ["order_id"]

    def test_model_level_composite_primary_key(self):
        uid = "model.project.order_items"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[
                        {"type": "primary_key", "columns": ["order_id", "item_id"]}
                    ]
                )
            }
        )
        result = extract_constraints(manifest)
        assert result.primary_keys[uid] == ["order_id", "item_id"]

    def test_column_level_primary_key(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    columns={"order_id": {"constraints": [{"type": "primary_key"}]}}
                )
            }
        )
        result = extract_constraints(manifest)
        assert result.primary_keys[uid] == ["order_id"]

    def test_column_level_composite_primary_key(self):
        uid = "model.project.order_items"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    columns={
                        "order_id": {"constraints": [{"type": "primary_key"}]},
                        "item_id": {"constraints": [{"type": "primary_key"}]},
                    }
                )
            }
        )
        result = extract_constraints(manifest)
        assert set(result.primary_keys[uid]) == {"order_id", "item_id"}

    def test_model_and_column_level_pk_accumulate(self):
        # Both levels contribute — combined they describe the full composite key
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[{"type": "primary_key", "columns": ["order_id"]}],
                    columns={"another_id": {"constraints": [{"type": "primary_key"}]}},
                )
            }
        )
        result = extract_constraints(manifest)
        assert set(result.primary_keys[uid]) == {"order_id", "another_id"}

    def test_model_level_foreign_key(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "expression": "customers(customer_id)",
                        }
                    ]
                )
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 1
        rel = result.foreign_key_relationships[0]
        assert isinstance(rel, ProcessorRelationship)
        assert rel.models == ["orders", "customers"]
        assert rel.cardinality == Cardinality.many_to_one
        assert rel.from_columns == ["customer_id"]
        assert rel.to_columns == ["customer_id"]

    def test_column_level_foreign_key(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    columns={
                        "customer_id": {
                            "constraints": [
                                {
                                    "type": "foreign_key",
                                    "expression": "customers(id)",
                                }
                            ]
                        }
                    }
                )
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 1
        rel = result.foreign_key_relationships[0]
        assert rel.models == ["orders", "customers"]
        assert rel.from_columns == ["customer_id"]
        assert rel.to_columns == ["id"]

    def test_duplicate_fk_deduplicated(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "expression": "customers(customer_id)",
                        },
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "expression": "customers(customer_id)",
                        },
                    ]
                )
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 1

    def test_invalid_fk_expression_skipped(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "expression": "not_valid_expression",
                        }
                    ]
                )
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 0

    def test_rel_name_built_from_parts(self):
        uid = "model.project.orders"
        manifest = _manifest_with(
            **{
                uid: _FakeNode(
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "expression": "customers(customer_id)",
                        }
                    ]
                )
            }
        )
        result = extract_constraints(manifest)
        rel = result.foreign_key_relationships[0]
        assert rel.name == "orders_customer_id_customers_customer_id"


# ---------------------------------------------------------------------------
# _resolve_to_model
# ---------------------------------------------------------------------------


class _FakeNodeWithRelation:
    def __init__(self, relation_name=None):
        self.relation_name = relation_name


class TestResolveToModel:
    def test_resolves_by_relation_name(self):
        nodes = {
            "model.project.customers": _FakeNodeWithRelation(
                "jaffle_shop.main.customers"
            ),
        }
        result = _resolve_to_model("jaffle_shop.main.customers", nodes)
        assert result == "customers"

    def test_model_node_preferred_over_seed(self):
        nodes = {
            "seed.project.customers": _FakeNodeWithRelation(
                "jaffle_shop.main.customers"
            ),
            "model.project.customers": _FakeNodeWithRelation(
                "jaffle_shop.main.customers"
            ),
        }
        result = _resolve_to_model("jaffle_shop.main.customers", nodes)
        assert result == "customers"

    def test_unmatched_returns_none(self):
        nodes = {
            "model.project.orders": _FakeNodeWithRelation("jaffle_shop.main.orders"),
        }
        assert _resolve_to_model("jaffle_shop.main.customers", nodes) is None

    def test_empty_string_returns_none(self):
        assert _resolve_to_model("", {}) is None


# ---------------------------------------------------------------------------
# extract_constraints — to/to_columns format (dbt v1.9+)
# ---------------------------------------------------------------------------


class _FakeNodeWithRelationAndConstraints(_FakeNode):
    def __init__(self, relation_name=None, **kwargs):
        super().__init__(**kwargs)
        self.relation_name = relation_name


def _manifest_with_relation_names(**nodes):
    """Manifest whose nodes also have relation_name for _resolve_to_model."""
    return _FakeManifest(nodes)


class TestExtractConstraintsToFormat:
    def test_model_level_fk_via_to_field(self):
        customers_uid = "model.project.customers"
        orders_uid = "model.project.orders"
        manifest = _manifest_with_relation_names(
            **{
                customers_uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.customers"
                ),
                orders_uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.orders",
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "to": "mydb.main.customers",
                            "to_columns": ["id"],
                        }
                    ],
                ),
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 1
        rel = result.foreign_key_relationships[0]
        assert rel.models == ["orders", "customers"]
        assert rel.from_columns == ["customer_id"]
        assert rel.to_columns == ["id"]

    def test_column_level_fk_via_to_field(self):
        customers_uid = "model.project.customers"
        orders_uid = "model.project.orders"
        manifest = _manifest_with_relation_names(
            **{
                customers_uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.customers"
                ),
                orders_uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.orders",
                    columns={
                        "customer_id": {
                            "constraints": [
                                {
                                    "type": "foreign_key",
                                    "to": "mydb.main.customers",
                                    "to_columns": ["customer_id"],
                                }
                            ]
                        }
                    },
                ),
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 1
        rel = result.foreign_key_relationships[0]
        assert rel.models == ["orders", "customers"]

    def test_to_format_without_matching_node_yields_no_rel(self):
        uid = "model.project.orders"
        manifest = _manifest_with_relation_names(
            **{
                uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.orders",
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "to": "mydb.main.nonexistent",
                            "to_columns": ["id"],
                        }
                    ],
                ),
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 0

    def test_expression_format_takes_priority_over_to(self):
        customers_uid = "model.project.customers"
        orders_uid = "model.project.orders"
        manifest = _manifest_with_relation_names(
            **{
                customers_uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.customers"
                ),
                orders_uid: _FakeNodeWithRelationAndConstraints(
                    relation_name="mydb.main.orders",
                    constraints=[
                        {
                            "type": "foreign_key",
                            "columns": ["customer_id"],
                            "expression": "customers(customer_id)",
                            # to field also present but expression takes priority
                            "to": "mydb.main.customers",
                            "to_columns": ["id"],
                        }
                    ],
                ),
            }
        )
        result = extract_constraints(manifest)
        assert len(result.foreign_key_relationships) == 1
        rel = result.foreign_key_relationships[0]
        # expression-derived: to_col = "customer_id" (from expression), not "id" (from to_columns)
        assert rel.to_columns == ["customer_id"]
