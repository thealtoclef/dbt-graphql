from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from wren import DataSource as WrenDataSource
from wren.memory.schema_indexer import describe_schema

from .models.data_source import get_active_connection
from .models.wrapper import (
    JoinType,
    Relationship,
    TableReference,
    WrenColumn,
    WrenMDLManifest,
    WrenModel,
    manifest_to_dict,
)
from .parsers.artifacts import load_catalog, load_manifest
from .parsers.profiles_parser import analyze_dbt_profiles, find_profiles_file
from .processors.columns import convert_columns
from .processors.constraints import extract_constraints
from .processors.lineage import build_lineage
from .processors.relationships import build_relationships
from .processors.tests_preprocessor import preprocess_tests

if TYPE_CHECKING:
    from .processors.lineage import LineageResult


@dataclass
class ConvertResult:
    manifest: WrenMDLManifest
    data_source: WrenDataSource
    connection_info: dict[str, Any]
    schema_description: str

    @property
    def manifest_str(self) -> str:
        from .models.wrapper import manifest_to_base64

        return manifest_to_base64(self.manifest)


def build_manifest(
    project_path: str | Path,
    profile_name: Optional[str] = None,
    target: Optional[str] = None,
    exclude_patterns: Optional[list[str]] = None,
    catalog_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> ConvertResult:
    """
    Convert a dbt project to a Wren MDL manifest without constructing an engine.

    Args:
        project_path: Path to the dbt project root (must contain dbt_project.yml and profiles.yml).
        profile_name: Profile name to use. Defaults to the first profile found.
        target: Target name within the profile. Defaults to the profile's default target.
        exclude_patterns: List of regex patterns matched against model names; a model is excluded
            if any pattern matches. Defaults to None (no models excluded).
            Example: [r"^stg_", r"^int_"] to skip staging and intermediate models.
        catalog_path: Path to catalog.json. Defaults to <project_path>/target/catalog.json.
        manifest_path: Path to manifest.json. Defaults to <project_path>/target/manifest.json.

    Returns:
        ConvertResult with manifest, data_source and connection_info.
    """
    project_path = Path(project_path)

    # 1. Validate dbt project
    if not (project_path / "dbt_project.yml").exists():
        raise FileNotFoundError(
            f"Not a valid dbt project (missing dbt_project.yml): {project_path}"
        )

    # 2. Load catalog
    resolved_catalog = catalog_path or (project_path / "target" / "catalog.json")
    if not resolved_catalog.exists():
        raise FileNotFoundError(
            f"catalog.json not found at {resolved_catalog}. Run 'dbt docs generate' first."
        )
    catalog = load_catalog(resolved_catalog)

    # 3. Load manifest
    resolved_manifest = manifest_path or (project_path / "target" / "manifest.json")
    if not resolved_manifest.exists():
        raise FileNotFoundError(
            f"manifest.json not found at {resolved_manifest}. "
            "Run 'dbt compile' or 'dbt run' first."
        )
    manifest = load_manifest(resolved_manifest)

    # 4. Find and parse profiles
    profiles_path = find_profiles_file(project_path)
    if profiles_path is None:
        raise FileNotFoundError(
            f"profiles.yml not found in project directory: {project_path}"
        )
    profiles = analyze_dbt_profiles(profiles_path)

    # 5. Get active connection
    data_source, connection_info = get_active_connection(
        profiles,
        profile_name=profile_name,
        target=target,
        dbt_home=project_path,
    )

    # 6. Preprocess tests (enums, not-null)
    tests_result = preprocess_tests(manifest)

    # 7. Extract constraints (PK/FK from dbt v1.5+)
    constraints_result = extract_constraints(manifest)

    # 8. Build models from catalog nodes
    wren_models: list[WrenModel] = []
    for key, catalog_node in catalog.nodes.items():
        if not key.startswith("model."):
            continue

        model_name: str = catalog_node.metadata.name or key.split(".")[-1]

        if exclude_patterns and any(re.search(p, model_name) for p in exclude_patterns):
            continue

        # Find matching manifest node
        manifest_node = manifest.nodes.get(key)

        columns = convert_columns(
            catalog_node=catalog_node,
            manifest_node=manifest_node,
            data_source=data_source,
            column_to_enum_name=tests_result.column_to_enum_name,
            column_to_not_null=tests_result.column_to_not_null,
        )

        schema = (
            catalog_node.metadata.schema_
            if hasattr(catalog_node.metadata, "schema_")
            else getattr(catalog_node.metadata, "schema", None)
        )
        db = catalog_node.metadata.database

        table_ref_kwargs: dict[str, Any] = {"table": model_name}
        if db:
            table_ref_kwargs["catalog"] = db
        if schema:
            table_ref_kwargs["schema"] = schema

        table_ref = TableReference(**table_ref_kwargs)

        # Description from manifest
        props: dict[str, str] = {}
        if manifest_node:
            desc = getattr(manifest_node, "description", None)
            if desc:
                props["description"] = desc

        wren_models.append(
            WrenModel(
                name=model_name,
                table_reference=table_ref,
                columns=columns,
                primary_key=constraints_result.primary_keys.get(key),
                properties=props if props else None,
            )
        )

    # 9. Build relationships (merge: constraints > tests)
    test_relationships = build_relationships(manifest)
    # Deduplicate: constraint-sourced relationships take priority
    seen_names: set[str] = set()
    relationships: list[Relationship] = []
    for rel in constraints_result.foreign_key_relationships:
        relationships.append(rel)
        seen_names.add(rel.name)
    for rel in test_relationships:
        if rel.name not in seen_names:
            relationships.append(rel)

    # 10. Add relationship columns to models
    model_by_name: dict[str, WrenModel] = {m.name: m for m in wren_models}
    for rel in relationships:
        from_name = rel.models[0]
        to_name = rel.models[1]

        if rel.join_type == JoinType.many_to_one:
            # MANY_TO_ONE: add relationship column on the "many" (from) model
            _add_relationship_column(model_by_name, from_name, to_name, rel.name)
        elif rel.join_type == JoinType.one_to_many:
            # ONE_TO_MANY: add relationship column on the "one" (to) model
            _add_relationship_column(model_by_name, to_name, from_name, rel.name)
        elif rel.join_type in (JoinType.one_to_one, JoinType.many_to_many):
            # Bidirectional: add on both
            _add_relationship_column(model_by_name, from_name, to_name, rel.name)
            _add_relationship_column(model_by_name, to_name, from_name, rel.name)

    # 11. Assemble MDL manifest
    # Use the first model's db/schema as catalog-level values, or fall back to empty
    mdl_catalog = ""
    mdl_schema = ""
    if wren_models:
        first = wren_models[0]
        mdl_catalog = first.table_reference.catalog or ""
        mdl_schema = first.table_reference.schema_ or ""

    wren_manifest = WrenMDLManifest(
        catalog=mdl_catalog,
        schema_=mdl_schema,
        data_source=str(data_source),
        models=wren_models,
        relationships=relationships,
        enum_definitions=tests_result.enum_definitions,
    )

    # 12. Extract lineage
    lineage = build_lineage(manifest, resolved_manifest, resolved_catalog)

    # 13. Embed lineage in model and column properties (bidirectional)
    _embed_lineage(model_by_name, lineage)

    return ConvertResult(
        manifest=wren_manifest,
        data_source=data_source,
        connection_info=connection_info,
        schema_description=describe_schema(manifest_to_dict(wren_manifest)),
    )


def _add_relationship_column(
    model_by_name: dict[str, WrenModel],
    source_model_name: str,
    target_model_name: str,
    relationship_name: str,
) -> None:
    """Add a relationship column to the source model pointing to the target model.

    Uses a lowercase target model name as the column name (e.g., 'customer' on orders).
    Skips if the source model doesn't exist in our models list.
    """
    model = model_by_name.get(source_model_name)
    if model is None:
        return

    col_name = target_model_name.lower()

    # Don't add duplicate relationship columns
    if model.columns and any(c.name == col_name for c in model.columns):
        return

    rel_col = WrenColumn(
        name=col_name,
        type=target_model_name,
        relationship=relationship_name,
    )
    if model.columns is None:
        model.columns = []
    model.columns.append(rel_col)


def _embed_lineage(
    model_by_name: dict[str, WrenModel],
    lineage: LineageResult,
) -> None:
    """Embed bidirectional lineage into model properties.

    Each model gets a ``lineage`` property with ``upstream`` and ``downstream``
    keys.  Each direction contains ``models`` (table-level) and ``columns``
    (column-level, keyed by this model's own column names).
    """
    # --- Reverse indices ---
    # Model-level: who depends on me?
    depended_by: dict[str, list[str]] = {}
    for model_name, upstream in lineage.table_lineage.items():
        for parent in upstream:
            depended_by.setdefault(parent, []).append(model_name)

    # Column-level: (source_model, source_col) -> [(target_model, target_col, type)]
    downstream_cols: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for model_name, col_map in lineage.column_lineage.items():
        for col_name, edges in col_map.items():
            for edge in edges:
                key = (edge.source_model, edge.source_column)
                downstream_cols.setdefault(key, []).append(
                    (model_name, col_name, edge.lineage_type)
                )

    for model in model_by_name.values():
        upstream_models = lineage.table_lineage.get(model.name, [])
        downstream_models = depended_by.get(model.name, [])

        # Upstream columns: this model's col -> [source edges]
        upstream_columns: dict[str, list[dict[str, str]]] = {}
        col_map = lineage.column_lineage.get(model.name, {})
        for col_name, edges in col_map.items():
            upstream_columns[col_name] = [
                {
                    "model": e.source_model,
                    "column": e.source_column,
                    "type": e.lineage_type,
                }
                for e in edges
            ]

        # Downstream columns: this model's col -> [target edges]
        downstream_columns: dict[str, list[dict[str, str]]] = {}
        if model.columns:
            for col in model.columns:
                ds_entries = downstream_cols.get((model.name, col.name))
                if ds_entries:
                    downstream_columns[col.name] = [
                        {"model": tgt_model, "column": tgt_col, "type": ln_type}
                        for tgt_model, tgt_col, ln_type in ds_entries
                    ]

        lineage_prop: dict[str, Any] = {}
        if upstream_models or upstream_columns:
            lineage_prop["upstream"] = {}
            if upstream_models:
                lineage_prop["upstream"]["models"] = upstream_models
            if upstream_columns:
                lineage_prop["upstream"]["columns"] = upstream_columns
        if downstream_models or downstream_columns:
            lineage_prop["downstream"] = {}
            if downstream_models:
                lineage_prop["downstream"]["models"] = downstream_models
            if downstream_columns:
                lineage_prop["downstream"]["columns"] = downstream_columns

        if lineage_prop:
            props: dict[str, Any] = model.properties or {}
            props["lineage"] = lineage_prop
            model.properties = props if props else None
