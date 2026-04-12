from __future__ import annotations

import base64
import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class EnumValue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    value: Optional[str] = None


class EnumDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    values: list[EnumValue]


class TableReference(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    catalog: Optional[str] = None
    schema_: Optional[str] = Field(None, alias="schema")
    table: str

    def model_dump_camel(self) -> dict:
        d: dict = {"table": self.table}
        if self.catalog is not None:
            d["catalog"] = self.catalog
        if self.schema_ is not None:
            d["schema"] = self.schema_
        return d


class WrenColumn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    type: str
    display_name: Optional[str] = Field(None, alias="displayName")
    is_calculated: bool = Field(False, alias="isCalculated")
    not_null: bool = Field(False, alias="notNull")
    expression: Optional[str] = None
    relationship: Optional[str] = None
    properties: Optional[dict[str, str]] = None

    def model_dump_camel(self) -> dict:
        d: dict = {"name": self.name, "type": self.type}
        if self.display_name is not None:
            d["displayName"] = self.display_name
        if self.is_calculated:
            d["isCalculated"] = self.is_calculated
        if self.not_null:
            d["notNull"] = self.not_null
        if self.expression is not None:
            d["expression"] = self.expression
        if self.relationship is not None:
            d["relationship"] = self.relationship
        if self.properties:
            d["properties"] = self.properties
        return d


class WrenModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    table_reference: TableReference = Field(alias="tableReference")
    columns: list[WrenColumn]
    primary_key: Optional[str] = Field(None, alias="primaryKey")
    cached: bool = False
    refresh_time: Optional[str] = Field(None, alias="refreshTime")
    properties: Optional[dict[str, str]] = None

    def model_dump_camel(self) -> dict:
        d: dict = {
            "name": self.name,
            "tableReference": self.table_reference.model_dump_camel(),
            "columns": [c.model_dump_camel() for c in self.columns],
        }
        if self.primary_key is not None:
            d["primaryKey"] = self.primary_key
        if self.cached:
            d["cached"] = self.cached
        if self.refresh_time is not None:
            d["refreshTime"] = self.refresh_time
        if self.properties:
            d["properties"] = self.properties
        return d


class Relationship(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    models: list[str]
    join_type: str = Field(alias="joinType")
    condition: str
    properties: Optional[dict[str, str]] = None

    def model_dump_camel(self) -> dict:
        d: dict = {
            "name": self.name,
            "models": self.models,
            "joinType": self.join_type,
            "condition": self.condition,
        }
        if self.properties:
            d["properties"] = self.properties
        return d


class WrenMDLManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    catalog: str
    schema_: str = Field(alias="schema")
    data_source: Optional[str] = Field(None, alias="dataSource")
    models: list[WrenModel] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    enum_definitions: list[EnumDefinition] = Field(
        default_factory=list, alias="enumDefinitions"
    )
    views: list[dict] = Field(default_factory=list)

    def to_camel_dict(self) -> dict:
        d: dict = {
            "catalog": self.catalog,
            "schema": self.schema_,
            "models": [m.model_dump_camel() for m in self.models],
            "relationships": [r.model_dump_camel() for r in self.relationships],
            "views": self.views,
        }
        if self.data_source is not None:
            d["dataSource"] = self.data_source
        if self.enum_definitions:
            d["enumDefinitions"] = [
                {
                    "name": e.name,
                    "values": [
                        {"name": v.name, **({"value": v.value} if v.value else {})}
                        for v in e.values
                    ],
                }
                for e in self.enum_definitions
            ]
        return d

    def to_manifest_str(self) -> str:
        """Return base64-encoded camelCase JSON string."""
        payload = json.dumps(self.to_camel_dict(), separators=(",", ":"))
        return base64.b64encode(payload.encode()).decode()
