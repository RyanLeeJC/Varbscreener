"""
Paired re-arm + optional grid reset (re-anchor on breach) — logic aligned with ``grid_rearm_sim.html``.

Pure functions + one mutating step; no filesystem / HTTP. Unit-test friendly.

See ``gridbot_new.md`` for the intended product spec; this module implements the simulator’s
deterministic fill / re-arm / reset ordering (fills for one mark step, then breach handling).
"""

from __future__ import annotations

# =============================================================================
# USER / DEFAULT SETTINGS (override via strategy.gridstrat env + GridConfig)
# =============================================================================

EPS_LEVEL: float = 1e-6  # same tolerance as grid_rearm_sim.html findOpen
REARM_POLICY_DEFAULT: str = "paired"  # paired | none (mirror deferred)
GRID_RESET_ON_BREACH_DEFAULT: bool = True  # matches sim ``gridReset: true`` (re-anchor ladder)


import math
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

Side = Literal["buy", "sell"]


@dataclass
class PairedGridNumericConfig:
    """Numeric inputs for derive + step (constructed from env in gridstrat.py)."""

    grid_num: int
    investment_usd: float
    leverage: float
    mark: float  # used for qty_per_grid sizing
    grid_reset: bool = GRID_RESET_ON_BREACH_DEFAULT


def derive_sim_ladder_params(
    *,
    anchor: float,
    lower: float,
    upper: float,
    cfg: PairedGridNumericConfig,
) -> Dict[str, Any]:
    """
    Same geometry as ``grid_rearm_sim.html`` deriveParams (arithmetic spacing, symmetric half).

    ``lower`` / ``upper`` are the active band edges (from explicit env or ±band% around mark).
    ``anchor`` is the ladder centre ``(lower + upper) / 2`` when explicit; for band-only it equals ``mark``.
    """
    a = float(anchor)
    lo = float(lower)
    hi = float(upper)
    if hi <= lo or a <= 0:
        raise ValueError("derive_sim_ladder_params: need upper > lower and positive anchor")
    total_range = hi - lo
    spacing = total_range / float(cfg.grid_num)
    notional_per_grid = (float(cfg.investment_usd) * float(cfg.leverage)) / float(cfg.grid_num)
    qty_per_grid = notional_per_grid / float(cfg.mark)
    half_count = float(cfg.grid_num) / 2.0
    max_i = int(half_count)
    levels: List[Dict[str, Any]] = []
    for i in range(1, max_i + 1):
        levels.append({"level": a - i * spacing, "side": "buy"})
        levels.append({"level": a + i * spacing, "side": "sell"})
    return {
        "anchor": a,
        "grid_lower": lo,
        "grid_upper": hi,
        "grid_num": int(cfg.grid_num),
        "grid_reset": bool(cfg.grid_reset),
        "spacing": spacing,
        "qty_per_grid": float(qty_per_grid),
        "levels": levels,
        "half_count": half_count,
        "max_i": max_i,
    }


def _new_order(
    oid: str,
    level: float,
    side: Side,
    origin: str,
    paired_from: Optional[str],
    tick: int,
) -> Dict[str, Any]:
    return {
        "id": oid,
        "level": float(level),
        "side": side,
        "status": "open",
        "origin": origin,
        "paired_from": paired_from,
        "fill_price": None,
        "filled_at_tick": None,
        "placed_at_tick": int(tick),
        "cancelled_at_tick": None,
    }


def init_paired_state(
    *,
    params: Dict[str, Any],
    tick: int = 0,
) -> Dict[str, Any]:
    """Build initial open orders from ``params['levels']`` (sim initial snapshot)."""
    orders: List[Dict[str, Any]] = []
    for i, o in enumerate(params["levels"]):
        orders.append(
            _new_order(
                f"o{i}",
                float(o["level"]),
                str(o["side"]),
                "initial",
                None,
                tick,
            )
        )
    return {
        "schema_version": 3,
        "rearm_policy": REARM_POLICY_DEFAULT,
        "grid_reset": bool(params.get("grid_reset", GRID_RESET_ON_BREACH_DEFAULT)),
        "grid_num": int(params["grid_num"]),
        "orders": orders,
        "next_id": len(orders),
        "tick": int(tick),
        "current_anchor": float(params["anchor"]),
        "current_grid_lower": float(params["grid_lower"]),
        "current_grid_upper": float(params["grid_upper"]),
        "spacing": float(params["spacing"]),
        "qty_per_grid": float(params["qty_per_grid"]),
        "inventory": 0.0,
        "inventory_cost": 0.0,
        "realized_pnl": 0.0,
        "volume_usd": 0.0,
        "reset_count": 0,
    }


