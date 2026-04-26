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
DEFAULT_IM_TARGET_PCT: float = 10.0
# IM-target per-order notional is rounded up to this USD step (e.g. 241.04 -> 250).
USD_NOTIONAL_ROUND_STEP: float = 10.0
# Default max slippage when --max-slippage and MAX_SLIPPAGE env are unset (fraction of notional).
_DEFAULT_MAX_SLIPPAGE: float = 0.0005

# Post-entry verification: /api/positions can lag shortly after fills.
_POST_ENTRY_POSITIONS_MAX_WAIT_S: float = 2.0
_POST_ENTRY_POSITIONS_POLL_S: float = 0.5

# Retry behavior for *this* script only (intentionally independent from closeallpositions.py).
_SLIPPAGE_RETRY_INCREMENT: float = 0.0003
_MAX_LIVE_ATTEMPTS: int = 6


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


def _positions_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _inst_sym(p: Dict[str, Any]) -> str:
    inst = p.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    if isinstance(inst, str) and inst.strip():
        return inst.strip().upper()
    pos_info = p.get("position_info")
    if isinstance(pos_info, dict):
        inst2 = pos_info.get("instrument")
        if isinstance(inst2, dict):
            u = inst2.get("underlying")
            if isinstance(u, str) and u.strip():
                return u.strip().upper()
    for k in ("underlying", "symbol", "asset"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return ""


def _fetch_positions_underlyings(ep: VariEndpoints) -> set[str]:
    raw = ep.get_positions()
    out: set[str] = set()
    for p in _positions_list(raw):
        sym = _inst_sym(p)
        if sym:
            out.add(sym)
    return out


def _fmt_slippage_pct(x: float) -> str:
    # Show as percent (fraction → percent), trimmed to 2dp.
    return f"{float(x) * 100.0:.2f}%"


def _order_response_rejected(resp: Any) -> bool:
    """
    Vari may return HTTP 200 with an order payload whose status is "Rejected" (e.g. max slippage exceeded).
    Treat that as a failure for retry purposes.
    """
    if not isinstance(resp, dict):
        return False
    status = resp.get("status") or resp.get("order_status") or resp.get("state")
    if isinstance(status, str) and status.strip().lower() in {"rejected", "reject", "failed", "error", "cancelled"}:
        return True
    # Some payloads may provide a reject reason without a status field.
    for k in ("reject_reason", "rejectReason", "error", "message", "reason"):
        v = resp.get(k)
        if isinstance(v, str) and "slippage" in v.lower() and ("exceed" in v.lower() or "exceeded" in v.lower()):
            return True
    return False


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

    buying_s = ", ".join(long_assets)
    selling_s = ", ".join(short_assets)
    usd_s = int(usd_per_order) if float(usd_per_order).is_integer() else float(usd_per_order)
    print(
        "Buying: "
        f"{buying_s}, "
        "Selling: "
        f"{selling_s}, "
        f"usd_per_order: {usd_s}, "
        f"max_slippage: {_fmt_slippage_pct(float(max_slippage))}, "
        f"live: {str(bool(args.live)).lower()}"
    )
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
            if args.set_leverage:
                lev_res = ep.set_leverage(asset=asset, leverage=int(leverage))
                item["steps"]["set_leverage"] = {
                    "requested": int(leverage),
                    "response": {"current": lev_res.current, "max": lev_res.max},
                }
            else:
                item["steps"]["set_leverage"] = {"skipped": True}

            # Quote + place order with closeall-style retry (increment slippage on failures).
            attempts: List[Dict[str, Any]] = []
            last_quote: Any = None
            qty_disp = "-"
            value_disp = f"${int(usd_per_order)}" if float(usd_per_order).is_integer() else f"${usd_per_order}"

            if not args.live:
                quote_id, quote = ep.quote_id_for_usd_notional(
                    instrument=instrument,
                    side=side,
                    usd_notional=float(usd_per_order),
                )
                last_quote = quote
                item["steps"]["quote"] = quote
                item["steps"]["note"] = "Dry-run only. Re-run with --live to actually place orders."
                try:
                    if isinstance(quote, dict) and quote.get("qty") is not None:
                        qty_disp = str(quote.get("qty"))
                except Exception:
                    qty_disp = "-"
            else:
                ok = False
                base_slip = float(max_slippage)
                for attempt in range(1, int(_MAX_LIVE_ATTEMPTS) + 1):
                    slip = base_slip + float(attempt - 1) * float(_SLIPPAGE_RETRY_INCREMENT)
                    try:
                        quote_id, quote = ep.quote_id_for_usd_notional(
                            instrument=instrument,
                            side=side,
                            usd_notional=float(usd_per_order),
                        )
                        last_quote = quote
                        if isinstance(quote, dict) and quote.get("qty") is not None:
                            qty_disp = str(quote.get("qty"))
                        order_payload: Dict[str, Any] = {
                            "quote_id": quote_id,
                            "side": side,
                            "max_slippage": float(slip),
                            "is_reduce_only": bool(args.reduce_only),
                        }
                        item["steps"]["order_request"] = {
                            "method": "POST",
                            "path": "/api/orders/new/market",
                            "json": order_payload,
                        }
                        resp = ep.place_order_market(
                            quote_id=quote_id,
                            side=side,
                            max_slippage=float(slip),
                            is_reduce_only=bool(args.reduce_only),
                        )
                        if _order_response_rejected(resp):
                            raise RuntimeError(f"order rejected (status={resp.get('status')})")
                        item["steps"]["order_response"] = resp
                        item["steps"]["rfq_id"] = _extract_rfq_id(resp)
                        item["steps"]["order_id"] = _extract_order_id(resp)
                        ok = True
                        attempts.append({"attempt": attempt, "max_slippage": float(slip), "ok": True})
                        break
                    except Exception as e:
                        attempts.append(
                            {
                                "attempt": attempt,
                                "max_slippage": float(slip),
                                "ok": False,
                                "error": {"type": type(e).__name__, "message": str(e)},
                            }
                        )
                        if attempt >= int(_MAX_LIVE_ATTEMPTS):
                            raise
                        time.sleep(0.5)

                item["steps"]["quote"] = last_quote
                item["steps"]["slippage_retry"] = {
                    "base_max_slippage": float(base_slip),
                    "slippage_retry_increment": float(_SLIPPAGE_RETRY_INCREMENT),
                    "max_attempts": int(_MAX_LIVE_ATTEMPTS),
                    "attempts": attempts,
                }
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
        # Final summary only (table already streamed during entry).
        n_ok = sum(1 for o in orders_out if not isinstance(o.get("error"), dict))
        print(f"Positions entered: {n_ok} (dry-run)")
        return 0

    if args.print_json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    # Compact live confirmation (best-effort).
    # Also reconcile against /api/positions to detect any missing tickers after entry.
    expected_syms: set[str] = {str(j.get("asset") or "").strip().upper() for j in jobs if j.get("asset")}
    missing: set[str] = set(expected_syms)
    seen: set[str] = set()
    start = time.time()
    while True:
        try:
            seen = _fetch_positions_underlyings(ep)
            missing = set(expected_syms) - set(seen)
        except Exception:
            missing = set(expected_syms)
        if not missing:
            break
        if (time.time() - start) >= float(_POST_ENTRY_POSITIONS_MAX_WAIT_S):
            break
        time.sleep(float(_POST_ENTRY_POSITIONS_POLL_S))

    # Final summary only (table already streamed during entry).
    verified = len(expected_syms) - len(missing)
    if len(expected_syms) > 0:
        print(f"Positions entered: {verified}")
    if missing:
        miss = ", ".join(sorted(missing))
        print(f"WARNING: missing position(s) after entry: {miss}")

        # Best-effort reattempt rounds: step slippage each round (base + k*increment).
        # Continue until all missing tickers show up in /api/positions, or we hit max rounds.
        missing2: set[str] = set(missing)
        for round_i in range(1, int(_MAX_LIVE_ATTEMPTS) + 1):
            if not missing2:
                break
            slip_round = float(max_slippage) + float(round_i) * float(_SLIPPAGE_RETRY_INCREMENT)
            print(f"Reattempting failed tickers with higher slippage {_fmt_slippage_pct(float(slip_round))}...")

            for sym in sorted(missing2):
                # Find original side from jobs (fall back to buy).
                side = "buy"
                for j in jobs:
                    if str(j.get("asset") or "").strip().upper() == sym:
                        side = str(j.get("side") or "buy")
                        break
                instrument = Instrument(
                    instrument_type="perpetual_future",
                    underlying=sym,
                    funding_interval_s=int(args.funding_interval_s),
                    settlement_asset=str(args.settlement_asset),
                )
                try:
                    quote_id, _quote = ep.quote_id_for_usd_notional(
                        instrument=instrument,
                        side=side,
                        usd_notional=float(usd_per_order),
                    )
                    resp2 = ep.place_order_market(
                        quote_id=quote_id,
                        side=side,
                        max_slippage=float(slip_round),
                        is_reduce_only=bool(args.reduce_only),
                    )
                    if _order_response_rejected(resp2):
                        raise RuntimeError("order rejected")
                except Exception:
                    # Keep it missing; we will try again next round with higher slippage.
                    pass

            # Re-check after each round.
            start2 = time.time()
            while True:
                try:
                    seen2 = _fetch_positions_underlyings(ep)
                    missing2 = set(expected_syms) - set(seen2)
                except Exception:
                    missing2 = set(expected_syms)
                if not missing2:
                    break
                if (time.time() - start2) >= float(_POST_ENTRY_POSITIONS_MAX_WAIT_S):
                    break
                time.sleep(float(_POST_ENTRY_POSITIONS_POLL_S))

            verified2 = len(expected_syms) - len(missing2)
            print(f"Positions entered: {verified2}")
            if missing2:
                print(f"WARNING: missing position(s) after reattempt: {', '.join(sorted(missing2))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
