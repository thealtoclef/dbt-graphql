"""Helper functions for column filtering."""

from __future__ import annotations

from .models import ColumnDef
from .constants import NUMERIC_GQL_TYPES


def numeric_columns(columns: list[ColumnDef]) -> list[ColumnDef]:
    """Return non-array columns that have numeric GQL types (Int, Float)."""
    return [c for c in columns if c.gql_type in NUMERIC_GQL_TYPES and not c.is_array]


def scalar_columns(columns: list[ColumnDef]) -> list[ColumnDef]:
    """Return all non-array columns."""
    return [c for c in columns if not c.is_array]
