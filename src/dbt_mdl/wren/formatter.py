"""Format dbt project info as Wren MDL (Metadata Definition Language).

Produces mdl.json and connection.json.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel, Field

from ..domain.models import (
    ColumnInfo,
    DbtProjectInfo,
    ModelInfo,
    RelationshipInfo,
)
from .models import (
    EnumDefinition,
    JoinType,
    Relationship,
    TableReference,
    Value,
    WrenColumn,
    WrenMDLManifest,
    WrenModel,
)
from .type_mapping import map_column_type

# dbt adapter type -> WrenMDL schema-compatible data_source value
_DB_TYPE_MAP: dict[str, str] = {
    "doris": "mysql",
    "sqlserver": "mssql",
    "postgresql": "postgres",
}


class ConvertResult(BaseModel):
    """Result of formatting dbt project info as Wren MDL."""

    manifest: WrenMDLManifest
    data_source: str | None = None
    connection_info: dict[str, Any] = Field(default_factory=dict)

    @property
    def manifest_str(self) -> str:
        payload = self.manifest.model_dump_json(by_alias=True, exclude_none=True)
        return base64.b64encode(payload.encode()).decode()


def format_mdl(project: DbtProjectInfo) -> ConvertResult:
    """Convert domain-neutral DbtProjectInfo into Wren MDL format."""
    conn_type = project.connection.type
    data_source = _parse_data_source(conn_type)

    # Map models
    wren_models: list[WrenModel] = []
    for model in project.models:
        wren_columns = [_column_to_mdl(c, data_source) for c in model.columns]

        # Add relationship columns
        for rel in model.relationships:
            col = _relationship_column(model, rel)
            if col:
                wren_columns.append(col)

        table_ref_kwargs: dict[str, str] = {
            "catalog": model.database,
            "schema": model.schema_,
            "table": model.relation_name,
        }

        props: dict[str, str] = {}
        if model.description:
            props["description"] = model.description

        wren_models.append(
            WrenModel(
                name=model.name,
                table_reference=TableReference(**table_ref_kwargs),
                columns=wren_columns,
                primary_key=model.primary_key,
                properties=props if props else None,
            )
        )

    # Map relationships
    wren_relationships = [_rel_to_mdl(r) for r in project.relationships]

    # Map enums
    enum_definitions = [
        EnumDefinition(name=name, values=[Value(name=v) for v in values])
        for name, values in project.enums.items()
    ]

    # Catalog/schema from first model
    mdl_catalog = ""
    mdl_schema = ""
    if project.models:
        first = project.models[0]
        mdl_catalog = first.database or ""
        mdl_schema = first.schema_ or ""

    data_source_value = _DB_TYPE_MAP.get(conn_type.lower(), conn_type)

    wren_manifest = WrenMDLManifest(
        catalog=mdl_catalog,
        schema_=mdl_schema,
        data_source=data_source_value,
        models=wren_models,
        relationships=wren_relationships,
        enum_definitions=enum_definitions if enum_definitions else None,
    )

    return ConvertResult(
        manifest=wren_manifest,
        data_source=data_source,
    )


def _parse_data_source(data_source: str) -> str:
    """Parse a data source string, normalizing via _DB_TYPE_MAP."""
    if "." in data_source:
        data_source = data_source.rsplit(".", 1)[-1].lower()
    return _DB_TYPE_MAP.get(data_source, data_source)


def _column_to_mdl(col: ColumnInfo, data_source: str) -> WrenColumn:
    """Convert a domain ColumnInfo to a Wren MDL Column."""
    wren_type = map_column_type(data_source, col.type)

    props: dict[str, str] = {}
    if col.description:
        props["description"] = col.description

    return WrenColumn(
        name=col.name,
        type=wren_type,
        not_null=col.not_null if col.not_null else None,
        properties=props if props else None,
    )


def _relationship_column(model: ModelInfo, rel: RelationshipInfo) -> WrenColumn | None:
    """Create a relationship column on the model if applicable."""
    join_type_map = {
        "many_to_one": JoinType.many_to_one,
        "one_to_many": JoinType.one_to_many,
        "one_to_one": JoinType.one_to_one,
        "many_to_many": JoinType.many_to_many,
    }
    join_type = join_type_map.get(rel.join_type, JoinType.many_to_one)

    # Determine if this model should get the relationship column
    is_from = model.name == rel.from_model
    is_to = model.name == rel.to_model

    if join_type == JoinType.many_to_one and is_from:
        target_name = rel.to_model
    elif join_type == JoinType.one_to_many and is_to:
        target_name = rel.from_model
    elif join_type in (JoinType.one_to_one, JoinType.many_to_many):
        target_name = rel.to_model if is_from else rel.from_model
    else:
        return None

    col_name = target_name.lower()

    # Don't add duplicate
    if any(c.name == col_name for c in model.columns):
        return None

    return WrenColumn(
        name=col_name,
        type=target_name,
        relationship=rel.name,
    )


def _rel_to_mdl(rel: RelationshipInfo) -> Relationship:
    """Convert a domain RelationshipInfo to Wren MDL Relationship."""
    join_type_map = {
        "many_to_one": JoinType.many_to_one,
        "one_to_many": JoinType.one_to_many,
        "one_to_one": JoinType.one_to_one,
        "many_to_many": JoinType.many_to_many,
    }
    condition = (
        f'"{rel.from_model}"."{rel.from_column}" = "{rel.to_model}"."{rel.to_column}"'
    )
    return Relationship(
        name=rel.name,
        models=[rel.from_model, rel.to_model],
        join_type=join_type_map.get(rel.join_type, JoinType.many_to_one),
        condition=condition,
    )
