"""Extract table-level and column-level lineage from dbt artifacts.

Table-level lineage comes from ``depends_on.nodes`` in the manifest (no SQL
parsing needed).  Column-level lineage is delegated to dbt-colibri which uses
sqlglot to trace each output column back to its source table columns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ColumnLineageEdge:
    source_model: str
    source_column: str
    target_column: str
    lineage_type: str  # "pass-through" | "rename" | "transformation"


@dataclass
class LineageResult:
    # model_name -> list of upstream model names
    table_lineage: dict[str, list[str]] = field(default_factory=dict)
    # model_name -> {target_column -> [edges]}
    column_lineage: dict[str, dict[str, list[ColumnLineageEdge]]] = field(
        default_factory=dict
    )


def _model_name_from_unique_id(unique_id: str) -> str:
    parts = unique_id.split(".")
    return parts[-1] if parts else unique_id


def extract_table_lineage(manifest: Any) -> dict[str, list[str]]:
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


def extract_column_lineage(
    manifest_path: Path,
    catalog_path: Path,
) -> dict[str, dict[str, list[ColumnLineageEdge]]]:
    """Extract column-level lineage via dbt-colibri.

    Returns a dict keyed by model name, then by target column name,
    with a list of :class:`ColumnLineageEdge` values.
    """
    try:
        from dbt_colibri.lineage_extractor.extractor import (
            DbtColumnLineageExtractor,
        )
    except ImportError:
        logger.warning(
            "dbt-colibri not installed — column lineage is unavailable. "
            "Install with: pip install dbt-colibri"
        )
        return {}

    extractor = DbtColumnLineageExtractor(
        manifest_path=str(manifest_path),
        catalog_path=str(catalog_path),
    )
    lineage_data = extractor.extract_project_lineage()
    parents: dict[str, Any] = lineage_data.get("lineage", {}).get("parents", {})

    result: dict[str, dict[str, list[ColumnLineageEdge]]] = {}
    for node_id, columns in parents.items():
        model_name = _model_name_from_unique_id(node_id)
        col_map: dict[str, list[ColumnLineageEdge]] = {}
        for col_name, sources in columns.items():
            if col_name.startswith("__colibri_"):
                continue
            edges: list[ColumnLineageEdge] = []
            for src in sources:
                src_node = src.get("dbt_node", "")
                src_model = _model_name_from_unique_id(src_node) if src_node else ""
                edges.append(
                    ColumnLineageEdge(
                        source_model=src_model,
                        source_column=src.get("column", ""),
                        target_column=col_name,
                        lineage_type=src.get("lineage_type", "transformation"),
                    )
                )
            if edges:
                col_map[col_name] = edges
        if col_map:
            result[model_name] = col_map

    return result


def build_lineage(
    manifest: Any,
    manifest_path: Path,
    catalog_path: Path,
) -> LineageResult:
    """Extract both table-level and column-level lineage."""
    return LineageResult(
        table_lineage=extract_table_lineage(manifest),
        column_lineage=extract_column_lineage(manifest_path, catalog_path),
    )
