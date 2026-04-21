"""Extract facts from dbt's **compiled SQL** via sqlglot.

This module is the third input surface (alongside ``constraints.py`` and
``data_tests.py``). Compiled SQL is the source; sqlglot is the tool.

Public functions, ordered by output:

- :func:`extract_table_lineage` — table edges from ``depends_on.nodes``. No SQL
  parsing needed, but it lives here because "what came from the compiled DAG"
  is the conceptual fit.
- :func:`extract_column_lineage` — column-level lineage. For each materialized
  model, compiled SQL is parsed, qualified against the parent-model schema,
  and every output column is traced through CTEs and subqueries to leaf source
  tables. Each hop is classified (``pass-through`` / ``rename`` /
  ``transformation``) and the overall lineage type is the max rank across the
  chain.
- :func:`extract_join_relationships` — JOIN-derived foreign-key relationships.
  Mines every ``JOIN ... ON <eq>`` in compiled SQL, resolves each column to
  its origin dbt model, and emits :class:`ProcessorRelationship` objects
  tagged ``origin="lineage"``. Direction rule: the current dbt model being
  processed is always ``from_model``. Joins where neither side resolves to
  the current model are skipped; self-joins are skipped.

Everything else (``build_table_lookup``, ``qualify_model_sql``,
``detect_dialect``, …) is internal sqlglot plumbing shared across the three
public extractors.
"""

from __future__ import annotations

import gc
from dbt_graphql.log import get_logger
import re
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer import find_all_in_scope
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from ...ir.models import JoinType, ProcessorRelationship, RelationshipOrigin
from ..artifacts import DbtCatalog, DbtManifest

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class ColumnLineageEdge:
    source_model: str
    source_column: str
    target_column: str
    lineage_type: str  # "pass_through" | "rename" | "transformation"


# ---------------------------------------------------------------------------
# sqlglot plumbing (internal)
# ---------------------------------------------------------------------------


def _model_name_from_unique_id(unique_id: str) -> str:
    parts = unique_id.split(".")
    return parts[-1] if parts else unique_id


def _normalize_relation_name(name: str) -> str:
    """Strip quotes/backticks from a relation name and lowercase it for lookup."""
    return name.replace('"', "").replace("`", "").lower()


def build_table_lookup(manifest: DbtManifest) -> dict[str, str]:
    """Map normalized identifiers (relation_name, alias, database.schema.alias)
    to the dbt model name used downstream.

    Handles model, source, seed, and snapshot nodes. Ephemeral nodes have no
    ``relation_name`` — build one from ``database.schema.alias``.
    """
    lookup: dict[str, str] = {}

    def _add_node(node_id: str, node: Any) -> None:
        model_name = _model_name_from_unique_id(node_id)
        relation_name = getattr(node, "relation_name", None) or ""
        if relation_name:
            lookup[_normalize_relation_name(relation_name)] = model_name

        database = getattr(node, "database", None) or ""
        schema = getattr(node, "schema", None) or ""
        alias = getattr(node, "alias", None) or getattr(node, "name", None) or ""
        if database and schema and alias:
            lookup[_normalize_relation_name(f"{database}.{schema}.{alias}")] = (
                model_name
            )
        if schema and alias:
            lookup[_normalize_relation_name(f"{schema}.{alias}")] = model_name
        if alias:
            lookup[alias.lower()] = model_name

    allowed = {"model", "source", "seed", "snapshot"}
    for node_id, node in manifest.nodes.items():
        if getattr(node, "resource_type", None) in allowed:
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
    parent_ids = list(getattr(depends_on, "nodes", None) or [])

    catalog_nodes = dict(getattr(catalog, "nodes", None) or {})
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
    adapter_type: str = getattr(manifest.metadata, "adapter_type", None) or ""
    return _ADAPTER_TO_DIALECT.get(adapter_type, adapter_type)


def sanitize_sql(sql: str, dialect: str) -> str:
    """Dialect-specific SQL rewrites that sqlglot can't parse as-is."""
    if dialect == "oracle":
        sql = re.sub(r"(?i)\bLISTAGG\s*\(\s*DISTINCT\s+", "LISTAGG(", sql)
        sql = re.sub(
            r"(?is)\bON\s+OVERFLOW\s+(?:TRUNCATE|ERROR)\b"
            r"(?:\s+'[^']*')?(?:\s+(?:WITH|WITHOUT)\s+COUNT)?",
            "",
            sql,
        )
    return sql


def _remove_identifier_quotes(expression: exp.Expression) -> exp.Expression:
    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Identifier) and node.quoted:
            return exp.Identifier(this=node.this, quoted=False)
        return node

    return expression.transform(_transform)


