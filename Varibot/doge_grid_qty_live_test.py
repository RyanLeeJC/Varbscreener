#!/usr/bin/env python3
"""
One-shot live DOGE grid: sync ladder, post missing limits, print expected vs venue qty.

Usage (from Varibot/):
  GRID_TRADING_TICKERS=DOGE:1 GRID_NUM=4 GRID_INVESTMENT_USD=50 GRID_LEVERAGE=40 \\
    GRIDSTRAT_RESET=1 python3 doge_grid_qty_live_test.py --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from variationalbot.config import load_config  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402
from variationalbot.vari.endpoints import Instrument, limit_price_key  # noqa: E402

from grid_limits_reconcile import (  # noqa: E402
    _fetch_pending_limit_rows,
    run_grid_limits_bootstrap,
)
from strategy.gridstrat import pick_tickers  # noqa: E402
import varibot  # noqa: E402


def _pending_doge_limits(ep: VariEndpoints) -> list[dict]:
    return list(_fetch_pending_limit_rows(ep, asset="DOGE"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Live DOGE grid limit qty check.")
    ap.add_argument("--live", action="store_true", help="Place limits (default dry-run).")
    ap.add_argument("--skip-cancel", action="store_true", help="Do not cancel existing pending limits first.")
    args = ap.parse_args()

    os.environ.setdefault("GRID_TRADING_TICKERS", "DOGE:1")
    os.environ.setdefault("GRID_NUM", "4")
    os.environ.setdefault("GRID_INVESTMENT_USD", "20")
    os.environ.setdefault("GRID_LEVERAGE", "5")
    os.environ.setdefault("GRID_BAND_PCT", "1")
    if not os.environ.get("GRIDSTRAT_RESET", "").strip():
        os.environ["GRIDSTRAT_RESET"] = "1"

    cfg = load_config()
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    listing_path = varibot._refresh_strategy_listing_snapshot_from_venue(ep, asset_hint="DOGE")
    print(f"listing: {listing_path}")

    _, _, meta = pick_tickers(listing_json=listing_path, account_flat=True)
    doge_meta = (meta.get("grid_by_asset") or {}).get("DOGE") or meta
    if doge_meta.get("error"):
        print(f"ERROR gridstrat: {doge_meta['error']}", file=sys.stderr)
        return 1

    expected_qty = str(doge_meta.get("grid_per_rung_qty") or "").strip()
    per_usd = doge_meta.get("grid_per_rung_usd")
    mark = doge_meta.get("grid_mark")
    n_buys = len(doge_meta.get("grid_buy_rungs") or [])
    n_sells = len(doge_meta.get("grid_sell_rungs") or [])
    print(
        f"DOGE grid: mark={mark} per_rung_usd={per_usd} expected_qty={expected_qty!r} "
        f"rungs buy={n_buys} sell={n_sells}"
    )

    print(f"meta per_rung_qty={doge_meta.get('grid_per_rung_qty')!r}")
    for side, rungs in (("buy", doge_meta.get("grid_buy_rungs") or []), ("sell", doge_meta.get("grid_sell_rungs") or [])):
        for px in rungs[:2]:
            print(f"  meta {side} @ {px}")

    if not args.live:
        print("\nDry-run only. Re-run with --live to post limits and compare venue qty.")
        return 0

    if not args.skip_cancel:
        print("\nCanceling all pending limits first...")
        rc = varibot._run_script(
            os.path.join(_VARIBOT_DIR, "cancelalllimitorders.py"),
            cwd=_VARIBOT_DIR,
            args=["--live"],
            timeout_s=600,
        )
        if rc != 0:
            print(f"WARNING: cancelalllimitorders exited {rc}", file=sys.stderr)

    ns = argparse.Namespace(live=True)
    place_limit = varibot._grid_limits_place_limit_fn(ep, ns)
    run_grid_limits_bootstrap(
        ep=ep,
        meta=meta,
        varibot_dir=_VARIBOT_DIR,
        cycle_index=1,
        has_positions=False,
        log=print,
        place_limit=place_limit,
        live=True,
        multi_script="multimarketorder.py",
    )

    pending = _pending_doge_limits(ep)
    print(f"\nVenue pending DOGE limits: {len(pending)}")
    mismatches = 0
    for o in pending:
        side = str(o.get("side") or "").strip().lower()
        lp = o.get("limit_price")
        qty = o.get("qty")
        try:
            px = float(lp)
        except (TypeError, ValueError):
            px = None
        key = limit_price_key(side, float(px)) if px is not None else None
        inst = Instrument(
            instrument_type="perpetual_future",
            underlying="DOGE",
            funding_interval_s=3600,
            settlement_asset="USDC",
        )
        venue_norm = ep.normalize_grid_limit_qty(
            instrument=inst,
            side=side,
            qty_raw=float(qty) if qty is not None else 0.0,
        )
        match = str(qty).strip() == expected_qty or venue_norm == expected_qty
        flag = "OK" if match else "MISMATCH"
        if not match:
            mismatches += 1
        print(f"  {flag} {side} @ {lp} venue_qty={qty!r} expected={expected_qty!r} key={key}")

    if mismatches:
        print(f"\n{ mismatches } limit(s) with qty != expected {expected_qty!r}", file=sys.stderr)
        return 2
    print("\nAll DOGE limit qty values match gridstrat template.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
