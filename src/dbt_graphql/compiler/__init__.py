"""Compiler package - lazy imports to avoid circular dependencies."""

from __future__ import annotations

__all__ = [
    "DatabaseManager",
    "build_db_url",
    "compile_query",
]


def __getattr__(name: str):
    if name == "DatabaseManager" or name == "build_db_url":
        from .connection import DatabaseManager, build_db_url

        return DatabaseManager if name == "DatabaseManager" else build_db_url
    if name == "compile_query":
        from .query import compile_query

        return compile_query
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
