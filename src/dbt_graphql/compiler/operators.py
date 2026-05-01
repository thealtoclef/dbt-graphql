"""Compile GraphQL operators to SQLAlchemy expressions.

This module translates GraphQL operator names to SQLAlchemy expressions.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import asc, desc, func, null
from sqlalchemy.sql.elements import ColumnElement


# ── Comparison operators ────────────────────────────────────────────────


def apply_comparison(col: ColumnElement, op: str, value: Any) -> ColumnElement:
    """Translate a comparison operator to a SQLAlchemy clause."""
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
    if op == "_regex":
        return col.regexp_match(value)
    if op == "_iregex":
        return col.regexp_match(f"(?i){value}")
    raise ValueError(f"unknown comparison operator {op!r}")


# ── Aggregate functions ─────────────────────────────────────────────────

AGG_FUNC_MAP: dict[str, Callable] = {
    "sum": func.sum,
    "avg": func.avg,
    "stddev": func.stddev,
    "var": func.variance,
    "min": func.min,
    "max": func.max,
}


# ── Order-by ────────────────────────────────────────────────────────────

ORDER_BY_MAP: dict[str, Callable] = {
    "asc": lambda c: asc(c),
    "desc": lambda c: desc(c),
}
