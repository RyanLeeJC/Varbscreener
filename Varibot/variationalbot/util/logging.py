from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional


def _json_default(o: Any) -> Any:
    if is_dataclass(o):
        return asdict(o)
    return str(o)


def log_json(event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"ts": time.time(), "event": event, **fields}
    sys.stdout.write(json.dumps(payload, default=_json_default) + "\n")
    sys.stdout.flush()

