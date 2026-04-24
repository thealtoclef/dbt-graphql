"""Unit tests for the access policy engine."""

from __future__ import annotations

import pytest

from dbt_graphql.api.policy import (
    AccessPolicy,
    ColumnLevelPolicy,
    PolicyEngine,
    PolicyEntry,
    TableAccessDenied,
    TablePolicy,
    load_access_policy,
    render_row_filter,
)
from dbt_graphql.api.security import JWTPayload


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
    with pytest.raises(ValueError, match="conflicting masks"):
        engine.evaluate("customers", _ctx())


def test_multi_policy_row_filter_or():
    engine = _engine(
        _policy(
            "a",
            "True",
            {"orders": TablePolicy(row_level="user_id = {{ jwt.sub }}")},
        ),
        _policy(
            "b",
            "True",
            {"orders": TablePolicy(row_level="is_public = TRUE")},
        ),
    )
    result = engine.evaluate("orders", _ctx(sub="u1"))
    assert result.row_filter_sql == "(user_id = :p0_0) OR (is_public = TRUE)"
    assert result.row_filter_params == {"p0_0": "u1"}


# ---------------------------------------------------------------------------
# row_level parameterized rendering
# ---------------------------------------------------------------------------


def test_render_row_filter_sub():
    sql, params = render_row_filter("user_id = {{ jwt.sub }}", _ctx(sub="user_42"))
    assert sql == "user_id = :p_0"
    assert params == {"p_0": "user_42"}


def test_render_row_filter_claims():
    sql, params = render_row_filter(
        "region = {{ jwt.claims.region }}", _ctx(claims={"region": "eu-west"})
    )
    assert sql == "region = :p_0"
    assert params == {"p_0": "eu-west"}


def test_render_row_filter_static():
    sql, params = render_row_filter("published = TRUE", _ctx())
    assert sql == "published = TRUE"
    assert params == {}


def test_render_row_filter_missing_claim_is_none():
    """Missing jwt paths resolve to None so the bind param is SQL NULL."""
    sql, params = render_row_filter("user_id = {{ jwt.sub }}", _ctx())
    assert sql == "user_id = :p_0"
    assert params == {"p_0": None}


def test_render_row_filter_injection_safe():
    """A malicious claim value cannot escape the bind param."""
    sql, params = render_row_filter(
        "user_id = {{ jwt.sub }}",
        _ctx(sub="x'; DROP TABLE orders; --"),
    )
    # The placeholder is a named bind param, not interpolated SQL.
    assert sql == "user_id = :p_0"
    assert "DROP TABLE" not in sql
    # The dangerous payload is carried only as a parameter value.
    assert params == {"p_0": "x'; DROP TABLE orders; --"}


def test_render_row_filter_multiple_placeholders():
    sql, params = render_row_filter(
        "org_id = {{ jwt.claims.org_id }} AND region = {{ jwt.claims.region }}",
        _ctx(claims={"org_id": 7, "region": "us"}),
    )
    assert sql == "org_id = :p_0 AND region = :p_1"
    assert params == {"p_0": 7, "p_1": "us"}


def test_render_row_filter_with_jinja_filter():
    """Jinja filters (| upper, | default, ...) run before finalize."""
    sql, params = render_row_filter(
        "region = {{ jwt.claims.region | upper }}",
        _ctx(claims={"region": "eu"}),
    )
    assert sql == "region = :p_0"
    assert params == {"p_0": "EU"}


def test_render_row_filter_with_if_block():
    """Conditional templates: the condition is raw-evaluated, outputs bind."""
    tmpl = (
        "{% if jwt.claims.region %}region = {{ jwt.claims.region }}"
        "{% else %}FALSE{% endif %}"
    )
    sql, params = render_row_filter(tmpl, _ctx(claims={"region": "us"}))
    assert sql == "region = :p_0"
    assert params == {"p_0": "us"}

    sql, params = render_row_filter(tmpl, _ctx())
    assert sql == "FALSE"
    assert params == {}


def test_render_row_filter_sandbox_blocks_dunder():
    """SandboxedEnvironment must reject __class__-style escapes."""
    from jinja2.exceptions import SecurityError

    with pytest.raises(SecurityError):
        render_row_filter("x = {{ jwt.__class__.__name__ }}", _ctx())


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
        row_level: "user_id = {{ jwt.sub }}"
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
    assert tbl.row_level == "user_id = {{ jwt.sub }}"


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
