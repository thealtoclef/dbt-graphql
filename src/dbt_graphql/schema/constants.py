"""Shared constants for GraphQL type checking, operators, and query building."""

from __future__ import annotations

# GraphQL type constants
NUMERIC_GQL_TYPES = frozenset({"Int", "Float"})
STANDARD_GQL_SCALARS = frozenset({"String", "Int", "Float", "Boolean"})
AGGREGATE_FIELD = "_aggregate"

# GraphQL argument name for limit
LIMIT_ARG = "first"

# Operator constants
LOGICAL_OPS = frozenset({"_and", "_or", "_not"})
LIST_OPS = frozenset({"_in", "_nin"})

COMPARISON_OPS = frozenset(
    {
        "_eq",
        "_neq",
        "_gt",
        "_gte",
        "_lt",
        "_lte",
        "_is_null",
        "_like",
        "_nlike",
        "_ilike",
        "_nilike",
        "_regex",
        "_iregex",
    }
    | LIST_OPS
)

# Which comparison operators are valid for each GraphQL scalar type.
SCALAR_FILTER_OPS = {
    "String": frozenset(
        {
            "_eq",
            "_neq",
            "_gt",
            "_gte",
            "_lt",
            "_lte",
            "_in",
            "_nin",
            "_is_null",
            "_like",
            "_nlike",
            "_ilike",
            "_nilike",
            "_regex",
            "_iregex",
        }
    ),
    "Int": frozenset(
        {
            "_eq",
            "_neq",
            "_gt",
            "_gte",
            "_lt",
            "_lte",
            "_in",
            "_nin",
            "_is_null",
        }
    ),
    "Float": frozenset(
        {
            "_eq",
            "_neq",
            "_gt",
            "_gte",
            "_lt",
            "_lte",
            "_in",
            "_nin",
            "_is_null",
        }
    ),
    "Boolean": frozenset(
        {
            "_eq",
            "_neq",
            "_is_null",
        }
    ),
}

_OPS_TAKING_BOOL = frozenset({"_is_null"})

AGG_OPS = frozenset(
    {
        "count",
        "count_distinct",
        "sum",
        "avg",
        "stddev",
        "var",
        "min",
        "max",
    }
)

ORDER_DIRECTIONS = frozenset({"asc", "desc"})
