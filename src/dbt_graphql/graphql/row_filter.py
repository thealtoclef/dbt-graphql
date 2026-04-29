"""Structured row-filter DSL.

A typed boolean-expression tree compiled to a SQLAlchemy ``ColumnElement``.
Column names are validated against the table registry at policy-load time;
the runtime emit goes through SQLAlchemy's expression language so bind
parameters, NULL handling, and dialect quirks are not our concern.

Grammar (YAML / dict) — Hasura convention. The example below uses YAML
flow style (``{ ... }``) for compactness; block style is equivalent.

    row_filter:
      _and:
        - org_id: { _eq: { jwt: claims.org_id } }
        - _or:
            - is_public: { _eq: true }
            - owner_id: { _eq: { jwt: sub } }
        - status: { _in: [active, pending] }

Logical operators: ``_and``, ``_or``, ``_not``.
Column-level operators (Hasura vocab, cross-dialect via SQLAlchemy):
  ``_eq``, ``_neq``, ``_gt``, ``_gte``, ``_lt``, ``_lte``
  ``_in``, ``_nin``, ``_is_null``
  ``_like``, ``_nlike``, ``_ilike``, ``_nilike``
RHS values are literals (str/int/float/bool) or ``{ jwt: <dotted.path> }``
references that resolve from the request JWT at compile time.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, bindparam, column, not_, or_
from sqlalchemy.sql.elements import ColumnElement

from ..sql_ops import COMPARISON_OPS, LIST_OPS, LOGICAL_OPS, apply_comparison

if TYPE_CHECKING:
    from .auth import JWTPayload


class RowFilterError(ValueError):
    """Raised on malformed DSL or unknown column / operator references."""


def validate_row_filter(
    node: Any, *, allowed_columns: set[str], path: str = ""
) -> None:
    """Walk the filter tree at policy-load time. Reject unknown columns,
    unknown operators, mixed logical/column keys at one node, and shape
    errors. Raising here means the policy fails to load — operators see
    the typo at startup, not as a per-request runtime error.
    """
    if not isinstance(node, dict):
        raise RowFilterError(
            f"row_filter at {path or '<root>'} must be a mapping, got {type(node).__name__}"
        )
    if not node:
        raise RowFilterError(f"row_filter at {path or '<root>'} is empty")

    keys: list[str] = [str(k) for k in node.keys()]
    has_logical = any(k in LOGICAL_OPS for k in keys)
    has_column = any(not k.startswith("_") for k in keys)
    if has_logical and has_column:
        raise RowFilterError(
            f"{path or '<root>'}: a node cannot mix logical operators "
            f"({sorted(k for k in keys if k in LOGICAL_OPS)}) with column keys "
            f"({sorted(k for k in keys if not k.startswith('_'))}). "
            "Wrap the column key in an explicit `_and`."
        )
    if has_logical and len(keys) > 1:
        raise RowFilterError(
            f"{path or '<root>'}: only one logical operator per node, "
            f"got {sorted(keys)}. Nest them explicitly."
        )

    for key, value in node.items():
        sub_path = f"{path}.{key}" if path else key
        if key in LOGICAL_OPS:
            if key == "_not":
                validate_row_filter(
                    value, allowed_columns=allowed_columns, path=sub_path
                )
                continue
            if not isinstance(value, list) or not value:
                raise RowFilterError(
                    f"{sub_path} must be a non-empty list of sub-expressions"
                )
            for i, child in enumerate(value):
                validate_row_filter(
                    child, allowed_columns=allowed_columns, path=f"{sub_path}[{i}]"
                )
            continue

        if key.startswith("_"):
            raise RowFilterError(
                f"{sub_path}: unknown logical operator {key!r}. "
                f"Logical operators are: {sorted(LOGICAL_OPS)}"
            )
        if key not in allowed_columns:
            raise RowFilterError(
                f"{sub_path}: unknown column {key!r}. "
                f"Allowed columns on this table: {sorted(allowed_columns)}"
            )
        _validate_comparison(value, path=sub_path)


def _validate_comparison(node: Any, *, path: str) -> None:
    if not isinstance(node, dict):
        raise RowFilterError(
            f"{path}: column comparison must be a mapping like "
            f"{{ _eq: <value> }}, got {type(node).__name__}"
        )
    if len(node) != 1:
        raise RowFilterError(
            f"{path}: a column comparison must contain exactly one operator, "
            f"got {sorted(node.keys())}"
        )
    op, value = next(iter(node.items()))
    if op not in COMPARISON_OPS:
        raise RowFilterError(
            f"{path}.{op}: unknown comparison operator. "
            f"Supported: {sorted(COMPARISON_OPS)}"
        )
    if op == "_is_null":
        if not isinstance(value, bool):
            raise RowFilterError(f"{path}._is_null: expected a bool, got {value!r}")
        return
    if op in LIST_OPS:
        if not isinstance(value, list) or not value:
            raise RowFilterError(
                f"{path}.{op}: expected a non-empty list, got {value!r}"
            )
        for i, v in enumerate(value):
            if v is None:
                raise RowFilterError(
                    f"{path}.{op}[{i}]: NULL is not a valid list element "
                    "(SQL `IN (NULL)` never matches). Use `_is_null` for "
                    "null checks."
                )
            _validate_value(v, path=f"{path}.{op}[{i}]")
        return
    _validate_value(value, path=f"{path}.{op}")


def _validate_value(value: Any, *, path: str) -> None:
    """A bind value must be a literal scalar or a ``{jwt: <path>}`` reference."""
    if isinstance(value, dict):
        if set(value.keys()) != {"jwt"} or not isinstance(value["jwt"], str):
            raise RowFilterError(
                f"{path}: dict values must be exactly {{ jwt: '<dotted.path>' }}, "
                f"got {value!r}"
            )
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    raise RowFilterError(
        f"{path}: literal values must be scalar (str/int/float/bool/None) "
        f"or {{ jwt: '...' }} references, got {type(value).__name__}"
    )


def compile_row_filter(
    node: dict[str, Any], ctx: JWTPayload, *, prefix: str = "p"
) -> ColumnElement:
    """Compile a validated DSL tree to a SQLAlchemy ``ColumnElement``.

    The resulting clause drops directly into ``stmt.where(clause)``.
    Column names are emitted as bare identifiers via ``sqlalchemy.column``
    — they were validated at load time. JWT-referenced values are resolved
    eagerly here and bound as named parameters; literal values are bound
    the same way so the caller never sees raw values in SQL text.
    """
    counter = itertools.count()

    def _bind(value: Any) -> ColumnElement:
        return bindparam(f"{prefix}_{next(counter)}", value)

    def _resolve(value: Any) -> Any:
        if isinstance(value, dict) and set(value.keys()) == {"jwt"}:
            return _resolve_jwt_path(ctx, value["jwt"])
        return value

    def _walk(n: dict[str, Any]) -> ColumnElement:
        if "_and" in n:
            return and_(*(_walk(c) for c in n["_and"]))
        if "_or" in n:
            return or_(*(_walk(c) for c in n["_or"]))
        if "_not" in n:
            return not_(_walk(n["_not"]))

        col_name, comparison = next(iter(n.items()))
        op, raw = next(iter(comparison.items()))
        col = column(col_name)

        if op == "_is_null":
            value: Any = raw
        elif op in LIST_OPS:
            value = [_bind(_resolve(v)) for v in raw]
        else:
            value = _bind(_resolve(raw))

        try:
            return apply_comparison(col, op, value)
        except ValueError as exc:
            raise RowFilterError(str(exc)) from exc

    return _walk(node)


def _resolve_jwt_path(ctx: JWTPayload, dotted: str) -> Any:
    """Walk ``ctx`` along a dotted path; return None if any segment is missing.

    A missing claim becomes a SQL NULL bind value (the comparison then
    typically yields UNKNOWN, which fails the WHERE clause — default deny).
    """
    cur: Any = ctx
    for segment in dotted.split("."):
        if cur is None:
            return None
        cur = getattr(cur, segment, None)
    return cur