def _lowercase_quoted_identifiers(expression: exp.Expression) -> exp.Expression:
    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Identifier) and node.quoted:
            return exp.Identifier(this=node.this.lower(), quoted=True)
        return node

    return expression.transform(_transform)


def qualify_model_sql(sql: str, dialect: str, schema: dict) -> Scope | None:
    """Parse, sanitize, qualify, and scope a model's compiled SQL.

    Returns the root :class:`Scope`, or ``None`` if parsing/qualification failed.
    """
    if not sql:
        return None

    sanitized = sanitize_sql(sql, dialect)
    try:
        expression = parse_one(sanitized, read=dialect or None)
        if dialect == "postgres":
            expression = _remove_identifier_quotes(expression)
        elif dialect == "bigquery":
            expression = _lowercase_quoted_identifiers(expression)
        qualified = qualify(
            expression,
            dialect=dialect or None,
            schema=schema,
            validate_qualify_columns=False,
            identify=False,
        )
        return build_scope(qualified)
    except SqlglotError as e:
        logger.debug("sqlglot processing failed (%s): %s", dialect, e)
        return None
    except Exception as e:  # Compiled user SQL can trigger unexpected internal errors
        logger.debug("unexpected error processing SQL (%s): %s", dialect, e)
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
        hit = table_lookup.get(_normalize_relation_name(candidate))
        if hit is not None:
            return hit
    return None


# ---------------------------------------------------------------------------
# Table lineage (from depends_on)
# ---------------------------------------------------------------------------


def extract_table_lineage(manifest: DbtManifest) -> dict[str, list[str]]:
    """Build table-level lineage from manifest ``depends_on.nodes``."""
    result: dict[str, list[str]] = {}

    for unique_id, node in manifest.nodes.items():
        if not unique_id.startswith("model."):
            continue

        model_name = _model_name_from_unique_id(unique_id)
        deps = getattr(node, "depends_on", None)
        if deps is None:
            continue

        dep_nodes = getattr(deps, "nodes", None) or []
        upstream = [
            _model_name_from_unique_id(d)
            for d in dep_nodes
            if d.startswith(("model.", "seed.", "source."))
        ]
        if upstream:
            result[model_name] = upstream

    return result


# ---------------------------------------------------------------------------
# Column lineage — ported from dbt-colibri (MIT), itself derived from sqlglot
# ---------------------------------------------------------------------------


# _LineageNode is an internal intermediate representation.
# ColumnLineageEdge is the public output type.
@dataclass
class _LineageNode:
    name: str
    expression: exp.Expression
    source: exp.Expression
    downstream: list[_LineageNode] = field(default_factory=list)
    lineage_type: str = ""

    def walk(self) -> list[_LineageNode]:
        result = [self]
        for d in self.downstream:
            if d is not None:
                result.extend(d.walk())
        return result


def _classify(select: exp.Expression) -> str:
    if isinstance(select, exp.Column):
        return "pass_through"
    if isinstance(select, exp.Alias):
        inner = select.this
        if isinstance(inner, exp.Column):
            return "pass_through" if inner.name == select.alias_or_name else "rename"
        return "transformation"
    return "transformation"


_RANK = {"pass_through": 0, "rename": 1, "transformation": 2}


