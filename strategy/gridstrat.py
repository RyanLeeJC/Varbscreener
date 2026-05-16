"""
Vari grid strategy (``gridbot.md`` / ``gridbot_new.md``).

**Default (``GRID_EXECUTION`` unset or ``paired_limit``):** sim-aligned **paired limit** engine
(``strategy/gridstrat_rearm.py``): arithmetic ladder, one-for-one re-arm after each crossed rung,
optional breach **re-anchor** (same as ``grid_rearm_sim.html`` with ``gridReset: true``). Desired venue
limits are written to ``Varibot/gridlimits.json`` via ``sync_gridlimits_json`` from Varibot; no
``grid_market_events`` in this mode.

**Legacy (``GRID_EXECUTION=legacy_market``):** discrete mark snapshots → ``grid_market_events`` market
legs (interior ladder + buy restoration gate). See ``advance_grid_state``.

Configure via environment variables (override file-level ``DEFAULT_*`` constants in this module):

  GRID_EXECUTION=paired_limit   # paired_limit (default) | legacy_market
  GRID_REARM_ON_BREACH=reanchor # reanchor | slide (same as reanchor) | halt — paired mode only
  GRID_ASSET=BTC
  GRID_LOWER=86000
  GRID_UPPER=89000
  GRID_BAND_PCT=0.5        # symmetric ±% around mark when either GRID_LOWER or GRID_UPPER is unset (see DEFAULT_GRID_BAND_PCT)
  GRID_NUM=30              # equal steps across [lower, upper] for paired spacing = (upper-lower)/GRID_NUM
  GRID_TYPE=arithmetic     # or geometric (paired ladder uses arithmetic spacing only)
  GRID_INVESTMENT_USD=300
  GRID_LEVERAGE=25
  GRID_MARKET_SIZING=qty   # legacy market mode: qty vs usd sizing for multimarket events
  GRID_MARK=               # optional override for mark (else from strategy listing JSON)
  GRIDSTRAT_STATE_PATH=    # optional; default Varibot/gridstrat_state.json under repo root
  GRIDSTRAT_RESET=1        # one-shot: re-init engine state on next pick_tickers

Optional compatibility: strategy key ``invert_extreme`` still resolves to this module on the GridBot
branch.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

from strategy.gridstrat_rearm import (
    PairedGridNumericConfig,
    apply_venue_cleared_limits_as_fills,
    derive_sim_ladder_params,
    ensure_bracket_rungs_around_mark,
    init_paired_state,
    open_rungs_for_meta,
    step_mark_pair_sequential,
)

try:
    from variationalbot.vari.endpoints import format_qty_for_grid_limit
except ImportError:

    def format_qty_for_grid_limit(qty: float) -> str:  # type: ignore[misc]
        qf = float(qty)
        if not math.isfinite(qf) or qf == 0.0:
            return "0"
        return format(qf, ".4g")
from strategy.gridstrat_state import load_state, save_state

STRATEGY_NAME: str = "vari_grid"

# Default ``paired_limit`` matches ``grid_rearm_sim.html``; set ``legacy_market`` for mark-only events.
GRID_EXECUTION_DEFAULT: str = "paired_limit"
# Paired mode: ``reanchor`` / ``slide`` = reset ladder on band breach (sim default); ``halt`` = no reset.
GRID_REARM_ON_BREACH_DEFAULT: str = "reanchor"

# -----------------------------------------------------------------------------
# Edit these defaults here; ``GRID_*`` environment variables override when set.
# (Same idea as ``DEFAULT_GRID_BAND_PCT`` — leave bounds NaN to use ±band around mark.)
# -----------------------------------------------------------------------------
DEFAULT_GRID_ASSET: str = "BTC"
DEFAULT_GRID_INVESTMENT_USD: float = 10.0
DEFAULT_GRID_LEVERAGE: float = 50.0
DEFAULT_GRID_NUM: int = 10  # paired mode → GRID_NUM/2 buys + GRID_NUM/2 sells
DEFAULT_GRID_MARKET_SIZING: str = "qty"  # legacy market mode only: "qty" | "usd"
DEFAULT_GRID_BAND_PCT: float = 0.2 # Symmetric bracket around mark when explicit GRID_LOWER+GRID_UPPER are not both set (see resolve_grid_bounds).
DEFAULT_GRID_LOWER: float = float("nan")  # set both bounds finite to pin explicit bracket
DEFAULT_GRID_UPPER: float = float("nan")
DEFAULT_GRID_TYPE: str = "arithmetic"  # "arithmetic" | "geometric" (paired uses arithmetic spacing)


# multimarketorder.py imports this as sizing divisor fallback when strategy import succeeds.
DEFAULT_MAX_TICKER_ENTRIES: int = 1

TRADE_THESIS: str = (
    "Default: paired arithmetic limit grid (sim-aligned) with optional breach re-anchor; "
    "Varibot mirrors open rungs to gridlimits.json. "
    "Legacy mode: interior ladder with market events and buy restoration after first-sell anchor cross."
)

WRITE_STRATEGY_OUTPUT_TXT: bool = True


def _repo_root_from_here() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _default_state_path() -> str:
    raw = (os.environ.get("GRIDSTRAT_STATE_PATH") or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(_repo_root_from_here(), "Varibot", "gridstrat_state.json")


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def grid_execution_mode() -> str:
    raw = (os.environ.get("GRID_EXECUTION") or "").strip().lower()
    if raw in ("legacy_market", "market", "legacy"):
        return "legacy_market"
    if raw in ("paired_limit", "paired", "limit"):
        return "paired_limit"
    d = (GRID_EXECUTION_DEFAULT or "paired_limit").strip().lower()
    return "legacy_market" if d in ("legacy_market", "market", "legacy") else "paired_limit"


def breach_reanchors_on_breach() -> bool:
    raw = (os.environ.get("GRID_REARM_ON_BREACH") or "").strip().lower()
    if not raw:
        raw = (GRID_REARM_ON_BREACH_DEFAULT or "reanchor").strip().lower()
    if raw in ("halt", "stop", "freeze"):
        return False
    return True


def env_grid_band_pct() -> float:
    raw = (os.environ.get("GRID_BAND_PCT") or "").strip()
    if not raw:
        return float(DEFAULT_GRID_BAND_PCT)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_GRID_BAND_PCT)


def resolve_grid_bounds(
    *,
    mark: float,
    cfg: GridConfig,
    state: Dict[str, Any],
) -> Tuple[float, float, bool, Optional[float], Dict[str, float], List[str]]:
    """
    Resolve effective lower/upper for the ladder.

    - If both GRID_LOWER and GRID_UPPER are finite and upper > lower: use them (explicit bracket).
    - Otherwise: symmetric band around **mark** using GRID_BAND_PCT (default DEFAULT_GRID_BAND_PCT),
      pinned in state (pinned_lower, pinned_upper, pinned_band_pct) so the bracket does not
      re-center each cycle until GRID_BAND_PCT changes, GRIDSTRAT_RESET, or you switch to explicit bounds.

    Returns:
        (eff_lower, eff_upper, explicit_bounds, band_pct_used_or_None, pin_updates, pin_delete_keys)
    """
    lo_c, hi_c = float(cfg.lower), float(cfg.upper)
    explicit = (not math.isnan(lo_c)) and (not math.isnan(hi_c)) and hi_c > lo_c
    if explicit:
        return lo_c, hi_c, True, None, {}, ["pinned_lower", "pinned_upper", "pinned_band_pct"]

    band = float(env_grid_band_pct())
    reset = _truthy_env("GRIDSTRAT_RESET")

    pin_lo = state.get("pinned_lower")
    pin_hi = state.get("pinned_upper")
    pin_pct = state.get("pinned_band_pct")
    try:
        pin_lo_f = float(pin_lo) if pin_lo is not None else float("nan")
        pin_hi_f = float(pin_hi) if pin_hi is not None else float("nan")
        pin_pct_f = float(pin_pct) if pin_pct is not None else float("nan")
    except (TypeError, ValueError):
        pin_lo_f = pin_hi_f = pin_pct_f = float("nan")

    pct_match = not math.isnan(pin_pct_f) and abs(pin_pct_f - band) <= 1e-9
    use_pins = (
        not reset
        and pct_match
        and not math.isnan(pin_lo_f)
        and not math.isnan(pin_hi_f)
        and pin_hi_f > pin_lo_f
    )
    if use_pins:
        return pin_lo_f, pin_hi_f, False, band, {}, []

    f = band / 100.0
    nl = float(mark) * (1.0 - f)
    nu = float(mark) * (1.0 + f)
    return (
        nl,
        nu,
        False,
        band,
        {"pinned_lower": nl, "pinned_upper": nu, "pinned_band_pct": band},
        [],
    )


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


GridType = Literal["arithmetic", "geometric"]


def grid_market_sizing_mode() -> str:
    """``qty`` (default) or ``usd`` — how grid market events size multimarket (see Varibot)."""
    v = (os.environ.get("GRID_MARKET_SIZING") or "").strip().lower()
    if v in ("qty", "usd"):
        return "usd" if v == "usd" else "qty"
    d = (DEFAULT_GRID_MARKET_SIZING or "qty").strip().lower()
    return "usd" if d == "usd" else "qty"


def _grid_open_leg_fields(*, usd_leg: float, mark: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {"usd": float(usd_leg)}
    if grid_market_sizing_mode() != "usd" and float(mark) > 0:
        out["qty"] = float(usd_leg) / float(mark)
    return out


@dataclass
class GridConfig:
    asset: str
    lower: float
    upper: float
    n_grids: int
    grid_type: GridType
    investment_usd: float
    leverage: float
    mark_override: Optional[float]

    @classmethod
    def from_env(cls) -> "GridConfig":
        asset = (os.environ.get("GRID_ASSET") or DEFAULT_GRID_ASSET).strip().upper()
        raw_lo = (os.environ.get("GRID_LOWER") or "").strip()
        raw_hi = (os.environ.get("GRID_UPPER") or "").strip()
        lower = float(raw_lo) if raw_lo else float(DEFAULT_GRID_LOWER)
        upper = float(raw_hi) if raw_hi else float(DEFAULT_GRID_UPPER)
        raw_n = (os.environ.get("GRID_NUM") or "").strip()
        n_grids = int(raw_n) if raw_n else int(DEFAULT_GRID_NUM)
        gt = (os.environ.get("GRID_TYPE") or DEFAULT_GRID_TYPE).strip().lower()
        grid_type: GridType = "geometric" if gt == "geometric" else "arithmetic"
        raw_inv = (os.environ.get("GRID_INVESTMENT_USD") or "").strip()
        inv = float(raw_inv) if raw_inv else float(DEFAULT_GRID_INVESTMENT_USD)
        raw_lev = (os.environ.get("GRID_LEVERAGE") or "").strip()
        lev = float(raw_lev) if raw_lev else float(DEFAULT_GRID_LEVERAGE)
        mo = (os.environ.get("GRID_MARK") or "").strip()
        mark_override = float(mo) if mo else None
        return cls(
            asset=asset,
            lower=lower,
            upper=upper,
            n_grids=n_grids,
            grid_type=grid_type,
            investment_usd=inv,
            leverage=lev,
            mark_override=mark_override,
        )

    def validate(self) -> Optional[str]:
        explicit = (not math.isnan(self.lower)) and (not math.isnan(self.upper))
        if explicit:
            if self.upper <= self.lower:
                return "GRID_UPPER must be > GRID_LOWER."
        else:
            pct = env_grid_band_pct()
            if pct <= 0:
                return "GRID_BAND_PCT must be > 0 when GRID_LOWER and GRID_UPPER are not both set."
        if self.n_grids < 2:
            return "GRID_NUM must be >= 2 (even count of rungs recommended in UI; we accept any integer >=2)."
        if math.isnan(self.investment_usd) or self.investment_usd <= 0:
            return "Set positive GRID_INVESTMENT_USD."
        if math.isnan(self.leverage) or self.leverage <= 0:
            return "Set positive GRID_LEVERAGE."
        return None


def _mark_from_listing(listing_json: str, asset: str) -> Optional[float]:
    try:
        with open(str(listing_json), "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    listings = doc.get("listings") if isinstance(doc, dict) else None
    if not isinstance(listings, list):
        return None
    want = str(asset).strip().upper()
    for row in listings:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("vari_ticker") or "").strip().upper()
        if sym != want:
            continue
        mp = _as_float(row.get("mark_price"))
        if mp is not None and mp > 0:
            return float(mp)
    return None


def build_price_ladder(*, lower: float, upper: float, n_grids: int, grid_type: GridType) -> List[float]:
    """
    Return sorted unique rung prices strictly between lower and upper (endpoints excluded),
    with n_grids interior points when possible.

    Arithmetic: equal absolute step.
    Geometric: equal ratio between consecutive positive prices (requires lower>0).
    """
    if upper <= lower or n_grids < 1:
        return []
    if grid_type == "arithmetic":
        step = (upper - lower) / float(n_grids + 1)
        return [lower + step * (i + 1) for i in range(n_grids)]
    # geometric
    if lower <= 0:
        return []
    ratio = (upper / lower) ** (1.0 / float(n_grids + 1))
    out: List[float] = []
    p = lower
    for _ in range(n_grids):
        p *= ratio
        if p >= upper:
            break
        out.append(float(p))
    return out


def split_buy_sell(levels: Sequence[float], mark: float) -> Tuple[List[float], List[float]]:
    buys = sorted({float(x) for x in levels if float(x) < mark})
    sells = sorted({float(x) for x in levels if float(x) > mark})
    return buys, sells


def per_rung_usd_notional(*, investment_usd: float, leverage: float, n_rungs: int) -> float:
    denom = max(1, int(n_rungs))
    return float(investment_usd) * float(leverage) / float(denom)


def _template_fingerprint(cfg: GridConfig, levels: Sequence[float]) -> str:
    return json.dumps(
        {
            "a": cfg.asset,
            "lo": cfg.lower,
            "hi": cfg.upper,
            "n": cfg.n_grids,
            "t": cfg.grid_type,
            "levels": [round(x, 8) for x in levels],
        },
        sort_keys=True,
    )


def _paired_state_fingerprint(
    cfg: GridConfig,
    *,
    eff_lo: float,
    eff_hi: float,
    breach_reanchor: bool,
) -> str:
    return json.dumps(
        {
            "mode": "paired_limit",
            "asset": cfg.asset,
            "lo": round(float(eff_lo), 8),
            "hi": round(float(eff_hi), 8),
            "n": int(cfg.n_grids),
            "inv": round(float(cfg.investment_usd), 8),
            "lev": round(float(cfg.leverage), 8),
            "breach_reanchor": bool(breach_reanchor),
        },
        sort_keys=True,
    )


def advance_grid_state(
    *,
    cfg: GridConfig,
    mark: float,
    prev_mark: Optional[float],
    levels_template: Sequence[float],
    state: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (grid_market_events, new_state).

    grid_market_events items:
      {"action": "open_buy"|"open_sell"|"grid_restore_buys", "asset": str, "usd": float, "price": float, "reason": str}
      For open_* when GRID_MARKET_SIZING is qty (default), also ``qty`` (base asset) ≈ usd_leg / mark.
    """
    events: List[Dict[str, Any]] = []
    levels_list = [float(x) for x in levels_template]
    fp = _template_fingerprint(cfg, levels_list)
    usd_leg = per_rung_usd_notional(
        investment_usd=cfg.investment_usd, leverage=cfg.leverage, n_rungs=max(1, len(levels_list))
    )

    if state.get("fingerprint") != fp or os.environ.get("GRIDSTRAT_RESET", "").strip() in ("1", "true", "yes"):
        buys, sells = split_buy_sell(levels_list, mark)
        first_sell = min(sells) if sells else None
        new_state: Dict[str, Any] = {
            "schema_version": 2,
            "fingerprint": fp,
            "last_mark": float(mark),
            "first_sell_price": first_sell,
            "levels_template": levels_list,
            "buy_armed": [float(x) for x in buys],
            "sell_armed": [float(x) for x in sells],
        }
        for _pk in ("pinned_lower", "pinned_upper", "pinned_band_pct"):
            if _pk in state:
                new_state[_pk] = state[_pk]
        state = new_state
        save_state(_default_state_path(), state)
        os.environ.pop("GRIDSTRAT_RESET", None)
        return events, state

    prev = float(prev_mark) if prev_mark is not None else float(state.get("last_mark") or mark)
    buy_armed: Set[float] = {float(x) for x in (state.get("buy_armed") or [])}
    sell_armed: Set[float] = {float(x) for x in (state.get("sell_armed") or [])}
    first_sell = state.get("first_sell_price")
    first_sell_f = float(first_sell) if first_sell is not None else None
    levels_stored = [float(x) for x in (state.get("levels_template") or levels_list)]

    # --- upward cross through first sell anchor → re-arm all buy rungs below mark ---
    if first_sell_f is not None and prev < first_sell_f <= mark:
        buy_armed = {float(p) for p in levels_stored if float(p) < float(mark)}
        events.append(
            {
                "action": "grid_restore_buys",
                "asset": cfg.asset,
                "usd": 0.0,
                "price": float(first_sell_f),
                "reason": "up_cross_first_sell_anchor_rearm_buys",
            }
        )

    # --- down-cross buys (may be multiple in one snapshot) ---
    if mark < prev:
        for b in sorted(buy_armed):
            if float(mark) < float(b) < float(prev):
                events.append(
                    {
                        "action": "open_buy",
                        "asset": cfg.asset,
                        **_grid_open_leg_fields(usd_leg=float(usd_leg), mark=float(mark)),
                        "price": float(b),
                        "reason": "down_cross_buy_rung",
                    }
                )
                buy_armed.discard(float(b))
    # --- up-cross sells ---
    if mark > prev:
        for s in sorted(sell_armed):
            if float(prev) < float(s) <= float(mark):
                events.append(
                    {
                        "action": "open_sell",
                        "asset": cfg.asset,
                        **_grid_open_leg_fields(usd_leg=float(usd_leg), mark=float(mark)),
                        "price": float(s),
                        "reason": "up_cross_sell_rung",
                    }
                )
                sell_armed.discard(float(s))

    state["buy_armed"] = sorted(buy_armed)
    state["sell_armed"] = sorted(sell_armed)
    state["last_mark"] = float(mark)
    state["levels_template"] = levels_stored
    save_state(_default_state_path(), state)
    return events, state


