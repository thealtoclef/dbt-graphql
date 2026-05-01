"""Access policy engine: column-level and row-level enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field, model_validator
from simpleeval import EvalWithCompoundTypes
from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from .row_filter import compile_row_filter, validate_row_filter

if TYPE_CHECKING:
    from .auth import JWTPayload


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
# Pydantic config models (defined inline under ``security.policies`` in
# ``config.yml``; centralized config — no separate access.yml file).
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


class Effect(str, Enum):
    """Allow vs deny — XACML / AWS IAM ``Effect`` field.

    Each policy entry must declare one explicitly; deny short-circuits or
    subtracts from the merged allow result, matching IAM / Cedar / SQL
    Server semantics where a deny always wins over an allow.
    """

    ALLOW = "allow"
    DENY = "deny"


class TablePolicy(BaseModel):
    # Allow-effect fields.
    column_level: ColumnLevelPolicy | None = None
    # Structured DSL — validated against the table registry by
    # ``validate_access_policy_against_registry`` at policy-load time.
    row_filter: dict[str, Any] | None = None

    # Deny-effect fields. Mutually exclusive with the allow-effect fields
    # above; the active set is checked against the parent ``PolicyEntry.effect``.
    deny_all: bool = False
    deny_columns: list[str] = Field(default_factory=list)


class PolicyEntry(BaseModel):
    name: str
    effect: Effect
    when: str
    tables: dict[str, TablePolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_effect_fields(self) -> "PolicyEntry":
        for tname, tp in self.tables.items():
            allow_set = tp.column_level is not None or tp.row_filter is not None
            deny_set = tp.deny_all or bool(tp.deny_columns)
            if self.effect == Effect.ALLOW:
                if deny_set:
                    raise ValueError(
                        f"policy {self.name!r} has effect=allow but table "
                        f"{tname!r} declares deny_all/deny_columns. Allow "
                        "rules use column_level/row_filter only."
                    )
                if not allow_set:
                    raise ValueError(
                        f"policy {self.name!r} table {tname!r}: allow rule "
                        "must declare column_level and/or row_filter."
                    )
            else:  # Effect.DENY
                if allow_set:
                    raise ValueError(
                        f"policy {self.name!r} has effect=deny but table "
                        f"{tname!r} declares column_level/row_filter. Deny "
                        "rules use deny_all/deny_columns only."
                    )
                if not deny_set:
                    raise ValueError(
                        f"policy {self.name!r} table {tname!r}: deny rule "
                        "must declare deny_all: true and/or deny_columns: [...]."
                    )
                if tp.deny_all and tp.deny_columns:
                    raise ValueError(
                        f"policy {self.name!r} table {tname!r}: deny_all "
                        "already covers the whole table; deny_columns is "
                        "redundant. Pick one."
                    )
        return self


class AccessPolicy(BaseModel):
    policies: list[PolicyEntry] = Field(default_factory=list)


def validate_access_policy_against_registry(
    policy: AccessPolicy, registry: Any
) -> None:
    """Walk every ``row_filter`` in the policy and verify column refs.

    Called from the CLI after the registry is built. Catches typos like
    ``orgg_id`` at startup instead of producing a per-request runtime error.
    """
    for entry in policy.policies:
        for table_name, table_policy in entry.tables.items():
            tdef = registry.get(table_name)
            if tdef is None:
                if table_policy.row_filter is not None or table_policy.deny_columns:
                    raise ValueError(
                        f"policy {entry.name!r} references unknown table {table_name!r}"
                    )
                continue
            allowed = {c.name for c in tdef.columns}
            if table_policy.row_filter is not None:
                try:
                    validate_row_filter(
                        table_policy.row_filter, allowed_columns=allowed
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"policy {entry.name!r} table {table_name!r}: {exc}"
                    ) from exc
            unknown = [c for c in table_policy.deny_columns if c not in allowed]
            if unknown:
                raise ValueError(
                    f"policy {entry.name!r} table {table_name!r}: "
                    f"deny_columns references unknown column(s) {sorted(unknown)}. "
                    f"Allowed columns: {sorted(allowed)}"
                )


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

    def is_column_allowed(self, name: str) -> bool:
        """Single source of truth for column visibility.

        Used by ``compile_query`` (to enforce strict denial when a query
        names a blocked column) and by MCP discovery tools (to filter the
        view a caller sees). Both must agree on what "allowed" means.
        """
        if self.allowed_columns is not None and name not in self.allowed_columns:
            return False
        if name in self.blocked_columns:
            return False
        return True


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class PolicyEngine:
    def __init__(self, access_policy: AccessPolicy) -> None:
        self._policy = access_policy

    def evaluate(self, table_name: str, ctx: JWTPayload) -> ResolvedPolicy:
        """Return the merged ResolvedPolicy for ``table_name`` given ``ctx``.

        Default-deny + IAM-style deny precedence:

        1. Partition matching entries (``when`` true and table listed) into
           allows and denies by ``effect``.
        2. Any matching deny with ``deny_all: true`` short-circuits to
           ``TableAccessDenied`` — deny always wins.
        3. If no allow matches, the table is denied (default-deny).
        4. The merged allow result is computed, then ``deny_columns`` from
           every matching deny is subtracted from ``allowed_columns``,
           added to ``blocked_columns``, and removed from ``masks``.
        """
        allows: list[TablePolicy] = []
        denies: list[TablePolicy] = []
        for entry in self._policy.policies:
            if not self._eval_when(entry.when, ctx):
                continue
            if table_name not in entry.tables:
                continue
            tp = entry.tables[table_name]
            if entry.effect == Effect.DENY:
                denies.append(tp)
            else:
                allows.append(tp)

        for tp in denies:
            if tp.deny_all:
                raise TableAccessDenied(table_name)

        if not allows:
            raise TableAccessDenied(table_name)

        resolved = self._merge(table_name, allows, ctx)

        denied_cols: set[str] = set()
        for tp in denies:
            denied_cols.update(tp.deny_columns)
        if denied_cols:
            allowed = resolved.allowed_columns
            if allowed is not None:
                allowed = allowed - denied_cols
            blocked = resolved.blocked_columns | frozenset(denied_cols)
            masks = {k: v for k, v in resolved.masks.items() if k not in denied_cols}
            resolved = ResolvedPolicy(
                allowed_columns=allowed,
                blocked_columns=blocked,
                masks=masks,
                row_filter_clause=resolved.row_filter_clause,
            )

        return resolved

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
