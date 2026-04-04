from __future__ import annotations

import json
import sys
from typing import Any, Dict


def main() -> int:
    """
    Example script showing the JSON-stdin → JSON-stdout contract.
    Replace this with your real signal generator later.
    """
    raw = sys.stdin.read()
    inp: Dict[str, Any] = json.loads(raw) if raw.strip() else {}

    # Emit a placeholder signal in USD notional terms.
    out = {
        "long": [{"asset": "SOL", "score": 0.5, "usd_size": 10.0}],
        "short": [],
        "meta": {"received_keys": sorted(list(inp.keys()))},
    }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

