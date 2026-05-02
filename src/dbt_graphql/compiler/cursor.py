from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ClauseElement


def cursor_where_clause(
    aliased,  # aliased table
    order_by: list[tuple[str, str]],  # [(col_name, "asc"|"desc"), ...]
    cursor_values: dict[str, Any],  # {col_name: last_value}
) -> ClauseElement:
    """Chained-OR predicate: all rows that sort AFTER the cursor position."""
    clauses = []
    for i, (col_name, direction) in enumerate(order_by):
        equal_parts = [
            aliased.c[order_by[j][0]] == cursor_values[order_by[j][0]] for j in range(i)
        ]
        col = aliased.c[col_name]
        val = cursor_values[col_name]
        compare = col < val if direction == "desc" else col > val
        clauses.append(and_(*equal_parts, compare))
    return or_(*clauses)
