"""Structured row-filter DSL.

A typed boolean-expression tree compiled to a SQLAlchemy ``ColumnElement``.
Column names are validated against the table registry at policy-load time;
the runtime emit goes through SQLAlchemy's expression language so bind
parameters, NULL handling, and dialect quirks are not our concern.

Grammar (YAML / dict) — Hasura convention:

    row_filter:
      _and:
        - org_id: { _eq: { jwt: claims.org_id } }
        - _or:
            - is_public: { _eq: true }
            - owner_id: { _eq: { jwt: sub } }
        - status: { _in: [active, pending] }

Logical operators: ``_and``, ``_or``, ``_not``. Column-level operators:
``_eq``, ``_ne``, ``_lt``, ``_lte``, ``_gt``, ``_gte``, ``_in``,
``_is_null``. RHS values are literals or ``{ jwt: <dotted.path> }``
references that resolve from the request JWT at compile time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, bindparam, column, not_, or_
from sqlalchemy.sql.elements import ColumnElement

from .auth import JWTPayload


class RowFilterError(ValueError):
    """Raised on malformed DSL or unknown column / operator references."""


_LOGICAL = {"_and", "_or", "_not"}
_COMPARISON_OPS = {"_eq", "_ne", "_lt", "_lte", "_gt", "_gte"}
_COMPARISON = _COMPARISON_OPS | {"_in", "_is_null"}


def validate_row_filter(node: Any, *, allowed_columns: set[str], path: str = "") -> None:
    """Walk the filter tree at policy-load time. Reject unknown columns,
    unknown operators, and shape errors.
    """
    if not isinstance(node, dict):
        raise RowFilterError(
            f"row_filter at {path or '<root>'} must be a mapping, got {type(node).__name__}"
        )
    if not node:
        raise RowFilterError(f"row_filter at {path or '<root>'} is empty")

    for key, value in node.items():
        sub_path = f"{path}.{key}" if path else key
        if key in _LOGICAL:
            if key == "_not":
                validate_row_filter(value, allowed_columns=allowed_columns, path=sub_path)
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
                f"Logical operators are: {sorted(_LOGICAL)}"
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
    if op not in _COMPARISON:
        raise RowFilterError(
            f"{path}.{op}: unknown comparison operator. "
            f"Supported: {sorted(_COMPARISON)}"
        )
    if op == "_is_null":
        if not isinstance(value, bool):
            raise RowFilterError(f"{path}._is_null: expected a bool, got {value!r}")
        return
    if op == "_in":
        if not isinstance(value, list) or not value:
            raise RowFilterError(
                f"{path}._in: expected a non-empty list, got {value!r}"
            )
        for i, v in enumerate(value):
            _validate_value(v, path=f"{path}._in[{i}]")
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
    counter = [0]

    def _bind(value: Any) -> ColumnElement:
        i = counter[0]
        counter[0] = i + 1
        return bindparam(f"{prefix}_{i}", value)

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
            return col.is_(None) if raw else col.isnot(None)
        if op == "_in":
            return col.in_([_bind(_resolve(v)) for v in raw])
        bound = _bind(_resolve(raw))
        if op == "_eq":
            return col == bound
        if op == "_ne":
            return col != bound
        if op == "_lt":
            return col < bound
        if op == "_lte":
            return col <= bound
        if op == "_gt":
            return col > bound
        if op == "_gte":
            return col >= bound
        raise RowFilterError(f"unreachable: unknown comparison operator {op!r}")

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