def _find_open(orders: List[Dict[str, Any]], level: float, side: str) -> Optional[Dict[str, Any]]:
    for o in orders:
        if (
            abs(float(o["level"]) - float(level)) < EPS_LEVEL
            and str(o["side"]) == side
            and o["status"] == "open"
        ):
            return o
    return None


def _unrealized_pnl(*, inventory: float, inventory_cost: float, price: float) -> float:
    if abs(inventory) < 1e-12:
        return 0.0
    avg = inventory_cost / inventory
    return float(inventory) * (float(price) - avg)


def _fill_open_order_and_rearm(
    state: Dict[str, Any],
    ord_: Dict[str, Any],
    *,
    tick: int,
    logs: List[str],
) -> None:
    """Mark one open order filled and append paired re-arm (same rules as ``step_mark_pair``)."""
    orders: List[Dict[str, Any]] = state["orders"]
    spacing = float(state["spacing"])
    q = float(state["qty_per_grid"])
    current_grid_lower = float(state["current_grid_lower"])
    current_grid_upper = float(state["current_grid_upper"])
    next_id = int(state["next_id"])

    inventory = float(state["inventory"])
    inventory_cost = float(state["inventory_cost"])
    realized_pnl = float(state["realized_pnl"])
    volume_usd = float(state["volume_usd"])

    ord_["status"] = "filled"
    ord_["filled_at_tick"] = tick
    ord_["fill_price"] = ord_["level"]
    trade_qty = q if ord_["side"] == "buy" else -q
    new_inventory = inventory + trade_qty

    if ord_.get("paired_from") is not None:
        realized_pnl += spacing * q
        logs.append(f"tick {tick}: round-trip +{spacing * q:g} @ {ord_['level']}")

    if inventory == 0:
        inventory_cost = trade_qty * float(ord_["level"])
    elif (trade_qty > 0) == (inventory > 0):
        inventory_cost += trade_qty * float(ord_["level"])
    else:
        if abs(trade_qty) <= abs(inventory):
            close_frac = abs(trade_qty) / abs(inventory)
            inventory_cost -= inventory_cost * close_frac
        else:
            inventory_cost = new_inventory * float(ord_["level"])

    inventory = new_inventory
    if abs(inventory) < 1e-10:
        inventory = 0.0
        inventory_cost = 0.0

    volume_usd += q * float(ord_["level"])
    side_u = str(ord_["side"]).upper()
    fill_px = float(ord_["level"])
    logs.append(f"tick {tick}: {side_u} filled @ {fill_px:g} (qty {q:g} BTC)")

    new_level = (
        float(ord_["level"]) - spacing
        if ord_["side"] == "sell"
        else float(ord_["level"]) + spacing
    )
    new_side: Side = "buy" if ord_["side"] == "sell" else "sell"
    conflict = _find_open(orders, new_level, new_side)
    in_range = current_grid_lower - 1e-6 <= new_level <= current_grid_upper + 1e-6
    if not conflict and in_range:
        oid = f"o{next_id}"
        next_id += 1
        orders.append(_new_order(oid, new_level, new_side, "rearm", str(ord_["id"]), tick))
        logs.append(
            f"tick {tick}: Re-arm: placed {new_side.upper()} @ {new_level:g} "
            f"(paired w/ {fill_px:g})"
        )
    elif not in_range:
        logs.append(
            f"tick {tick}: re-arm skipped (outside bounds) {new_side} @ {new_level:g}"
        )

    state["orders"] = orders
    state["next_id"] = next_id
    state["inventory"] = inventory
    state["inventory_cost"] = inventory_cost
    state["realized_pnl"] = realized_pnl
    state["volume_usd"] = volume_usd


def apply_venue_cleared_limits_as_fills(
    state: Dict[str, Any],
    *,
    pending_keys: Set[Tuple[str, str]],
) -> List[str]:
    """
    Venue sync before mark step: resting limits cleared on exchange but still OPEN in sim
    are treated as filled, with paired re-arm (interval check catches fills between marks).

    Requires a non-empty ``pending_keys`` snapshot. An empty set means "no orders on book yet",
    not "every sim order filled" — callers must skip when ``len(pending_keys) == 0``.
    """
    if not pending_keys:
        return []
    logs: List[str] = []
    t = int(state.get("tick") or 0)
    for ord_ in list(state.get("orders") or []):
        if ord_.get("status") != "open":
            continue
        side = str(ord_.get("side") or "")
        try:
            lv = float(ord_["level"])
            pxk = f"{round(lv, 2):.2f}"
        except (TypeError, ValueError):
            continue
        if (side, pxk) in pending_keys:
            continue
        parent_px = float(ord_["level"])
        _fill_open_order_and_rearm(state, ord_, tick=t, logs=logs)
        logs.append(
            f"tick {t}: venue sync {side.upper()} filled @ {parent_px:g} (cleared from book)"
        )
    return logs


