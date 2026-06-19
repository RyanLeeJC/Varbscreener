#!/usr/bin/env python3
"""Enter speedrunners.json long/short basket live via multimarketorder."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_VARIBOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_VARIBOT))

from wallet_env import apply_wallet_from_env  # noqa: E402

WALLET = "0x1c993748bd640c7263fa72fdf3e1a506fe2baf96"
NOTIONAL_USD = 500.0
BASE_SLIPPAGE = 0.001  # 0.10%
SLIPPAGE_CAP = 0.006  # 0.60%
SLIPPAGE_STEP = 0.001  # +0.10% per retry


def main() -> int:
    apply_wallet_from_env(WALLET)
    os.environ["MAX_SLIPPAGE"] = str(SLIPPAGE_CAP)

    spec = json.loads((_VARIBOT / "speedrunners.json").read_text(encoding="utf-8"))
    longs = ",".join(spec["long"])
    shorts = ",".join(spec["short"])

    import multimarketorder as mmo

    mmo._SLIPPAGE_RETRY_INCREMENT = SLIPPAGE_STEP
    mmo._MAX_LIVE_ATTEMPTS = int(round((SLIPPAGE_CAP - BASE_SLIPPAGE) / SLIPPAGE_STEP)) + 1

    sys.argv = [
        "multimarketorder.py",
        "--live",
        "--usd",
        str(NOTIONAL_USD),
        "--max-slippage",
        str(BASE_SLIPPAGE),
        "--long",
        longs,
        "--short",
        shorts,
    ]
    return int(mmo.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
