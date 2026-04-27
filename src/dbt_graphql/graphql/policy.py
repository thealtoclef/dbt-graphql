"""Access policy engine: column-level and row-level enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, Field, model_validator
from simpleeval import EvalWithCompoundTypes
from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from .auth import JWTPayload
from .row_filter import compile_row_filter, validate_row_filter


# ---------------------------------------------------------------------------
# Policy violation exceptions
# ---------------------------------------------------------------------------


class PolicyError(Exception):
    """Base class for access-policy denials. Raised at compile time.

    Carries a machine-readable ``code`` so resolvers can project it into a
    GraphQL error's ``extensions`` block.
    """

    code: str = "FORBIDDEN"


class TableAccessDenied(PolicyError):
    """Raised when no policy grants access to a requested table."""

    code = "FORBIDDEN_TABLE"

    def __init__(self, table: str) -> None:
        super().__init__(
            f"access denied: no policy authorizes table '{table}' for this subject"
        )
        self.table = table


class ColumnAccessDenied(PolicyError):
    """Raised when the query selects columns not authorized by policy."""

    code = "FORBIDDEN_COLUMN"

    def __init__(self, table: str, columns: list[str]) -> None:
        cols = ", ".join(sorted(columns))
        super().__init__(
            f"access denied: columns [{cols}] on table '{table}' "
            "are not authorized by policy"
        )
        self.table = table
        self.columns = sorted(columns)


class MaskConflictError(PolicyError):
    """Raised when matching policies disagree on the mask expression for a column."""

    code = "POLICY_MASK_CONFLICT"

    def __init__(self, table: str, column: str, exprs: list[str | None]) -> None:
        super().__init__(
            f"conflicting masks for column {column!r} on table {table!r}: "
            f"{sorted(exprs, key=lambda x: x or '')}"
        )
        self.table = table
        self.column = column


# ---------------------------------------------------------------------------
# Pydantic config models (parsed from access.yml)
# ---------------------------------------------------------------------------


_MASK_REJECT_TOKENS = (";", "--", "/*", "*/")


class ColumnLevelPolicy(BaseModel):
    include_all: bool = False
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    mask: dict[str, str | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_exclusive(self) -> "ColumnLevelPolicy":
        if self.include_all and self.includes:
            raise ValueError("include_all and includes are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _check_mask_safety(self) -> "ColumnLevelPolicy":
        for col, expr in self.mask.items():
            if expr is None:
                continue
            for tok in _MASK_REJECT_TOKENS:
                if tok in expr:
                    raise ValueError(
                        f"mask for column {col!r} contains forbidden token "
                        f"{tok!r}: {expr!r}. Statement terminators and "
                        f"comment markers are rejected to prevent malformed "
                        f"compiled SQL."
                    )
        return self


class TablePolicy(BaseModel):
    column_level: ColumnLevelPolicy | None = None
    # Structured DSL — validated against the table registry by
    # ``validate_access_policy_against_registry`` at policy-load time.
    row_filter: dict[str, Any] | None = None


class PolicyEntry(BaseModel):
    name: str
    when: str
    tables: dict[str, TablePolicy] = Field(default_factory=dict)


class AccessPolicy(BaseModel):
    policies: list[PolicyEntry] = Field(default_factory=list)


def load_access_policy(path: str | Path) -> AccessPolicy:
    """Parse access.yml into an AccessPolicy model."""
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("access.yml must be a YAML mapping")
    return AccessPolicy(**data)


def validate_access_policy_against_registry(
    policy: AccessPolicy, registry: Any
) -> None:
    """Walk every ``row_filter`` in the policy and verify column refs.

    Called from the CLI after the registry is built. Catches typos like
    ``orgg_id`` at startup instead of producing a per-request runtime error.
    """
    for entry in policy.policies:
        for table_name, table_policy in entry.tables.items():
            if table_policy.row_filter is None:
                continue
            tdef = registry.get(table_name)
            if tdef is None:
                raise ValueError(
                    f"policy {entry.name!r} references row_filter on unknown "
                    f"table {table_name!r}"
                )
            allowed = {c.name for c in tdef.columns}
            try:
                validate_row_filter(table_policy.row_filter, allowed_columns=allowed)
            except ValueError as exc:
                raise ValueError(
                    f"policy {entry.name!r} table {table_name!r}: {exc}"
                ) from exc


# ---------------------------------------------------------------------------
# Runtime resolved policy (produced per-request per-table)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPolicy:
    # None means unrestricted — all columns allowed.
    allowed_columns: frozenset[str] | None = None
    blocked_columns: frozenset[str] = field(default_factory=frozenset)
    masks: dict[str, str | None] = field(default_factory=dict)
    # SQLAlchemy clause to AND into the SELECT's WHERE. None = no row filter.
    row_filter_clause: ColumnElement | None = None


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    def __init__(self, access_policy: AccessPolicy) -> None:
        self._policy = access_policy

    def evaluate(self, table_name: str, ctx: JWTPayload) -> ResolvedPolicy:
        """Return the merged ResolvedPolicy for ``table_name`` given ``ctx``.

        Default-deny: if no loaded policy matches both the ``when`` clause
        and the requested table, raise ``TableAccessDenied``. Operators
        must explicitly list every table a role may read.
        """
        matching: list[TablePolicy] = []
        for entry in self._policy.policies:
            if self._eval_when(entry.when, ctx) and table_name in entry.tables:
                matching.append(entry.tables[table_name])

        if not matching:
            raise TableAccessDenied(table_name)
        return self._merge(table_name, matching, ctx)

    def _eval_when(self, expr: str, ctx: JWTPayload) -> bool:
        """Evaluate a when-clause safely via simpleeval."""
        try:
            return bool(EvalWithCompoundTypes(names={"jwt": ctx}).eval(expr))
        except Exception as exc:
            logger.warning("policy when-clause failed: {!r}: {}", expr, exc)
            return False

    def _merge(
        self, table_name: str, policies: list[TablePolicy], ctx: JWTPayload
    ) -> ResolvedPolicy:
        col_policies = [p.column_level for p in policies if p.column_level is not None]

        allowed: frozenset[str] | None = None
        blocked: frozenset[str] = frozenset()
        masks: dict[str, str | None] = {}

        if col_policies:
            if any(cp.include_all for cp in col_policies):
                allowed = None
            else:
                union: set[str] = set()
                for cp in col_policies:
                    union.update(cp.includes)
                allowed = frozenset(union)

            # intersection: most-permissive — blocked only when all policies agree
            exclude_sets = [frozenset(cp.excludes) for cp in col_policies]
            blocked = (
                frozenset.intersection(*exclude_sets) if exclude_sets else frozenset()
            )

            # mask only when all matching policies specify it AND agree on the expression
            common = set.intersection(*(set(cp.mask.keys()) for cp in col_policies))
            for col in common:
                exprs = {cp.mask[col] for cp in col_policies}
                if len(exprs) > 1:
                    raise MaskConflictError(table_name, col, list(exprs))
                masks[col] = next(iter(exprs))

        clauses: list[ColumnElement] = []
        for idx, p in enumerate(policies):
            if p.row_filter is None:
                continue
            clauses.append(compile_row_filter(p.row_filter, ctx, prefix=f"p{idx}"))

        if not clauses:
            row_filter_clause: ColumnElement | None = None
        elif len(clauses) == 1:
            row_filter_clause = clauses[0]
        else:
            row_filter_clause = or_(*clauses)

        return ResolvedPolicy(
            allowed_columns=allowed,
            blocked_columns=blocked,
            masks=masks,
            row_filter_clause=row_filter_clause,
        )
