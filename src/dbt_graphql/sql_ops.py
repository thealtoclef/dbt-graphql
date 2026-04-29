"""Hasura-vocab → SQLAlchemy translation.

Single source of truth for comparison-operator dispatch shared by:
- ``compiler.query`` (GraphQL ``{T}_bool_exp`` WHERE/ORDER BY)
- ``graphql.row_filter`` (policy row-filter DSL)

Lives at the top level (not under ``compiler/``) so importing it does not
re-enter ``compiler/__init__.py`` — that would cycle through ``query`` →
``graphql.policy`` → ``graphql.row_filter`` → here.

Both modules accept the same Hasura operator names (``_eq``, ``_neq``,
``_in``, ``_like``, …); only the *value* form differs (literal vs.
``bindparam``). SQLAlchemy treats them identically, so the dispatch
table is the same for both call sites.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import null
from sqlalchemy.sql.elements import ColumnElement

LOGICAL_OPS: frozenset[str] = frozenset({"_and", "_or", "_not"})

# Operators whose RHS is a list (rather than a scalar / bool / bindparam).
LIST_OPS: frozenset[str] = frozenset({"_in", "_nin"})

# All supported column-level comparison operators.
COMPARISON_OPS: frozenset[str] = frozenset(
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
    }
    | LIST_OPS
)


def apply_comparison(col: ColumnElement, op: str, value: Any) -> ColumnElement:
    """Translate a Hasura comparison op into a SQLAlchemy clause.

    ``value`` is whatever the caller already prepared:
    - a Python literal (SQLAlchemy auto-binds it on ``col == value``),
    - a pre-built ``bindparam`` (used by the policy row-filter for
      named, JWT-resolved binds),
    - a ``list`` of either of the above for ``_in`` / ``_nin``,
    - a ``bool`` for ``_is_null``.
    """
    if op == "_eq":
        return col == value
    if op == "_neq":
        return col != value
    if op == "_gt":
        return col > value
    if op == "_gte":
        return col >= value
    if op == "_lt":
        return col < value
    if op == "_lte":
        return col <= value
    if op == "_in":
        return col.in_(value)
    if op == "_nin":
        return col.not_in(value)
    if op == "_is_null":
        return col.is_(null()) if value else col.is_not(null())
    if op == "_like":
        return col.like(value)
    if op == "_nlike":
        return col.not_like(value)
    if op == "_ilike":
        return col.ilike(value)
    if op == "_nilike":
        return col.not_ilike(value)
    raise ValueError(f"unknown comparison operator {op!r}")
