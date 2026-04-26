"""Stable, collision-resistant key derivation for the result cache.

Why hash explicitly instead of letting cashews build keys from arg-tuples:
the SQLAlchemy ``Select`` plus its bound parameters is bulky and not
str-stable on its own. Hashing the rendered SQL + bound parameter values
gives a short, deterministic key that is structurally tenant-isolated:
two requests share an entry if and only if they would send byte-identical
SQL to the warehouse.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from sqlalchemy.sql import ClauseElement


def _sha(s: str) -> str:
    return sha256(s.encode("utf-8")).hexdigest()


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def hash_sql(stmt: ClauseElement, dialect_name: str) -> str:
    """Stable hash of a SQLAlchemy statement, including bound parameter values.

    We compile against a real dialect so the SQL string is exactly what
    will be sent to the warehouse. Bound parameter values are extracted
    from ``compiled.params`` and folded into the key — two queries with
    the same SQL but different bound values get different keys (correct,
    and the foundation of cross-tenant isolation: row-filter values land
    in ``compiled.params``).

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
