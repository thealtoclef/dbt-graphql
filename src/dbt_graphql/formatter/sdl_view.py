"""Effective SDL view: prune the parsed db.graphql AST per caller.

``effective_document`` walks the boot-time ``DocumentNode``, drops types
and fields the caller cannot see, and injects ``@masked`` / ``@filtered``
directives on survivors flagged by the per-request ``ResolvedPolicy``.
``render_sdl`` emits the result via graphql-core's ``print_ast``.
"""

from __future__ import annotations

import copy

from graphql import (
    ConstDirectiveNode,
    DocumentNode,
    FieldDefinitionNode,
    NameNode,
    ObjectTypeDefinitionNode,
    print_ast,
)

from .schema import TableRegistry


def effective_document(
    doc: DocumentNode,
    effective_reg: TableRegistry,
    *,
    restrict_to: set[str] | None = None,
) -> DocumentNode:
    """Return a pruned copy of ``doc`` for the given effective registry.

    The result keeps only the tables and columns present in
    ``effective_reg``. ``@masked`` / ``@filtered`` directives are
    injected on survivors whose flags are set on the registry.

    Args:
        doc: The boot-time parsed SDL document.
        effective_reg: The caller's effective registry.
        restrict_to: Optional whitelist of table names. When provided,
            the output is intersected with this set — only tables whose
            name appears here AND in ``effective_reg`` are kept. Names
            in this set that are not in ``effective_reg`` (whether
            denied by policy or simply nonexistent) are silently
            skipped; this function does not raise on misses. Callers
            that need to error on bad input should validate the set
            against ``effective_reg`` themselves before calling.
    """
    keep_tables = {t.name: t for t in effective_reg}
    if restrict_to is not None:
        keep_tables = {n: t for n, t in keep_tables.items() if n in restrict_to}

    new_defs = []
    for defn in doc.definitions:
        if not isinstance(defn, ObjectTypeDefinitionNode):
            new_defs.append(defn)
            continue
        eff_table = keep_tables.get(defn.name.value)
        if eff_table is None:
            continue

        keep_cols = {c.name: c for c in eff_table.columns}
        new_fields: list[FieldDefinitionNode] = []
        for field in defn.fields or []:
            eff_col = keep_cols.get(field.name.value)
            if eff_col is None:
                continue
            new_field = field
            if eff_col.masked and not _has_directive(field.directives, "masked"):
                new_field = copy.copy(field)
                new_field.directives = tuple(field.directives or ()) + (
                    _make_directive("masked"),
                )
            new_fields.append(new_field)

        new_def = copy.copy(defn)
        new_def.fields = tuple(new_fields)
        if eff_table.filtered and not _has_directive(defn.directives, "filtered"):
            new_def.directives = tuple(defn.directives or ()) + (
                _make_directive("filtered"),
            )
        new_defs.append(new_def)

    new_doc = copy.copy(doc)
    new_doc.definitions = tuple(new_defs)
    return new_doc


def render_sdl(doc: DocumentNode) -> str:
    """Render an SDL ``DocumentNode`` back to text via ``print_ast``."""
    return print_ast(doc) + "\n"


def _has_directive(directives, name: str) -> bool:
    for d in directives or ():
        if d.name.value == name:
            return True
    return False


def _make_directive(name: str) -> ConstDirectiveNode:
    return ConstDirectiveNode(name=NameNode(value=name), arguments=())
