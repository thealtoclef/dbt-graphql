from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from wren import DataSource as WrenDataSource, WrenEngine

from .engine_builder import build_engine
from .models.data_source import get_active_connection
from .models.wren_mdl import TableReference, WrenMDLManifest, WrenModel
from .parsers.artifacts import load_catalog, load_manifest
from .parsers.profiles_parser import analyze_dbt_profiles, find_profiles_file
from .processors.columns import convert_columns
from .processors.relationships import build_relationships
from .processors.tests_preprocessor import preprocess_tests

# Staging model name prefixes that can be excluded
_STAGING_PREFIXES = ("stg_", "staging_")


@dataclass
class ConvertResult:
    manifest: WrenMDLManifest
    data_source: WrenDataSource
    connection_info: dict[str, Any]

    @property
    def manifest_str(self) -> str:
        return self.manifest.to_manifest_str()


def build_manifest(
    project_path: str | Path,
    profile_name: Optional[str] = None,
    target: Optional[str] = None,
    include_staging: bool = False,
) -> ConvertResult:
    """
    Convert a dbt project to a Wren MDL manifest without constructing an engine.

    Args:
        project_path: Path to the dbt project root.
        profile_name: Profile name to use. Defaults to the first profile found.
        target: Target name within the profile. Defaults to the profile's default target.
        include_staging: If True, include models with staging prefixes.

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
    catalog_path = project_path / "target" / "catalog.json"
    if not catalog_path.exists():
        raise FileNotFoundError(
            f"catalog.json not found at {catalog_path}. Run 'dbt docs generate' first."
        )
    catalog = load_catalog(catalog_path)

    # 3. Load manifest
    manifest_path = project_path / "target" / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"manifest.json not found at {manifest_path}. "
            "Run 'dbt compile' or 'dbt run' first."
        )
    manifest = load_manifest(manifest_path)

    # 4. Find and parse profiles
    profiles_path = find_profiles_file(project_path)
    if profiles_path is None:
        raise FileNotFoundError(
            "profiles.yml not found in project directory, .dbt/, or ~/.dbt/"
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

    # 7. Build models from catalog nodes
    wren_models: list[WrenModel] = []
    for key, catalog_node in catalog.nodes.items():
        if not key.startswith("model."):
            continue

        model_name: str = catalog_node.metadata.name or key.split(".")[-1]

        if not include_staging and any(
            model_name.startswith(p) for p in _STAGING_PREFIXES
        ):
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

        table_ref = TableReference(
            catalog=db or None,
            schema=schema or None,
            table=model_name,
        )

        # Description from manifest
        props: dict[str, str] = {}
        if manifest_node:
            desc = getattr(manifest_node, "description", None)
            if desc:
                props["description"] = desc

        wren_models.append(
            WrenModel(
                name=model_name,
                tableReference=table_ref,
                columns=columns,
                properties=props if props else None,
            )
        )

    # 8. Build relationships
    relationships = build_relationships(manifest)

    # 9. Assemble MDL manifest
    # Use the first model's db/schema as catalog-level values, or fall back to empty
    mdl_catalog = ""
    mdl_schema = ""
    if wren_models:
        first = wren_models[0]
        mdl_catalog = first.table_reference.catalog or ""
        mdl_schema = first.table_reference.schema_ or ""

    wren_manifest = WrenMDLManifest(
        catalog=mdl_catalog,
        schema=mdl_schema,
        dataSource=str(data_source),
        models=wren_models,
        relationships=relationships,
        enumDefinitions=tests_result.enum_definitions,
    )

    return ConvertResult(
        manifest=wren_manifest,
        data_source=data_source,
        connection_info=connection_info,
    )


def from_dbt_project(
    project_path: str | Path,
    profile_name: Optional[str] = None,
    target: Optional[str] = None,
    include_staging: bool = False,
) -> WrenEngine:
    """
    Convert a dbt project to a ready-to-use WrenEngine instance.

    Args:
        project_path: Path to the dbt project root.
        profile_name: Profile name to use.
        target: Target name within the profile.
        include_staging: If True, include models with staging prefixes.

    Returns:
        WrenEngine constructed from the dbt project's MDL manifest.
    """
    result = build_manifest(
        project_path=project_path,
        profile_name=profile_name,
        target=target,
        include_staging=include_staging,
    )
    return build_engine(result.manifest, result.data_source, result.connection_info)