def _ladder_rung_below_mark(
    mark: float,
    *,
    anchor: float,
    spacing: float,
    lo: float,
    hi: float,
) -> Optional[float]:
    """Nearest arithmetic buy rung strictly below ``mark`` (anchor − i×spacing)."""
    if spacing <= 0:
        return None
    i_max = int(math.floor((float(anchor) - float(mark)) / float(spacing) + 1e-9))
    for i in range(i_max, 0, -1):
        level = float(anchor) - i * float(spacing)
        if level < float(mark) - EPS_LEVEL and float(lo) - 1e-6 <= level <= float(hi) + 1e-6:
            return level
    return None


def _ladder_rung_above_mark(
    mark: float,
    *,
    anchor: float,
    spacing: float,
    lo: float,
    hi: float,
) -> Optional[float]:
    """Nearest arithmetic sell rung strictly above ``mark`` (anchor + i×spacing)."""
    if spacing <= 0:
        return None
    i_min = max(1, int(math.ceil((float(mark) - float(anchor)) / float(spacing) - 1e-9)))
    i_cap = max(i_min, int(math.ceil((float(hi) - float(anchor)) / float(spacing)) + 2))
    for i in range(i_min, i_cap + 1):
        level = float(anchor) + i * float(spacing)
        if level > float(mark) + EPS_LEVEL and float(lo) - 1e-6 <= level <= float(hi) + 1e-6:
            return level
        if level > float(hi) + 1e-6:
            break
    return None


def ensure_bracket_rungs_around_mark(
    state: Dict[str, Any],
    *,
    mark: float,
) -> List[str]:
    """
    Guarantee at least one open buy below and one open sell above ``mark`` (next ladder rungs).
    Used after mark steps / venue fills so the book tracks price like the HTML simulator.
    """
    logs: List[str] = []
    orders: List[Dict[str, Any]] = state["orders"]
    spacing = float(state["spacing"])
    anchor = float(state["current_anchor"])
    lo = float(state["current_grid_lower"])
    hi = float(state["current_grid_upper"])
    t = int(state.get("tick") or 0)
    next_id = int(state["next_id"])
    mark_f = float(mark)

    open_buys = [
        o for o in orders if o.get("status") == "open" and str(o.get("side")) == "buy"
    ]
    open_sells = [
        o for o in orders if o.get("status") == "open" and str(o.get("side")) == "sell"
    ]
    has_buy_below = any(float(o["level"]) < mark_f - EPS_LEVEL for o in open_buys)
    has_sell_above = any(float(o["level"]) > mark_f + EPS_LEVEL for o in open_sells)

    if not has_buy_below:
        lv = _ladder_rung_below_mark(mark_f, anchor=anchor, spacing=spacing, lo=lo, hi=hi)
        if lv is not None and _find_open(orders, lv, "buy") is None:
            oid = f"o{next_id}"
            next_id += 1
            orders.append(_new_order(oid, lv, "buy", "bracket", None, t))
            logs.append(f"tick {t}: bracket buy @ {lv:g} (below mark {mark_f:g})")

    if not has_sell_above:
        lv = _ladder_rung_above_mark(mark_f, anchor=anchor, spacing=spacing, lo=lo, hi=hi)
        if lv is not None and _find_open(orders, lv, "sell") is None:
            oid = f"o{next_id}"
            next_id += 1
            orders.append(_new_order(oid, lv, "sell", "bracket", None, t))
            logs.append(f"tick {t}: bracket sell @ {lv:g} (above mark {mark_f:g})")

    state["orders"] = orders
    state["next_id"] = next_id
    return logs


def step_mark_pair_sequential(
    state: Dict[str, Any],
    *,
    p_prev: float,
    p_now: float,
    grid_reset: bool,
) -> List[str]:
    """
    Walk mark in sub-steps (~half spacing) so each crossed rung fills and re-arms in order
    (matches simulator tick-by-tick behaviour when the live interval is long).
    """
    logs: List[str] = []
    prev = float(p_prev)
    target = float(p_now)
    if abs(target - prev) < 1e-12:
        return logs
    spacing = float(state.get("spacing") or 0.0)
    if spacing <= 0:
        return step_mark_pair(state, p_prev=prev, p_now=target, grid_reset=grid_reset)

    step_max = max(float(spacing) * 0.25, float(spacing) * 0.5)
    direction = 1.0 if target >= prev else -1.0
    cursor = prev
    while True:
        remaining = target - cursor
        if abs(remaining) <= step_max + 1e-9:
            logs.extend(
                step_mark_pair(
                    state,
                    p_prev=cursor,
                    p_now=target,
                    grid_reset=bool(grid_reset),
                )
            )
            break
        next_p = cursor + direction * step_max
        logs.extend(
            step_mark_pair(
                state,
                p_prev=cursor,
                p_now=next_p,
                grid_reset=False,
            )
        )
        cursor = next_p
    return logs


