from __future__ import annotations

import hashlib
import json
from typing import Mapping


def event_operation_key(
        event_id: str,
        operation_name: str,
        arguments: Mapping[str, object]) -> str:
    normalized = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    source = f"{event_id}\n{operation_name}\n{normalized}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
