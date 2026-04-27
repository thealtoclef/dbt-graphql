"""Unit tests for the access policy engine."""

from __future__ import annotations

import pytest

from dbt_graphql.graphql.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    MaskConflictError,
    PolicyEngine,
    PolicyEntry,
    TableAccessDenied,
    TablePolicy,
    load_access_policy,
)
from dbt_graphql.graphql.auth import JWTPayload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(sub=None, email=None, groups=None, claims=None) -> JWTPayload:
    data: dict = {"groups": groups or []}
    if sub is not None:
        data["sub"] = sub
    if email is not None:
        data["email"] = email
    if claims:
        data["claims"] = claims
    return JWTPayload(data)


def _policy(name: str, when: str, tables: dict) -> PolicyEntry:
    return PolicyEntry(name=name, when=when, tables=tables)


def _engine(*entries: PolicyEntry) -> PolicyEngine:
    return PolicyEngine(AccessPolicy(policies=list(entries)))


# ---------------------------------------------------------------------------
# when expression evaluation (simpleeval)
# ---------------------------------------------------------------------------


def test_eval_when_group_match():
    engine = _engine()
    assert engine._eval_when("'analysts' in jwt.groups", _ctx(groups=["analysts"]))


def test_eval_when_group_no_match():
    engine = _engine()
    assert not engine._eval_when("'analysts' in jwt.groups", _ctx(groups=["finance"]))


def test_eval_when_compound_or():
    engine = _engine()
    expr = "('analysts' in jwt.groups) or ('finance' in jwt.groups)"
    assert engine._eval_when(expr, _ctx(groups=["finance"]))


def test_eval_when_claims_attribute():
    engine = _engine()
    ctx = _ctx(claims={"level": 3})
    assert engine._eval_when("jwt.claims.level >= 3", ctx)
    assert not engine._eval_when("jwt.claims.level >= 4", ctx)


def test_eval_when_sub_none():
    engine = _engine()
    assert engine._eval_when("jwt.sub == None", _ctx())


def test_eval_when_bad_expr_returns_false():
    engine = _engine()
    assert not engine._eval_when("this is not valid python!!!", _ctx())


def test_eval_when_dunder_is_blocked():
    """simpleeval must reject attribute escapes like __class__."""
    engine = _engine()
    assert not engine._eval_when("jwt.__class__.__name__ == 'JWTPayload'", _ctx())


def test_eval_when_cannot_call_builtins():
    """No builtins — anything like open/exec/eval must fail."""
    engine = _engine()
    assert not engine._eval_when("open('/etc/passwd')", _ctx())


# ---------------------------------------------------------------------------
# column_level model validation
# ---------------------------------------------------------------------------


def test_column_level_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        ColumnLevelPolicy(include_all=True, includes=["col1"])


@pytest.mark.parametrize(
    "expr",
    [
        "NULL; DROP TABLE users",
        "'***'-- comment",
        "/* hidden */ NULL",
        "NULL */",
    ],
)
def test_mask_rejects_forbidden_tokens(expr):
    """Mask SQL fragments are operator-trusted but a statement terminator or
    comment marker is almost always a typo that would produce malformed SQL.
    Reject at policy load time."""
    with pytest.raises(ValueError, match="forbidden token"):
        ColumnLevelPolicy(includes=["email"], mask={"email": expr})


def test_mask_allows_legitimate_expressions():
    """Sanity: realistic mask expressions still load."""
    p = ColumnLevelPolicy(
        include_all=True,
        mask={
            "email": "CONCAT('***@', SPLIT_PART(email, '@', 2))",
            "ssn": "NULL",
            "phone": None,
        },
    )
    assert p.mask["email"] is not None
    assert p.mask["email"].startswith("CONCAT")


def test_column_level_include_all_no_includes_ok():
    p = ColumnLevelPolicy(include_all=True, excludes=["secret"])
    assert p.include_all is True
    assert p.excludes == ["secret"]


def test_column_level_includes_list_ok():
    p = ColumnLevelPolicy(includes=["id", "name"])
    assert p.includes == ["id", "name"]
    assert p.include_all is False


# ---------------------------------------------------------------------------
# PolicyEngine.evaluate — column restrictions
# ---------------------------------------------------------------------------


def test_no_matching_policy_is_denied():
    """Default-deny: no policy entry whose when-clause fires → TableAccessDenied."""
    engine = _engine(_policy("analyst", "'analysts' in jwt.groups", {}))
    with pytest.raises(TableAccessDenied) as exc_info:
        engine.evaluate("orders", _ctx(groups=["finance"]))
    assert exc_info.value.table == "orders"
    assert exc_info.value.code == "FORBIDDEN_TABLE"


