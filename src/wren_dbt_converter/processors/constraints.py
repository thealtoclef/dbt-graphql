"""Extract primary key and foreign key information from dbt constraints (v1.5+).

dbt constraints are the authoritative source for PK/FK declarations, superseding
inference from tests. They appear in manifest.json when models use
`config(contract.enforced=true)` with column or model-level constraints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models.wrapper import Relationship, JoinType


@dataclass
class ConstraintsResult:
    # unique_id → primary key column name
    primary_keys: dict[str, str] = field(default_factory=dict)
    # List of FK relationships extracted from foreign_key constraints
    foreign_key_relationships: list[Relationship] = field(default_factory=list)


def _model_name_from_unique_id(unique_id: str) -> str:
    parts = unique_id.split(".")
    return parts[-1] if parts else unique_id


def _parse_fk_expression(expression: str) -> tuple[str, str] | None:
    """Parse a dbt foreign_key expression like 'other_table(column)' into (table, column).

    Returns None if the expression cannot be parsed.
    """
    # Common formats:
    #   table_name(column_name)
    #   schema.table_name(column_name)
    #   catalog.schema.table_name(column_name)
    match = re.match(r"^(.+?)\((.+?)\)$", expression.strip())
    if not match:
        return None
    table_ref, col = match.group(1), match.group(2)
    # Extract just the table name (last part before the paren)
    parts = table_ref.strip().split(".")
    table_name = parts[-1].strip('"').strip("`")
    return table_name, col.strip('"').strip("`")


def extract_constraints(manifest) -> ConstraintsResult:
    """Scan manifest model nodes for dbt constraints (v1.5+).

    Extracts:
    - primary_key constraints → model primary key mapping
    - foreign_key constraints → relationship candidates
    """
    result = ConstraintsResult()
    seen_fk: set[tuple] = set()

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
                else getattr(constraint, "type", "")
            )

            if ctype == "primary_key":
                columns = (
                    constraint.get("columns", [])
                    if isinstance(constraint, dict)
                    else getattr(constraint, "columns", [])
                )
                if columns:
                    # Use the first column as primary key (composite PKs not supported in MDL)
                    col_name = columns[0] if isinstance(columns[0], str) else columns[0]
                    result.primary_keys[unique_id] = col_name

            elif ctype == "foreign_key":
                expression = (
                    constraint.get("expression", "")
                    if isinstance(constraint, dict)
                    else getattr(constraint, "expression", "")
                )
                fk_columns = (
                    constraint.get("columns", [])
                    if isinstance(constraint, dict)
                    else getattr(constraint, "columns", [])
                )
                if expression and fk_columns:
                    parsed = _parse_fk_expression(expression)
                    if parsed:
                        to_table, to_col = parsed
                        from_col = (
                            fk_columns[0]
                            if isinstance(fk_columns[0], str)
                            else fk_columns[0]
                        )
                        rel_name = f"{from_model}_{from_col}_{to_table}_{to_col}"
                        condition = (
                            f'"{from_model}"."{from_col}" = "{to_table}"."{to_col}"'
                        )

                        dedup_key = (rel_name, condition)
                        if dedup_key not in seen_fk:
                            seen_fk.add(dedup_key)
                            result.foreign_key_relationships.append(
                                Relationship(
                                    name=rel_name,
                                    models=[from_model, to_table],
                                    join_type=JoinType.many_to_one,
                                    condition=condition,
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
                        else getattr(constraint, "type", "")
                    )

                    if ctype == "primary_key":
                        # Column-level primary_key constraint
                        if unique_id not in result.primary_keys:
                            result.primary_keys[unique_id] = col_name

                    elif ctype == "foreign_key":
                        expression = (
                            constraint.get("expression", "")
                            if isinstance(constraint, dict)
                            else getattr(constraint, "expression", "")
                        )
                        if expression:
                            parsed = _parse_fk_expression(expression)
                            if parsed:
                                to_table, to_col = parsed
                                rel_name = (
                                    f"{from_model}_{col_name}_{to_table}_{to_col}"
                                )
                                condition = f'"{from_model}"."{col_name}" = "{to_table}"."{to_col}"'

                                dedup_key = (rel_name, condition)
                                if dedup_key not in seen_fk:
                                    seen_fk.add(dedup_key)
                                    result.foreign_key_relationships.append(
                                        Relationship(
                                            name=rel_name,
                                            models=[from_model, to_table],
                                            join_type=JoinType.many_to_one,
                                            condition=condition,
                                        )
                                    )

    return result
