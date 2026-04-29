"""Intermediate representation: format-agnostic domain models.

These Pydantic models decouple dbt artifact parsing from any specific output
format. Processors populate these types, and formatters
consume them to produce format-specific output.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import StrEnum, auto
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


# ---------------------------------------------------------------------------
# Processor types (used by dbt/processors — format-agnostic)
# ---------------------------------------------------------------------------


class Cardinality(StrEnum):
    many_to_one = auto()
    one_to_many = auto()
    one_to_one = auto()
    many_to_many = auto()


class RelationshipOrigin(StrEnum):
    constraint = auto()
    data_test = auto()
    lineage = auto()


@dataclass
class ProcessorRelationship:
    name: str
    models: list[str]
    cardinality: Cardinality
    origin: RelationshipOrigin
    from_columns: list[str] = dc_field(default_factory=list)
    to_columns: list[str] = dc_field(default_factory=list)


@dataclass
class EnumValue:
    name: str


@dataclass
class EnumDefinition:
    name: str
    values: list[EnumValue] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Project / model / column / relationship
# ---------------------------------------------------------------------------


class ColumnInfo(BaseModel):
    name: str
    type: str  # raw DB type from catalog.json (e.g. "INTEGER", "VARCHAR(255)")
    not_null: bool = False
    unique: bool = False
    is_primary_key: bool = False
    description: str = ""
    enum_values: list[str] | None = None


class RelationshipInfo(BaseModel):
    """A foreign-key relationship between two models."""

    name: str
    from_model: str
    to_model: str
    from_columns: list[str] = Field(default_factory=list)
    to_columns: list[str] = Field(default_factory=list)
    cardinality: Cardinality
    origin: RelationshipOrigin


class ModelInfo(BaseModel):
    """A dbt model (maps to a physical table in the database)."""

    name: str  # dbt model name
    alias: str | None = None  # warehouse entity name (defaults to name if not set)
    database: str
    schema_: str = Field(alias="schema")
    columns: list[ColumnInfo] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def relation_name(self) -> str:
        """Warehouse entity name: alias if set, else name."""
        return self.alias if self.alias else self.name


class ProjectInfo(BaseModel):
    """Complete extracted information from a dbt project.

    This is the intermediate representation that formatters consume.
    """

    project_name: str
    adapter_type: str
    models: list[ModelInfo] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)
    enums: dict[str, list[str]] = Field(default_factory=dict)
    table_lineage: list[TableLineageItem] = Field(default_factory=list)
    column_lineage: list[ColumnLineageItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


class TableLineageItem(BaseModel):
    """A single table-level lineage edge (source feeds into target)."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    source: Annotated[str, StringConstraints(min_length=1)] = Field(
        ..., description="The upstream (feeding) model name."
    )
    target: Annotated[str, StringConstraints(min_length=1)] = Field(
        ..., description="The downstream (consuming) model name."
    )


class LineageType(StrEnum):
    """Classification of how a column value is propagated (mirrors dbt-colibri)."""

    pass_through = auto()
    rename = auto()
    transformation = auto()


class Column(BaseModel):
    """A single column-level lineage mapping within an edge."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    source_column: Annotated[str, StringConstraints(min_length=1)] = Field(
        ..., alias="sourceColumn", description="Column name in the source model."
    )
    target_column: Annotated[str, StringConstraints(min_length=0)] = Field(
        ...,
        alias="targetColumn",
        description="Column name in the target model. Empty for structural edges (filter/join/unknown).",
    )
    lineage_type: LineageType = Field(
        ...,
        alias="lineageType",
        description="Values: pass_through, rename, transformation, filter, join, unknown.",
    )


class ColumnLineageItem(BaseModel):
    """Column-level lineage edges grouped by a single table-level relationship."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    source: Annotated[str, StringConstraints(min_length=1)] = Field(
        ..., description="The upstream (feeding) model name."
    )
    target: Annotated[str, StringConstraints(min_length=1)] = Field(
        ..., description="The downstream (consuming) model name."
    )
    columns: list[Column] = Field(..., description="Column-level lineage mappings.")
