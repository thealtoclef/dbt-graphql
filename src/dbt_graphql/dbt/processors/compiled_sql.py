"""Extract facts from dbt's **compiled SQL** via sqlglot + dbt-colibri.

Public functions:
- :func:`extract_table_lineage` — table edges from ``depends_on.nodes``.
- :func:`extract_column_lineage` — column-level lineage via dbt-colibri.
- :func:`extract_join_relationships` — JOIN-derived FK relationships.

Everything else is internal plumbing shared across the three public extractors.
"""

from __future__ import annotations

import gc
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope
from dbt_colibri.lineage_extractor.lineage import prepare_scope, to_node
from dbt_colibri.utils.parsing_utils import (
    normalize_table_relation_name,
    remove_quotes,
    remove_upper,
)

from ...ir.models import (
    Column,
    ColumnLineageItem,
    JoinType,
    LineageType,
    ProcessorRelationship,
    RelationshipOrigin,
    TableLineageItem,
)
from ..artifacts import DbtCatalog, DbtManifest

from loguru import logger


# ---------------------------------------------------------------------------
# sqlglot plumbing (internal)
# ---------------------------------------------------------------------------


def _model_name_from_unique_id(unique_id: str) -> str:
    parts = unique_id.split(".")
    return parts[-1] if parts else unique_id


def build_table_lookup(manifest: DbtManifest) -> dict[str, str]:
    """Map normalized identifiers to the dbt model name used downstream.

    Keys: ``relation_name``, ``database.schema.alias``, ``schema.alias``.
    The alias-only key is intentionally omitted to prevent cross-package
    collisions where two packages both define a model with the same short name.
    """
    lookup: dict[str, str] = {}

    def _add_node(node_id: str, node: Any) -> None:
        model_name = _model_name_from_unique_id(node_id)
        relation_name = getattr(node, "relation_name", None) or ""
        if relation_name:
            lookup[normalize_table_relation_name(relation_name)] = model_name

        database = node.database
        schema = node.schema_
        # Sources expose ``identifier``; models/seeds/snapshots expose ``alias``.
        alias = (
            getattr(node, "alias", None)
            or getattr(node, "identifier", None)
            or node.name
        )
        if database and schema and alias:
            lookup[normalize_table_relation_name(f"{database}.{schema}.{alias}")] = (
                model_name
            )
        if schema and alias:
            lookup[normalize_table_relation_name(f"{schema}.{alias}")] = model_name

    allowed = {"model", "source", "seed", "snapshot"}
    for node_id, node in manifest.nodes.items():
        if node.resource_type in allowed:
            _add_node(node_id, node)

    sources = getattr(manifest, "sources", None) or {}
    for source_id, source in sources.items():
        _add_node(source_id, source)

    return lookup


def build_schema_for_model(
    model_node: Any, manifest: DbtManifest, catalog: DbtCatalog
) -> dict:
    """Build a sqlglot schema dict ``{database: {schema: {table: {col: type}}}}``
    restricted to the parent models/sources/seeds of ``model_node``.
    """
    schema: dict[str, dict[str, dict[str, dict[str, str]]]] = {}

    depends_on = getattr(model_node, "depends_on", None)
    parent_ids = list(depends_on.nodes) if depends_on else []

    catalog_nodes = dict(catalog.nodes)
    catalog_sources = dict(getattr(catalog, "sources", None) or {})

    for parent_id in parent_ids:
        cat_node = catalog_nodes.get(parent_id) or catalog_sources.get(parent_id)
        if cat_node is None:
            continue
        metadata = getattr(cat_node, "metadata", None)
        if metadata is None:
            continue
        db = getattr(metadata, "database", None)
        sch = getattr(metadata, "schema_", None) or getattr(metadata, "schema", None)
        tbl = getattr(metadata, "name", None)
        if not (db and sch and tbl):
            continue

        cols: dict[str, str] = {}
        for col_name, col_meta in (cat_node.columns or {}).items():
            cols[col_name] = getattr(col_meta, "type", None) or ""

        schema.setdefault(db, {}).setdefault(sch, {})[tbl] = cols

    return schema


_ADAPTER_TO_DIALECT = {
    "sqlserver": "tsql",
}


def detect_dialect(manifest: DbtManifest) -> str:
    """Map manifest ``adapter_type`` to a sqlglot dialect name."""
    adapter_type = manifest.metadata.adapter_type
    if not adapter_type:
        raise ValueError("manifest.metadata.adapter_type is missing or empty")
    return _ADAPTER_TO_DIALECT.get(adapter_type, adapter_type)