def test_when_matches_but_table_absent_is_denied():
    """when-clause fires but the queried table isn't in the policy's tables dict.
    Default-deny applies — the policy doesn't cover the table → denied."""
    engine = _engine(
        _policy(
            "orders_only",
            "True",
            {"orders": TablePolicy(column_level=ColumnLevelPolicy(include_all=True))},
        )
    )
    with pytest.raises(TableAccessDenied):
        engine.evaluate("customers", _ctx(groups=["analysts"]))


def test_include_all_sets_allowed_none():
    engine = _engine(
        _policy(
            "analyst",
            "'analysts' in jwt.groups",
            {"orders": TablePolicy(column_level=ColumnLevelPolicy(include_all=True))},
        )
    )
    result = engine.evaluate("orders", _ctx(groups=["analysts"]))
    assert result.allowed_columns is None


def test_includes_whitelist():
    engine = _engine(
        _policy(
            "analyst",
            "'analysts' in jwt.groups",
            {
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["order_id", "status"])
                )
            },
        )
    )
    result = engine.evaluate("orders", _ctx(groups=["analysts"]))
    assert result.allowed_columns == frozenset({"order_id", "status"})


def test_excludes_blocks_columns():
    engine = _engine(
        _policy(
            "analyst",
            "True",
            {
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True, excludes=["salary", "ssn"]
                    )
                )
            },
        )
    )
    result = engine.evaluate("customers", _ctx())
    assert "salary" in result.blocked_columns
    assert "ssn" in result.blocked_columns


def test_mask_captured():
    engine = _engine(
        _policy(
            "analyst",
            "True",
            {
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True,
                        mask={"email": "CONCAT('***@', SPLIT_PART(email, '@', 2))"},
                    )
                )
            },
        )
    )
    result = engine.evaluate("customers", _ctx())
    assert result.masks["email"] == "CONCAT('***@', SPLIT_PART(email, '@', 2))"


# ---------------------------------------------------------------------------
# Multi-policy OR merge
# ---------------------------------------------------------------------------


def test_multi_policy_column_union():
    engine = _engine(
        _policy(
            "a",
            "True",
            {
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["order_id"])
                )
            },
        ),
        _policy(
            "b",
            "True",
            {
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["order_id", "status"])
                )
            },
        ),
    )
    result = engine.evaluate("orders", _ctx())
    assert result.allowed_columns == frozenset({"order_id", "status"})


def test_multi_policy_include_all_wins():
    engine = _engine(
        _policy(
            "a",
            "True",
            {
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(includes=["order_id"])
                )
            },
        ),
        _policy(
            "b",
            "True",
            {"orders": TablePolicy(column_level=ColumnLevelPolicy(include_all=True))},
        ),
    )
    result = engine.evaluate("orders", _ctx())
    assert result.allowed_columns is None


def test_multi_policy_mask_only_if_all_agree():
    """Mask applied only when every matching policy masks the column."""
    engine = _engine(
        _policy(
            "a",
            "True",
            {
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True, mask={"email": None}
                    )
                )
            },
        ),
        _policy(
            "b",
            "True",
            {
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True)
                )
            },
        ),
    )
    result = engine.evaluate("customers", _ctx())
    assert "email" not in result.masks


def test_multi_policy_mask_conflict_raises():
    """Two matching policies with different mask SQL for the same column
    must raise — operators must resolve the conflict in access.yml."""
    engine = _engine(
        _policy(
            "a",
            "True",
            {
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True, mask={"email": None}
                    )
                )
            },
        ),
        _policy(
            "b",
            "True",
            {
                "customers": TablePolicy(
                    column_level=ColumnLevelPolicy(
                        include_all=True,
                        mask={"email": "CONCAT('*', email)"},
                    )
                )
            },
        ),
    )
    with pytest.raises(MaskConflictError) as exc_info:
        engine.evaluate("customers", _ctx())
    assert exc_info.value.code == "POLICY_MASK_CONFLICT"
    assert exc_info.value.table == "customers"
    assert exc_info.value.column == "email"


def test_mask_conflict_surfaces_column_in_graphql_extensions():
    """Resolvers must project ``MaskConflictError.column`` into the
    GraphQL error's ``extensions`` block so clients can identify the
    offending column without parsing the message string."""
    from dbt_graphql.graphql.resolvers import _to_graphql_error

    err = MaskConflictError("customers", "email", [None, "CONCAT('*', email)"])
    gql_err = _to_graphql_error(err)
    assert gql_err.extensions is not None
    assert gql_err.extensions["code"] == "POLICY_MASK_CONFLICT"
    assert gql_err.extensions["table"] == "customers"
    assert gql_err.extensions["column"] == "email"


def _render_clause(clause) -> tuple[str, dict]:
    from sqlalchemy.dialects import postgresql

    compiled = clause.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    return str(compiled), dict(compiled.params)


