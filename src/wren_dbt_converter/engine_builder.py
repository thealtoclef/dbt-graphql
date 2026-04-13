from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wren import DataSource as WrenDataSource
from wren import WrenEngine
from wren.config import WrenConfig

from .models.wren_mdl import WrenMDLManifest


@dataclass
class EngineConfig:
    """Extra parameters forwarded verbatim to WrenEngine."""

    function_path: str | None = None
    fallback: bool = field(default=True)
    config: WrenConfig | None = None


def build_engine(
    manifest: WrenMDLManifest,
    data_source: WrenDataSource,
    connection_info: dict[str, Any],
    function_path: str | None = None,
    fallback: bool = True,
    config: WrenConfig | None = None,
) -> WrenEngine:
    return WrenEngine(
        manifest_str=manifest.to_manifest_str(),
        data_source=data_source,
        connection_info=connection_info,
        function_path=function_path,
        fallback=fallback,
        config=config,
    )
