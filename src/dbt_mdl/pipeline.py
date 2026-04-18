"""Core pipeline: extract domain-neutral project info from dbt artifacts.

This module implements the parsing/extraction pipeline. It produces a
:class:`ProjectInfo` which is then consumed by formatters to produce
format-specific output (MDL, GraphJin, etc.).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .ir.models import ColumnInfo, ProjectInfo, ModelInfo, RelationshipInfo
from .dbt.artifacts import load_catalog, load_manifest
from .dbt.processors.constraints import extract_constraints
from .dbt.processors.lineage import extract_table_lineage
from .dbt.processors.relationships import build_relationships
from .dbt.processors.tests_preprocessor import preprocess_tests


def extract_project(
    catalog_path: str | Path,
    manifest_path: str | Path,
    exclude_patterns: Optional[list[str]] = None,
) -> ProjectInfo:
    """Extract domain-neutral project information from a dbt project.

    Args:
        catalog_path: Path to catalog.json.
        manifest_path: Path to manifest.json.
        exclude_patterns: Regex patterns matched against model names; matching models excluded.

    Returns:
        ProjectInfo with models, relationships, enums, and lineage.
    """
    catalog_path = Path(catalog_path)
    manifest_path = Path(manifest_path)

    # 1. Load catalog
    if not catalog_path.exists():
        raise FileNotFoundError(
            f"catalog.json not found at {catalog_path}. Run 'dbt docs generate' first."
        )
    catalog = load_catalog(catalog_path)

    # 2. Load manifest
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"manifest.json not found at {manifest_path}. "
            "Run 'dbt compile' or 'dbt run' first."
        )
    manifest = load_manifest(manifest_path)

    # 3. Get adapter type from manifest metadata
    adapter_type: str = getattr(manifest.metadata, "adapter_type", "") or ""

    # 4. Preprocess tests (enums, not-null)
    tests_result = preprocess_tests(manifest)

    # 5. Extract constraints (PK/FK from dbt v1.5+)
    constraints_result = extract_constraints(manifest)

    # 6. Build models from catalog nodes
    models: list[ModelInfo] = []
    for key, catalog_node in catalog.nodes.items():
        if not key.startswith("model."):
            continue

        model_name: str = catalog_node.metadata.name or key.split(".")[-1]

        if exclude_patterns and any(re.search(p, model_name) for p in exclude_patterns):
            continue

        # Find matching manifest node
        manifest_node = manifest.nodes.get(key)

        # Build columns
        catalog_columns: dict = catalog_node.columns or {}
        manifest_columns: dict = getattr(manifest_node, "columns", {})

        pk_col = constraints_result.primary_keys.get(key)

        columns: list[ColumnInfo] = []
        for col_name, col_meta in catalog_columns.items():
            col_key = f"{key}.{col_name}"
            raw_type = col_meta.type or ""

            not_null = tests_result.column_to_not_null.get(col_key, False)
            unique = tests_result.column_to_unique.get(col_key, False)
            enum_name = tests_result.column_to_enum_name.get(col_key)
            enum_values = None
            if enum_name:
                for enum_def in tests_result.enum_definitions:
                    if enum_def.name == enum_name:
                        enum_values = [v.name for v in enum_def.values]
                        break

            description = getattr(manifest_columns.get(col_name), "description", "")

            columns.append(
                ColumnInfo(
                    name=col_name,
                    type=raw_type,
                    not_null=not_null,
                    unique=unique,
                    description=description,
                    enum_values=enum_values,
                    is_primary_key=(col_name == pk_col),
                    is_hidden=False,
                )
            )

        # Sort by catalog index, then by name
        def sort_key(col: ColumnInfo) -> tuple:
            cat_col = catalog_columns.get(col.name)
            idx = cat_col.index if cat_col and cat_col.index is not None else 9999
            return (idx, col.name)

        columns.sort(key=sort_key)

        # Get database and schema from catalog
        database = catalog_node.metadata.database
        if database is None:
            raise ValueError(
                f"database is required for model {model_name} but could not be "
                f"determined from catalog"
            )
        schema = catalog_node.metadata.schema_

        # Get alias and description from manifest node
        model_alias = getattr(manifest_node, "alias", None)
        description = getattr(manifest_node, "description", "")

        models.append(
            ModelInfo(
                name=model_name,
                alias=model_alias,
                database=database,
                schema_=schema,
                columns=columns,
                primary_key=pk_col,
                description=description,
            )
        )

    # 7. Build relationships (merge: constraints > tests)
    test_relationships = build_relationships(manifest)
    seen_names: set[str] = set()
    relationships: list[RelationshipInfo] = []

    for rel in constraints_result.foreign_key_relationships:
        relationships.append(_wren_rel_to_domain(rel))
        seen_names.add(rel.name)
    for rel in test_relationships:
        if rel.name not in seen_names:
            relationships.append(_wren_rel_to_domain(rel))

    # 8. Attach relationships to models
    model_by_name: dict[str, ModelInfo] = {m.name: m for m in models}
    for rel in relationships:
        from_model = model_by_name.get(rel.from_model)
        to_model = model_by_name.get(rel.to_model)
        if from_model:
            from_model.relationships.append(rel)
        if to_model:
            to_model.relationships.append(rel)

    # 9. Extract lineage
    table_lineage = extract_table_lineage(manifest)

    # Column lineage (via dbt-colibri)
    column_lineage: dict[str, dict[str, list[dict[str, str]]]] = {}
    try:
        from .dbt.processors.lineage import extract_column_lineage

        col_result = extract_column_lineage(manifest_path, catalog_path)
        for model_name, col_map in col_result.items():
            column_lineage[model_name] = {}
            for col_name, edges in col_map.items():
                column_lineage[model_name][col_name] = [
                    {
                        "source_model": e.source_model,
                        "source_column": e.source_column,
                        "target_column": e.target_column,
                        "lineage_type": e.lineage_type,
                    }
                    for e in edges
                ]
    except Exception:
        pass  # dbt-colibri not available

    # 10. Build enums dict
    enums: dict[str, list[str]] = {}
    for enum_def in tests_result.enum_definitions:
        enums[enum_def.name] = [v.name for v in enum_def.values]

    return ProjectInfo(
        models=models,
        relationships=relationships,
        enums=enums,
        table_lineage=table_lineage,
        column_lineage=column_lineage,
        adapter_type=adapter_type,
    )


def _wren_rel_to_domain(rel: Any) -> RelationshipInfo:
    """Convert a Wren Relationship object to domain RelationshipInfo."""
    from_col = ""
    to_col = ""
    if hasattr(rel, "condition") and rel.condition:
        match = re.match(r'"(\w+)"\."(\w+)"\s*=\s*"(\w+)"\."(\w+)"', rel.condition)
        if match:
            from_col = match.group(2)
            to_col = match.group(4)

    return RelationshipInfo(
        name=rel.name,
        from_model=rel.models[0],
        from_column=from_col,
        to_model=rel.models[1],
        to_column=to_col,
        join_type=str(rel.join_type.value)
        if hasattr(rel.join_type, "value")
        else str(rel.join_type),
    )
