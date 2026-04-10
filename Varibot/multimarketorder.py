from __future__ import annotations

"""
Duplicate of multimarketorder.py for a fixed cadence test:
  default --sleep-between-s 1 (pause after each ticker job before the next)
  default --batch-size 0 (no extra batch pauses)

Use multimarketorder.py for batch-10 / batch-sleep defaults.
"""

import argparse
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from variationalbot.config import load_config
from variationalbot.domain import parse_portfolio_snapshot
from variationalbot.vari import VariAuth, VariClient, VariEndpoints
from variationalbot.vari.endpoints import Instrument

# Used when neither --usd nor --im-target-pct is passed (change here to retarget default sizing).
DEFAULT_IM_TARGET_PCT: float = 50.0
# IM-target per-order notional is rounded up to this USD step (e.g. 241.04 -> 250).
USD_NOTIONAL_ROUND_STEP: float = 10.0
# Default max slippage when --max-slippage and MAX_SLIPPAGE env are unset (fraction of notional).
_DEFAULT_MAX_SLIPPAGE: float = 0.0025

# Retry policy for per-ticker market orders when venue rejects due to slippage.
# Mirrors closeallpositions.py's stepped approach, but for individual orders.
SLIPPAGE_RETRY_INCREMENT: float = 0.0005  # +0.05% notional per retry
MAX_LIVE_ATTEMPTS: int = 6
POST_ORDER_INITIAL_DELAY_S: float = 1.0
POST_ORDER_POLL_INTERVAL_S: float = 1.0
POST_ORDER_POLL_MAX: int = 8


