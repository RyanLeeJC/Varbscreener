"""
Book-direction hedge: when grid-book net signed notional exceeds a multiple of
portfolio value, offset with equal USD legs on BTC, ETH, and SOL (excluded from book).

Runs at the **end** of each Varibot cycle (after grid limit reconcile / cancellations;
see ``varibot._run_book_hedge_at_cycle_end``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from variationalbot.domain import PortfolioSnapshot
from variationalbot.vari.endpoints import VariEndpoints, format_qty_for_indicative_api

from portfolio_rebalance import (
    HEDGE_TICKERS,
    LivePosition,
    _place_market_leg_with_slippage_retry,
    parse_live_positions_from_raw,
    rebalance_sleep_between_market_orders_s,
)

ENV_ENABLED = "VARIBOT_BOOK_HEDGE_ENABLED"
ENV_PORT_MULT = "VARIBOT_BOOK_HEDGE_PORT_MULT"
ENV_EXIT_PORT_MULT = "VARIBOT_BOOK_HEDGE_EXIT_PORT_MULT"
ENV_ADJUST_USD = "VARIBOT_BOOK_HEDGE_ADJUST_USD"
ENV_SLIPPAGE = "VARIBOT_BOOK_HEDGE_SLIPPAGE"
ENV_SLIPPAGE_SOL = "VARIBOT_BOOK_HEDGE_SLIPPAGE_SOL"
ENV_MIN_ORDER_USD = "VARIBOT_BOOK_HEDGE_MIN_ORDER_USD"

DEFAULT_ENABLED: bool = False
DEFAULT_PORT_MULT: float = 2.0  # enter: open/adjust hedge when |book_net| exceeds this × port
DEFAULT_EXIT_PORT_MULT: float = 2.5  # exit: close hedge only below this × port (hysteresis)
DEFAULT_ADJUST_USD: float = 1000.0
DEFAULT_SLIPPAGE: float = 0.0003  # 0.03% (BTC, ETH)
DEFAULT_SLIPPAGE_SOL: float = 0.0005  # 0.05% — SOL hedge needs wider cap vs rejects
DEFAULT_MIN_ORDER_USD: float = 5.0


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def book_hedge_slippage_for_ticker(ticker: str, *, default_slip: Optional[float] = None) -> float:
    """Per-hedge-leg slippage cap (fraction of notional). SOL defaults to 0.05%."""
    sym = str(ticker).strip().upper()
    if sym == "SOL":
        return _env_float(ENV_SLIPPAGE_SOL, DEFAULT_SLIPPAGE_SOL)
    base = float(default_slip) if default_slip is not None else _env_float(ENV_SLIPPAGE, DEFAULT_SLIPPAGE)
    return base


def book_hedge_constants() -> Tuple[bool, float, float, float, float, float]:
    return (
        _env_bool(ENV_ENABLED, DEFAULT_ENABLED),
        _env_float(ENV_PORT_MULT, DEFAULT_PORT_MULT),
        _env_float(ENV_EXIT_PORT_MULT, DEFAULT_EXIT_PORT_MULT),
        _env_float(ENV_ADJUST_USD, DEFAULT_ADJUST_USD),
        _env_float(ENV_SLIPPAGE, DEFAULT_SLIPPAGE),
        _env_float(ENV_MIN_ORDER_USD, DEFAULT_MIN_ORDER_USD),
    )


def hedge_is_active(hedge_net_usd: float, *, min_order_usd: float = DEFAULT_MIN_ORDER_USD) -> bool:
    """True when BTC/ETH/SOL hedge legs are materially open."""
    return abs(float(hedge_net_usd)) >= float(min_order_usd)


def _signed_notional_map(positions: Sequence[LivePosition]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in positions:
        out[p.ticker] = float(p.signed_notional)
    return out


def compute_book_net(positions: Sequence[LivePosition]) -> float:
    """Signed USD notional: long positive, short negative; excludes BTC/ETH/SOL."""
    hedge_set = set(HEDGE_TICKERS)
    return sum(p.signed_notional for p in positions if p.ticker not in hedge_set)


def compute_hedge_net(positions: Sequence[LivePosition]) -> float:
    """Signed USD notional on BTC + ETH + SOL only."""
    hedge_set = set(HEDGE_TICKERS)
    return sum(p.signed_notional for p in positions if p.ticker in hedge_set)


@dataclass(frozen=True)
class PlannedHedgeLeg:
    ticker: str
    order_side: str  # buy | sell
    order_quantity: float
    order_notional: float
    current_signed_notional: float
    target_signed_notional: float
    is_reduce_only: bool


@dataclass(frozen=True)
class BookHedgePlan:
    portfolio_value_usd: float
    book_net_usd: float
    hedge_net_usd: float
    port_mult: float
    exit_port_mult: float
    enter_trigger_usd: float
    exit_trigger_usd: float
    hedge_active: bool
    hedge_target_usd: float
    action: str  # skip | hold | open_adjust | close_all
    legs: Tuple[PlannedHedgeLeg, ...]


def plan_book_hedge(
    *,
    portfolio_value_usd: float,
    positions: Sequence[LivePosition],
    port_mult: float = DEFAULT_PORT_MULT,
    exit_port_mult: float = DEFAULT_EXIT_PORT_MULT,
    adjust_usd: float = DEFAULT_ADJUST_USD,
    min_order_usd: float = DEFAULT_MIN_ORDER_USD,
    mark_by_ticker: Optional[dict[str, float]] = None,
) -> Optional[BookHedgePlan]:
    """
    Plan BTC/ETH/SOL hedge legs.

    - Book net = sum signed notional on all tickers except BTC, ETH, SOL.
    - **Enter** when |book_net| > port_mult × portfolio_value (default 2×): target hedge = −book_net,
      split equally across BTC, ETH, SOL (1× book net total).
    - **Exit** (close hedge) only when |book_net| ≤ exit_port_mult × port (default 2.5×) while
      hedge is active — hysteresis vs enter so book flirting with the enter threshold does not flip on/off.
    - Adjust only if |hedge_target − hedge_net| > adjust_usd.
    """
    pv = float(portfolio_value_usd)
    if pv <= 0 or not (pv == pv):
        return None

    book_net = compute_book_net(positions)
    hedge_net = compute_hedge_net(positions)
    enter_trigger = float(port_mult) * pv
    exit_trigger = float(exit_port_mult) * pv
    active = hedge_is_active(hedge_net, min_order_usd=min_order_usd)
    notionals = _signed_notional_map(positions)
    marks: dict[str, float] = {}
    if mark_by_ticker:
        for k, v in mark_by_ticker.items():
            if v is not None and float(v) > 0:
                marks[str(k).strip().upper()] = float(v)
    for p in positions:
        marks[p.ticker] = float(p.mark_price)

    def _leg_for_ticker(ticker: str, target_n: float) -> Optional[PlannedHedgeLeg]:
        sym = ticker.upper()
        cur = float(notionals.get(sym, 0.0))
        delta = float(target_n) - cur
        if abs(delta) < float(min_order_usd):
            return None
        mk = marks.get(sym)
        if mk is None or mk <= 0:
            return None
        qty = abs(delta) / mk
        if qty <= 0:
            return None
        side = "buy" if delta > 0 else "sell"
        reduce_only = (cur > 0 and delta < 0) or (cur < 0 and delta > 0)
        return PlannedHedgeLeg(
            ticker=sym,
            order_side=side,
            order_quantity=float(qty),
            order_notional=abs(delta),
            current_signed_notional=cur,
            target_signed_notional=float(target_n),
            is_reduce_only=bool(reduce_only),
        )

    if not active and abs(book_net) <= enter_trigger:
        return BookHedgePlan(
            portfolio_value_usd=pv,
            book_net_usd=book_net,
            hedge_net_usd=hedge_net,
            port_mult=float(port_mult),
            exit_port_mult=float(exit_port_mult),
            enter_trigger_usd=enter_trigger,
            exit_trigger_usd=exit_trigger,
            hedge_active=active,
            hedge_target_usd=0.0,
            action="skip",
            legs=(),
        )

    if active and abs(book_net) <= exit_trigger:
        legs: List[PlannedHedgeLeg] = []
        for sym in HEDGE_TICKERS:
            cur = float(notionals.get(sym, 0.0))
            if abs(cur) < float(min_order_usd):
                continue
            leg = _leg_for_ticker(sym, 0.0)
            if leg is not None:
                legs.append(leg)
        return BookHedgePlan(
            portfolio_value_usd=pv,
            book_net_usd=book_net,
            hedge_net_usd=hedge_net,
            port_mult=float(port_mult),
            exit_port_mult=float(exit_port_mult),
            enter_trigger_usd=enter_trigger,
            exit_trigger_usd=exit_trigger,
            hedge_active=active,
            hedge_target_usd=0.0,
            action="close_all" if legs else "skip",
            legs=tuple(legs),
        )

    if active and abs(book_net) <= enter_trigger:
        # Hysteresis band: hedge stays on until |book_net| drops below exit_trigger.
        return BookHedgePlan(
            portfolio_value_usd=pv,
            book_net_usd=book_net,
            hedge_net_usd=hedge_net,
            port_mult=float(port_mult),
            exit_port_mult=float(exit_port_mult),
            enter_trigger_usd=enter_trigger,
            exit_trigger_usd=exit_trigger,
            hedge_active=active,
            hedge_target_usd=float(hedge_net),
            action="hold",
            legs=(),
        )

    hedge_target = -float(book_net)
    if abs(hedge_target - hedge_net) <= float(adjust_usd):
        return BookHedgePlan(
            portfolio_value_usd=pv,
            book_net_usd=book_net,
            hedge_net_usd=hedge_net,
            port_mult=float(port_mult),
            exit_port_mult=float(exit_port_mult),
            enter_trigger_usd=enter_trigger,
            exit_trigger_usd=exit_trigger,
            hedge_active=active,
            hedge_target_usd=hedge_target,
            action="hold",
            legs=(),
        )

    per_leg = hedge_target / float(len(HEDGE_TICKERS))
    legs2: List[PlannedHedgeLeg] = []
    for sym in HEDGE_TICKERS:
        leg = _leg_for_ticker(sym, per_leg)
        if leg is not None:
            legs2.append(leg)

    return BookHedgePlan(
        portfolio_value_usd=pv,
        book_net_usd=book_net,
        hedge_net_usd=hedge_net,
        port_mult=float(port_mult),
        exit_port_mult=float(exit_port_mult),
        enter_trigger_usd=enter_trigger,
        exit_trigger_usd=exit_trigger,
        hedge_active=active,
        hedge_target_usd=hedge_target,
        action="open_adjust",
        legs=tuple(legs2),
    )


def maybe_book_hedge(
    *,
    ep: VariEndpoints,
    snap: PortfolioSnapshot,
    positions_raw: object,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    mark_fetcher: Optional[Callable[[str], float]] = None,
) -> bool:
    """Evaluate book net vs portfolio and place BTC/ETH/SOL hedge market orders if needed."""
    enabled, port_mult, exit_port_mult, adjust_usd, slip, min_usd = book_hedge_constants()
    if not enabled:
        return False

    pv = snap.portfolio_value_usd
    if pv is None or float(pv) <= 0:
        log("book_hedge: skip — portfolio_value_usd missing or non-positive")
        return False

    positions = parse_live_positions_from_raw(positions_raw)
    if mark_fetcher is not None:
        enriched: List[LivePosition] = []
        for p in positions:
            try:
                mk = float(mark_fetcher(p.ticker))
            except Exception as e:
                log(
                    f"book_hedge: skip {p.ticker} — mark fetch failed "
                    f"({type(e).__name__}: {e})"
                )
                continue
            if mk <= 0:
                log(f"book_hedge: skip {p.ticker} — invalid mark")
                continue
            enriched.append(
                LivePosition(
                    ticker=p.ticker,
                    side=p.side,
                    quantity=p.quantity,
                    mark_price=mk,
                    upnl_usd=p.upnl_usd,
                )
            )
        positions = enriched

    marks_extra: dict[str, float] = {p.ticker: float(p.mark_price) for p in positions}
    if mark_fetcher is not None:
        for sym in HEDGE_TICKERS:
            if sym in marks_extra and marks_extra[sym] > 0:
                continue
            try:
                mk = float(mark_fetcher(sym))
                if mk > 0:
                    marks_extra[sym] = mk
            except Exception:
                pass

    plan = plan_book_hedge(
        portfolio_value_usd=float(pv),
        positions=positions,
        port_mult=port_mult,
        exit_port_mult=exit_port_mult,
        adjust_usd=adjust_usd,
        min_order_usd=min_usd,
        mark_by_ticker=marks_extra,
    )
    if plan is None:
        return False

    log(
        f"book_hedge: port=${plan.portfolio_value_usd:.2f} "
        f"book_net=${plan.book_net_usd:+.2f} hedge_net=${plan.hedge_net_usd:+.2f} "
        f"enter=${plan.enter_trigger_usd:.2f} ({plan.port_mult:g}×) "
        f"exit=${plan.exit_trigger_usd:.2f} ({plan.exit_port_mult:g}×) "
        f"hedge_active={plan.hedge_active} "
        f"target_hedge=${plan.hedge_target_usd:+.2f} action={plan.action}"
    )

    if plan.action in ("skip", "hold") or not plan.legs:
        if plan.action == "hold":
            if plan.hedge_active and abs(plan.book_net_usd) <= plan.enter_trigger_usd:
                log(
                    f"book_hedge: hold — hysteresis band "
                    f"(|book|=${abs(plan.book_net_usd):.2f} between "
                    f"exit ${plan.exit_trigger_usd:.2f} and enter ${plan.enter_trigger_usd:.2f})"
                )
            else:
                log(
                    f"book_hedge: hold — |target−hedge|="
                    f"${abs(plan.hedge_target_usd - plan.hedge_net_usd):.2f} "
                    f"≤ ${adjust_usd:g}"
                )
        return plan.action != "skip"

    total_vol = sum(l.order_notional for l in plan.legs)
    log(
        f"book_hedge: {plan.action} — {len(plan.legs)} leg(s) "
        f"slippage BTC/ETH={slip * 100:.4f}% SOL={book_hedge_slippage_for_ticker('SOL', default_slip=slip) * 100:.4f}% "
        f"projected_volume≈${total_vol:.2f}"
    )
    for leg in plan.legs:
        leg_slip = book_hedge_slippage_for_ticker(leg.ticker, default_slip=slip)
        log(
            f"book_hedge[{leg.ticker}]: "
            f"cur=${leg.current_signed_notional:+.2f} → "
            f"target=${leg.target_signed_notional:+.2f} "
            f"{leg.order_side} qty={format_qty_for_indicative_api(leg.order_quantity)} "
            f"notional≈${leg.order_notional:.2f} slip={leg_slip * 100:.4f}%"
            + (" reduce-only" if leg.is_reduce_only else "")
        )

    if dry_run or not live:
        log(
            f"book_hedge: dry-run — would place {len(plan.legs)} market order(s); "
            f"volume≈${total_vol:.2f}"
        )
        return True

    pace_s = rebalance_sleep_between_market_orders_s()
    log(f"book_hedge: live market orders; pacing {pace_s:.2f}s between legs")
    ok, fail = 0, 0
    n = len(plan.legs)
    for idx, leg in enumerate(plan.legs):
        leg_slip = book_hedge_slippage_for_ticker(leg.ticker, default_slip=slip)
        rc, oid, err = _place_market_leg_with_slippage_retry(
            ep,
            ticker=leg.ticker,
            side=leg.order_side,
            quantity=leg.order_quantity,
            max_slippage=float(leg_slip),
            is_reduce_only=leg.is_reduce_only,
            log=log,
        )
        if rc != 0:
            fail += 1
            log(
                f"book_hedge[{leg.ticker}]: {leg.order_side} "
                f"qty={format_qty_for_indicative_api(leg.order_quantity)} "
                f"FAILED ({err or 'unknown'})"
            )
        else:
            ok += 1
            log(
                f"book_hedge[{leg.ticker}]: {leg.order_side} "
                f"qty={format_qty_for_indicative_api(leg.order_quantity)} "
                f"ok order_id={oid!r}"
            )
        if pace_s > 0 and idx < n - 1:
            time.sleep(pace_s)

    log(f"book_hedge: complete — ok={ok} failed={fail}")
    return fail == 0


def plan_flatten_hedge_legs(
    positions: Sequence[LivePosition],
    *,
    min_order_usd: float = DEFAULT_MIN_ORDER_USD,
    mark_by_ticker: Optional[dict[str, float]] = None,
) -> Tuple[PlannedHedgeLeg, ...]:
    """Reduce-only market legs to close all material BTC/ETH/SOL hedge positions (target 0)."""
    notionals = _signed_notional_map(positions)
    marks: dict[str, float] = {}
    if mark_by_ticker:
        for k, v in mark_by_ticker.items():
            if v is not None and float(v) > 0:
                marks[str(k).strip().upper()] = float(v)
    for p in positions:
        if float(p.mark_price) > 0:
            marks[p.ticker] = float(p.mark_price)

    legs: List[PlannedHedgeLeg] = []
    for sym in HEDGE_TICKERS:
        cur = float(notionals.get(sym, 0.0))
        if abs(cur) < float(min_order_usd):
            continue
        delta = -cur
        mk = marks.get(sym)
        if mk is None or mk <= 0:
            continue
        qty = abs(delta) / mk
        if qty <= 0:
            continue
        side = "buy" if delta > 0 else "sell"
        reduce_only = (cur > 0 and delta < 0) or (cur < 0 and delta > 0)
        legs.append(
            PlannedHedgeLeg(
                ticker=sym,
                order_side=side,
                order_quantity=float(qty),
                order_notional=abs(delta),
                current_signed_notional=cur,
                target_signed_notional=0.0,
                is_reduce_only=bool(reduce_only),
            )
        )
    return tuple(legs)


def flatten_hedge_legs(
    *,
    ep: VariEndpoints,
    positions_raw: object,
    live: bool,
    dry_run: bool,
    log: Callable[[str], None],
    mark_fetcher: Optional[Callable[[str], float]] = None,
) -> bool:
    """Close BTC/ETH/SOL hedge legs to flat (ignores VARIBOT_BOOK_HEDGE_ENABLED)."""
    _, _, _, _, slip, min_usd = book_hedge_constants()
    positions = parse_live_positions_from_raw(positions_raw)
    if mark_fetcher is not None:
        enriched: List[LivePosition] = []
        for p in positions:
            try:
                mk = float(mark_fetcher(p.ticker))
            except Exception as e:
                log(
                    f"flatten_hedge: skip {p.ticker} — mark fetch failed "
                    f"({type(e).__name__}: {e})"
                )
                continue
            if mk <= 0:
                log(f"flatten_hedge: skip {p.ticker} — invalid mark")
                continue
            enriched.append(
                LivePosition(
                    ticker=p.ticker,
                    side=p.side,
                    quantity=p.quantity,
                    mark_price=mk,
                    upnl_usd=p.upnl_usd,
                )
            )
        positions = enriched

    marks_extra: dict[str, float] = {p.ticker: float(p.mark_price) for p in positions}
    if mark_fetcher is not None:
        for sym in HEDGE_TICKERS:
            if sym in marks_extra and marks_extra[sym] > 0:
                continue
            try:
                mk = float(mark_fetcher(sym))
                if mk > 0:
                    marks_extra[sym] = mk
            except Exception:
                pass

    hedge_net = compute_hedge_net(positions)
    if not hedge_is_active(hedge_net, min_order_usd=min_usd):
        log(f"flatten_hedge: no material hedge legs (hedge_net=${hedge_net:+.2f})")
        return False

    legs = plan_flatten_hedge_legs(
        positions, min_order_usd=min_usd, mark_by_ticker=marks_extra
    )
    if not legs:
        log("flatten_hedge: hedge active but no closable legs (missing marks?)")
        return False

    log(
        f"flatten_hedge: hedge_net=${hedge_net:+.2f} — closing {len(legs)} leg(s) "
        f"slippage BTC/ETH={slip * 100:.4f}% "
        f"SOL={book_hedge_slippage_for_ticker('SOL', default_slip=slip) * 100:.4f}%"
    )
    for leg in legs:
        leg_slip = book_hedge_slippage_for_ticker(leg.ticker, default_slip=slip)
        log(
            f"flatten_hedge[{leg.ticker}]: "
            f"cur=${leg.current_signed_notional:+.2f} → flat "
            f"{leg.order_side} qty={format_qty_for_indicative_api(leg.order_quantity)} "
            f"notional≈${leg.order_notional:.2f} slip={leg_slip * 100:.4f}%"
            + (" reduce-only" if leg.is_reduce_only else "")
        )

    if dry_run or not live:
        log(f"flatten_hedge: dry-run — would place {len(legs)} market order(s)")
        return True

    pace_s = rebalance_sleep_between_market_orders_s()
    ok, fail = 0, 0
    n = len(legs)
    for idx, leg in enumerate(legs):
        leg_slip = book_hedge_slippage_for_ticker(leg.ticker, default_slip=slip)
        rc, oid, err = _place_market_leg_with_slippage_retry(
            ep,
            ticker=leg.ticker,
            side=leg.order_side,
            quantity=leg.order_quantity,
            max_slippage=float(leg_slip),
            is_reduce_only=leg.is_reduce_only,
            log=log,
        )
        if rc != 0:
            fail += 1
            log(f"flatten_hedge[{leg.ticker}]: FAILED ({err or 'unknown'})")
        else:
            ok += 1
            log(f"flatten_hedge[{leg.ticker}]: ok order_id={oid!r}")
        if pace_s > 0 and idx < n - 1:
            time.sleep(pace_s)

    log(f"flatten_hedge: complete — ok={ok} failed={fail}")
    return fail == 0