def test_engine_uses_dsl_row_filter():
    """A policy using the DSL ``row_filter`` produces a SQLAlchemy clause the
    compiler downstream drops directly into ``stmt.where(...)``."""
    engine = _engine(
        _policy(
            "analyst",
            "'analysts' in jwt.groups",
            {
                "orders": TablePolicy(
                    column_level=ColumnLevelPolicy(include_all=True),
                    row_filter={"org_id": {"_eq": {"jwt": "claims.org_id"}}},
                )
            },
        )
    )
    resolved = engine.evaluate("orders", _ctx(groups=["analysts"], claims={"org_id": 7}))
    assert resolved.row_filter_clause is not None
    sql, params = _render_clause(resolved.row_filter_clause)
    assert "org_id =" in sql
    assert params == {"p0_0": 7}


def test_multi_policy_row_filter_or():
    """Two policies that both match must OR-merge their row filters."""
    engine = _engine(
        _policy(
            "a",
            "True",
            {
                "orders": TablePolicy(
                    row_filter={"user_id": {"_eq": {"jwt": "sub"}}}
                )
            },
        ),
        _policy(
            "b",
            "True",
            {
                "orders": TablePolicy(
                    row_filter={"is_public": {"_eq": True}}
                )
            },
        ),
    )
    result = engine.evaluate("orders", _ctx(sub="u1"))
    assert result.row_filter_clause is not None
    sql, params = _render_clause(result.row_filter_clause)
    assert " OR " in sql
    assert "user_id =" in sql
    assert "is_public =" in sql
    assert set(params.values()) == {"u1", True}


def test_validate_against_registry_catches_unknown_column():
    """Sec-H's load-time validation must catch column typos before any
    request hits the policy."""
    from dbt_graphql.graphql.policy import (
        AccessPolicy,
        validate_access_policy_against_registry,
    )

    class _Col:
        def __init__(self, name):
            self.name = name

    class _TDef:
        columns = [_Col("id"), _Col("org_id")]

    class _Registry:
        def get(self, _name):
            return _TDef()

    policy = AccessPolicy(
        policies=[
            _policy(
                "analyst",
                "True",
                {
                    "orders": TablePolicy(
                        column_level=ColumnLevelPolicy(include_all=True),
                        row_filter={"orgg_id": {"_eq": 1}},
                    )
                },
            )
        ]
    )
    with pytest.raises(ValueError, match="unknown column 'orgg_id'"):
        validate_access_policy_against_registry(policy, _Registry())


# ---------------------------------------------------------------------------
# load_access_policy
# ---------------------------------------------------------------------------


def test_load_access_policy(tmp_path):
    yml = tmp_path / "access.yml"
    yml.write_text(
        """
policies:
  - name: analyst
    when: "'analysts' in jwt.groups"
    tables:
      orders:
        column_level:
          include_all: true
          excludes: [internal_notes]
          mask:
            email: ~
        row_filter:
          user_id: { _eq: { jwt: sub } }
"""
    )
    policy = load_access_policy(yml)
    assert len(policy.policies) == 1
    entry = policy.policies[0]
    assert entry.name == "analyst"
    tbl = entry.tables["orders"]
    assert tbl.column_level is not None
    assert tbl.column_level.include_all is True
    assert tbl.column_level.excludes == ["internal_notes"]
    assert tbl.column_level.mask["email"] is None
    assert tbl.row_filter == {"user_id": {"_eq": {"jwt": "sub"}}}


def test_load_access_policy_invalid_yaml(tmp_path):
    yml = tmp_path / "access.yml"
    yml.write_text("- just a list")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_access_policy(yml)


# ---------------------------------------------------------------------------
# Default-deny behavior
# ---------------------------------------------------------------------------


def test_empty_policy_file_denies_every_table():
    """An empty policies list → every table is denied by default."""
    engine = PolicyEngine(AccessPolicy(policies=[]))
    with pytest.raises(TableAccessDenied, match="orders"):
        engine.evaluate("orders", _ctx())


def test_table_denial_carries_table_name_in_exception():
    engine = _engine(
        _policy(
            "orders_only",
            "True",
            {"orders": TablePolicy(column_level=ColumnLevelPolicy(include_all=True))},
        )
    )
    with pytest.raises(TableAccessDenied) as exc_info:
        engine.evaluate("customers", _ctx())
    assert exc_info.value.table == "customers"
    assert "customers" in str(exc_info.value)


def test_denied_when_condition_never_fires_even_if_table_listed():
    """If no policy's when-clause evaluates True, deny — even if the table is
    listed under some other policy that didn't match."""
    engine = _engine(
        _policy(
            "analyst",
            "'analysts' in jwt.groups",
            {"orders": TablePolicy(column_level=ColumnLevelPolicy(include_all=True))},
        )
    )
    with pytest.raises(TableAccessDenied):
        engine.evaluate("orders", _ctx(groups=["guest"]))