def _positions_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _instrument_underlying(p: Dict[str, Any]) -> str:
    inst = p.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    if isinstance(inst, str) and inst.strip():
        return inst.strip().upper()
    pi = p.get("position_info")
    if isinstance(pi, dict):
        inst2 = pi.get("instrument")
        if isinstance(inst2, dict):
            u = inst2.get("underlying")
            if isinstance(u, str) and u.strip():
                return u.strip().upper()
    for k in ("underlying", "symbol", "asset"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return "UNKNOWN"


def _position_qty(p: Dict[str, Any]) -> Optional[float]:
    for k in ("qty", "quantity", "position_qty", "net_qty", "net_position", "size", "positionSize"):
        if k not in p:
            continue
        try:
            return float(p[k])
        except (TypeError, ValueError):
            continue
    pi = p.get("position_info")
    if isinstance(pi, dict) and "qty" in pi:
        try:
            return float(pi["qty"])
        except (TypeError, ValueError):
            return None
    return None


def _current_qty_for_asset(ep: VariEndpoints, asset: str) -> Optional[float]:
    want = str(asset).strip().upper()
    raw = ep.get_positions()
    for p in _positions_list(raw):
        if _instrument_underlying(p) != want:
            continue
        q = _position_qty(p)
        if q is None:
            continue
        return float(q)
    return 0.0


def _looks_like_slippage_reject(msg: str) -> bool:
    m = (msg or "").lower()
    return ("max slippage" in m and ("exceed" in m or "exceeded" in m)) or ("slippage" in m and "exceed" in m)


def _verify_position_after_order(
    *,
    ep: VariEndpoints,
    asset: str,
    side: str,
    reduce_only: bool,
    prev_qty: Optional[float],
) -> bool:
    """
    Best-effort: poll GET /api/positions to confirm the intended position state.
      - non-reduce-only buy: qty should be > 0
      - non-reduce-only sell: qty should be < 0
      - reduce-only: abs(qty) should be smaller than before; for full closes it should reach ~0
    """
    time.sleep(POST_ORDER_INITIAL_DELAY_S)
    for _ in range(POST_ORDER_POLL_MAX):
        q = _current_qty_for_asset(ep, asset)
        if q is None:
            time.sleep(POST_ORDER_POLL_INTERVAL_S)
            continue

        if not reduce_only:
            if str(side).lower() == "buy" and float(q) > 1e-12:
                return True
            if str(side).lower() == "sell" and float(q) < -1e-12:
                return True
        else:
            # If we knew the prior qty, require improvement (closer to zero). Otherwise just accept ~flat.
            if prev_qty is not None:
                if abs(float(q)) <= max(1e-12, abs(float(prev_qty)) * 0.2):
                    return True
                if abs(float(q)) < abs(float(prev_qty)) - 1e-12:
                    return True
            else:
                if abs(float(q)) <= 1e-12:
                    return True

        time.sleep(POST_ORDER_POLL_INTERVAL_S)
    return False


def _split_assets(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    assets: List[str] = []
    for part in str(raw).replace(";", ",").split(","):
        a = part.strip().upper()
        if a:
            assets.append(a)
    # de-dupe while preserving order
    seen: set[str] = set()
    out: List[str] = []
    for a in assets:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _extract_order_id(order_response: Any) -> Optional[str]:
    if not isinstance(order_response, dict):
        return None
    for k in ("order_id", "orderId", "id"):
        v = order_response.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_rfq_id(order_response: Any) -> Optional[str]:
    if not isinstance(order_response, dict):
        return None
    for k in ("rfq_id", "rfqId"):
        v = order_response.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Place multiple Variational market orders (cadence test: "
            "default 1s between each ticker job; batching off by default)."
        )
    )
    p.add_argument(
        "--long",
        default=None,
        help="Comma-separated tickers to go long (maps to side=buy).",
    )
    p.add_argument(
        "--short",
        default=None,
        help="Comma-separated tickers to go short (maps to side=sell).",
    )
    p.add_argument(
        "--assets",
        default=None,
        help="Comma-separated underlyings, e.g. SOL,ETH (preferred for multi-order).",
    )
    p.add_argument(
        "--asset",
        default=None,
        help="Single underlying asset/ticker (legacy). If provided alongside --assets, both are used.",
    )
    p.add_argument(
        "--side",
        default=None,
        choices=["buy", "sell"],
        help="Order side (only used with --assets/--asset mode). For --long/--short, sides are implied.",
    )
    p.add_argument(
        "--usd",
        type=float,
        default=None,
        help="USD notional per order (NOT token qty). If omitted, uses --im-target-pct or a built-in default.",
    )
    p.add_argument(
        "--im-target-pct",
        type=float,
        default=None,
        metavar="PCT",
        dest="im_target_pct",
        help=(
            f"Size each order as (portfolio_value_usd × --leverage × PCT/100) / number_of_orders "
            f"(GET /api/portfolio?compute_margin=true). If --usd is omitted and this is omitted, "
            f"defaults to {DEFAULT_IM_TARGET_PCT:g}%%. Do not pass both --usd and --im-target-pct."
        ),
    )
    p.add_argument(
        "--leverage",
        type=int,
        default=20,
        help="Leverage when using --set-leverage (default: 20).",
    )
    p.add_argument(
        "--set-leverage",
        action="store_true",
        help="Call POST /api/settlement_pools/set_leverage before each order. Default: skip (saves 1 request per ticker).",
    )
    p.add_argument(
        "--max-slippage",
        type=float,
        default=None,
        help=f"Max slippage fraction of notional (default: MAX_SLIPPAGE env or {_DEFAULT_MAX_SLIPPAGE}).",
    )
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
        "--confirm",
        action="store_true",
        help="After submitting live orders, fetch /api/orders/v2?status=pending to confirm order_id/status.",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print the full raw JSON output (debug mode).",
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
        "--sleep-between-s",
        type=float,
        default=1.0,
        help="Seconds to sleep after each ticker job before the next (default 1 for this script).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="After this many orders, extra sleep (0 = disabled). Default 0 for this script.",
    )
    p.add_argument(
        "--batch-sleep-s",
        type=float,
        default=5.0,
        help="Seconds to sleep after each full batch. Ignored if --batch-size is 0.",
    )
    return p


def _instrument_query_param(*, asset: str, settlement_asset: str, funding_interval_s: int) -> str:
    # Observed in DevTools: instrument=P-BTC-USDC-3600
    return f"P-{asset.upper()}-{settlement_asset.upper()}-{int(funding_interval_s)}"