def pick_tickers(
    *,
    listing_json: str,
    marketstate_json: Optional[str] = None,
    top_n: Optional[int] = None,
    venue_pending_keys: Optional[Set[Tuple[str, str]]] = None,
    venue_mark: Optional[float] = None,
    account_flat: bool = False,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    _ = top_n
    _ = marketstate_json
    cfg = GridConfig.from_env()
    err = cfg.validate()
    state_path = _default_state_path()
    state = load_state(state_path)
    if err:
        return (
            [],
            [],
            {
                "strategy": STRATEGY_NAME,
                "thesis": TRADE_THESIS,
                "error": err,
                "grid_market_events": [],
                "grid_mode": True,
            },
        )

    mark_source = "listing_json"
    if cfg.mark_override is not None:
        mark = float(cfg.mark_override)
        mark_source = "env_override"
    elif venue_mark is not None and float(venue_mark) > 0:
        mark = float(venue_mark)
        mark_source = "venue_indicative"
    else:
        mark = _mark_from_listing(listing_json, cfg.asset)
        mark_source = "listing_json"
    fresh_flat_start = False
    if mark is None or mark <= 0:
        return (
            [],
            [],
            {
                "strategy": STRATEGY_NAME,
                "thesis": TRADE_THESIS,
                "error": f"No mark_price for {cfg.asset} in listing json.",
                "grid_market_events": [],
                "grid_mode": True,
            },
        )

    # Flat venue account + no resting limits: new symmetric 5+5 session (ignore stale sim inventory).
    if (
        bool(account_flat)
        and venue_pending_keys is not None
        and len(venue_pending_keys) == 0
        and not _truthy_env("GRIDSTRAT_RESET")
    ):
        fresh_flat_start = True
        for _pk in ("pinned_lower", "pinned_upper", "pinned_band_pct"):
            state.pop(_pk, None)
        state["inventory"] = 0.0
        state["inventory_cost"] = 0.0

    eff_lo, eff_hi, explicit_bounds, band_pct_used, pin_updates, pin_delete_keys = resolve_grid_bounds(
        mark=float(mark), cfg=cfg, state=state
    )
    for _k in pin_delete_keys:
        state.pop(_k, None)
    state.update(pin_updates)
    cfg_eff = replace(cfg, lower=float(eff_lo), upper=float(eff_hi))

    if grid_execution_mode() == "legacy_market":
        levels = build_price_ladder(
            lower=float(eff_lo),
            upper=float(eff_hi),
            n_grids=cfg_eff.n_grids,
            grid_type=cfg_eff.grid_type,
        )
        buys, sells = split_buy_sell(levels, float(mark))
        prev_mark_f: Optional[float] = None
        lm = state.get("last_mark")
        if isinstance(lm, (int, float)):
            prev_mark_f = float(lm)

        events, new_state = advance_grid_state(
            cfg=cfg_eff,
            mark=float(mark),
            prev_mark=prev_mark_f,
            levels_template=levels,
            state=state,
        )

        meta: Dict[str, Any] = {
            "strategy": STRATEGY_NAME,
            "thesis": TRADE_THESIS,
            "grid_mode": True,
            "grid_execution": "legacy_market",
            "grid_paired_limit_mode": False,
            "grid_market_events": events,
            "grid_asset": cfg_eff.asset,
            "grid_mark": float(mark),
            "grid_lower": float(eff_lo),
            "grid_upper": float(eff_hi),
            "grid_bounds_explicit": explicit_bounds,
            "grid_bounds_auto": not explicit_bounds,
            "grid_band_pct": band_pct_used,
            "grid_num": cfg_eff.n_grids,
            "grid_type": cfg_eff.grid_type,
            "grid_buy_rungs": buys,
            "grid_sell_rungs": sells,
            "grid_per_rung_usd": per_rung_usd_notional(
                investment_usd=cfg_eff.investment_usd,
                leverage=cfg_eff.leverage,
                n_rungs=max(1, len(levels)),
            ),
            "grid_state_path": os.path.abspath(state_path),
            "first_sell_anchor": new_state.get("first_sell_price"),
            "grid_market_sizing": grid_market_sizing_mode(),
            "long_count": 0,
            "short_count": 0,
        }
        return [], [], meta

    breach_re = breach_reanchors_on_breach()
    fp = _paired_state_fingerprint(
        cfg_eff,
        eff_lo=float(eff_lo),
        eff_hi=float(eff_hi),
        breach_reanchor=breach_re,
    )
    reset_flag = _truthy_env("GRIDSTRAT_RESET")
    if reset_flag:
        os.environ.pop("GRIDSTRAT_RESET", None)

    anchor = (float(eff_lo) + float(eff_hi)) / 2.0
    pcfg = PairedGridNumericConfig(
        grid_num=int(cfg_eff.n_grids),
        investment_usd=float(cfg_eff.investment_usd),
        leverage=float(cfg_eff.leverage),
        mark=float(mark),
        grid_reset=bool(breach_re),
    )
    params = derive_sim_ladder_params(
        anchor=anchor,
        lower=float(eff_lo),
        upper=float(eff_hi),
        cfg=pcfg,
    )
    reinit = (
        reset_flag
        or fresh_flat_start
        or int(state.get("schema_version") or 0) != 3
        or str(state.get("cfg_fingerprint") or "") != fp
        or not isinstance(state.get("orders"), list)
    )
    paired_step_logs: List[str] = []
    if fresh_flat_start:
        paired_step_logs.append(
            f"fresh flat start: reinit paired ladder at mark {float(mark):g} "
            f"(source={mark_source}, venue pending empty, account flat)"
        )
    if reinit:
        paired = init_paired_state(params=params, tick=0)
        for _pk in ("pinned_lower", "pinned_upper", "pinned_band_pct"):
            if _pk in state:
                paired[_pk] = state[_pk]
        if fresh_flat_start:
            paired["reset_count"] = 0
        paired["last_mark"] = float(mark)
        paired["cfg_fingerprint"] = fp
        state = paired
    else:
        if venue_pending_keys is not None and len(venue_pending_keys) > 0:
            paired_step_logs.extend(
                apply_venue_cleared_limits_as_fills(state, pending_keys=venue_pending_keys)
            )
        prev = float(state.get("last_mark") or float(mark))
        if abs(prev - float(mark)) > 1e-12:
            paired_step_logs.extend(
                step_mark_pair_sequential(
                    state,
                    p_prev=prev,
                    p_now=float(mark),
                    grid_reset=breach_re,
                )
            )
        state["last_mark"] = float(mark)
        state["cfg_fingerprint"] = fp
        state["grid_reset"] = bool(breach_re)

    paired_step_logs.extend(ensure_bracket_rungs_around_mark(state, mark=float(mark)))
    save_state(state_path, state)

    buys, sells = open_rungs_for_meta(state)
    qty_pg = float(state.get("qty_per_grid") or params["qty_per_grid"])
    per_usd = per_rung_usd_notional(
        investment_usd=cfg_eff.investment_usd,
        leverage=cfg_eff.leverage,
        n_rungs=max(1, int(cfg_eff.n_grids)),
    )
    meta = {
        "strategy": STRATEGY_NAME,
        "thesis": TRADE_THESIS,
        "grid_mode": True,
        "grid_execution": "paired_limit",
        # Varibot ``grid_limits_reconcile.run_grid_limits_bootstrap`` treats this as live limit mode.
        "grid_order_execution": "limit",
        "grid_paired_limit_mode": True,
        "grid_rearm_on_breach": "reanchor" if breach_re else "halt",
        "grid_market_events": [],
        "grid_asset": cfg_eff.asset,
        "grid_mark": float(mark),
        "grid_mark_source": mark_source,
        "grid_lower": float(eff_lo),
        "grid_upper": float(eff_hi),
        "grid_bounds_explicit": explicit_bounds,
        "grid_bounds_auto": not explicit_bounds,
        "grid_band_pct": band_pct_used,
        "grid_num": cfg_eff.n_grids,
        "grid_type": cfg_eff.grid_type,
        "grid_buy_rungs": buys,
        "grid_sell_rungs": sells,
        "grid_per_rung_usd": per_usd,
        "grid_per_rung_qty": format_qty_for_grid_limit(qty_pg),
        "grid_limit_sizing": "qty",
        "grid_state_path": os.path.abspath(state_path),
        "grid_market_sizing": grid_market_sizing_mode(),
        "grid_inventory": float(state.get("inventory") or 0.0),
        "grid_realized_pnl": float(state.get("realized_pnl") or 0.0),
        "grid_volume_usd": float(state.get("volume_usd") or 0.0),
        "grid_reset_count": int(state.get("reset_count") or 0),
        "grid_paired_step_logs": paired_step_logs[-30:] if paired_step_logs else [],
        "grid_fresh_flat_start": bool(fresh_flat_start),
        "long_count": 0,
        "short_count": 0,
    }
    return [], [], meta


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Vari gridstrat: print ladder + dry state bump.")
    ap.add_argument(
        "--json-path",
        default=os.path.join(_repo_root_from_here(), "Varibot", "strategy_listing_snapshot.json"),
    )
    ap.add_argument("--print-json", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    longs, shorts, meta = pick_tickers(listing_json=str(args.json_path), marketstate_json=None, top_n=0)
    if args.print_json:
        print(json.dumps({**meta, "long": longs, "short": shorts}, indent=2))
        return 0
    print(f"Strategy={meta.get('strategy')} mark={meta.get('grid_mark')} events={meta.get('grid_market_events')}")
    print(f"Buys={meta.get('grid_buy_rungs')}\nSells={meta.get('grid_sell_rungs')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- Loader: shared strategy_output.txt table + run_strategy() ---

STRATEGIES: Dict[str, str] = {
    "invert_extreme": "gridstrat",
    "gridstrat": "gridstrat",
    "vari_grid": "gridstrat",
}

STRATEGY_OUTPUT_TXT: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_output.txt")

OUTPUT_COLUMNS: List[str] = [
    "side",
    "24hChg%",
    "mcap_usd",
    "vol_24h_usd",
    "oi_usd",
    "oi_skew",
    "ann_fundingrate",
]


def _parse_pct_field(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace("%", "")
    try:
        return float(s)
    except Exception:
        return None


def _load_listing_index(listing_json: str) -> Dict[str, Dict[str, Any]]:
    with open(listing_json, "r", encoding="utf-8") as f:
        doc = json.load(f)
    listings = doc.get("listings") if isinstance(doc, dict) else None
    if not isinstance(listings, list):
        return {}
    idx: Dict[str, Dict[str, Any]] = {}
    for d in listings:
        if not isinstance(d, dict):
            continue
        sym = str(d.get("vari_ticker") or "").strip().upper()
        if not sym:
            continue
        idx[sym] = d
    return idx


def _fmt_num(v: Optional[float], *, decimals: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{decimals}f}"
    except Exception:
        return "-"


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        x = float(v)
    except Exception:
        return "-"
    if x >= 1_000_000_000:
        return f"${x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.2f}K"
    return f"${x:.0f}"


def _row_for_symbol(
    sym: str,
    *,
    side: str,
    listing: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    d = listing or {}
    chg24 = _parse_pct_field(d.get("price_change_24h_pct"))
    mcap = _as_float(d.get("market_cap"))
    vol = _as_float(d.get("vol_24h") if "vol_24h" in d else d.get("volume_24h"))
    oi = _as_float(d.get("OI") if "OI" in d else d.get("open_interest"))
    skew = _as_float(d.get("OI Skew"))
    afr = _as_float(d.get("ann_fundingrate"))

    out: Dict[str, str] = {
        "Symbol": sym,
        "side": side,
        "24hChg%": (f"{chg24:.2f}%" if chg24 is not None else "-"),
        "mcap_usd": _fmt_usd(mcap),
        "vol_24h_usd": _fmt_usd(vol),
        "oi_usd": _fmt_usd(oi),
        "oi_skew": _fmt_num(skew, decimals=3),
        "ann_fundingrate": _fmt_num(afr, decimals=3),
    }
    return out


def write_strategy_output_txt(
    *,
    strategy_key: str,
    meta: Dict[str, Any],
    listing_json: str,
    longs: Iterable[str],
    shorts: Iterable[str],
) -> None:
    fetched_at_sgt: Optional[str] = None
    try:
        with open(listing_json, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            fa = doc.get("fetched_at")
            if isinstance(fa, str) and fa.strip():
                fetched_at_sgt = fa.strip()
    except Exception:
        fetched_at_sgt = None

    idx = _load_listing_index(listing_json)

    rows: List[Dict[str, str]] = []
    for s in [str(x).strip().upper() for x in longs if str(x).strip()]:
        rows.append(_row_for_symbol(s, side="Long", listing=idx.get(s)))
    for s in [str(x).strip().upper() for x in shorts if str(x).strip()]:
        rows.append(_row_for_symbol(s, side="Short", listing=idx.get(s)))

    cols = ["Symbol"] + [c for c in OUTPUT_COLUMNS]
    widths: Dict[str, int] = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(r.get(c, "-"))))

    def line(parts: List[str]) -> str:
        out_parts: List[str] = []
        for i in range(len(cols)):
            cell = str(parts[i])
            if i >= 2:
                out_parts.append(cell.rjust(widths[cols[i]]))
            else:
                out_parts.append(cell.ljust(widths[cols[i]]))
        return "  ".join(out_parts)

    header_lines: List[str] = [
        f"Strategy: {meta.get('strategy') or strategy_key}",
        f"listing_json: {os.path.abspath(str(listing_json))}",
        f"fetched_at: {fetched_at_sgt or '-'}",
    ]
    thesis = meta.get("thesis") if isinstance(meta, dict) else None
    if isinstance(thesis, str) and thesis.strip():
        parts = [p.strip() for p in thesis.strip().split(". ") if p.strip()]
        if len(parts) <= 1:
            header_lines.append(thesis.strip())
        else:
            for i, p in enumerate(parts):
                header_lines.append(p if p.endswith(".") else (p + "."))

    body: List[str] = []
    body.extend(header_lines)
    body.append("")
    if meta.get("grid_mode"):
        body.append(f"grid_asset={meta.get('grid_asset')} mark={meta.get('grid_mark')}")
        if meta.get("grid_bounds_explicit"):
            body.append(
                f"grid_bounds=explicit lower={meta.get('grid_lower')} upper={meta.get('grid_upper')}"
            )
        else:
            bp = meta.get("grid_band_pct")
            body.append(
                f"grid_bounds=±{bp}% (pinned) lower={meta.get('grid_lower')} upper={meta.get('grid_upper')}"
                if bp is not None
                else f"grid_bounds=percent_band lower={meta.get('grid_lower')} upper={meta.get('grid_upper')}"
            )
        body.append(f"grid_events_this_cycle={json.dumps(meta.get('grid_market_events') or [])}")
        body.append("")
    if not rows:
        body.append("(no symbol rows — grid uses GRID_* env + listing mark)")
    else:
        body.append(line(cols))
        body.append(line(["-" * widths[c] for c in cols]))
        for r in rows:
            body.append(line([str(r.get(c, "-")) for c in cols]))

    with open(STRATEGY_OUTPUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")


def resolve_strategy_module_name(key: str) -> str:
    k = (key or "").strip().lower()
    if not k:
        raise ValueError("strategy key is empty")
    if k.endswith(".py"):
        k = k[:-3]
    mod = STRATEGIES.get(k)
    if mod:
        return mod
    return k


def load_strategy_module(key: str):
    mod_name = resolve_strategy_module_name(key)
    return importlib.import_module(f"strategy.{mod_name}")


def run_strategy(
    *,
    strategy_key: str,
    listing_json: str,
    marketstate_json: Optional[str],
    top_n: int,
    write_output_txt: bool = True,
    venue_pending_keys: Optional[Set[Tuple[str, str]]] = None,
    venue_mark: Optional[float] = None,
    account_flat: bool = False,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    mod = load_strategy_module(strategy_key)
    if not hasattr(mod, "pick_tickers"):
        raise AttributeError(
            f"Strategy module {mod.__name__!r} missing required function pick_tickers(listing_json, marketstate_json)"
        )
    pick_kw: Dict[str, Any] = {
        "listing_json": listing_json,
        "marketstate_json": marketstate_json,
        "top_n": int(top_n),
    }
    if venue_pending_keys is not None:
        pick_kw["venue_pending_keys"] = venue_pending_keys
    if venue_mark is not None:
        pick_kw["venue_mark"] = float(venue_mark)
    pick_kw["account_flat"] = bool(account_flat)
    longs, shorts, meta = mod.pick_tickers(**pick_kw)
    if not isinstance(meta, dict):
        meta = {"strategy": str(getattr(mod, "STRATEGY_NAME", mod.__name__))}
    meta.setdefault("strategy", str(getattr(mod, "STRATEGY_NAME", mod.__name__)))
    meta.setdefault("long_count", len(longs))
    meta.setdefault("short_count", len(shorts))

    if write_output_txt:
        try:
            write_strategy_output_txt(
                strategy_key=strategy_key,
                meta=meta,
                listing_json=listing_json,
                longs=longs,
                shorts=shorts,
            )
        except OSError:
            pass

    return list(longs), list(shorts), meta
