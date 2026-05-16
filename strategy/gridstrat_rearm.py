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
        ord_["status"] = "filled"
        ord_["filled_at_tick"] = t
        ord_["fill_price"] = ord_["level"]
        trade_qty = q if ord_["side"] == "buy" else -q
        new_inventory = inventory + trade_qty

        if ord_.get("paired_from") is not None:
            realized_pnl += spacing * q
            logs.append(f"tick {t}: round-trip +{spacing * q:g} @ {ord_['level']}")

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
        logs.append(f"tick {t}: fill {ord_['side']} @ {ord_['level']}")

        new_level = (
            float(ord_["level"]) - spacing
            if ord_["side"] == "sell"
            else float(ord_["level"]) + spacing
        )
        new_side: Side = "buy" if ord_["side"] == "sell" else "sell"
        conflict = _find_open(orders, new_level, new_side)
        in_range = (
            current_grid_lower - 1e-6 <= new_level <= current_grid_upper + 1e-6
        )
        if not conflict and in_range:
            oid = f"o{next_id}"
            next_id += 1
            orders.append(
                _new_order(oid, new_level, new_side, "rearm", str(ord_["id"]), t)
            )
            logs.append(f"tick {t}: re-arm {new_side} @ {new_level}")
        elif not in_range:
            logs.append(f"tick {t}: re-arm skipped (outside bounds) {new_side} @ {new_level}")

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
