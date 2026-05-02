from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _serialize_cursor_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


@dataclass
class CursorPayload:
    order_by: list[list]  # [[col, "asc"|"desc", value], ...]
    where: dict | None = None  # filter applied when cursor was created


def encode_cursor(payload: CursorPayload) -> str:
    wire: dict[str, Any] = {
        "p": [
            [col, direction, _serialize_cursor_value(value)]
            for col, direction, value in payload.order_by
        ],
    }
    if payload.where is not None:
        wire["w"] = payload.where
    return base64.urlsafe_b64encode(json.dumps(wire).encode()).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> CursorPayload | None:
    try:
        json_bytes = base64.urlsafe_b64decode(cursor + "==")
        data = json.loads(json_bytes)
        return CursorPayload(
            order_by=data["p"],
            where=data.get("w"),
        )
    except Exception:
        return None
