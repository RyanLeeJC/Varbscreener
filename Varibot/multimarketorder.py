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
from variationalbot.vari.endpoints import Instrument, format_qty_for_indicative_api

# Varibot reads this after each run to substitute tickers on OI-skew / risk-cap rejects (near_median).
MULTIMARKET_LAST_RESULT_JSON: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".multimarket_last_result.json"
)


def _write_multimarket_last_result(
    orders_out: List[Dict[str, Any]],
    *,
    skew_extra: Optional[List[Dict[str, str]]] = None,
    slippage_extra: Optional[List[Dict[str, str]]] = None,
) -> None:
    """Persist venue rejects so varibot can substitute tickers (OI skew / slippage exhausted)."""
    skew: List[Dict[str, str]] = []
    slip: List[Dict[str, str]] = []
    for o in orders_out:
        err = o.get("error") if isinstance(o, dict) else None
        if not isinstance(err, dict):
            continue
        et = err.get("type")
        if et == "OiSkewRiskCapReject":
            skew.append(
                {
                    "asset": str(o.get("asset") or "").strip().upper(),
                    "side": str(o.get("side") or "").strip().lower(),
                }
            )
        elif et == "SlippageExhausted":
            slip.append(
                {
                    "asset": str(err.get("asset") or o.get("asset") or "").strip().upper(),
                    "side": str(err.get("side") or o.get("side") or "").strip().lower(),
                }
            )
    if skew_extra:
        skew.extend(skew_extra)
    if slippage_extra:
        slip.extend(slippage_extra)
    try:
        with open(MULTIMARKET_LAST_RESULT_JSON, "w", encoding="utf-8") as f:
            json.dump(
                {"skew_rejected": skew, "slippage_exhausted": slip, "ts": time.time()},
                f,
                indent=2,
            )
    except OSError:
        pass


# Used when neither --usd nor --im-target-pct is passed (change here to retarget default sizing).
DEFAULT_IM_TARGET_PCT: float = 50.0
# Default --leverage: IM-target sizing (portfolio × this × PCT) and set_leverage API before each live order.
# Same-named env overrides this (aligned with varibot._multimarket_effective_leverage).
# If pool leverage stays below this while notionals are sized for higher leverage, IM usage rises faster than gap math.
DEFAULT_LEVERAGE: int = 50

# Default number of "slots" across the book for sizing (per-order USD = pv×lev×(im_target_pct/100) / slots).
# Prefer the strategy constant when available so strategy and order script stay aligned.
try:
    from strategy.gridstrat import DEFAULT_MAX_TICKER_ENTRIES as DEFAULT_MAX_TICKER_ENTRIES  # type: ignore
except Exception:
    DEFAULT_MAX_TICKER_ENTRIES: int = 40


def default_leverage_from_env() -> int:
    """Prefer DEFAULT_LEVERAGE env (digits only), then DEFAULT_LEVERAGE constant."""
    v = (os.environ.get("DEFAULT_LEVERAGE", "") or "").strip()
    if v:
        try:
            return max(1, int(float(v)))
        except Exception:
            pass
    return int(DEFAULT_LEVERAGE)
# IM-target per-order notional is rounded up to this USD step (e.g. 241.04 -> 250).
USD_NOTIONAL_ROUND_STEP: float = 10.0
# Default max slippage when --max-slippage and MAX_SLIPPAGE env are unset (fraction of notional).
_DEFAULT_MAX_SLIPPAGE: float = 0.001

# Post-entry verification: /api/positions can lag shortly after fills.
_POST_ENTRY_POSITIONS_MAX_WAIT_S: float = 2.0
_POST_ENTRY_POSITIONS_POLL_S: float = 0.5

# Retry behavior for *this* script only (intentionally independent from closeallpositions.py).
_SLIPPAGE_RETRY_INCREMENT: float = 0.0005
_MAX_LIVE_ATTEMPTS: int = 6

