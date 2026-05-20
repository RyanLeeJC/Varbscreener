"""
IM-triggered portfolio rebalance: equal long/short target notionals with soft side assignment.

Callable from varibot ``one_cycle`` when venue initial margin usage (IM%) exceeds the trigger threshold.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from variationalbot.domain import PortfolioSnapshot
from variationalbot.vari.endpoints import Instrument, VariEndpoints, format_qty_for_indicative_api

# --- constants (override via VARIBOT_REBALANCE_* env in helpers) ---
IM_TRIGGER: float = 0.50
IM_TARGET: float = 0.20
ROUND_TO: float = 10.0
MIN_ORDER_USD: float = 5.0

ENV_IM_TRIGGER = "VARIBOT_REBALANCE_IM_TRIGGER"
ENV_MM_TRIGGER = "VARIBOT_REBALANCE_MM_TRIGGER"  # deprecated alias for IM trigger
ENV_IM_TARGET = "VARIBOT_REBALANCE_IM_TARGET"
ENV_ROUND_TO = "VARIBOT_REBALANCE_ROUND_TO"
ENV_MIN_ORDER_USD = "VARIBOT_REBALANCE_MIN_ORDER_USD"
ENV_REBALANCE_ORDER_INTERVAL_S = "VARIBOT_REBALANCE_ORDER_INTERVAL_S"
# Each market leg: 2× indicative quote + 1× POST market (see VariEndpoints.quote_id_for_order_qty).
MARKET_LEG_HTTP_CALLS: int = 3


@dataclass(frozen=True)
class LivePosition:
    ticker: str
    side: str  # "long" | "short"
    quantity: float  # always positive
    mark_price: float

    @property
    def notional(self) -> float:
        return float(self.quantity) * float(self.mark_price)

    @property
    def signed_notional(self) -> float:
        return self.notional if self.side == "long" else -self.notional

    @property
    def signed_qty(self) -> float:
        return float(self.quantity) if self.side == "long" else -float(self.quantity)


@dataclass(frozen=True)
class ExecutionLeg:
    ticker: str
    side: str  # buy | sell
    quantity: float  # positive
    order_notional: float


@dataclass
class PlannedRebalanceOrder:
    ticker: str
    current_side: str
    assigned_side: str
    flip: bool
    current_notional: float
    target_notional: float
    delta_qty: float
    order_side: str
    order_quantity: float
    order_notional: float
    legs: Tuple[ExecutionLeg, ...] = field(default_factory=tuple)


@dataclass
class RebalancePlan:
    target_notional: float
    n_eff: int
    dropped_ticker: Optional[str]
    working_tickers: Tuple[str, ...]
    orders: List[PlannedRebalanceOrder]
    total_volume_usd: float


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _vari_rate_limit_settings() -> Tuple[int, float]:
    try:
        rate_max = int(os.getenv("VARI_RATE_LIMIT_MAX", "10") or "10")
    except (TypeError, ValueError):
        rate_max = 10
    try:
        rate_window = float(os.getenv("VARI_RATE_LIMIT_WINDOW_S", "10") or "10")
    except (TypeError, ValueError):
        rate_window = 10.0
    return rate_max, rate_window


def rebalance_sleep_between_market_orders_s() -> float:
    """
    Pause between each ticker's market order so rebalance stays under Vari per-IP limits.

    Default: (window / max) × MARKET_LEG_HTTP_CALLS — e.g. 10 req/10s and 3 calls/leg → ~3.2s.
    Override with VARIBOT_REBALANCE_ORDER_INTERVAL_S. VariClient also enforces limits per request.
    """
    raw = (os.environ.get(ENV_REBALANCE_ORDER_INTERVAL_S) or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    rate_max, rate_window = _vari_rate_limit_settings()
    if rate_max <= 0:
        return 1.0
    per_call = rate_window / float(rate_max)
    return max(1.0, per_call * float(MARKET_LEG_HTTP_CALLS) * 1.08)


def rebalance_constants() -> Tuple[float, float, float, float]:
    im_trig = (os.environ.get(ENV_IM_TRIGGER) or "").strip()
    if not im_trig:
        im_trig = (os.environ.get(ENV_MM_TRIGGER) or "").strip()
    trigger = float(im_trig) if im_trig else float(IM_TRIGGER)
    return (
        trigger,
        _env_float(ENV_IM_TARGET, IM_TARGET),
        _env_float(ENV_ROUND_TO, ROUND_TO),
        _env_float(ENV_MIN_ORDER_USD, MIN_ORDER_USD),
    )


def round_to_nearest(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return round(float(value) / float(step)) * float(step)


def _position_label(p: Dict[str, Any]) -> str:
    inst = p.get("instrument")
    if isinstance(inst, dict):
        u = inst.get("underlying")
        if isinstance(u, str) and u.strip():
            return u.strip().upper()
    for k in ("underlying", "symbol", "instrument_name"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    pi = p.get("position_info")
    if isinstance(pi, dict):
        inst2 = pi.get("instrument")
        if isinstance(inst2, dict):
            u = inst2.get("underlying")
            if isinstance(u, str) and u.strip():
                return u.strip().upper()
    return "UNKNOWN"


def _position_qty_signed(p: Dict[str, Any]) -> Optional[float]:
    for k in ("qty", "quantity", "position_qty", "net_qty", "net_position", "size"):
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
            pass
    return None


def _position_mark(p: Dict[str, Any]) -> Optional[float]:
    pi = p.get("price_info")
    if isinstance(pi, dict) and pi.get("price") is not None:
        try:
            return float(pi["price"])
        except (TypeError, ValueError):
            pass
    for k in ("mark", "mark_price", "markPrice", "mark_px"):
        if k in p and p[k] is not None:
            try:
                return float(p[k])
            except (TypeError, ValueError):
                continue
    return None


def parse_live_positions_from_raw(positions_raw: Any) -> List[LivePosition]:
    """Build live positions from ``GET /api/positions`` payload."""
    out: List[LivePosition] = []
    rows: List[Dict[str, Any]]
    if isinstance(positions_raw, list):
        rows = [p for p in positions_raw if isinstance(p, dict)]
    elif isinstance(positions_raw, dict) and isinstance(positions_raw.get("positions"), list):
        rows = [p for p in positions_raw["positions"] if isinstance(p, dict)]
    else:
        rows = []

    for p in rows:
        sym = _position_label(p)
        if not sym or sym == "UNKNOWN":
            continue
        q_signed = _position_qty_signed(p)
        if q_signed is None or abs(q_signed) <= 1e-12:
            continue
        mark = _position_mark(p)
        if mark is None or mark <= 0 or not (mark == mark):  # NaN guard
            continue
        side = "long" if q_signed > 0 else "short"
        out.append(
            LivePosition(
                ticker=sym,
                side=side,
                quantity=abs(float(q_signed)),
                mark_price=float(mark),
            )
        )
    return out


def _assign_sides(working: List[LivePosition], n_eff: int) -> Dict[str, str]:
    """ticker -> assigned_side (long|short) via signed-notional descending sort."""
    ranked = sorted(working, key=lambda p: (-p.signed_notional, p.ticker))
    half = n_eff // 2
    out: Dict[str, str] = {}
    for p in ranked[:half]:
        out[p.ticker] = "long"
    for p in ranked[half:]:
        out[p.ticker] = "short"
    return out


def _execution_leg_for_order(
    *,
    pos: LivePosition,
    order_side: str,
    order_quantity: float,
    order_notional: float,
) -> ExecutionLeg:
    """One market order per ticker: net delta (target − current), not reduce-only."""
    return ExecutionLeg(
        ticker=pos.ticker,
        side=order_side,
        quantity=float(order_quantity),
        order_notional=float(order_notional),
    )


def plan_portfolio_rebalance(
    *,
    portfolio_value: float,
    max_leverage: float,
    positions: Sequence[LivePosition],
    current_margin_usage: Optional[float] = None,
    current_im_usage: Optional[float] = None,
    margin_trigger: Optional[float] = None,
    im_trigger: Optional[float] = None,
    im_target: Optional[float] = None,
    round_to: Optional[float] = None,
    min_order_usd: Optional[float] = None,
) -> Optional[RebalancePlan]:
    """
    Pure rebalance planner. Returns None when IM usage is below trigger or inputs are invalid.
    """
    if current_im_usage is not None:
        usage = float(current_im_usage)
    elif current_margin_usage is not None:
        usage = float(current_margin_usage)
    else:
        raise TypeError("plan_portfolio_rebalance requires current_im_usage or current_margin_usage")
    trig, tgt, rnd, min_usd = rebalance_constants()
    if im_trigger is not None:
        trig = float(im_trigger)
    elif margin_trigger is not None:
        trig = float(margin_trigger)
    if im_target is not None:
        tgt = float(im_target)
    if round_to is not None:
        rnd = float(round_to)
    if min_order_usd is not None:
        min_usd = float(min_order_usd)

    if float(usage) < float(trig):
        return None
    if float(portfolio_value) <= 0 or float(max_leverage) <= 0:
        return None

    live = [p for p in positions if p.mark_price > 0 and p.quantity > 0]
    n = len(live)
    if n == 0:
        return None

    dropped: Optional[str] = None
    working = list(live)
    if n % 2 == 1:
        drop_pos = min(working, key=lambda p: (p.notional, p.ticker))
        dropped = drop_pos.ticker
        working = [p for p in working if p.ticker != dropped]

    n_eff = len(working)
    if n_eff < 2 or n_eff % 2 != 0:
        return None

    target_notional = round_to_nearest(
        float(portfolio_value) * float(max_leverage) * float(tgt) / float(n_eff),
        rnd,
    )
    if target_notional <= 0:
        return None

    side_by_ticker = _assign_sides(working, n_eff)
    orders: List[PlannedRebalanceOrder] = []
    total_vol = 0.0

    for pos in working:
        assigned = side_by_ticker[pos.ticker]
        sign_tgt = 1.0 if assigned == "long" else -1.0
        sign_cur = 1.0 if pos.side == "long" else -1.0
        target_qty_signed = sign_tgt * float(target_notional) / float(pos.mark_price)
        current_qty_signed = sign_cur * float(pos.quantity)
        delta_qty = target_qty_signed - current_qty_signed
        order_notional = abs(delta_qty) * float(pos.mark_price)

        if order_notional < float(min_usd):
            continue

        order_side = "buy" if delta_qty > 0 else "sell"
        order_quantity = abs(delta_qty)
        flip = assigned != pos.side
        leg = _execution_leg_for_order(
            pos=pos,
            order_side=order_side,
            order_quantity=float(order_quantity),
            order_notional=float(order_notional),
        )
        legs = (leg,)
        leg_vol = leg.order_notional
        total_vol += leg_vol
        orders.append(
            PlannedRebalanceOrder(
                ticker=pos.ticker,
                current_side=pos.side,
                assigned_side=assigned,
                flip=flip,
                current_notional=float(pos.notional),
                target_notional=float(target_notional),
                delta_qty=float(delta_qty),
                order_side=order_side,
                order_quantity=float(order_quantity),
                order_notional=float(order_notional),
                legs=legs,
            )
        )

    return RebalancePlan(
        target_notional=float(target_notional),
        n_eff=n_eff,
        dropped_ticker=dropped,
        working_tickers=tuple(p.ticker for p in working),
        orders=orders,
        total_volume_usd=float(total_vol),
    )


def _order_response_rejected(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    st = str(resp.get("status") or resp.get("order_status") or "").strip().lower()
    return st in ("rejected", "reject", "failed", "failure", "error")


def _extract_order_id(resp: Any) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for k in ("order_id", "orderId", "id", "rfq_id", "rfqId"):
        v = resp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _place_market_leg(
    ep: VariEndpoints,
    *,
    ticker: str,
    side: str,
    quantity: float,
    max_slippage: float,
) -> Tuple[int, Optional[str], Optional[str]]:
    """Returns (rc, order_id, error_message). rc 0 = venue accepted the market order."""
    sym = str(ticker).strip().upper()
    sd = str(side).strip().lower()
    if sd not in ("buy", "sell"):
        return 1, None, "invalid side"
    qty = float(quantity)
    if qty <= 0:
        return 0, None, None
    # Must match grid / indicative path: RWAs use perpetual_rwa_future, not P-*-USDC-3600.
    inst = Instrument.for_underlying(sym)
    try:
        quote_id, _ = ep.quote_id_for_order_qty(instrument=inst, side=sd, order_qty=qty)
        resp = ep.place_order_market(
            quote_id=str(quote_id),
            side=sd,
            max_slippage=float(max_slippage),
            is_reduce_only=False,
        )
    except Exception as e:
        return 1, None, f"{type(e).__name__}: {e}"
    if _order_response_rejected(resp):
        preview = str(resp)[:400] if resp is not None else ""
        return 1, None, f"venue rejected: {preview}"
    return 0, _extract_order_id(resp), None


def rebalance_portfolio(
    *,
    ep: VariEndpoints,
    snap: PortfolioSnapshot,
    positions_raw: Any,
    max_leverage: int,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    max_slippage: float = 0.001,
    mark_fetcher: Optional[Callable[[str], float]] = None,
    varibot_dir: Optional[str] = None,
) -> bool:
    """
    IM interval risk: market orders from a single plan snapshot (no position/IM re-check mid-run).

    Runs whenever IM% ≥ trigger on each call (e.g. every Varibot interval). ``varibot_dir`` is kept for
    API compatibility but no longer used for persistence.
    """
    _ = varibot_dir
    im_usage = snap.im_usage
    pv = snap.portfolio_value_usd
    trig, _, _, _ = rebalance_constants()

    if im_usage is None:
        log("interval risk: skip — im_usage missing from portfolio snapshot")
        return False
    if float(im_usage) < float(trig):
        return False
    if pv is None or float(pv) <= 0:
        log("interval risk: skip — portfolio_value_usd missing or non-positive")
        return False

    positions = parse_live_positions_from_raw(positions_raw)
    if mark_fetcher is not None:
        enriched: List[LivePosition] = []
        for p in positions:
            try:
                mk = float(mark_fetcher(p.ticker))
            except Exception as e:
                log(
                    f"interval risk: skip {p.ticker} — could not fetch mark "
                    f"({type(e).__name__}: {e})"
                )
                continue
            if mk <= 0:
                log(f"interval risk: skip {p.ticker} — stale/zero mark")
                continue
            enriched.append(
                LivePosition(ticker=p.ticker, side=p.side, quantity=p.quantity, mark_price=mk)
            )
        positions = enriched
    elif len(positions) < len(parse_live_positions_from_raw(positions_raw)):
        log("interval risk: some positions skipped — mark_price missing in API payload")

    trig, tgt, rnd, min_usd = rebalance_constants()
    plan = plan_portfolio_rebalance(
        portfolio_value=float(pv),
        max_leverage=float(max_leverage),
        current_im_usage=float(im_usage),
        positions=positions,
    )
    if plan is None:
        if float(im_usage) >= trig:
            log(
                f"interval risk: IM%={float(im_usage) * 100:.2f}% (>= {trig * 100:g}%) "
                "but no rebalance plan (no positions or planner returned None)"
            )
        return False

    if not plan.orders:
        log(
            f"interval risk: IM%={float(im_usage) * 100:.2f}% — all positions within "
            f"${min_usd:g} of target ${plan.target_notional:g}; no orders"
        )
        return False

    log(
        f"interval risk: triggered IM%={float(im_usage) * 100:.2f}% "
        f"target_notional=${plan.target_notional:g} n_eff={plan.n_eff} "
        f"orders={len(plan.orders)} projected_volume=${plan.total_volume_usd:.2f}"
        + (f" dropped={plan.dropped_ticker}" if plan.dropped_ticker else "")
    )

    for o in plan.orders:
        log(
            f"interval risk[{o.ticker}]: {o.current_side}→{o.assigned_side} flip={o.flip} "
            f"current=${o.current_notional:.2f} target=${o.target_notional:.2f} "
            f"delta_qty={o.delta_qty:g} {o.order_side} qty={o.order_quantity:g} "
            f"notional=${o.order_notional:.2f}"
        )

    if dry_run or not live:
        log(
            f"interval risk: dry-run — would place {sum(len(o.legs) for o in plan.orders)} "
            f"market leg(s) on {list(plan.working_tickers)} (pending orders untouched); "
            f"volume≈${plan.total_volume_usd:.2f}"
        )
        return True

    pace_s = rebalance_sleep_between_market_orders_s()
    rate_max, rate_window = _vari_rate_limit_settings()
    log(
        "interval risk: one-shot market orders (pending limits untouched; "
        "no post-fill position or IM re-check). "
        f"Pacing {pace_s:.2f}s between tickers "
        f"(~{MARKET_LEG_HTTP_CALLS} HTTP calls/leg; Vari limit {rate_max}/{rate_window:g}s)."
    )

    legs_ok = 0
    legs_fail = 0
    n_orders = len(plan.orders)
    for idx, o in enumerate(plan.orders):
        leg_results: List[str] = []
        for leg in o.legs:
            rc, oid, err = _place_market_leg(
                ep,
                ticker=leg.ticker,
                side=leg.side,
                quantity=leg.quantity,
                max_slippage=float(max_slippage),
            )
            if rc != 0:
                legs_fail += 1
                leg_results.append(
                    f"{leg.side} qty={format_qty_for_indicative_api(leg.quantity)} "
                    f"FAILED ({err or 'unknown'})"
                )
            else:
                legs_ok += 1
                leg_results.append(
                    f"{leg.side} qty={format_qty_for_indicative_api(leg.quantity)} "
                    f"ok order_id={oid!r}"
                )
        log(
            f"interval risk[{o.ticker}]: {o.current_side}→{o.assigned_side} flip={o.flip} — "
            + "; ".join(leg_results)
        )
        if pace_s > 0 and idx < n_orders - 1:
            time.sleep(pace_s)

    log(
        f"interval risk: complete — market legs ok={legs_ok} failed={legs_fail} "
        f"(planned volume ${plan.total_volume_usd:.2f}); "
        f"will run again next interval if IM% still ≥ {trig * 100:g}%"
    )
    return legs_fail == 0
