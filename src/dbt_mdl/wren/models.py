"""Wren MDL models: codegen wrappers and DataSource enum.

The generated models live in ``codegen/mdl.py`` and are produced by
``datamodel-codegen`` from the upstream Wren MDL JSON schema.  This module
re-exports them under the names used throughout the converter so that downstream
code only imports from here — never directly from the generated module.
"""

from __future__ import annotations

from .codegen.mdl import (  # noqa: F401
    Column,
    EnumDefinition,
    JoinType,
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
