from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models.wren_mdl import EnumDefinition, EnumValue


@dataclass
class TestsResult:
    enum_definitions: list[EnumDefinition] = field(default_factory=list)
    # key: "{unique_id}.{column_name}" → enum name
    column_to_enum_name: dict[str, str] = field(default_factory=dict)
    # key: "{unique_id}.{column_name}" → True
    column_to_not_null: dict[str, bool] = field(default_factory=dict)


def _sanitize_enum_name(raw: str) -> str:
    """Remove non-alphanumeric chars; prefix underscore if starts with digit."""
    name = re.sub(r"[^a-zA-Z0-9_]", "", raw)
    if name and name[0].isdigit():
        name = "_" + name
    return name or "enum"


def preprocess_tests(manifest) -> TestsResult:
    """
    Scan manifest test nodes to extract:
    - not_null constraints  → column_to_not_null map
    - accepted_values tests → EnumDefinition list + column_to_enum_name map
    """
    result = TestsResult()

    # Map sorted-value-set → enum name (deduplication)
    seen_value_sets: dict[tuple, str] = {}

    for unique_id, node in manifest.nodes.items():
        if not unique_id.startswith("test."):
            continue

        # Nodes6 in v12 (resource_type='test') has test_metadata
        test_metadata = getattr(node, "test_metadata", None)
        if test_metadata is None:
            continue

        attached_node = getattr(node, "attached_node", None) or ""
        column_name = getattr(node, "column_name", None) or ""
        col_key = f"{attached_node}.{column_name}"

        if test_metadata.name == "not_null":
            result.column_to_not_null[col_key] = True

        elif test_metadata.name == "accepted_values":
            raw_values: list[Any] = (test_metadata.kwargs or {}).get("values", [])
            values = [str(v) for v in raw_values]
            if not values:
                continue

            value_key = tuple(sorted(values))
            if value_key in seen_value_sets:
                enum_name = seen_value_sets[value_key]
            else:
                # Derive enum name from the column name
                raw_name = f"{column_name}_enum"
                enum_name = _sanitize_enum_name(raw_name)
                # Ensure uniqueness: append suffix if collision
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
