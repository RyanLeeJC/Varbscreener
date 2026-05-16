#!/usr/bin/env python3
"""
Minimal limit batch: N buy limits + M sell limits for one perp underlying.

This does NOT replace multimarketorder.py (Varibot still imports that script).
Uses the same Vari config (.env / load_config) and POST /api/orders/new/limit.

Examples (dry-run prints only; add --live to POST):

  python3 multimarketorder_simple.py \\
    --asset BTC --usd 500 --max-slippage 0.001 \\
    --buy-prices 79183.33,79501.97 \\
    --sell-prices 79820.62,80139.26

  python3 multimarketorder_simple.py --live ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints
from variationalbot.vari.endpoints import Instrument


def _parse_prices_csv(s: str) -> List[float]:
    out: List[float] = []
    for part in (s or "").split(","):
        p = part.strip()
        if not p:
            continue
        out.append(float(p))
    return out


def _fmt_lp(v: float) -> str:
    return f"{round(float(v), 2):.2f}"


def _instrument_query_param(*, asset: str, settlement_asset: str, funding_interval_s: int) -> str:
    return f"P-{str(asset).strip().upper()}-{str(settlement_asset).strip().upper()}-{int(funding_interval_s)}"


def _extract_rfq_id(resp: Any) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for k in ("rfq_id", "rfqId"):
        v = resp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _resp_looks_rejected(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    st = str(resp.get("status") or resp.get("order_status") or resp.get("state") or "").strip().lower()
    return st in ("rejected", "reject", "failed", "error", "cancelled")


def _poll_pending_row_for_rfq(
    ep: VariEndpoints,
    *,
    instrument_param: str,
    rfq_id: str,
    deadline_s: float = 10.0,
    poll_s: float = 0.45,
) -> Optional[Dict[str, Any]]:
    """Best-effort: match rfq_id in GET .../orders/v2?status=pending (same idea as multimarketorder)."""
    t0 = time.time()
    while time.time() - t0 < float(deadline_s):
        try:
            raw = ep.client.request_json(
                "GET", f"/api/orders/v2?status=pending&instrument={instrument_param}"
            )
        except Exception:
            time.sleep(poll_s)
            continue
        items: Optional[List[Any]] = None
        if isinstance(raw, dict):
            for k in ("result", "orders", "data", "items"):
                v = raw.get(k)
                if isinstance(v, list):
                    items = v
                    break
        elif isinstance(raw, list):
            items = raw
        if not items:
            time.sleep(poll_s)
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("rfq_id") or item.get("rfqId") or "")
            if rid == rfq_id:
                return item
        time.sleep(poll_s)
    return None


def _instrument(asset: str, *, funding_interval_s: int, settlement: str) -> Instrument:
    return Instrument(
        instrument_type="perpetual_future",
        underlying=str(asset).strip().upper(),
        funding_interval_s=int(funding_interval_s),
        settlement_asset=str(settlement),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Post N buy + M sell limit orders (minimal).")
    ap.add_argument("--asset", default="BTC", help="Underlying, e.g. BTC")
    ap.add_argument("--usd", type=float, required=True, help="USDC notional per order")
    ap.add_argument(
        "--buy-prices",
        required=True,
        help="Comma-separated limit prices for buys (below mark), e.g. 79000,78500",
    )
    ap.add_argument(
        "--sell-prices",
        required=True,
        help="Comma-separated limit prices for sells (above mark), e.g. 81000,81500",
    )
    ap.add_argument("--live", action="store_true", help="Actually POST orders (default: dry-run)")
    ap.add_argument("--max-slippage", type=float, default=None, help="Slippage limit fraction (default from env or 0.001)")
    ap.add_argument("--leverage", type=int, default=None, help="If set, POST set_leverage before each order")
    ap.add_argument("--funding-interval-s", type=int, default=3600)
    ap.add_argument("--settlement-asset", default="USDC")
    ap.add_argument("--use-mark-price", action="store_true")
    ap.add_argument("--sleep-between-s", type=float, default=0.35)
    ap.add_argument(
        "--verify-pending-s",
        type=float,
        default=10.0,
        help="After each live limit, poll pending orders up to this many seconds (0=skip)",
    )
    args = ap.parse_args()

    buys = _parse_prices_csv(args.buy_prices)
    sells = _parse_prices_csv(args.sell_prices)
    if not buys or not sells:
        print("Need at least one buy price and one sell price.", file=sys.stderr)
        return 2

    slip = float(args.max_slippage) if args.max_slippage is not None else float(
        os.environ.get("MAX_SLIPPAGE", "0.001")
    )
    asset = str(args.asset).strip().upper()
    inst = _instrument(asset, funding_interval_s=args.funding_interval_s, settlement=args.settlement_asset)

    cfg = load_config()
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    jobs: List[Tuple[str, float]] = [("buy", p) for p in buys] + [("sell", p) for p in sells]

    inst_q = _instrument_query_param(
        asset=asset,
        settlement_asset=str(args.settlement_asset),
        funding_interval_s=int(args.funding_interval_s),
    )

    for side, px in jobs:
        lp = round(float(px), 2)
        qty_str, _qh, _qa = ep.qty_string_for_usd_at_price(
            instrument=inst,
            side=side,
            usd_notional=float(args.usd),
            price=float(lp),
        )
        if args.leverage is not None:
            ep.set_leverage(asset=asset, leverage=int(args.leverage))

        payload = {
            "order_type": "limit",
            "limit_price": _fmt_lp(lp),
            "side": side,
            "instrument": {
                "instrument_type": inst.instrument_type,
                "underlying": inst.underlying,
                "funding_interval_s": int(inst.funding_interval_s),
                "settlement_asset": inst.settlement_asset,
            },
            "qty": qty_str,
            "slippage_limit": str(float(slip)),
            "use_mark_price": bool(args.use_mark_price),
            "is_reduce_only": False,
            "is_auto_resize": False,
        }
        tag = "LIVE" if args.live else "DRY"
        print(f"[{tag}] {side.upper()} limit px={_fmt_lp(lp)} qty={qty_str} usd≈{args.usd:g}")

        if args.live:
            resp = ep.place_order_limit(
                instrument=inst,
                side=side,
                limit_price=float(lp),
                qty=qty_str,
                slippage_limit=float(slip),
                use_mark_price=bool(args.use_mark_price),
                is_reduce_only=False,
                is_auto_resize=False,
            )
            print(json.dumps(resp, indent=2, default=str)[:1200])
            if _resp_looks_rejected(resp):
                print(
                    "  -> Response status looks rejected; order may not rest. "
                    "Try higher --max-slippage (e.g. 0.001 = 0.1%).",
                    file=sys.stderr,
                )
            else:
                rid = _extract_rfq_id(resp)
                if rid and float(args.verify_pending_s) > 0:
                    row = _poll_pending_row_for_rfq(
                        ep,
                        instrument_param=inst_q,
                        rfq_id=rid,
                        deadline_s=float(args.verify_pending_s),
                    )
                    if row:
                        oid = row.get("order_id") or row.get("orderId") or row.get("id")
                        st = row.get("status") or row.get("order_status")
                        print(f"  -> Pending book: matched rfq_id (status={st!r} order_id={oid!r}).")
                    else:
                        print(
                            "  -> No matching pending row yet. Vari returned rfq_id: open the app and "
                            "confirm any RFQ / quote request, or wait and refresh Open Orders.",
                            file=sys.stderr,
                        )
                elif rid:
                    print(
                        "  -> rfq_id returned (--verify-pending-s 0 skipped). "
                        "If nothing appears in Open Orders, confirm the RFQ in the Vari UI.",
                        file=sys.stderr,
                    )

        if float(args.sleep_between_s) > 0:
            time.sleep(float(args.sleep_between_s))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