def _try_fetch_pending_order_by_rfq(
    *,
    ep: VariEndpoints,
    instrument_param: str,
    rfq_id: str,
) -> Optional[Dict[str, Any]]:
    # Best-effort: order may move out of pending quickly, or endpoint schema may vary.
    try:
        resp = ep.client.request_json("GET", f"/api/orders/v2?status=pending&instrument={instrument_param}")
    except Exception:
        return None
    if not isinstance(resp, dict):
        return None
    result = resp.get("result")
    if not isinstance(result, list):
        return None
    for item in result:
        if not isinstance(item, dict):
            continue
        if str(item.get("rfq_id") or "") == rfq_id:
            return item
    return None


def _print_positions_entered_table(rows: List[Tuple[str, str, str, str]]) -> None:
    # Match positions.py table vibe: dynamic widths and dashed separator, 2-space join.
    cols = ["Symbol", "Qty", "Value", "Side"]
    data: List[List[str]] = [[sym, qty, value, side] for sym, qty, value, side in rows]

    widths = [len(c) for c in cols]
    for r in data:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def line(parts: List[str]) -> str:
        return "  ".join(parts[i].ljust(widths[i]) for i in range(len(parts)))

    print(line(cols))
    print(line(["-" * w for w in widths]))
    for r in data:
        print(line(r))


