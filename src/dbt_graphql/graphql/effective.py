"""The effective view of a TableRegistry for a single caller.

The effective registry contains only the tables and columns the caller
is allowed to see after every visibility rule has been applied — today
that means ``PolicyEngine.evaluate``. Surviving tables and columns
carry ``filtered`` / ``masked`` flags reflecting the per-request
``ResolvedPolicy`` so downstream renderers emit the corresponding
directives.
"""

from __future__ import annotations

import copy

from ..schema.models import TableDef, TableRegistry
from .auth import JWTPayload
from .policy import PolicyEngine, PolicyError


def effective_registry(
    registry: TableRegistry,
    jwt_payload: JWTPayload,
    policy_engine: PolicyEngine | None,
) -> TableRegistry:
    """Return the registry the caller is allowed to see.

    With ``policy_engine is None`` the registry is returned unchanged
    (dev / unauthenticated mode). Otherwise every table is run through
    ``PolicyEngine.evaluate``: tables that raise ``PolicyError`` are
    dropped; surviving tables get a copy with blocked columns removed
    and ``masked`` / ``filtered`` flags set so downstream renderers
    emit the corresponding directives.
    """
    if policy_engine is None:
        return registry

    kept: list[TableDef] = []
    for table in registry:
        try:
            resolved = policy_engine.evaluate(table.name, jwt_payload)
        except PolicyError:
            continue

        new_table = copy.deepcopy(table)
        new_table.filtered = resolved.row_filter_clause is not None
        new_cols = []
        for col in new_table.columns:
            if not resolved.is_column_allowed(col.name):
                continue
            mask_expr = resolved.masks.get(col.name)
            if mask_expr is not None:
                col.masked = True
            new_cols.append(col)
        new_table.columns = new_cols
        kept.append(new_table)

    return TableRegistry(kept)
