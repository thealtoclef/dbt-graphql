"""Extract primary key and foreign key information from dbt constraints (v1.5+).

dbt constraints are the authoritative source for PK/FK declarations, superseding
inference from tests. They appear in manifest.json when models use
`config(contract.enforced=true)` with column or model-level constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot import parse_one as _sqlglot_parse
from sqlglot.errors import SqlglotError

from ...ir.models import Cardinality, ProcessorRelationship, RelationshipOrigin
from ..artifacts import DbtManifest


@dataclass
class ConstraintsResult:
    # unique_id → list of primary key column names (multiple = composite PK)
    primary_keys: dict[str, list[str]] = field(default_factory=dict)
    # List of FK relationships extracted from foreign_key constraints
    foreign_key_relationships: list[ProcessorRelationship] = field(default_factory=list)


def _model_name_from_unique_id(unique_id: str) -> str:
    parts = unique_id.split(".")
    return parts[-1] if parts else unique_id


def _resolve_to_model(to_str: str, manifest_nodes: dict) -> str | None:
    """Resolve a fully-qualified relation name to a model name.

    dbt v1.9+ model contract constraints store FK targets as a fully-qualified
    relation name (e.g. ``"jaffle_shop"."main"."customers"``) in the ``to`` field,
    matched against each manifest node's ``relation_name``.

    Model nodes take priority over seeds and other resource types.
    Returns the model name (last segment of the node unique_id), or None if not found.
    """
    if not to_str:
        return None
    sorted_nodes = sorted(
        manifest_nodes.items(),
        key=lambda x: 0 if x[0].startswith("model.") else 1,
    )
    for node_id, node in sorted_nodes:
        if getattr(node, "relation_name", None) == to_str:
            return _model_name_from_unique_id(node_id)
    return None


def _parse_fk_expression(expression: str) -> tuple[str, str] | None:
    """Parse a dbt foreign_key expression like 'other_table(column)' into (table, column).

    Formats: ``table(col)``, ``schema.table(col)``, ``catalog.schema.table(col)``.
    Uses sqlglot to parse each identifier so quoted forms (``"tbl"``, backtick) are handled.
    """
    stripped = expression.strip()
    paren_start = stripped.find("(")
    if paren_start <= 0 or not stripped.endswith(")"):
        return None
    table_ref = stripped[:paren_start].strip()
    col_raw = stripped[paren_start + 1 : -1].strip()
    if not table_ref or not col_raw:
        return None

    try:
        table_name = _sqlglot_parse(table_ref, into=exp.Table).name
    except SqlglotError:
        table_name = table_ref.split(".")[-1].strip('"').strip("`")

    try:
        col = _sqlglot_parse(col_raw, into=exp.Column).name
    except SqlglotError:
        col = col_raw.strip('"').strip("`")

    return (table_name, col) if table_name and col else None


def extract_constraints(manifest: DbtManifest) -> ConstraintsResult:
    """Scan manifest model nodes for dbt constraints (v1.5+).

    Extracts:
    - primary_key constraints → model primary key mapping
    - foreign_key constraints → relationship candidates
    """
    result = ConstraintsResult()
    seen_fk: set[str] = set()

    for unique_id, node in manifest.nodes.items():
        if not unique_id.startswith("model."):
            continue

        from_model = _model_name_from_unique_id(unique_id)

        # --- Model-level constraints ---
        model_constraints = getattr(node, "constraints", None) or []
        for constraint in model_constraints:
            ctype = (
                constraint.get("type", "")
                if isinstance(constraint, dict)
                else getattr(constraint, "type", None) or ""
            )

            if ctype == "primary_key":
                columns = (
                    constraint.get("columns", [])
                    if isinstance(constraint, dict)
                    else getattr(constraint, "columns", None) or []
                )
                if columns:
                    result.primary_keys[unique_id] = [str(c) for c in columns]

            elif ctype == "foreign_key":
                expression = (
                    constraint.get("expression", "")
                    if isinstance(constraint, dict)
                    else getattr(constraint, "expression", None) or ""
                )
                fk_columns = (
                    constraint.get("columns", [])
                    if isinstance(constraint, dict)
                    else getattr(constraint, "columns", None) or []
                )
                to_str = (
                    constraint.get("to", "")
                    if isinstance(constraint, dict)
                    else getattr(constraint, "to", None) or ""
                )
                to_columns = (
                    constraint.get("to_columns", [])
                    if isinstance(constraint, dict)
                    else getattr(constraint, "to_columns", None) or []
                ) or []

                to_table: str | None = None
                to_col_list: list[str] = []
                from_col_list: list[str] = []

                if expression and fk_columns:
                    # Older format: expression="customers(customer_id)", columns=["customer_id"]
                    parsed = _parse_fk_expression(expression)
                    if parsed:
                        to_table, to_col = parsed
                        to_col_list = [to_col]
                        from_col_list = [fk_columns[0]]
                elif to_str and fk_columns and to_columns:
                    # dbt v1.9+ format: to="db.schema.customers", to_columns=["id"], columns=["customer_id"]
                    to_table = _resolve_to_model(to_str, manifest.nodes)
                    to_col_list = list(to_columns)
                    from_col_list = list(fk_columns)

                if to_table and to_col_list and from_col_list:
                    first_from = from_col_list[0]
                    first_to = to_col_list[0]
                    rel_name = f"{from_model}_{first_from}_{to_table}_{first_to}"

                    dedup_key = rel_name
                    if dedup_key not in seen_fk:
                        seen_fk.add(dedup_key)
                        result.foreign_key_relationships.append(
                            ProcessorRelationship(
                                name=rel_name,
                                models=[from_model, to_table],
                                cardinality=Cardinality.many_to_one,
                                origin=RelationshipOrigin.constraint,
                                from_columns=from_col_list,
                                to_columns=to_col_list,
                            )
                        )

        # --- Column-level constraints ---
        node_columns = getattr(node, "columns", None) or {}
        if isinstance(node_columns, dict):
            for col_name, col_def in node_columns.items():
                col_constraints = (
                    col_def.get("constraints", [])
                    if isinstance(col_def, dict)
                    else getattr(col_def, "constraints", None) or []
                )
                for constraint in col_constraints:
                    ctype = (
                        constraint.get("type", "")
                        if isinstance(constraint, dict)
                        else getattr(constraint, "type", None) or ""
                    )

                    if ctype == "primary_key":
                        result.primary_keys.setdefault(unique_id, [])
                        if col_name not in result.primary_keys[unique_id]:
                            result.primary_keys[unique_id].append(col_name)

                    elif ctype == "foreign_key":
                        expression = (
                            constraint.get("expression", "")
                            if isinstance(constraint, dict)
                            else getattr(constraint, "expression", None) or ""
                        )
                        to_str = (
                            constraint.get("to", "")
                            if isinstance(constraint, dict)
                            else getattr(constraint, "to", None) or ""
                        )
                        to_columns = (
                            constraint.get("to_columns", [])
                            if isinstance(constraint, dict)
                            else getattr(constraint, "to_columns", None) or []
                        ) or []

                        to_table = None
                        to_col_list: list[str] = []

                        if expression:
                            # Older format: expression="customers(customer_id)"
                            parsed = _parse_fk_expression(expression)
                            if parsed:
                                to_table, to_col_list = parsed[0], [parsed[1]]
                        elif to_str and to_columns:
                            # dbt v1.9+ format: to="db.schema.customers", to_columns=["id"]
                            to_table = _resolve_to_model(to_str, manifest.nodes)
                            to_col_list = list(to_columns)

                        if to_table and to_col_list:
                            first_to = to_col_list[0]
                            rel_name = f"{from_model}_{col_name}_{to_table}_{first_to}"

                            dedup_key = rel_name
                            if dedup_key not in seen_fk:
                                seen_fk.add(dedup_key)
                                result.foreign_key_relationships.append(
                                    ProcessorRelationship(
                                        name=rel_name,
                                        models=[from_model, to_table],
                                        cardinality=Cardinality.many_to_one,
                                        origin=RelationshipOrigin.constraint,
                                        from_columns=[col_name],
                                        to_columns=to_col_list,
                                    )
                                )

    return result