# If you re-enable/extend position-verification helpers, these defaults are used for polling.
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
        "--qty",
        type=float,
        default=None,
        help=(
            "Base-asset quantity per order (e.g. BTC). Indicative qty strings use significant figures "
            "(see vari.endpoints.format_qty_for_indicative_api / INDICATIVE_QTY_SIGFIGS). "
            "Mutually exclusive with --usd and --im-target-pct."
        ),
    )
    p.add_argument(
        "--im-target-pct",
        type=float,
        default=None,
        metavar="PCT",
        dest="im_target_pct",
        help=(
            "Size each order as (portfolio_value_usd × --leverage × PCT/100) / --max-ticker-entries "
            "(GET /api/portfolio?compute_margin=true). "
            f"If --usd is omitted and this is omitted, defaults to {DEFAULT_IM_TARGET_PCT:g}%%. "
            "Do not pass both --usd and --im-target-pct."
        ),
    )
    p.add_argument(
        "--max-ticker-entries",
        type=int,
        default=int(DEFAULT_MAX_TICKER_ENTRIES),
        help=(
            "Sizing divisor used with --im-target-pct (per-order USD uses pv×lev×pct/this). "
            f"Default {int(DEFAULT_MAX_TICKER_ENTRIES)}."
        ),
    )
    p.add_argument(
        "--leverage",
        type=int,
        default=default_leverage_from_env(),
        help=(
            "Cross leverage for POST /api/settlement_pools/set_leverage before each order "
            f"(default: DEFAULT_LEVERAGE env if set, else {DEFAULT_LEVERAGE}). "
            "Use --skip-set-leverage to leave pool leverage unchanged."
        ),
    )
    p.add_argument(
        "--skip-set-leverage",
        action="store_true",
        help=(
            "Do not call set_leverage before each order (previous default; saves one API request per ticker). "
            "Otherwise each instrument is set to --leverage (default from DEFAULT_LEVERAGE env or constant). "
            "Skipping set_leverage can leave low cross leverage → higher IM per $ notional vs PM sizing."
        ),
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
        "--skip-im-hard-cap",
        action="store_true",
        help="Skip the IM usage hard-cap guard (GET /api/portfolio) before new opens. Testing / probes only.",
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


def _flatten_resp_text(obj: Any, *, max_len: int = 6000) -> str:
    """Join string-like fields from nested dict/list for keyword checks."""
    parts: List[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return " ".join(parts)[:max_len]


def _venue_risk_substitute_ticker_message(blob_lower: str) -> bool:
    """
    Venue-side risk rejects where raising max_slippage will not help — substitute another ticker
    (same path as OI skew / FDV skew limits).

    Includes: OI skew vs FDV, global/total OI cap on the asset (Omni 'Risk Check Failed' toast / order history).
    """
    b = blob_lower
    if "risk checks have failed" in b:
        return True
    if "risk check failed" in b:
        return True
    if "total oi on this asset" in b and "too large" in b:
        return True
    if "too large to accommodate your trade" in b:
        return True
    # Wording variants seen on venue / order history.
    if "accommodate your trade" in b and ("oi" in b or "open interest" in b):
        return True
    if "total oi" in b and "too large" in b:
        return True
    if "skewaspercentoffdv" in b or "skew as percent" in b:
        return True
    if "skew" in b and "too large" in b and ("oi" in b or "open interest" in b):
        return True
    if "fully diluted" in b and "skew" in b:
        return True
    return False


def _order_reject_is_skew_or_risk_cap(resp: Any) -> bool:
    """
    Venue risk: OI skew / FDV cap (SkewAsPercentOfFdvLimit), total-OI cap, and similar risk checks.
    Retrying with higher max_slippage does not help.
    """
    return _venue_risk_substitute_ticker_message(_flatten_resp_text(resp).lower())


def _orders_v2_result_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("result", "orders", "data", "items"):
            v = raw.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _order_row_underlying(row: Dict[str, Any]) -> str:
    inst = row.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    for k in ("underlying", "asset", "symbol"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            s = v.strip().upper()
            if s.endswith("-PERP"):
                s = s[: -len("-PERP")]
            return s
    return ""


def _venue_risk_visible_in_orders_v2(
    ep: VariEndpoints,
    sym: str,
    *,
    settlement_asset: str,
    funding_interval_s: int,
) -> bool:
    """Best-effort: recent rejected row for this instrument still carries risk/OI text."""
    sym_u = str(sym).strip().upper()
    inst_q = _instrument_query_param(
        asset=sym_u, settlement_asset=str(settlement_asset), funding_interval_s=int(funding_interval_s)
    )
    try:
        raw = ep.client.request_json("GET", f"/api/orders/v2?instrument={inst_q}")
    except Exception:
        return False
    for item in _orders_v2_result_items(raw):
        if _order_row_underlying(item) != sym_u:
            continue
        st = str(item.get("status") or item.get("order_status") or item.get("state") or "").lower()
        if "reject" not in st and st not in ("failed", "error", "cancelled"):
            continue
        if _venue_risk_substitute_ticker_message(_flatten_resp_text(item).lower()):
            return True
    return False


def _annotate_skew_reject_for_missing_asset(
    sym: str,
    orders_out: List[Dict[str, Any]],
    jobs: List[Dict[str, str]],
    *,
    ep: Optional[VariEndpoints],
    settlement_asset: str,
    funding_interval_s: int,
) -> bool:
    """
    If we treated an order as OK but have no position, recover OI / risk-cap from stored
    order_response or /api/orders/v2 so we do not waste cycles on slippage reattempts.
    """
    sym_u = str(sym).strip().upper()
    side_fm = "buy"
    for j in jobs:
        if str(j.get("asset") or "").strip().upper() == sym_u:
            side_fm = str(j.get("side") or "buy").strip().lower()
            break
    for o in orders_out:
        if str(o.get("asset") or "").strip().upper() != sym_u:
            continue
        if isinstance(o.get("error"), dict) and o.get("error", {}).get("type") == "OiSkewRiskCapReject":
            return True
        resp_stored = (o.get("steps") or {}).get("order_response")
        blob = ""
        if resp_stored is not None:
            blob = _flatten_resp_text(resp_stored).lower()
        if blob and _venue_risk_substitute_ticker_message(blob):
            snippet = _flatten_resp_text(resp_stored)
            snippet = (snippet[:280] + "…") if len(snippet) > 280 else snippet
            o.setdefault("steps", {})["slippage_retry_stopped"] = "oi_skew_or_risk_cap"
            o["error"] = {
                "type": "OiSkewRiskCapReject",
                "message": snippet or "Venue risk: OI / risk checks",
            }
            print(
                f"NOTE: {sym_u} missing position but order response shows venue risk/OI cap; "
                f"skipping slippage retries. {snippet}"
            )
            return True
        if ep is not None and _venue_risk_visible_in_orders_v2(
            ep, sym_u, settlement_asset=settlement_asset, funding_interval_s=funding_interval_s
        ):
            o.setdefault("steps", {})["slippage_retry_stopped"] = "oi_skew_or_risk_cap"
            o["error"] = {
                "type": "OiSkewRiskCapReject",
                "message": f"Venue risk (from /api/orders/v2): {sym_u} {side_fm}",
            }
            print(
                f"NOTE: {sym_u} missing position; /api/orders/v2 shows rejected risk/OI — "
                "skipping slippage retries."
            )
            return True
        return False
    return False


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
    if _venue_risk_substitute_ticker_message(_flatten_resp_text(resp).lower()):
        return True
    return False


def main() -> int:
    args = build_parser().parse_args()
    # Default: align pool leverage with DEFAULT_LEVERAGE / --leverage before each order (Varibot runs rely on this).
    args.set_leverage = not bool(getattr(args, "skip_set_leverage", False))
    if args.usd is not None and args.im_target_pct is not None:
        raise SystemExit("Pass at most one of --usd and --im-target-pct.")
    qty_arg = getattr(args, "qty", None)
    if qty_arg is not None:
        if args.usd is not None or args.im_target_pct is not None:
            raise SystemExit("Pass at most one of --qty, --usd, and --im-target-pct.")
    if qty_arg is None and args.usd is None and args.im_target_pct is None:
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

    # Hard cap (no new opens) + optional IM-target sizing uses portfolio snapshot.
    # Note: reduce-only orders are always allowed (they reduce risk / margin usage).
    portfolio_value_for_sizing: Optional[float] = None
    snap_for_guard: Optional[Any] = None
    if not bool(args.reduce_only) and not bool(getattr(args, "skip_im_hard_cap", False)):
        try:
            raw_pf_guard = ep.get_portfolio(compute_margin=True)
            snap_for_guard = parse_portfolio_snapshot(raw_pf_guard)
        except Exception:
            snap_for_guard = None
        if snap_for_guard is None or getattr(snap_for_guard, "im_usage", None) is None:
            raise SystemExit(
                "Cannot enforce IM hard cap: /api/portfolio?compute_margin=true did not provide im_usage."
            )
        cap_pct = float(args.im_target_pct) if args.im_target_pct is not None else float(DEFAULT_IM_TARGET_PCT)
        if float(getattr(snap_for_guard, "im_usage")) >= (cap_pct / 100.0):
            raise SystemExit(
                f"IM hard cap: current im_usage={float(getattr(snap_for_guard, 'im_usage')) * 100.0:.2f}% "
                f">= cap {cap_pct:g}% — blocking new opens."
            )

    use_fixed_qty = qty_arg is not None
    qty_per_order: Optional[float] = float(qty_arg) if use_fixed_qty else None
    usd_per_order: Optional[float] = None

    if not use_fixed_qty:
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
            slots = int(
                getattr(args, "max_ticker_entries", int(DEFAULT_MAX_TICKER_ENTRIES))
                or int(DEFAULT_MAX_TICKER_ENTRIES)
            )
            if slots <= 0:
                raise SystemExit("--max-ticker-entries must be positive.")
            raw_usd = (portfolio_value_for_sizing * float(lev) * (pct / 100.0)) / float(slots)
            step = float(USD_NOTIONAL_ROUND_STEP)
            usd_per_order = math.ceil(raw_usd / step) * step
            print(
                f"Sizing: portfolio_value_usd={portfolio_value_for_sizing} leverage={lev}x im_target_pct={pct}% "
                f"max_ticker_entries={slots} -> ${raw_usd:.2f} raw -> "
                f"${usd_per_order:.0f} per order (ceil to nearest ${step:g})"
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
        "long": long_assets,
        "short": short_assets,
        "assets": flat_assets,
        "job_count": len(jobs),
        "batch_size": int(getattr(args, "batch_size", 0) or 0),
        "batch_sleep_s": float(getattr(args, "batch_sleep_s", 0.0) or 0.0),
        "sleep_between_s": float(args.sleep_between_s),
        "orders": [],
    }
    if use_fixed_qty:
        out["qty_per_order"] = float(qty_per_order or 0.0)
        out["sizing"] = "fixed_qty"
    else:
        out["usd_notional_per_asset"] = float(usd_per_order or 0.0)
        out["sizing"] = ("im_target_pct" if args.usd is None else "fixed_usd")
    if not use_fixed_qty and args.usd is None:
        out["im_target_pct"] = float(args.im_target_pct)  # type: ignore[arg-type]
        out["im_target_mm_notional_scale"] = 1.0  # applied factor on (pv×lev×pct) per order in this script
        out["portfolio_value_usd_for_sizing"] = portfolio_value_for_sizing

    orders_out: List[Dict[str, Any]] = []

    buying_s = ", ".join(long_assets)
    selling_s = ", ".join(short_assets)
    if use_fixed_qty:
        qty_s = format_qty_for_indicative_api(float(qty_per_order or 0.0))
        sizing_disp = f"qty_per_order={qty_s}"
    else:
        usd_s = int(usd_per_order) if float(usd_per_order or 0).is_integer() else float(usd_per_order or 0)
        sizing_disp = f"usd_per_order: {usd_s}"
    print(
        "Buying: "
        f"{buying_s}, "
        "Selling: "
        f"{selling_s}, "
        f"{sizing_disp}, "
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

            # Quote + place order with closeall-style retry (increment slippage on failures).
            attempts: List[Dict[str, Any]] = []
            last_quote: Any = None
            qty_disp = "-"
            value_disp = (
                format_qty_for_indicative_api(float(qty_per_order or 0.0))
                if use_fixed_qty
                else (
                    f"${int(usd_per_order)}" if float(usd_per_order or 0).is_integer() else f"${usd_per_order}"
                )
            )

            if not args.live:
                if use_fixed_qty:
                    quote_id, quote = ep.quote_id_for_order_qty(
                        instrument=instrument,
                        side=side,
                        order_qty=float(qty_per_order or 0.0),
                    )
                else:
                    quote_id, quote = ep.quote_id_for_usd_notional(
                        instrument=instrument,
                        side=side,
                        usd_notional=float(usd_per_order or 0.0),
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
                        if use_fixed_qty:
                            quote_id, quote = ep.quote_id_for_order_qty(
                                instrument=instrument,
                                side=side,
                                order_qty=float(qty_per_order or 0.0),
                            )
                        else:
                            quote_id, quote = ep.quote_id_for_usd_notional(
                                instrument=instrument,
                                side=side,
                                usd_notional=float(usd_per_order or 0.0),
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
                        flat_r = _flatten_resp_text(resp).lower()
                        # Classify from full body: venue sometimes omits standard status fields on HTTP 200.
                        if _venue_risk_substitute_ticker_message(flat_r):
                            item["steps"]["order_response"] = resp
                            item["steps"]["slippage_retry_stopped"] = "oi_skew_or_risk_cap"
                            snippet = _flatten_resp_text(resp)
                            snippet = (snippet[:280] + "…") if len(snippet) > 280 else snippet
                            print(
                                f"STOP retrying {asset}: OI skew / risk cap — "
                                f"higher slippage will not help. {snippet}"
                            )
                            attempts.append(
                                {
                                    "attempt": attempt,
                                    "max_slippage": float(slip),
                                    "ok": False,
                                    "stopped": "oi_skew_or_risk_cap",
                                }
                            )
                            item["error"] = {
                                "type": "OiSkewRiskCapReject",
                                "message": snippet or "Venue risk: OI skew / FDV skew limit",
                            }
                            break
                        if _order_response_rejected(resp):
                            blob = _flatten_resp_text(resp)
                            if attempt >= int(_MAX_LIVE_ATTEMPTS):
                                item["steps"]["order_response"] = resp
                                attempts.append(
                                    {
                                        "attempt": attempt,
                                        "max_slippage": float(slip),
                                        "ok": False,
                                        "error": {"message": (blob[:400] + "…") if len(blob) > 400 else blob},
                                    }
                                )
                                if _looks_like_slippage_reject(blob):
                                    msg = (blob[:500] + "…") if len(blob) > 500 else blob
                                    item["error"] = {
                                        "type": "SlippageExhausted",
                                        "asset": str(asset).strip().upper(),
                                        "side": str(side).strip().lower(),
                                        "message": msg,
                                    }
                                    print(
                                        f"Slippage cap exhausted for {asset} ({side}) after "
                                        f"{int(_MAX_LIVE_ATTEMPTS)} attempts; use alternate ticker."
                                    )
                                    break
                                item["error"] = {
                                    "type": "OrderRejected",
                                    "message": f"order rejected after {attempt} attempts (status={resp.get('status')})",
                                }
                                break
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
                        em = str(e)
                        if _venue_risk_substitute_ticker_message(em.lower()):
                            item["steps"]["slippage_retry_stopped"] = "oi_skew_or_risk_cap"
                            snippet = (em[:280] + "…") if len(em) > 280 else em
                            print(
                                f"STOP retrying {asset}: OI skew / risk cap — "
                                f"higher slippage will not help. {snippet}"
                            )
                            attempts[-1]["stopped"] = "oi_skew_or_risk_cap"
                            item["error"] = {
                                "type": "OiSkewRiskCapReject",
                                "message": snippet or "Venue risk: risk checks failed (e.g. total OI cap)",
                            }
                            break
                        if attempt >= int(_MAX_LIVE_ATTEMPTS):
                            if _looks_like_slippage_reject(em):
                                item["error"] = {
                                    "type": "SlippageExhausted",
                                    "asset": str(asset).strip().upper(),
                                    "side": str(side).strip().lower(),
                                    "message": em[:500],
                                }
                                print(
                                    f"Slippage cap exhausted for {asset} ({side}) after "
                                    f"{int(_MAX_LIVE_ATTEMPTS)} attempts; use alternate ticker."
                                )
                                break
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
            _write_multimarket_last_result(orders_out, skew_extra=None)
            return 0
        # Final summary only (table already streamed during entry).
        n_ok = sum(1 for o in orders_out if not isinstance(o.get("error"), dict))
        print(f"Positions entered: {n_ok} (dry-run)")
        _write_multimarket_last_result(orders_out, skew_extra=None)
        return 0

    if args.print_json:
        print(json.dumps(out, indent=2, default=str))
        _write_multimarket_last_result(orders_out, skew_extra=None)
        return 0

    skew_extra_from_reattempt: List[Dict[str, str]] = []
    slippage_extra_from_reattempt: List[Dict[str, str]] = []

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
        risk_skip_syms: set[str] = set()
        for sym in list(missing2):
            if _annotate_skew_reject_for_missing_asset(
                sym,
                orders_out,
                jobs,
                ep=ep,
                settlement_asset=str(args.settlement_asset),
                funding_interval_s=int(args.funding_interval_s),
            ):
                risk_skip_syms.add(str(sym).strip().upper())
                missing2.discard(str(sym).strip().upper())
        for round_i in range(1, int(_MAX_LIVE_ATTEMPTS) + 1):
            to_retry = missing2 - risk_skip_syms
            if not to_retry:
                break
            slip_round = float(max_slippage) + float(round_i) * float(_SLIPPAGE_RETRY_INCREMENT)
            print(f"Reattempting failed tickers with higher slippage {_fmt_slippage_pct(float(slip_round))}...")

            for sym in sorted(to_retry):
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
                    if use_fixed_qty:
                        quote_id, _quote = ep.quote_id_for_order_qty(
                            instrument=instrument,
                            side=side,
                            order_qty=float(qty_per_order or 0.0),
                        )
                    else:
                        quote_id, _quote = ep.quote_id_for_usd_notional(
                            instrument=instrument,
                            side=side,
                            usd_notional=float(usd_per_order or 0.0),
                        )
                    resp2 = ep.place_order_market(
                        quote_id=quote_id,
                        side=side,
                        max_slippage=float(slip_round),
                        is_reduce_only=bool(args.reduce_only),
                    )
                    flat_r2 = _flatten_resp_text(resp2).lower()
                    if _venue_risk_substitute_ticker_message(flat_r2):
                        risk_skip_syms.add(sym)
                        sn = _flatten_resp_text(resp2)
                        sn = (sn[:240] + "…") if len(sn) > 240 else sn
                        print(
                            f"STOP reattempt {sym}: OI skew / risk cap (no further slippage retries). {sn}"
                        )
                        skew_extra_from_reattempt.append(
                            {"asset": str(sym).strip().upper(), "side": str(side).strip().lower()}
                        )
                        continue
                    if _order_response_rejected(resp2):
                        raise RuntimeError("order rejected")
                except Exception as ex:
                    if _venue_risk_substitute_ticker_message(str(ex).lower()):
                        risk_skip_syms.add(sym)
                        sn = str(ex)
                        sn = (sn[:240] + "…") if len(sn) > 240 else sn
                        print(
                            f"STOP reattempt {sym}: OI skew / risk cap (no further slippage retries). {sn}"
                        )
                        skew_extra_from_reattempt.append(
                            {"asset": str(sym).strip().upper(), "side": str(side).strip().lower()}
                        )
                        continue
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
        if risk_skip_syms:
            print(
                "NOTE: venue OI skew / risk cap (no slippage fix): "
                f"{', '.join(sorted(risk_skip_syms))}"
            )

        # Still missing after all slippage rounds → same substitute path as SlippageExhausted (Varibot reads JSON).
        # Skew-only failures are already in skew_extra_from_reattempt; do not duplicate those here.
        try:
            seen_final = _fetch_positions_underlyings(ep)
            still_missing = set(expected_syms) - set(seen_final)
        except Exception:
            still_missing = set(missing2)
        for sym in sorted(still_missing):
            if sym in risk_skip_syms:
                continue
            side_fm = "buy"
            for j in jobs:
                if str(j.get("asset") or "").strip().upper() == sym:
                    side_fm = str(j.get("side") or "buy").strip().lower()
                    break
            slippage_extra_from_reattempt.append(
                {"asset": str(sym).strip().upper(), "side": str(side_fm).strip().lower()}
            )
        if slippage_extra_from_reattempt:
            print(
                "NOTE: position(s) still missing after slippage reattempts — "
                "will try alternate tickers (Varibot): "
                f"{', '.join(sorted(x['asset'] for x in slippage_extra_from_reattempt))}"
            )
    _write_multimarket_last_result(
        orders_out,
        skew_extra=skew_extra_from_reattempt or None,
        slippage_extra=slippage_extra_from_reattempt or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
