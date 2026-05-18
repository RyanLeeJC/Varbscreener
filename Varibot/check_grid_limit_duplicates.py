#!/usr/bin/env python3
"""Report duplicate pending limits by (asset, side, normalized price key)."""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints
from variationalbot.vari.endpoints import limit_price_key

from cancelalllimitorders import (
    _fetch_pending_rows,
    _is_limit_row,
    _is_terminal_status,
    _order_row_underlying,
    _row_rfq_id,
    _row_status,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Check pending limits for duplicate price keys.")
    p.add_argument("--assets", nargs="*", default=["XRP", "MON", "SUI"], help="Underlyings to check")
    args = p.parse_args()
    want = {str(a).strip().upper() for a in args.assets}

    cfg = load_config()
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    rows = _fetch_pending_rows(ep, instrument=None)
    by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    total = 0
    for row in rows:
        if not _is_limit_row(row):
            continue
        if _is_terminal_status(_row_status(row)):
            continue
        sym = _order_row_underlying(row)
        if sym not in want:
            continue
        side = str(row.get("side") or "").strip().lower()
        lp = row.get("limit_price") or row.get("trigger_price")
        if lp is None:
            continue
        try:
            k = limit_price_key(side, float(lp))
        except (TypeError, ValueError):
            continue
        by_key[(sym, k[0], k[1])].append(row)
        total += 1

    print(f"Pending limits for {sorted(want)}: {total} order(s)")
    dup_groups = [(k, v) for k, v in sorted(by_key.items()) if len(v) > 1]
    if not dup_groups:
        print("OK: no duplicate (side, price) keys.")
        for k in sorted(by_key):
            print(f"  {k[0]} {k[1]} @ {k[2]}: 1 order")
        return 0

    print(f"FAIL: {len(dup_groups)} duplicate key group(s):", file=sys.stderr)
    for k, group in dup_groups:
        print(f"  {k[0]} {k[1]} @ {k[2]}: {len(group)} orders", file=sys.stderr)
        for row in group:
            rid = _row_rfq_id(row) or "?"
            qty = row.get("qty") or "?"
            print(f"    rfq_id={rid} qty={qty}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
