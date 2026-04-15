"""Wraps generated MDL types with domain names and serialization helpers.

The generated models (mdl.py) define the canonical types matching the JSON schema.
This module re-exports them under the names used throughout the converter, so that
downstream code only imports from here — never directly from mdl.py.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .mdl import (  # noqa: F401
    Column,
    EnumDefinition,
    JoinType,
    Models,
    Models1,
    Models2,
    Relationship,
    TableReference,
    Value,
    WrenmdlManifestSchema,
)

# Domain aliases — the rest of the codebase uses these names
WrenColumn = Column
WrenModel = Models2
WrenMDLManifest = WrenmdlManifestSchema
EnumValue = Value

AnyModel = Models | Models1 | Models2


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def manifest_to_dict(manifest: WrenMDLManifest) -> dict[str, Any]:
    """Serialize a WrenMDLManifest to a camelCase dict (matching JSON schema).

    Uses mode='json' to ensure enums and other non-JSON-native types are
    converted to their JSON-compatible representations.
    """
    return manifest.model_dump(by_alias=True, exclude_none=True, mode="json")


def manifest_to_base64(manifest: WrenMDLManifest) -> str:
    """Return base64-encoded camelCase JSON string (what wren-engine expects)."""
    payload = json.dumps(manifest_to_dict(manifest), separators=(",", ":"))
    return base64.b64encode(payload.encode()).decode()
