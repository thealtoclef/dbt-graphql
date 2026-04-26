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

from sqlalchemy.dialects import registry as _dialect_registry
from sqlalchemy.sql import ClauseElement


def _sha(s: str) -> str:
    return sha256(s.encode("utf-8")).hexdigest()


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _resolve_dialect(name: str):
    """Resolve a SA dialect by name. Raises on unknown names rather than
    falling back to ``str(stmt)`` — that fallback would silently strip
    bound parameter values from the cache key and let two queries with
    different bind values share an entry (cross-tenant leak)."""
    try:
        return _dialect_registry.load(name)()
    except Exception as exc:  # noqa: BLE001 — registry raises various types
        raise ValueError(
            f"hash_sql: cannot resolve SQLAlchemy dialect {name!r}. "
            "Result-cache keying requires a dialect SA can load."
        ) from exc


def hash_sql(stmt: ClauseElement, dialect_name: str) -> str:
    """Stable hash of a SQLAlchemy statement, including bound parameter values.

    We compile against the named dialect so the SQL string is exactly
    what will be sent to the warehouse. Bound parameter values are
    extracted from ``compiled.params`` and folded into the key — two
    queries with the same SQL but different bound values get different
    keys (correct, and the foundation of cross-tenant isolation:
    row-filter values land in ``compiled.params``).

    ``dialect_name`` goes into the key as well: same query against
    Postgres vs MySQL produces different SQL syntax and thus must not
    share a cache entry across replicas pointed at different warehouses.

    Raises ``ValueError`` for dialects SA cannot load. We refuse to emit
    a key rather than silently emit an unsafe one.
    """
    d = _resolve_dialect(dialect_name)
    compiled = stmt.compile(dialect=d)
    sql_text = str(compiled)
    params = dict(compiled.params)

    payload = _stable_json({"sql": sql_text, "params": params, "dialect": dialect_name})
    return f"sql:{_sha(payload)}"
