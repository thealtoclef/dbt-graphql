"""Stable, collision-resistant key derivation for each cache layer.

Why hash explicitly instead of letting cashews build keys from arg-tuples:
the values we key on are big (GraphQL ASTs, SQLAlchemy ``Select`` objects,
dialect-rendered SQL with bound params). Hashing keeps the key short and
canonical, and makes it trivial to reason about cross-tenant collision risk.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from graphql import parse, print_ast
from sqlalchemy.sql import ClauseElement


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(s: str) -> str:
    return sha256(s.encode("utf-8")).hexdigest()


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# L1 — parsed-doc
# ---------------------------------------------------------------------------


def canonicalize_doc(query: str) -> str:
    """Return a whitespace-/format-stable form of ``query``.

    Two strings that produce the same AST land on the same L1 key. We
    print_ast the parse result; on parse failure (still cacheable as
    "this query is invalid") we fall back to the original string.
    """
    try:
        return print_ast(parse(query))
    except Exception:
        return query


def parsed_doc_key(query: str) -> str:
    return f"parse:{_sha(canonicalize_doc(query))}"


# ---------------------------------------------------------------------------
# L2 — compiled-plan
# ---------------------------------------------------------------------------


def doc_subtree_hash(field_node) -> str:
    """Hash for a single root-field selection.

    The full GraphQL document may contain many root selections; the L2 cache
    is per-resolver-call, so we key on the per-field AST subtree.
    """
    return _sha(print_ast(field_node))


def jwt_signature(jwt_payload: Any) -> str:
    """Stable hash of a JWT payload object.

    L2 keys include the full JWT signature (rather than a per-policy claim
    subset). Rationale: ``compile_query`` recurses into nested-table policies
    which we cannot enumerate without compiling. Including the full JWT keeps
    correctness; the per-tenant sharing optimization is realized at L3 (where
    the key is rendered SQL + bound params).
    """
    payload = _payload_to_dict(jwt_payload)
    return _sha(_stable_json(payload))


def _payload_to_dict(obj: Any) -> Any:
    """Recursively turn a JWTPayload (or nested) into a plain dict.

    JWTPayload uses ``object.__setattr__`` for storage; ``vars()`` returns
    its real attrs. Nested JWTPayloads recurse. Anything else is returned
    as-is (assumed JSON-serializable).
    """
    from ..api.security import JWTPayload  # local import: avoids cycle

    if isinstance(obj, JWTPayload):
        return {k: _payload_to_dict(v) for k, v in vars(obj).items()}
    return obj


def compiled_plan_key(
    *,
    field_node,
    table_name: str,
    where: dict[str, Any] | None,
    limit: int | None,
    offset: int | None,
    dialect: str,
    jwt_sig: str,
) -> str:
    args_sig = _sha(
        _stable_json(
            {
                "where": where or {},
                "limit": limit,
                "offset": offset,
                "dialect": dialect,
            }
        )
    )
    return f"plan:{table_name}:{doc_subtree_hash(field_node)}:{args_sig}:{jwt_sig}"


# ---------------------------------------------------------------------------
# L3 — result
# ---------------------------------------------------------------------------


def hash_sql(stmt: ClauseElement, dialect_name: str) -> str:
    """Stable hash of a SQLAlchemy statement, including bound parameter values.

    We compile against a real dialect so the SQL string is exactly what
    will be sent to the warehouse. Bound parameter values are extracted
    from ``compiled.params`` and folded into the key — two queries with
    the same SQL but different bound values get different keys (correct).

    ``dialect_name`` is required and goes into the key: same query against
    Postgres vs MySQL produces different SQL syntax and thus must not
    share a cache entry across replicas pointed at different warehouses.
    """
    from sqlalchemy.dialects import mysql, postgresql

    dialects = {
        "postgresql": postgresql.dialect(),
        "mysql": mysql.dialect(),
    }
    d = dialects.get(dialect_name)
    if d is None:
        # Fallback: best-effort string repr. Keys still work, just less stable
        # across SA versions.
        sql_text = str(stmt)
        params: dict[str, Any] = {}
    else:
        compiled = stmt.compile(dialect=d)
        sql_text = str(compiled)
        params = dict(compiled.params)

    payload = _stable_json({"sql": sql_text, "params": params, "dialect": dialect_name})
    return f"sql:{_sha(payload)}"