def qualify_model_sql(sql: str, dialect: str, schema: dict) -> Scope | None:
    """Parse, qualify, and scope a model's compiled SQL.

    Returns the root :class:`Scope`, or ``None`` if parsing/qualification failed.
    """
    if not sql:
        return None

    try:
        expression = parse_one(sql, read=dialect or None)
        if dialect == "postgres":
            expression = remove_quotes(expression)
        elif dialect == "bigquery":
            expression = remove_upper(expression)
        _, scope = prepare_scope(expression, schema=schema, dialect=dialect or None)
        return scope
    except SqlglotError as e:
        logger.debug("sqlglot processing failed ({}): {}", dialect, e)
        return None
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning("unexpected error processing SQL ({}): {}", dialect, e)
        return None


def resolve_table_to_model(
    table_node: exp.Table, table_lookup: dict[str, str]
) -> str | None:
    """Resolve a leaf ``exp.Table`` node to a dbt model name using the lookup."""
    catalog_part = (table_node.catalog or "").strip()
    db_part = (table_node.db or "").strip()
    name_part = (table_node.name or "").strip()

    if not name_part:
        return None

    candidates: list[str] = []
    if catalog_part and db_part:
        candidates.append(f"{catalog_part}.{db_part}.{name_part}")
    if db_part:
        candidates.append(f"{db_part}.{name_part}")
    candidates.append(name_part)

    for candidate in candidates:
        hit = table_lookup.get(normalize_table_relation_name(candidate))
        if hit is not None:
            return hit
    return None


# ---------------------------------------------------------------------------
# Table lineage (from depends_on)
# ---------------------------------------------------------------------------


def extract_table_lineage(manifest: DbtManifest) -> list[TableLineageItem]:
    """Build table-level lineage edges from manifest ``depends_on.nodes``."""
    result: list[TableLineageItem] = []

    for unique_id, node in manifest.nodes.items():
        if not unique_id.startswith("model."):
            continue

        model_name = _model_name_from_unique_id(unique_id)
        deps = getattr(node, "depends_on", None)
        dep_nodes = list(getattr(deps, "nodes", None) or []) if deps else []

        for d in dep_nodes:
            if d.startswith(("model.", "seed.", "source.")):
                result.append(
                    TableLineageItem(
                        source=_model_name_from_unique_id(d),
                        target=model_name,
                    )
                )

    return result


# ---------------------------------------------------------------------------
# Column lineage
# ---------------------------------------------------------------------------


def _edges_for_model(
    model_name: str,
    scope: Scope,
    table_lookup: dict[str, str],
    dialect: str,
) -> list[ColumnLineageItem]:
    """Walk all SELECT columns in ``scope`` via colibri's ``to_node`` and
    emit typed column lineage edges grouped by source model.
    """
    if not hasattr(scope.expression, "selects"):
        return []

    by_source: dict[str, list[Column]] = {}

    for select in scope.expression.selects:
        if isinstance(select, exp.Star):
            continue
        target_col = select.alias_or_name
        if not target_col:
            continue

        root = to_node(target_col, scope, dialect, visited=set())
        if root is None:
            continue

        # colibri uses hyphens ("pass-through"); LineageType uses underscores.
        lineage_type = LineageType(root.lineage_type.replace("-", "_"))
        seen: set[tuple[str, str]] = set()

        for n in root.walk():
            if n.downstream:
                continue
            if not isinstance(n.source, exp.Table):
                continue
            source_model = resolve_table_to_model(n.source, table_lookup)
            if source_model is None:
                continue
            # colibri leaf names are "table_alias.column" — strip table prefix.
            col_name = n.name.rsplit(".", 1)[-1].strip('"').strip("`")
            key = (source_model, col_name)
            if key in seen:
                continue
            seen.add(key)
            by_source.setdefault(source_model, []).append(
                Column(
                    source_column=col_name,  # type:ignore[ty:unknown-argument]
                    target_column=target_col,  # type:ignore[ty:unknown-argument]
                    lineage_type=lineage_type,  # type:ignore[ty:unknown-argument]
                )
            )

    return [
        ColumnLineageItem(
            source=src,
            target=model_name,
            columns=cols,
        )
        for src, cols in by_source.items()
    ]


def extract_column_lineage(
    manifest: DbtManifest,
    catalog: DbtCatalog,
) -> list[ColumnLineageItem]:
    """Extract column-level lineage for every materialized model in the project.

    Returns a list of :class:`ColumnLineageItem` directed edges (source → target model).
    """
    result, _ = _extract_both(manifest, catalog)
    return result


# ---------------------------------------------------------------------------
# JOIN-derived relationships (sqlglot — JOIN ON mining)
# ---------------------------------------------------------------------------


def _find_select_by_name(scope: Scope, column_name: str) -> exp.Expression | None:
    if not hasattr(scope.expression, "selects"):
        return None
    lower_target = column_name.lower()
    fallback: exp.Expression | None = None
    for select in scope.expression.selects:
        name = select.alias_or_name
        if name == column_name:
            return select
        if fallback is None and name.lower() == lower_target:
            fallback = select
    return fallback