def main() -> int:
    args = build_parser().parse_args()
    if args.usd is not None and args.im_target_pct is not None:
        raise SystemExit("Pass at most one of --usd and --im-target-pct.")
    if args.usd is None and args.im_target_pct is None:
        args.im_target_pct = DEFAULT_IM_TARGET_PCT

    cfg = load_config()

    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    long_assets = _split_assets(args.long)
    short_assets = _split_assets(args.short)
    flat_assets = _split_assets(args.assets) + _split_assets(args.asset)

    jobs: List[Dict[str, str]] = []
    for a in long_assets:
        jobs.append({"asset": a, "side": "buy"})
    for a in short_assets:
        jobs.append({"asset": a, "side": "sell"})
    for a in flat_assets:
        if not args.side:
            raise SystemExit("When using --assets/--asset you must provide --side {buy,sell}.")
        jobs.append({"asset": a, "side": str(args.side)})

    if not jobs:
        raise SystemExit("Provide --long/--short, or provide --assets/--asset with --side {buy,sell}.")

    portfolio_value_for_sizing: Optional[float] = None
    if args.usd is None:
        pct = float(args.im_target_pct)  # type: ignore[arg-type]
        if pct <= 0:
            raise SystemExit("--im-target-pct must be positive.")
        raw_pf = ep.get_portfolio(compute_margin=True)
        snap = parse_portfolio_snapshot(raw_pf)
        pv = snap.portfolio_value_usd
        if pv is None or float(pv) <= 0:
            raise SystemExit(
                "IM-target sizing needs portfolio_value_usd from /api/portfolio (missing or non-positive)."
            )
        portfolio_value_for_sizing = float(pv)
        lev = int(args.leverage)
        n_jobs = len(jobs)
        raw_usd = (portfolio_value_for_sizing * float(lev) * (pct / 100.0)) / float(n_jobs)
        step = float(USD_NOTIONAL_ROUND_STEP)
        usd_per_order = math.ceil(raw_usd / step) * step
        print(
            f"Sizing: portfolio_value_usd={portfolio_value_for_sizing} leverage={lev}x im_target_pct={pct}% "
            f"jobs={n_jobs} -> ${raw_usd:.2f} raw -> ${usd_per_order:.0f} per order "
            f"(ceil to nearest ${step:g})"
        )
    else:
        usd_per_order = float(args.usd)

    leverage = int(args.leverage)
    max_slippage = (
        float(args.max_slippage)
        if args.max_slippage is not None
        else float(os.environ.get("MAX_SLIPPAGE", str(_DEFAULT_MAX_SLIPPAGE)))
    )

    out: Dict[str, Any] = {
        "ts": time.time(),
        "script": "multimarketorder.py",
        "base_url": cfg.base_url,
        "wallet": cfg.wallet_address,
        "live": bool(args.live),
        "max_slippage": float(max_slippage),
        "reduce_only": bool(args.reduce_only),
        "leverage": int(leverage),
        "set_leverage_per_order": bool(args.set_leverage),
        "usd_notional_per_asset": float(usd_per_order),
        "sizing": ("im_target_pct" if args.usd is None else "fixed_usd"),
        "long": long_assets,
        "short": short_assets,
        "assets": flat_assets,
        "job_count": len(jobs),
        "batch_size": int(getattr(args, "batch_size", 0) or 0),
        "batch_sleep_s": float(getattr(args, "batch_sleep_s", 0.0) or 0.0),
        "sleep_between_s": float(args.sleep_between_s),
        "orders": [],
    }
    if args.usd is None:
        out["im_target_pct"] = float(args.im_target_pct)  # type: ignore[arg-type]
        out["portfolio_value_usd_for_sizing"] = portfolio_value_for_sizing

    orders_out: List[Dict[str, Any]] = []
    for i, job in enumerate(jobs):
        asset = job["asset"]
        side = job["side"]
        instrument = Instrument(
            instrument_type="perpetual_future",
            underlying=asset,
            funding_interval_s=int(args.funding_interval_s),
            settlement_asset=str(args.settlement_asset),
        )

        item: Dict[str, Any] = {
            "asset": asset,
            "side": side,
            "instrument": {
                "instrument_type": instrument.instrument_type,
                "underlying": instrument.underlying,
                "funding_interval_s": instrument.funding_interval_s,
                "settlement_asset": instrument.settlement_asset,
            },
            "steps": {},
        }

        try:
            prev_qty: Optional[float] = None
            if args.live:
                try:
                    prev_qty = _current_qty_for_asset(ep, asset)
                except Exception:
                    prev_qty = None

            if args.set_leverage:
                lev_res = ep.set_leverage(asset=asset, leverage=int(leverage))
                item["steps"]["set_leverage"] = {
                    "requested": int(leverage),
                    "response": {"current": lev_res.current, "max": lev_res.max},
                }
            else:
                item["steps"]["set_leverage"] = {"skipped": True}

            quote_id, quote = ep.quote_id_for_usd_notional(
                instrument=instrument,
                side=side,
                usd_notional=float(usd_per_order),
            )
            item["steps"]["quote"] = quote

            order_payload: Dict[str, Any] = {
                "quote_id": quote_id,
                "side": side,
                "max_slippage": float(max_slippage),
                "is_reduce_only": bool(args.reduce_only),
            }
            item["steps"]["order_request"] = {
                "method": "POST",
                "path": "/api/orders/new/market",
                "json": order_payload,
            }

            if args.live:
                attempts: List[Dict[str, Any]] = []
                last_err: Optional[Exception] = None
                for attempt in range(1, MAX_LIVE_ATTEMPTS + 1):
                    slip = float(max_slippage) + float(attempt - 1) * float(SLIPPAGE_RETRY_INCREMENT)
                    try:
                        quote_id2, quote2 = ep.quote_id_for_usd_notional(
                            instrument=instrument,
                            side=side,
                            usd_notional=float(usd_per_order),
                        )
                        resp = ep.place_order_market(
                            quote_id=quote_id2,
                            side=side,
                            max_slippage=float(slip),
                            is_reduce_only=bool(args.reduce_only),
                        )
                        ok = _verify_position_after_order(
                            ep=ep,
                            asset=asset,
                            side=side,
                            reduce_only=bool(args.reduce_only),
                            prev_qty=prev_qty,
                        )
                        attempts.append(
                            {
                                "attempt": attempt,
                                "max_slippage": float(slip),
                                "quote": quote2,
                                "order_response": resp,
                                "verified_by_positions": bool(ok),
                            }
                        )
                        item["steps"]["attempts"] = attempts
                        item["steps"]["order_response"] = resp
                        item["steps"]["rfq_id"] = _extract_rfq_id(resp)
                        item["steps"]["order_id"] = _extract_order_id(resp)
                        if not ok:
                            item["steps"]["warning"] = (
                                "Order submitted but positions check did not confirm expected state within polling window."
                            )
                        break
                    except Exception as e:
                        last_err = e
                        msg = str(e)
                        attempts.append(
                            {
                                "attempt": attempt,
                                "max_slippage": float(slip),
                                "error": {"type": type(e).__name__, "message": msg},
                            }
                        )
                        if not _looks_like_slippage_reject(msg):
                            raise
                        if attempt < MAX_LIVE_ATTEMPTS:
                            time.sleep(0.25)
                else:
                    if last_err is not None:
                        raise last_err
            else:
                item["steps"]["note"] = "Dry-run only. Re-run with --live to actually place orders."
        except Exception as e:
            item["error"] = {"type": type(e).__name__, "message": str(e)}

        orders_out.append(item)

        if float(args.sleep_between_s) > 0 and i != (len(jobs) - 1):
            time.sleep(float(args.sleep_between_s))

        batch_size = int(args.batch_size)
        if batch_size > 0 and (i + 1) % batch_size == 0 and (i + 1) < len(jobs):
            time.sleep(float(args.batch_sleep_s))

    out["orders"] = orders_out

    if not args.live:
        out["note"] = "Dry-run only. Re-run with --live to actually place the orders."
        if args.print_json:
            print(json.dumps(out, indent=2, default=str))
            return 0
        # Compact dry-run summary
        rows: List[Tuple[str, str, str, str]] = []
        for o in orders_out:
            asset = str(o.get("asset") or "").upper()
            side = str(o.get("side") or "").lower()
            qty = "-"
            usd_val = float(usd_per_order)
            value = f"${int(usd_val)}" if usd_val.is_integer() else f"${usd_val}"
            steps = o.get("steps") if isinstance(o.get("steps"), dict) else {}
            quote = steps.get("quote") if isinstance(steps.get("quote"), dict) else None
            if isinstance(quote, dict) and "qty" in quote:
                qty = str(quote.get("qty"))
            if asset and side:
                rows.append((asset, qty, value, side.capitalize()))
        print(f"Positions entered: {len(rows)} (dry-run)")
        if rows:
            _print_positions_entered_table(rows)
        return 0

    if args.print_json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    # Compact live confirmation (best-effort).
    rows2: List[Tuple[str, str, str, str]] = []
    for o in orders_out:
        asset = str(o.get("asset") or "").upper()
        side = str(o.get("side") or "").lower()
        qty = "-"
        usd_val = float(usd_per_order)
        value = f"${int(usd_val)}" if usd_val.is_integer() else f"${usd_val}"
        steps = o.get("steps") if isinstance(o.get("steps"), dict) else {}
        quote = steps.get("quote") if isinstance(steps.get("quote"), dict) else None
        if isinstance(quote, dict) and "qty" in quote:
            qty = str(quote.get("qty"))

        # Try to confirm via orders/v2 pending (this is where DevTools shows order_id/status/qty).
        if args.confirm and isinstance(steps, dict):
            rfq_id = steps.get("rfq_id")
            if isinstance(rfq_id, str) and rfq_id:
                instrument_param = _instrument_query_param(
                    asset=asset,
                    settlement_asset=str(args.settlement_asset),
                    funding_interval_s=int(args.funding_interval_s),
                )
                confirmed = _try_fetch_pending_order_by_rfq(
                    ep=ep,
                    instrument_param=instrument_param,
                    rfq_id=rfq_id,
                )
                if isinstance(confirmed, dict):
                    # Prefer confirmed qty/side if present.
                    if confirmed.get("qty") is not None:
                        qty = str(confirmed.get("qty"))
                    if confirmed.get("side") is not None:
                        side = str(confirmed.get("side")).lower()
                    # Stash for anyone still using --print-json in future
                    steps["confirmed_v2_pending"] = {
                        "order_id": confirmed.get("order_id"),
                        "status": confirmed.get("status"),
                        "qty": confirmed.get("qty"),
                        "side": confirmed.get("side"),
                    }

        if asset and side:
            rows2.append((asset, qty, value, side.capitalize()))

    print(f"Positions entered: {len(rows2)}")
    if rows2:
        _print_positions_entered_table(rows2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
