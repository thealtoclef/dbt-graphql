"""Effective SDL view: prune the parsed db.graphql AST per caller.

``effective_document`` walks the boot-time ``DocumentNode``, drops types
and fields the caller cannot see, prunes ``@lineage`` references that
point at hidden upstream models/columns, and injects ``@masked`` /
``@filtered`` directives on survivors flagged by the per-request
``ResolvedPolicy``.
``render_sdl`` emits the result via graphql-core's ``print_ast``.
"""

from __future__ import annotations

import copy

from graphql import (
    ConstDirectiveNode,
    DocumentNode,
    FieldDefinitionNode,
    ListValueNode,
    NameNode,
    ObjectTypeDefinitionNode,
    StringValueNode,
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

    # Lineage filter set: full effective_reg, NOT restrict_to. A caller can
    # legitimately reference upstream models via lineage even when the
    # current view restricts to a subset.
    visible_models: set[str] = {t.name for t in effective_reg}
    visible_cols: dict[str, set[str]] = {
        t.name: {c.name for c in t.columns} for t in effective_reg
    }

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
            new_directives = _filter_field_lineage(
                field.directives, visible_models, visible_cols
            )
            if new_directives is not field.directives:
                new_field = copy.copy(field)
                new_field.directives = new_directives
            if eff_col.masked and not _has_directive(new_field.directives, "masked"):
                if new_field is field:
                    new_field = copy.copy(field)
                new_field.directives = tuple(new_field.directives or ()) + (
                    _make_directive("masked"),
                )
            new_fields.append(new_field)

        new_def = copy.copy(defn)
        new_def.fields = tuple(new_fields)
        new_def.directives = _filter_type_lineage(defn.directives, visible_models)
        if eff_table.filtered and not _has_directive(new_def.directives, "filtered"):
            new_def.directives = tuple(new_def.directives or ()) + (
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


def _filter_type_lineage(directives, visible_models: set[str]):
    """Rewrite type-level @lineage to only reference visible source models.

    Drops the directive entirely if no sources remain.
    """
    if not directives:
        return directives
    out = []
    changed = False
    for d in directives:
        if d.name.value != "lineage":
            out.append(d)
            continue
        sources_arg = next(
            (a for a in (d.arguments or ()) if a.name.value == "sources"), None
        )
        if sources_arg is None or not isinstance(sources_arg.value, ListValueNode):
            out.append(d)
            continue
        kept = [
            v
            for v in sources_arg.value.values
            if isinstance(v, StringValueNode) and v.value in visible_models
        ]
        if not kept:
            changed = True
            continue
        if len(kept) == len(sources_arg.value.values):
            out.append(d)
            continue
        new_d = copy.copy(d)
        new_list = copy.copy(sources_arg.value)
        new_list.values = tuple(kept)
        new_arg = copy.copy(sources_arg)
        new_arg.value = new_list
        new_d.arguments = tuple(
            new_arg if a is sources_arg else a for a in (d.arguments or ())
        )
        out.append(new_d)
        changed = True
    return tuple(out) if changed else directives


def _filter_field_lineage(
    directives, visible_models: set[str], visible_cols: dict[str, set[str]]
):
    """Drop field-level @lineage directives whose source/column is hidden."""
    if not directives:
        return directives
    out = []
    changed = False
    for d in directives:
        if d.name.value != "lineage":
            out.append(d)
            continue
        args = {a.name.value: a.value for a in (d.arguments or ())}
        src = args.get("source")
        col = args.get("column")
        if not (isinstance(src, StringValueNode) and isinstance(col, StringValueNode)):
            out.append(d)
            continue
        if src.value not in visible_models or col.value not in visible_cols.get(
            src.value, set()
        ):
            changed = True
            continue
        out.append(d)
    return tuple(out) if changed else directives
