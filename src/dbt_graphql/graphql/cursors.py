from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

import decimal

import orjson


def _orjson_default(obj: Any) -> Any:
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    raise TypeError


_ORMJSON_DUMP_KWARGS: dict[str, Any] = {
    "default": _orjson_default,
    "option": orjson.OPT_SORT_KEYS,
}


def _digest(data: Any) -> str:
    """Deterministic SHA-256 hex digest of canonicalised JSON data."""
    canonical = orjson.dumps(data, **_ORMJSON_DUMP_KWARGS)
    return hashlib.sha256(canonical).hexdigest()


def _query_fingerprint(
    *,
    order_by: list[tuple[str, str]] | None = None,
    where: dict | None = None,
    distinct: bool = False,
    group_by_cols: set[str] | None = None,
) -> str:
    """SHA-256 fingerprint of the query signature for cursor stale detection."""
    return _digest(
        {
            "o": order_by,
            "w": where,
            "d": distinct,
            "g": sorted(group_by_cols) if group_by_cols else None,
        }
    )


@dataclass
class CursorPayload:
    """Decoded cursor payload.

    - ``values`` — column→value dict used to build the chained-OR predicate.
    - ``digest`` — SHA-256 fingerprint to validate cursor compatibility.
    """

    values: dict[str, Any]  # {col: value} — cursor position
    digest: str  # SHA-256 of query parameters


def encode_cursor(
    *,
    cursor_values: dict[str, Any],
    order_by_parsed: list[tuple[str, str]],
    where: dict | None = None,
    distinct: bool = False,
    group_by_cols: set[str] | None = None,
) -> str:
    """Encode a cursor from resolver-level data."""
    wire: dict[str, Any] = {
        "v": cursor_values,
        "d": _query_fingerprint(
            order_by=order_by_parsed,
            where=where,
            distinct=distinct,
            group_by_cols=group_by_cols,
        ),
    }
    raw = orjson.dumps(wire, **_ORMJSON_DUMP_KWARGS)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> CursorPayload | None:
    try:
        raw = base64.urlsafe_b64decode(cursor + "==")
        data = orjson.loads(raw)
        return CursorPayload(
            values=data["v"],
            digest=data["d"],
        )
    except Exception:
        return None
