from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints
from variationalbot.vari.endpoints import Instrument


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Place a single Variational market order (safe-by-default).")
    p.add_argument("--asset", required=True, help="Underlying asset/ticker, e.g. SOL")
    p.add_argument("--side", required=True, choices=["buy", "sell"], help="Order side")
    p.add_argument("--usd", required=True, type=float, help="USD notional size (NOT token qty)")
    p.add_argument("--leverage", type=int, default=None, help="Leverage to set (default: DEFAULT_LEVERAGE env)")
    p.add_argument("--max-slippage", type=float, default=None, help="Max slippage (default: MAX_SLIPPAGE env)")
    p.add_argument(
        "--reduce-only",
        action="store_true",
        help="Send is_reduce_only=true (useful for closing/reducing).",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Actually place the order. Without this flag, script is dry-run only.",
    )
    p.add_argument(
        "--funding-interval-s",
        type=int,
        default=3600,
        help="Instrument funding interval seconds (placeholder default 3600).",
    )
    p.add_argument(
        "--settlement-asset",
        default="USDC",
        help="Instrument settlement asset (placeholder default USDC).",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print full JSON request/quote/response (default: silent).",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config()

    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    asset = args.asset.strip().upper()
    import os

    leverage = int(args.leverage) if args.leverage is not None else int(os.environ.get("DEFAULT_LEVERAGE", "20"))
    max_slippage = float(args.max_slippage) if args.max_slippage is not None else float(os.environ.get("MAX_SLIPPAGE", "0.002"))

    instrument = Instrument(
        instrument_type="perpetual_future",
        underlying=asset,
        funding_interval_s=int(args.funding_interval_s),
        settlement_asset=str(args.settlement_asset),
    )

    # 1) set leverage (required pre-trade step)
    lev_res = ep.set_leverage(asset=asset, leverage=int(leverage))

    # 2) quote_id for USD size (quote endpoint uses qty token amount internally)
    quote_id, quote = ep.quote_id_for_usd_notional(
        instrument=instrument,
        side=args.side,
        usd_notional=float(args.usd),
    )

    # 3) order payload (placeholders are filled here)
    order_payload: Dict[str, Any] = {
        "quote_id": quote_id,
        "side": args.side,
        "max_slippage": float(max_slippage),
        "is_reduce_only": bool(args.reduce_only),
    }

    out: Dict[str, Any] = {
        "ts": time.time(),
        "base_url": cfg.base_url,
        "wallet": cfg.wallet_address,
        "live": bool(args.live),
        "set_leverage": {"requested": int(leverage), "response": {"current": lev_res.current, "max": lev_res.max}},
        "instrument": {
            "instrument_type": instrument.instrument_type,
            "underlying": instrument.underlying,
            "funding_interval_s": instrument.funding_interval_s,
            "settlement_asset": instrument.settlement_asset,
        },
        "usd_notional": float(args.usd),
        "quote": quote,
        "order_request": {
            "method": "POST",
            "path": "/api/orders/new/market",
            "json": order_payload,
        },
    }

    if not args.live:
        out["note"] = "Dry-run only. Re-run with --live to actually place the order."
        if args.print_json:
            print(json.dumps(out, indent=2, default=str))
        return 0

    resp = ep.place_order_market(
        quote_id=quote_id,
        side=args.side,
        max_slippage=float(max_slippage),
        is_reduce_only=bool(args.reduce_only),
    )
    out["order_response"] = resp
    if args.print_json:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

