"""Unit tests for query guard validation rules.

Exercises the rules at the same layer Ariadne and MCP run them: parse the
query into a ``DocumentNode``, then call ``graphql.validate(schema, doc,
rules)``. Errors carry ``extensions.code`` directly — no message-string
sniffing.
"""

from __future__ import annotations

import pytest
from graphql import build_schema, parse, validate

from dbt_graphql.graphql.guards import (
    MAX_DEPTH_CODE,
    MAX_FIELDS_CODE,
    MAX_LIMIT_CODE,
    make_query_guard_rules,
)


# A schema permissive enough for the queries below — names match what
# the rules walk syntactically; the rules don't care about the actual
# field types since they do AST-only checks.
SDL = """
    type Customer {
        customer_id: ID
        first_name: String
        last_name: String
        c1: String
        c2: String
        c3: String
        c4: String
        c5: String
        c6: String
        c7: String
        c8: String
        c9: String
        c10: String
        c11: String
        c12: String
        c13: String
        c14: String
        c15: String
        orders(limit: Int, first: Int): [Order]
    }
    type Order {
        order_id: ID
        status: String
        line_items: [LineItem]
    }
    type LineItem {
        item_id: ID
        quantity: Int
        product: Product
    }
    type Product {
        product_id: ID
        name: String
        supplier: Supplier
    }
    type Supplier {
        supplier_id: ID
        warehouse: Warehouse
    }
    type Warehouse {
        warehouse_id: ID
        name: String
    }
    type Query {
        customers(limit: Int, first: Int): [Customer]
    }
"""

SCHEMA = build_schema(SDL)


def _validate(query: str, *, max_depth=5, max_fields=50, max_limit=None):
    rules = make_query_guard_rules(
        max_depth=max_depth,
        max_fields=max_fields,
        max_limit=max_limit,
    )
    return validate(SCHEMA, parse(query), rules)


def _codes(errors) -> list[str]:
    return [(e.extensions or {}).get("code") for e in errors]


class TestQueryShape:
    def test_within_limits_returns_no_errors(self):
        q = "{ customers { customer_id first_name last_name } }"
        assert _validate(q, max_depth=5, max_fields=50) == []

    def test_depth_violation_emits_max_depth_code(self):
        q = (
            "{ customers { orders { line_items { product { supplier "
            "{ warehouse { name } } } } } } }"
        )
        errors = _validate(q, max_depth=5, max_fields=50)
        assert MAX_DEPTH_CODE in _codes(errors)

    def test_fields_violation_emits_max_fields_code(self):
        q = "{ customers { c1 c2 c3 c4 c5 c6 c7 c8 c9 c10 c11 c12 c13 c14 c15 } }"
        errors = _validate(q, max_depth=5, max_fields=10)
        assert MAX_FIELDS_CODE in _codes(errors)

    def test_both_violations_reported_independently(self):
        q = "{ customers { orders { c1: order_id c2: status } } }"
        errors = _validate(q, max_depth=2, max_fields=1)
        codes = _codes(errors)
        assert MAX_DEPTH_CODE in codes
        assert MAX_FIELDS_CODE in codes

    def test_introspection_only_query_excluded_from_depth(self):
        q = "{ __schema { types { name } } }"
        # depth 0 with introspection stripped → no error
        assert _validate(q, max_depth=0, max_fields=50) == []

    def test_introspection_leaf_inside_data_field_not_counted(self):
        q = "{ customers { __typename } }"
        # __typename stripped → customers becomes a leaf at depth 1
        assert _validate(q, max_depth=1, max_fields=50) == []
        errors = _validate(q, max_depth=0, max_fields=50)
        assert MAX_DEPTH_CODE in _codes(errors)

    def test_mixed_data_and_introspection_subfields(self):
        q = "{ customers { first_name __typename } }"
        # data leaf is at depth 2 after stripping __typename
        assert _validate(q, max_depth=2, max_fields=50) == []
        errors = _validate(q, max_depth=1, max_fields=50)
        assert MAX_DEPTH_CODE in _codes(errors)

    def test_real_data_model_depth(self):
        # customers(1) → orders(2) → line_items(3) → quantity(4)
        q = "{ customers { orders { line_items { quantity } } } }"
        assert _validate(q, max_depth=4, max_fields=50) == []
        errors = _validate(q, max_depth=3, max_fields=50)
        assert MAX_DEPTH_CODE in _codes(errors)

    def test_introspection_fragment_skips_depth_check(self):
        # GraphiQL / Apollo-Studio introspection queries declare fragments on
        # ``__Type`` and recursively spread ``ofType`` to ~8 levels. The walk
        # is over the meta-schema, not user data — depth must not apply.
        q = """
            query IntrospectionQuery {
              __schema {
                types { ...FullType }
              }
            }
            fragment FullType on __Type {
              kind
              name
              fields { type { ...TypeRef } }
            }
            fragment TypeRef on __Type {
              kind
              name
              ofType {
                kind
                name
                ofType { kind name ofType { kind name ofType { kind name } } }
              }
            }
        """
        assert _validate(q, max_depth=5, max_fields=50) == []

    def test_data_fragment_still_subject_to_depth(self):
        # A fragment on a non-introspection type must still be walked.
        q = """
            query Q { customers { ...Deep } }
            fragment Deep on Customer {
              orders { line_items { product { supplier { warehouse { name } } } } }
            }
        """
        errors = _validate(q, max_depth=3, max_fields=50)
        assert MAX_DEPTH_CODE in _codes(errors)

    def test_invalid_syntax_is_caller_responsibility(self):
        # Validation rules don't catch parse errors — graphql-core's parse()
        # raises before the rules run. Guards are only for well-formed docs.
        from graphql.error import GraphQLSyntaxError

        with pytest.raises(GraphQLSyntaxError):
            parse("{ not valid graphql {{{")


class TestLimitRule:
    def test_inline_limit_within_cap_passes(self):
        q = "{ customers(limit: 50) { customer_id } }"
        assert _validate(q, max_limit=100) == []

    def test_inline_limit_above_cap_emits_max_limit_code(self):
        q = "{ customers(limit: 1000000) { customer_id } }"
        errors = _validate(q, max_limit=1000)
        assert MAX_LIMIT_CODE in _codes(errors)

    def test_first_argument_also_capped(self):
        q = "{ customers(first: 5000) { customer_id } }"
        errors = _validate(q, max_limit=1000)
        assert MAX_LIMIT_CODE in _codes(errors)

    def test_nested_list_limit_capped(self):
        q = "{ customers { orders(limit: 10000) { order_id } } }"
        errors = _validate(q, max_limit=1000)
        assert MAX_LIMIT_CODE in _codes(errors)

    def test_no_cap_disables_rule(self):
        q = "{ customers(limit: 10000000) { customer_id } }"
        # max_limit=None → rule not registered
        assert _validate(q, max_limit=None) == []

    def test_variable_value_is_not_checked(self):
        # Variables bind at execution; validation runs before. Resolvers
        # are responsible for runtime caps when accepting variables.
        q = "query Q($n: Int) { customers(limit: $n) { customer_id } }"
        assert _validate(q, max_limit=10) == []
