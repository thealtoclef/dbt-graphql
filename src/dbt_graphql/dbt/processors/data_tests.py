"""Extract everything that comes from dbt **data tests**.

dbt has two kinds of tests: *data tests* (schema tests like ``not_null``,
``unique``, ``accepted_values``, ``relationships``) and *unit tests* (model
logic assertions — a newer, separate feature). This module handles the former.

Outputs:
- :class:`TestsResult` — not-null / unique boolean maps plus enum definitions
  derived from ``accepted_values``.
- :func:`build_relationships` — :class:`ProcessorRelationship` objects from
  ``relationships`` tests, tagged ``origin="data_test"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...ir.models import (
    EnumDefinition,
    EnumValue,
    JoinType,
    ProcessorRelationship,
    RelationshipOrigin,
)
from ..artifacts import DbtManifest


# ---------------------------------------------------------------------------
# not_null / unique / accepted_values
# ---------------------------------------------------------------------------


@dataclass
class TestsResult:
    enum_definitions: list[EnumDefinition] = field(default_factory=list)
    # key: "{unique_id}.{column_name}" → enum name
    column_to_enum_name: dict[str, str] = field(default_factory=dict)
    # key: "{unique_id}.{column_name}" → True
    column_to_not_null: dict[str, bool] = field(default_factory=dict)
    column_to_unique: dict[str, bool] = field(default_factory=dict)


def _sanitize_enum_name(raw: str) -> str:
    """Remove non-alphanumeric chars; prefix underscore if starts with digit."""
    name = re.sub(r"[^a-zA-Z0-9_]", "", raw)
    if name and name[0].isdigit():
        name = "_" + name
    return name or "enum"


def preprocess_tests(manifest: DbtManifest) -> TestsResult:
    """
    Scan manifest test nodes to extract:
    - not_null constraints  → column_to_not_null map
    - unique constraints    → column_to_unique map
    - accepted_values tests → EnumDefinition list + column_to_enum_name map
    """
    result = TestsResult()

    # Map sorted-value-set → enum name (deduplication)
    seen_value_sets: dict[tuple, str] = {}

    for unique_id, node in manifest.nodes.items():
        if not unique_id.startswith("test."):
            continue

        test_metadata = getattr(node, "test_metadata", None)
        if test_metadata is None:
            continue

        attached_node = getattr(node, "attached_node", None) or ""
        column_name = getattr(node, "column_name", None) or ""
        col_key = f"{attached_node}.{column_name}"

        if test_metadata.name == "not_null":
            result.column_to_not_null[col_key] = True

        elif test_metadata.name == "unique":
            result.column_to_unique[col_key] = True

        elif test_metadata.name == "accepted_values":
            raw_values: list[Any] = (test_metadata.kwargs or {}).get("values", [])
            values = [str(v) for v in raw_values]
            if not values:
                continue

            value_key = tuple(sorted(values))
            if value_key in seen_value_sets:
                enum_name = seen_value_sets[value_key]
            else:
                raw_name = f"{column_name}_enum"
                enum_name = _sanitize_enum_name(raw_name)
                base_name = enum_name
                suffix = 1
                while any(e.name == enum_name for e in result.enum_definitions):
                    enum_name = f"{base_name}_{suffix}"
                    suffix += 1

                enum_def = EnumDefinition(
                    name=enum_name,
                    values=[EnumValue(name=v) for v in values],
                )
                result.enum_definitions.append(enum_def)
                seen_value_sets[value_key] = enum_name

            result.column_to_enum_name[col_key] = enum_name

    return result


# ---------------------------------------------------------------------------
# relationships test → ProcessorRelationship
# ---------------------------------------------------------------------------


def _model_name_from_unique_id(unique_id: str) -> str:
    """Extract model name from a unique_id like 'model.project.customers'."""
    parts = unique_id.split(".")
    return parts[-1] if parts else unique_id


def _clean_col(raw: str) -> str:
    """Strip SQL quoting characters from a column name."""
    return raw.strip('"').strip("`")


def build_relationships(manifest: DbtManifest) -> list[ProcessorRelationship]:
    """
    Generate Relationship objects from 'relationships' data-test nodes.

    Uses the typed manifest — no regex needed:
      node.attached_node                → source model unique_id
      node.column_name                  → source column
      node.refs[0].name                 → target model name
      node.test_metadata.kwargs["field"] → target column
    """
    seen: set[str] = set()
    relationships: list[ProcessorRelationship] = []

    for unique_id, node in manifest.nodes.items():
        if not unique_id.startswith("test."):
            continue

        test_metadata = getattr(node, "test_metadata", None)
        if test_metadata is None or test_metadata.name != "relationships":
            continue

        attached_node = getattr(node, "attached_node", None) or ""
        from_model = _model_name_from_unique_id(attached_node)
        from_col = _clean_col(getattr(node, "column_name", None) or "")

        refs = getattr(node, "refs", None) or []
        if not refs:
            continue
        to_model = refs[0].name

        to_col = _clean_col((test_metadata.kwargs or {}).get("field", ""))
        if not to_col or not from_col or not from_model or not to_model:
            continue

        rel_name = f"{from_model}_{from_col}_{to_model}_{to_col}"

        if rel_name in seen:
            continue
        seen.add(rel_name)

        relationships.append(
            ProcessorRelationship(
                name=rel_name,
                models=[from_model, to_model],
                join_type=JoinType.many_to_one,
                origin=RelationshipOrigin.data_test,
                from_columns=[from_col],
                to_columns=[to_col],
            )
        )

    return relationships