def _resolve_column_to_model(
    col: exp.Column,
    scope: Scope,
    table_lookup: dict[str, str],
    visited: set[tuple[int, str]] | None = None,
) -> str | None:
    """Resolve a join-column reference to its origin dbt model."""
    if visited is None:
        visited = set()

    table_ref = col.table
    col_name = col.name
    source = scope.sources.get(table_ref)

    if isinstance(source, exp.Table):
        return resolve_table_to_model(source, table_lookup)

    if isinstance(source, Scope):
        key = (id(source), col_name.lower())
        if key in visited:
            return None
        visited.add(key)
        select = _find_select_by_name(source, col_name)
        if select is None:
            return None
        for inner_col in select.find_all(exp.Column):
            resolved = _resolve_column_to_model(
                inner_col, source, table_lookup, visited
            )
            if resolved is not None:
                return resolved
    return None


def _extract_join_columns(
    on_clause: exp.Expression,
) -> list[tuple[exp.Column, exp.Column]]:
    """Return (left, right) column pairs from every ``=`` node in an ON clause."""
    pairs: list[tuple[exp.Column, exp.Column]] = []
    for eq in on_clause.find_all(exp.EQ):
        left = eq.args.get("this")
        right = eq.args.get("expression")
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            pairs.append((left, right))
    return pairs


def _joins_in_scope(scope: Scope) -> list[exp.Join]:
    expr = scope.expression
    joins = expr.args.get("joins") if hasattr(expr, "args") else None
    return list(joins or [])


def _relationships_for_model(
    current_model: str,
    scope: Scope,
    table_lookup: dict[str, str],
) -> list[ProcessorRelationship]:
    relationships: list[ProcessorRelationship] = []
    seen: set[tuple[str, str, str, str]] = set()

    for sub_scope in scope.traverse():
        for join in _joins_in_scope(sub_scope):
            on_clause = join.args.get("on")
            if on_clause is None:
                continue
            for left_col, right_col in _extract_join_columns(on_clause):
                left_model = _resolve_column_to_model(left_col, sub_scope, table_lookup)
                right_model = _resolve_column_to_model(
                    right_col, sub_scope, table_lookup
                )
                if left_model is None or right_model is None:
                    continue
                # Skip self-joins only when the column is identical on both sides
                # (true self-reference); hierarchical FKs like employee.manager_id
                # → employee.employee_id are preserved.
                if left_model == right_model and left_col.name == right_col.name:
                    continue

                if left_model == current_model:
                    from_model, from_col = left_model, left_col.name
                    to_model, to_col = right_model, right_col.name
                elif right_model == current_model:
                    from_model, from_col = right_model, right_col.name
                    to_model, to_col = left_model, left_col.name
                else:
                    continue

                key = (from_model, from_col, to_model, to_col)
                if key in seen:
                    continue
                seen.add(key)

                rel_name = f"{from_model}_{from_col}_{to_model}_{to_col}"
                relationships.append(
                    ProcessorRelationship(
                        name=rel_name,
                        models=[from_model, to_model],
                        join_type=JoinType.many_to_one,
                        origin=RelationshipOrigin.lineage,
                        from_columns=[from_col],
                        to_columns=[to_col],
                    )
                )

    return relationships


def extract_join_relationships(
    manifest: DbtManifest,
    catalog: DbtCatalog,
) -> list[ProcessorRelationship]:
    """Scan every model's compiled SQL for JOIN-derived relationships.

    Output is deduplicated by relationship name.
    """
    _, result = _extract_both(manifest, catalog)
    return result


def _extract_both(
    manifest: DbtManifest,
    catalog: DbtCatalog,
) -> tuple[list[ColumnLineageItem], list[ProcessorRelationship]]:
    """Single pass over compiled SQL producing both column lineage and join relationships."""
    table_lookup = build_table_lookup(manifest)
    dialect = detect_dialect(manifest)

    col_result: list[ColumnLineageItem] = []
    join_seen_names: set[str] = set()
    join_result: list[ProcessorRelationship] = []
    processed = 0

    for unique_id, node in manifest.nodes.items():
        if getattr(node, "resource_type", None) not in ("model", "snapshot"):
            continue

        compiled_code = getattr(node, "compiled_code", None) or ""
        if not compiled_code:
            continue

        schema = build_schema_for_model(node, manifest, catalog)
        scope = qualify_model_sql(compiled_code, dialect, schema)
        if scope is None:
            continue

        model_name = _model_name_from_unique_id(unique_id)

        # Column lineage
        col_result.extend(_edges_for_model(model_name, scope, table_lookup, dialect))

        # JOIN relationships
        for rel in _relationships_for_model(model_name, scope, table_lookup):
            if rel.name not in join_seen_names:
                join_seen_names.add(rel.name)
                join_result.append(rel)

        processed += 1
        if processed % 50 == 0:
            gc.collect()

    return col_result, join_result