def step_mark_pair(
    state: Dict[str, Any],
    *,
    p_prev: float,
    p_now: float,
    grid_reset: bool,
) -> List[str]:
    """
    Apply one mark transition (``p_prev`` → ``p_now``), mutating ``state`` in place.

    Returns human-readable log lines (optional for Varibot logs / tests).
    """
    logs: List[str] = []
    t = int(state["tick"]) + 1
    state["tick"] = t

    orders: List[Dict[str, Any]] = state["orders"]
    spacing = float(state["spacing"])
    q = float(state["qty_per_grid"])
    current_grid_lower = float(state["current_grid_lower"])
    current_grid_upper = float(state["current_grid_upper"])
    current_anchor = float(state["current_anchor"])
    next_id = int(state["next_id"])

    inventory = float(state["inventory"])
    inventory_cost = float(state["inventory_cost"])
    realized_pnl = float(state["realized_pnl"])
    volume_usd = float(state["volume_usd"])

    lo = min(p_prev, p_now)
    hi = max(p_prev, p_now)
    if p_now > p_prev:
        direction = "up"
    elif p_now < p_prev:
        direction = "down"
    else:
        direction = "flat"

    candidates = [
        o
        for o in orders
        if o["status"] == "open"
        and float(o["level"]) >= lo - 1e-6
        and float(o["level"]) <= hi + 1e-6
        and (
            (o["side"] == "sell" and direction == "up")
            or (o["side"] == "buy" and direction == "down")
        )
    ]
    candidates.sort(key=lambda o: abs(float(o["level"]) - p_prev))

    for ord_ in candidates:
        _fill_open_order_and_rearm(state, ord_, tick=t, logs=logs)
        orders = state["orders"]
        next_id = int(state["next_id"])
        inventory = float(state["inventory"])
        inventory_cost = float(state["inventory_cost"])
        realized_pnl = float(state["realized_pnl"])
        volume_usd = float(state["volume_usd"])

    if grid_reset and (
        p_now > current_grid_upper + 1e-6 or p_now < current_grid_lower - 1e-6
    ):
        breach_up = p_now > current_grid_upper
        new_anchor = current_grid_upper if breach_up else current_grid_lower
        state["reset_count"] = int(state["reset_count"]) + 1
        logs.append(f"tick {t}: GRID RESET breach -> anchor {new_anchor}")
        for o in orders:
            if o["status"] == "open":
                o["status"] = "cancelled"
                o["cancelled_at_tick"] = t
        gn = int(state.get("grid_num") or 0)
        if gn <= 0:
            raise ValueError("step_mark_pair: state missing grid_num for breach reset")
        # Same as sim: halfCount = cfg.gridNum / 2 (float); loop i = 1 .. floor(halfCount)
        half_count_f = gn / 2.0
        max_i = int(half_count_f)
        current_anchor = float(new_anchor)
        current_grid_lower = current_anchor - half_count_f * spacing
        current_grid_upper = current_anchor + half_count_f * spacing
        for i in range(1, max_i + 1):
            buy_level = current_anchor - i * spacing
            sell_level = current_anchor + i * spacing
            orders.append(_new_order(f"o{next_id}", buy_level, "buy", "reset", None, t))
            next_id += 1
            orders.append(_new_order(f"o{next_id}", sell_level, "sell", "reset", None, t))
            next_id += 1

    state["orders"] = orders
    state["next_id"] = next_id
    state["current_anchor"] = current_anchor
    state["current_grid_lower"] = current_grid_lower
    state["current_grid_upper"] = current_grid_upper
    state["inventory"] = inventory
    state["inventory_cost"] = inventory_cost
    state["realized_pnl"] = realized_pnl
    state["volume_usd"] = volume_usd
    return logs


def paired_totals(state: Dict[str, Any], *, mark: float) -> Tuple[float, float, float]:
    """(realized, unrealized, total) PnL for current state at ``mark``."""
    r = float(state.get("realized_pnl") or 0.0)
    u = _unrealized_pnl(
        inventory=float(state.get("inventory") or 0.0),
        inventory_cost=float(state.get("inventory_cost") or 0.0),
        price=float(mark),
    )
    return r, u, r + u


def open_rungs_for_meta(state: Dict[str, Any]) -> Tuple[List[float], List[float]]:
    """Active OPEN buy / sell limit prices for ``gridlimits.json`` / UI."""
    buys: List[float] = []
    sells: List[float] = []
    for o in state.get("orders") or []:
        if o.get("status") != "open":
            continue
        lv = float(o["level"])
        if o["side"] == "buy":
            buys.append(lv)
        else:
            sells.append(lv)
    return sorted(buys), sorted(sells)