def _max_lineage(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def _to_node(
    column: str | int,
    scope: Scope,
    dialect: str,
    upstream: _LineageNode | None = None,
    visited: set | None = None,
) -> _LineageNode | None:
    if visited is None:
        visited = set()
    key = (column, id(scope))
    if key in visited:
        return None
    visited.add(key)

    select = (
        scope.expression.selects[column]
        if isinstance(column, int)
        else next(
            (s for s in scope.expression.selects if s.alias_or_name == column),
            exp.Star() if scope.expression.is_star else scope.expression,
        )
    )
    lineage_type = _classify(select)

    if isinstance(scope.expression, exp.Subquery):
        for src in scope.subquery_scopes:
            return _to_node(
                column, scope=src, dialect=dialect, upstream=upstream, visited=visited
            )

    if isinstance(scope.expression, exp.SetOperation):
        name = type(scope.expression).__name__.upper()
        upstream = upstream or _LineageNode(
            name=name, source=scope.expression, expression=select
        )
        index = (
            column
            if isinstance(column, int)
            else next(
                (
                    i
                    for i, s in enumerate(scope.expression.selects)
                    if s.alias_or_name == column or s.is_star
                ),
                -1,
            )
        )
        if index == -1:
            return upstream
        for s in scope.union_scopes:
            _to_node(
                index, scope=s, dialect=dialect, upstream=upstream, visited=visited
            )
        agg = "pass_through"
        for child in upstream.downstream:
            if child is not None:
                agg = _max_lineage(agg, child.lineage_type)
        upstream.lineage_type = agg
        return upstream

    node = _LineageNode(
        name=str(column),
        source=scope.expression,
        expression=select,
        lineage_type=lineage_type,
    )
    if upstream is not None:
        upstream.downstream.append(node)

    subquery_scopes = {id(sq.expression): sq for sq in scope.subquery_scopes}
    for subquery in find_all_in_scope(select, exp.UNWRAPPED_QUERIES):
        sq_scope = subquery_scopes.get(id(subquery))
        if not sq_scope:
            continue
        for name in subquery.named_selects:
            _to_node(
                name, scope=sq_scope, dialect=dialect, upstream=node, visited=visited
            )

    if select.is_star:
        for src in scope.sources.values():
            if isinstance(src, Scope):
                src = src.expression
            node.downstream.append(_LineageNode(name="*", source=src, expression=src))

    for c in find_all_in_scope(select, exp.Column):
        table = c.table
        src = scope.sources.get(table)
        if isinstance(src, Scope):
            _to_node(c.name, scope=src, dialect=dialect, upstream=node, visited=visited)
        elif isinstance(src, exp.Table):
            pivot = src.find(exp.Pivot)
            if pivot and hasattr(src, "this") and hasattr(src.this, "alias_or_name"):
                pivot_scope = scope.sources.get(src.this.alias_or_name)
                if isinstance(pivot_scope, Scope):
                    _to_node(
                        c.name,
                        scope=pivot_scope,
                        dialect=dialect,
                        upstream=node,
                        visited=visited,
                    )
                    continue
            node.downstream.append(_LineageNode(name=c.name, source=src, expression=c))

    agg = lineage_type
    for child in node.downstream:
        if child is not None:
            agg = _max_lineage(agg, child.lineage_type)
    node.lineage_type = agg
    return node


def _edges_from_node(
    root: _LineageNode, target_col: str, table_lookup: dict[str, str]
) -> list[ColumnLineageEdge]:
    edges: list[ColumnLineageEdge] = []
    seen: set[tuple[str, str]] = set()
    for n in root.walk():
        if n.downstream:
            continue
        if not isinstance(n.source, exp.Table):
            continue
        source_model = resolve_table_to_model(n.source, table_lookup)
        if source_model is None:
            continue
        key = (source_model, n.name)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            ColumnLineageEdge(
                source_model=source_model,
                source_column=n.name,
                target_column=target_col,
                lineage_type=root.lineage_type,
            )
        )
    return edges


def _edges_for_model(
    scope: Scope, table_lookup: dict[str, str], dialect: str
) -> dict[str, list[ColumnLineageEdge]]:
    result: dict[str, list[ColumnLineageEdge]] = {}
    if not hasattr(scope.expression, "selects"):
        return result

    for select in scope.expression.selects:
        if isinstance(select, exp.Star):
            continue
        target_col = select.alias_or_name
        if not target_col:
            continue

        root = _to_node(target_col, scope, dialect, visited=set())
        if root is None:
            continue
        edges = _edges_from_node(root, target_col, table_lookup)
        if edges:
            result[target_col] = edges

    return result


def extract_column_lineage(
    manifest: DbtManifest,
    catalog: DbtCatalog,
) -> dict[str, dict[str, list[ColumnLineageEdge]]]:
    """Extract column-level lineage for every materialized model in the project.

    Returns a dict keyed by model name, then by target column name, with a list
    of :class:`ColumnLineageEdge` values.
    """
    table_lookup = build_table_lookup(manifest)
    dialect = detect_dialect(manifest)

    result: dict[str, dict[str, list[ColumnLineageEdge]]] = {}
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

        edges = _edges_for_model(scope, table_lookup, dialect)
        if edges:
            result[_model_name_from_unique_id(unique_id)] = edges

        processed += 1
        if processed % 50 == 0:
            gc.collect()

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
    """Resolve a join-column reference to its origin dbt model.

    Walks through CTE/subquery scopes via the inner select expression's first
    column reference until it lands on a leaf ``exp.Table``.
    """
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
                if left_model == right_model:
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
                condition = f'"{from_model}"."{from_col}" = "{to_model}"."{to_col}"'
                relationships.append(
                    ProcessorRelationship(
                        name=rel_name,
                        models=[from_model, to_model],
                        join_type=JoinType.many_to_one,
                        origin=RelationshipOrigin.lineage,
                        condition=condition,
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
    table_lookup = build_table_lookup(manifest)
    dialect = detect_dialect(manifest)

    seen_names: set[str] = set()
    result: list[ProcessorRelationship] = []
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

        current_model = _model_name_from_unique_id(unique_id)
        for rel in _relationships_for_model(current_model, scope, table_lookup):
            if rel.name in seen_names:
                continue
            seen_names.add(rel.name)
            result.append(rel)

        processed += 1
        if processed % 50 == 0:
            gc.collect()

    return result
