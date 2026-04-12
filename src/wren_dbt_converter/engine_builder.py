from __future__ import annotations

from typing import Any

from wren import DataSource as WrenDataSource, WrenEngine

from .models.wren_mdl import WrenMDLManifest


def build_engine(
    manifest: WrenMDLManifest,
    data_source: WrenDataSource,
    connection_info: dict[str, Any],
) -> WrenEngine:
    return WrenEngine(
        manifest_str=manifest.to_manifest_str(),
        data_source=data_source,
        connection_info=connection_info,
    )
