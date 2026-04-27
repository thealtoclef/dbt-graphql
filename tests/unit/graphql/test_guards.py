"""Unit tests for query guard limits (depth + field count)."""

from __future__ import annotations

import pytest

from dbt_graphql.graphql.guards import check_query_limits


class TestCheckQueryLimits:
    def test_query_within_depth_and_field_limits_returns_empty_list(self):
        q = "{ customers { customer_id first_name last_name } }"
        # customers is depth 1, 3 leaf fields
        assert check_query_limits(q, max_depth=5, max_fields=50) == []

    def test_query_exceeding_depth_returns_error(self):
        # customers(1) → orders(2) → line_items(3) → product(4) → supplier(5) → warehouse(6) → name(7)
        q = "{ customers { orders { line_items { product { supplier { warehouse { name } } } } } } }"
        errors = check_query_limits(q, max_depth=5, max_fields=50)
        assert len(errors) == 1
        assert "depth" in errors[0].lower()
        assert "exceeds" in errors[0].lower()
        assert "5" in errors[0]

    def test_query_exceeding_field_count_returns_error(self):
        # 15 leaf fields on customers
        q = "{ customers { c1 c2 c3 c4 c5 c6 c7 c8 c9 c10 c11 c12 c13 c14 c15 } }"
        errors = check_query_limits(q, max_depth=5, max_fields=10)
        assert len(errors) == 1
        assert "fields" in errors[0].lower()
        assert "exceeds" in errors[0].lower()
        assert "10" in errors[0]

    def test_query_exceeding_both_depth_and_fields_returns_both_errors(self):
        # depth 3 (customers→orders→l*) and 12 fields
        q = "{ customers { orders { l1 l2 l3 l4 l5 l6 l7 l8 l9 l10 l11 l12 } } }"
        errors = check_query_limits(q, max_depth=2, max_fields=5)
        assert len(errors) == 2
        messages_lower = [e.lower() for e in errors]
        assert any("depth" in m for m in messages_lower)
        assert any("fields" in m for m in messages_lower)

    def test_introspection_query_excluded_from_depth_count(self):
        # __schema introspection — excluded entirely from depth counting
        q = "{ __schema { types { name } } }"
        assert check_query_limits(q, max_depth=0, max_fields=50) == []

    def test_introspection_leaf_not_counted_as_field(self):
        # __schema introspection — introspection leaf fields are not counted
        q = "{ __schema { types { name kind description } } }"
        # depth 0 (excluded), introspection leaf fields not counted
        assert check_query_limits(q, max_depth=0, max_fields=0) == []

    def test_typename_inside_data_field_not_counted_for_depth(self):
        # __typename is introspection, so { customers { __typename } } is depth 1
        q = "{ customers { __typename } }"
        assert check_query_limits(q, max_depth=1, max_fields=50) == []
        # max_depth=0 should fail since customers is depth 1
        errors = check_query_limits(q, max_depth=0, max_fields=50)
        assert len(errors) == 1
        assert "depth" in errors[0].lower()

    def test_mixed_data_and_introspection_subfields(self):
        # customers has both a data field and __typename
        # After filtering __typename, first_name is a leaf at depth 2 (customers is depth 1,
        # but leaf fields inside a selection set are at depth+1 = 2)
        q = "{ customers { first_name __typename } }"
        # depth 2, max_depth=2 should pass
        assert check_query_limits(q, max_depth=2, max_fields=50) == []
        # depth 2, max_depth=1 should fail
        errors = check_query_limits(q, max_depth=1, max_fields=50)
        assert len(errors) == 1
        assert "depth" in errors[0].lower()

    def test_deep_nested_query_against_real_data_model(self):
        # customers(1) → orders(2) → line_items(3) → quantity(4)
        q = "{ customers { orders { line_items { quantity } } } }"
        # depth 4 should pass max_depth=4
        assert check_query_limits(q, max_depth=4, max_fields=50) == []
        # depth 4 should fail max_depth=3
        errors = check_query_limits(q, max_depth=3, max_fields=50)
        assert len(errors) == 1
        assert "depth" in errors[0].lower()

    def test_mutation_query_depth_from_selections(self):
        # Mutations count depth from their selection set
        q = "mutation { createCustomer { customer_id } }"
        # createCustomer(1) → customer_id(2)
        assert check_query_limits(q, max_depth=2, max_fields=50) == []

    def test_fragment_definition_depth_counted(self):
        # Fragment on a nested selection
        q = "fragment CFields on Customer { first_name last_name }"
        # first_name(1), last_name(1) — depth 1
        assert check_query_limits(q, max_depth=1, max_fields=50) == []
