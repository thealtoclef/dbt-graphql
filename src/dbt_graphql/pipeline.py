"""Core pipeline: extract domain-neutral project info from dbt artifacts.

This module implements the parsing/extraction pipeline. It produces a
:class:`ProjectInfo` which is then consumed by formatters to produce
format-specific output.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .ir.models import (
    ColumnInfo,
    ProjectInfo,
    ModelInfo,
    RelationshipInfo,
)
from .dbt.artifacts import load_catalog, load_manifest
from .dbt.processors.compiled_sql import (
    extract_column_lineage,
    extract_join_relationships,
    extract_table_lineage,
)
from .dbt.processors.constraints import extract_constraints
from .dbt.processors.data_tests import build_relationships, preprocess_tests


def extract_project(
    catalog_path: str,
    manifest_path: str,
    exclude_patterns: Optional[list[str]] = None,
) -> ProjectInfo:
    """Extract domain-neutral project information from a dbt project.

    Args:
        catalog_path: fsspec-compatible URI to catalog.json (local path,
            ``file://``, ``gs://``, ``s3://``, ``http(s)://``, ...).
        manifest_path: fsspec-compatible URI to manifest.json.
        exclude_patterns: Regex patterns matched against model names; matching models excluded.

    Returns:
        ProjectInfo with models, relationships, enums, and lineage.
    """
    catalog = load_catalog(str(catalog_path))
    manifest = load_manifest(str(manifest_path))

    # 3. Get project name and adapter type from manifest metadata
    project_name: str = manifest.metadata.project_name
    adapter_type: str = manifest.metadata.adapter_type

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
        catalog_columns: dict = catalog_node.columns
        manifest_columns: dict = manifest_node.columns if manifest_node else {}

        pk_cols: list[str] = constraints_result.primary_keys.get(key, [])

        enum_values_by_name: dict[str, list[str]] = {
            ed.name: [v.name for v in ed.values] for ed in tests_result.enum_definitions
        }

        columns: list[ColumnInfo] = []
        for raw_col_name, col_meta in catalog_columns.items():
            # Strip SQL quoting characters that some adapters emit in the catalog
            col_name = raw_col_name.strip('"').strip("`")
            col_key = f"{key}.{col_name}"
            raw_type = col_meta.type or ""

            not_null = tests_result.column_to_not_null.get(col_key, False)
            unique = tests_result.column_to_unique.get(col_key, False)
            enum_name = tests_result.column_to_enum_name.get(col_key)
            enum_values = enum_values_by_name.get(enum_name) if enum_name else None

            man_col = manifest_columns.get(col_name)
            description = man_col.description if man_col else ""

            columns.append(
                ColumnInfo(
                    name=col_name,
                    type=raw_type,
                    not_null=not_null,
                    unique=unique,
                    is_primary_key=col_name in pk_cols,
                    description=description,
                    enum_values=enum_values,
                )
            )

        # Sort by catalog index, then by name
        def sort_key(col: ColumnInfo) -> tuple:
            cat_col = catalog_columns.get(col.name)
            idx = cat_col.index if cat_col and cat_col.index is not None else 9999
            return (idx, col.name)

        columns.sort(key=sort_key)

        # Get database and schema from catalog.
        # MySQL doesn't populate database; fall back to schema.
        schema = catalog_node.metadata.schema_
        database = catalog_node.metadata.database or schema

        # Get alias, description, and tags from manifest node
        model_alias = manifest_node.alias if manifest_node else None
        description = manifest_node.description if manifest_node else ""
        tags = list(getattr(manifest_node, "tags", None) or []) if manifest_node else []

        models.append(
            ModelInfo(
                name=model_name,
                alias=model_alias,
                database=database,
                schema_=schema,  # type: ignore[ty:unknown-argument]
                columns=columns,
                primary_keys=pk_cols,
                description=description,
                tags=tags,
            )  # type: ignore[ty:missing-argument]
        )

    # 7. Build relationships (merge: constraints > data_tests > compiled_sql)
    constraint_relationships = constraints_result.foreign_key_relationships
    data_test_relationships = build_relationships(manifest)
    compiled_sql_relationships = extract_join_relationships(manifest, catalog)
    seen_names: set[str] = set()
    relationships: list[RelationshipInfo] = []

    # Build a set of (model_name, col_name) pairs known to be unique,
    # from both unique tests and primary-key constraints. Used to infer cardinality.
    unique_cols: set[tuple[str, str]] = set()
    for col_key in tests_result.column_to_unique:
        uid, _, col = col_key.rpartition(".")
        unique_cols.add((uid.split(".")[-1], col))
    for uid, pk_cols_list in constraints_result.primary_keys.items():
        if (
            len(pk_cols_list) == 1
        ):  # composite PKs don't make any individual column unique
            unique_cols.add((uid.split(".")[-1], pk_cols_list[0]))

    for rel in constraint_relationships:
        relationships.append(_rel_to_domain(rel, unique_cols))
        seen_names.add(rel.name)
    for rel in data_test_relationships:
        if rel.name not in seen_names:
            relationships.append(_rel_to_domain(rel, unique_cols))
            seen_names.add(rel.name)
    for rel in compiled_sql_relationships:
        if rel.name not in seen_names:
            relationships.append(_rel_to_domain(rel, unique_cols))
            seen_names.add(rel.name)

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
    column_lineage = extract_column_lineage(manifest, catalog)

    # 10. Build enums dict
    enums: dict[str, list[str]] = {}
    for enum_def in tests_result.enum_definitions:
        enums[enum_def.name] = [v.name for v in enum_def.values]

    return ProjectInfo(
        project_name=project_name,
        adapter_type=adapter_type,
        models=models,
        relationships=relationships,
        enums=enums,
        table_lineage=table_lineage,
        column_lineage=column_lineage,
    )


def _rel_to_domain(
    rel: Any, unique_cols: set[tuple[str, str]] | None = None
) -> RelationshipInfo:
    """Convert a ProcessorRelationship to domain RelationshipInfo."""
    from_cols = list(rel.from_columns)
    to_cols = list(rel.to_columns)

    from_model = rel.models[0]
    to_model = rel.models[1]

    return RelationshipInfo(
        name=rel.name,
        from_model=from_model,
        to_model=to_model,
        from_columns=from_cols,
        to_columns=to_cols,
        cardinality=rel.cardinality,
        origin=rel.origin,
    )
