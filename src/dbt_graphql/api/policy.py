"""Access policy engine: column-level and row-level enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2.sandbox import SandboxedEnvironment
from loguru import logger
from pydantic import BaseModel, Field, model_validator
from simpleeval import EvalWithCompoundTypes

from .security import JWTPayload


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


# ---------------------------------------------------------------------------
# Pydantic config models (parsed from access.yml)
# ---------------------------------------------------------------------------


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


class TablePolicy(BaseModel):
    column_level: ColumnLevelPolicy | None = None
    row_level: str | None = None


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


# ---------------------------------------------------------------------------
# Runtime resolved policy (produced per-request per-table)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPolicy:
    # None means unrestricted — all columns allowed.
    allowed_columns: frozenset[str] | None = None
    blocked_columns: frozenset[str] = field(default_factory=frozenset)
    masks: dict[str, str | None] = field(default_factory=dict)
    # Pre-rendered SQL fragment with :named placeholders. The actual values
    # live in row_filter_params and are passed via SQLAlchemy bindparams.
    row_filter_sql: str | None = None
    row_filter_params: dict[str, Any] = field(default_factory=dict)


def render_row_filter(
    template: str, ctx: JWTPayload, *, prefix: str = "p"
) -> tuple[str, dict[str, Any]]:
    """Render a Jinja row-filter template; every {{ expr }} becomes a :bindparam so claim values never reach SQL."""
    params: dict[str, Any] = {}

    def _bind(value: Any) -> str:
        name = f"{prefix}_{len(params)}"
        params[name] = value
        return f":{name}"

    env = SandboxedEnvironment(finalize=_bind)
    return env.from_string(template).render(jwt=ctx), params


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
        return self._merge(matching, ctx)

    def _eval_when(self, expr: str, ctx: JWTPayload) -> bool:
        """Evaluate a when-clause safely via simpleeval."""
        try:
            return bool(EvalWithCompoundTypes(names={"jwt": ctx}).eval(expr))
        except Exception as exc:
            logger.warning("policy when-clause failed: {!r}: {}", expr, exc)
            return False

    def _merge(self, policies: list[TablePolicy], ctx: JWTPayload) -> ResolvedPolicy:
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
                    raise ValueError(
                        f"conflicting masks for column {col!r}: {sorted(exprs, key=lambda x: x or '')}"
                    )
                masks[col] = next(iter(exprs))

        row_parts: list[str] = []
        row_params: dict[str, Any] = {}
        for idx, p in enumerate(policies):
            if not p.row_level:
                continue
            sql, params = render_row_filter(p.row_level, ctx, prefix=f"p{idx}")
            sql = sql.strip()
            if sql:
                row_parts.append(f"({sql})")
                row_params.update(params)

        return ResolvedPolicy(
            allowed_columns=allowed,
            blocked_columns=blocked,
            masks=masks,
            row_filter_sql=" OR ".join(row_parts) if row_parts else None,
            row_filter_params=row_params,
        )
