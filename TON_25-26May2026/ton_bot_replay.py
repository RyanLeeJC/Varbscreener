#!/usr/bin/env python3
"""
TON grid bot replay — current paired_limit + remnant re-arm logic.

Runs one Varibot-style cycle per 1m bar from BINANCE_TONUSDT_1m.csv:
  1. Intrabar OHLC fills on simulated venue limits
  2. gridstrat pick_tickers (sim state)
  3. Remnant reconcile (venue cancel/post)

Outputs:
  - ton_replay_embed.js — baked into TON_bot_replay.html (open HTML directly)
  - ton_replay_data.json — optional fetch fallback
  - ton_replay_terminal.log — full per-cycle terminal

Usage (from repo root):
  python3 TON_25-26May2026/ton_bot_replay.py   # after logic or CSV changes
  open TON_25-26May2026/TON_bot_replay.html    # no server required
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parents[1]
TON_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "Varibot"))

# Defaults aligned with gridstrat.py TON config
os.environ.setdefault("GRID_ASSET", "TON")
os.environ.setdefault("GRID_NUM", "10")
os.environ.setdefault("GRID_INVESTMENT_USD", "20")
os.environ.setdefault("GRID_LEVERAGE", "50")
os.environ.setdefault("GRID_REARM_ON_BREACH", "halt")

from strategy.gridstrat import (  # noqa: E402
    GridConfig,
    _pick_tickers_one_asset,
    breach_reanchors_on_breach,
    grid_band_pct_for_asset,
    per_rung_usd_notional,
)
from strategy.gridstrat_rearm import (  # noqa: E402
    EPS_LEVEL,
    _fill_open_order_and_rearm,
    _pending_keys_from_state,
    record_venue_pending_snapshot,
)
from strategy.gridstrat_remnant import (  # noqa: E402
    RemnantInferenceResult,
    compute_venue_actions,
    infer_ladder_from_remnants,
)

try:
    from variationalbot.vari.endpoints import grid_limit_price_key, limit_price_key
except ImportError:
    from strategy.gridstrat_remnant import grid_limit_price_key  # type: ignore

    def limit_price_key(side: str, price: float) -> Tuple[str, str]:
        return (str(side).strip().lower(), grid_limit_price_key(price))


ASSET = "TON"
BAND_PCT = 3.0
PRICE_DECIMALS = 3


def _fmt(px: float) -> str:
    return f"{px:.{PRICE_DECIMALS}f}"


def _load_bars(csv_path: Path) -> List[Dict[str, float]]:
    bars: List[Dict[str, float]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append(
                {
                    "time": row["time"].strip(),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
    if len(bars) < 2:
        raise SystemExit(f"Need at least 2 bars in {csv_path}")
    return bars


def _keys_to_limits(keys: Set[Tuple[str, str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for side, pxk in sorted(keys, key=lambda k: (-float(k[1]) if k[0] == "buy" else float(k[1]))):
        try:
            px = float(pxk)
        except ValueError:
            continue
        out.append({"side": side, "price": px, "price_key": pxk})
    return out


def _find_open_order(state: Dict[str, Any], level: float, side: str) -> Optional[Dict[str, Any]]:
    """
    Find a sim open order corresponding to a venue limit price.

    Venue pending keys are stored at venue price-key precision (e.g. 1.7940), while sim levels
    can be slightly different (e.g. 1.79417). Match by venue price-key first to avoid
    double-counting fills (venue fill + later venue-sync fill) due to rounding.
    """
    want_key = grid_limit_price_key(float(level))
    for o in state.get("orders") or []:
        if o.get("status") != "open":
            continue
        if str(o.get("side")) != side:
            continue
        try:
            if grid_limit_price_key(float(o["level"])) == want_key:
                return o
        except Exception:
            continue
    # Fallback: raw tolerance
    for o in state.get("orders") or []:
        if (
            o.get("status") == "open"
            and str(o.get("side")) == side
            and abs(float(o["level"]) - float(level)) < EPS_LEVEL
        ):
            return o
    return None


def _apply_untracked_venue_fill(
    state: Dict[str, Any],
    *,
    side: str,
    price: float,
    tick: int,
    logs: List[str],
) -> None:
    """
    Apply a venue limit fill to PnL/inventory even when the sim does not currently
    have a matching open order at that exact level.

    This keeps top-bar Inventory/RPNL/UPNL consistent with the replay's venue book,
    especially now that remnant maintenance can post levels not present in the sim book.
    """
    q = float(state.get("qty_per_grid") or 0.0)
    if q <= 0:
        return
    fill_px = float(price)
    inventory = float(state.get("inventory") or 0.0)
    inventory_cost = float(state.get("inventory_cost") or 0.0)
    realized_pnl = float(state.get("realized_pnl") or 0.0)
    volume_usd = float(state.get("volume_usd") or 0.0)

    trade_qty = q if side == "buy" else -q
    new_inventory = inventory + trade_qty

    # Realize PnL on any trade that reduces an existing position (avg-entry cost basis).
    if abs(inventory) > 1e-12 and (trade_qty > 0) != (inventory > 0):
        avg_entry = inventory_cost / inventory
        close_qty = min(abs(trade_qty), abs(inventory))
        pnl_delta = close_qty * (fill_px - avg_entry) * (1.0 if inventory > 0 else -1.0)
        realized_pnl += pnl_delta
        logs.append(
            f"tick {tick}: realized {pnl_delta:+g} (venue {side.upper()} close {close_qty:g} @ {_fmt(fill_px)} vs avg {_fmt(avg_entry)})"
        )

    # Update inventory cost (same rules as gridstrat_rearm).
    if abs(inventory) < 1e-12:
        inventory_cost = trade_qty * fill_px
    elif (trade_qty > 0) == (inventory > 0):
        inventory_cost += trade_qty * fill_px
    else:
        if abs(trade_qty) <= abs(inventory):
            close_frac = abs(trade_qty) / abs(inventory)
            inventory_cost -= inventory_cost * close_frac
        else:
            inventory_cost = new_inventory * fill_px

    inventory = new_inventory
    if abs(inventory) < 1e-10:
        inventory = 0.0
        inventory_cost = 0.0

    volume_usd += q * fill_px

    state["inventory"] = inventory
    state["inventory_cost"] = inventory_cost
    state["realized_pnl"] = realized_pnl
    state["volume_usd"] = volume_usd


def _apply_intrabar_fills(
    state: Dict[str, Any],
    *,
    bar: Dict[str, float],
    venue_pending: Set[Tuple[str, str]],
    logs: List[str],
) -> List[Tuple[str, str]]:
    """Simulate limit fills when bar high/low trades through resting venue prices."""
    filled: List[Tuple[str, str]] = []
    hi = float(bar["high"])
    lo = float(bar["low"])
    tick = int(state.get("tick") or 0)

    for key in list(venue_pending):
        side, pxk = key
        try:
            px = float(pxk)
        except ValueError:
            continue
        hit = False
        if side == "buy" and lo <= px + 1e-9:
            hit = True
        elif side == "sell" and hi >= px - 1e-9:
            hit = True
        if not hit:
            continue

        ord_ = _find_open_order(state, px, side)
        if ord_ is not None:
            _fill_open_order_and_rearm(state, ord_, tick=tick, logs=logs)
            logs.append(f"tick {tick}: venue OHLC {side.upper()} filled @ {_fmt(px)} (intrabar)")
        else:
            _apply_untracked_venue_fill(state, side=side, price=px, tick=tick, logs=logs)
            logs.append(
                f"tick {tick}: venue OHLC {side.upper()} filled @ {_fmt(px)} (intrabar, untracked)"
            )
        venue_pending.discard(key)
        filled.append(key)

    return filled


def _remnant_reconcile_log(
    *,
    asset: str,
    meta: Dict[str, Any],
    mark: float,
    venue_pending: Set[Tuple[str, str]],
    drift_cancel: bool = True,
    is_init: bool = False,
) -> Tuple[List[str], Set[Tuple[str, str]]]:
    """Mirror grid_limits_reconcile._remnant_rearm_one_ticker logging (no HTTP)."""
    tag = f"[{asset}]"
    label = "init" if is_init else "remnant"
    lines: List[str] = []

    spacing = float(meta.get("grid_spacing") or 0.0)
    lower = float(meta.get("grid_lower") or 0.0)
    upper = float(meta.get("grid_upper") or 0.0)
    grid_num = int(meta.get("grid_num") or 10)

    if spacing <= 0 or upper <= lower:
        lines.append(f"gridlimits{tag} {label}: skip — no valid spacing/bounds in meta.")
        return lines, venue_pending

    band_pct_meta = meta.get("grid_band_pct")
    try:
        grid_band_pct = float(band_pct_meta) if band_pct_meta is not None else None
    except (TypeError, ValueError):
        grid_band_pct = None

    result: RemnantInferenceResult = infer_ladder_from_remnants(
        mark=mark,
        venue_pending_keys=venue_pending,
        configured_spacing=spacing,
        lower=lower,
        upper=upper,
        grid_num=grid_num,
        grid_band_pct=grid_band_pct,
    )

    if is_init:
        lines.append(
            f"gridlimits{tag} init: mark={mark:g}, venue empty — seeding grid"
        )
    else:
        lines.append(
            f"gridlimits{tag} remnant: {result} "
            f"(venue pending={len(venue_pending)}, mark={mark:g})"
        )

    pending = set(venue_pending)

    cancel_keys, post_rungs = compute_venue_actions(
        result=result, venue_pending_keys=pending, mark=mark
    )
    if cancel_keys and drift_cancel:
        for k in cancel_keys:
            pending.discard(k)
        lines.append(f"gridlimits{tag} {label}: canceled {len(cancel_keys)} out-of-band orphan(s).")
        _, post_rungs = compute_venue_actions(
            result=result, venue_pending_keys=pending, mark=mark
        )
    if post_rungs:
        n_b = sum(1 for s, _ in post_rungs if s == "buy")
        n_s = len(post_rungs) - n_b
        if is_init:
            lines.append(
                f"gridlimits{tag} init: posting {len(post_rungs)} limits "
                f"({n_b} buy, {n_s} sell)"
            )
            for side, px in post_rungs:
                pending.add(limit_price_key(side, px))
                lines.append(f"gridlimits{tag} init: {side.upper()} @ {_fmt(px)}")
        else:
            lines.append(
                f"gridlimits{tag} remnant: posting {len(post_rungs)} missing window rung(s) "
                f"(nearest-first: buys={n_b} sells={n_s})"
            )
            for side, px in post_rungs:
                pending.add(limit_price_key(side, px))
                lines.append(f"gridlimits{tag} remnant: POST {side.upper()} @ {_fmt(px)}")
    else:
        lines.append(f"gridlimits{tag} {label}: window complete — no rungs to post.")
    return lines, pending

    return lines, pending


def _trim_orders_for_export(
    orders: List[Dict[str, Any]], *, tick: int, max_records: int = 100
) -> List[Dict[str, Any]]:
    """Keep open orders plus recent fills/cancels so JSON stays small."""
    open_ = [o for o in orders if o.get("status") == "open"]
    closed = [
        o
        for o in orders
        if o.get("status") in ("filled", "cancelled")
        and int(o.get("filledAtTick") or o.get("cancelledAtTick") or -1) >= tick - 120
    ]
    closed.sort(
        key=lambda o: int(o.get("filledAtTick") or o.get("cancelledAtTick") or 0),
        reverse=True,
    )
    out = open_ + closed[: max(0, max_records - len(open_))]
    return out


def _snapshot_from_state(
    state: Dict[str, Any],
    *,
    mark: float,
    bar: Dict[str, float],
    venue_pending: Set[Tuple[str, str]],
    terminal: List[str],
    venue_new: Set[Tuple[str, str]],
) -> Dict[str, Any]:
    orders_out: List[Dict[str, Any]] = []
    new_order_ids: List[str] = []
    tick_n = int(state.get("tick") or 0)
    raw_orders = _trim_orders_for_export(state.get("orders") or [], tick=tick_n)
    for o in raw_orders:
        od = dict(o)
        if "placedAtTick" not in od:
            od["placedAtTick"] = int(o.get("placed_at_tick") or tick_n)
        od["newThisTick"] = int(od.get("placedAtTick") or 0) == tick_n
        orders_out.append(od)
        if od["newThisTick"]:
            new_order_ids.append(str(o.get("id")))

    inv = float(state.get("inventory") or 0.0)
    inv_cost = float(state.get("inventory_cost") or 0.0)
    upnl = 0.0
    if abs(inv) > 1e-12:
        avg = inv_cost / inv
        upnl = inv * (float(mark) - avg)

    return {
        "time": bar["time"],
        "price": float(mark),
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "orders": orders_out,
        "venue_limits": [
            {**lim, "newThisTick": limit_price_key(lim["side"], lim["price"]) in venue_new}
            for lim in _keys_to_limits(venue_pending)
        ],
        "terminal": terminal,
        "inventory": inv,
        "realizedPnl": float(state.get("realized_pnl") or 0.0),
        "upnl": upnl,
        "volumeUsd": float(state.get("volume_usd") or 0.0),
        "resetCount": int(state.get("reset_count") or 0),
        "anchor": float(state.get("current_anchor") or mark),
        "gridLower": float(state.get("current_grid_lower") or 0.0),
        "gridUpper": float(state.get("current_grid_upper") or 0.0),
        "spacing": float(state.get("spacing") or 0.0),
        "newOrderIds": new_order_ids,
        "resetThisTick": any("GRID RESET" in " ".join(terminal) for _ in terminal),
    }


@dataclass
class ReplayResult:
    prices: List[float]
    times: List[str]
    snapshots: List[Dict[str, Any]]
    cycle_logs: List[List[str]]
    config: Dict[str, Any]


def run_replay(
    bars: List[Dict[str, float]],
    *,
    listing_json: str,
    log_print: bool = True,
) -> ReplayResult:
    cfg = GridConfig.from_env(ASSET)
    band = float(grid_band_pct_for_asset(ASSET))
    state: Dict[str, Any] = {}
    venue_pending: Set[Tuple[str, str]] = set()
    snapshots: List[Dict[str, Any]] = []
    cycle_logs: List[List[str]] = []
    prices = [b["close"] for b in bars]

    per_usd = per_rung_usd_notional(
        investment_usd=cfg.investment_usd,
        leverage=cfg.leverage,
        n_rungs=cfg.n_grids,
    )

    lines_out: List[str] = []

    def emit(msg: str) -> None:
        lines_out.append(msg)
        if log_print:
            print(msg)

    emit("=" * 72)
    emit("TON bot replay — current logic (paired_limit + remnant re-arm)")
    emit(
        f"config: GRID_NUM={cfg.n_grids} band=±{band}% "
        f"inv=${cfg.investment_usd} lev={cfg.leverage}x "
        f"breach={breach_reanchors_on_breach()}"
    )
    emit(f"bars: {len(bars)} × 1m  {bars[0]['time']} → {bars[-1]['time']}")
    emit("=" * 72)

    for i, bar in enumerate(bars):
        mark = float(bar["close"])
        term: List[str] = []
        venue_new: Set[Tuple[str, str]] = set()

        def tlog(msg: str) -> None:
            term.append(msg)

        if i == 0:
            tlog("--- cycle 0 (INIT) ---")
        else:
            tlog(f"--- cycle {i} ---")

        tlog(f"step: venue mark {ASSET}={mark:g} (supported_assets)")
        tlog(f"step: pending bulk fetch OK — {len(venue_pending)} limit(s) on venue")

        # Intrabar venue fills (before pick_tickers)
        fill_logs: List[str] = []
        filled_keys = _apply_intrabar_fills(
            state, bar=bar, venue_pending=venue_pending, logs=fill_logs
        )
        for ln in fill_logs:
            tlog(f"gridstrat[{ASSET}]: {ln}")

        pending_for_pick = set(venue_pending)

        state, meta = _pick_tickers_one_asset(
            asset=ASSET,
            band_pct=band,
            listing_json=listing_json,
            state=state,
            venue_pending_keys=pending_for_pick,
            venue_mark=mark,
            account_flat=(i == 0),
            reset_flag=False,
        )

        tlog("step: strategy feed ready")
        tlog(
            f"step: gridstrat[{ASSET}] rungs buy={len(meta.get('grid_buy_rungs') or [])} "
            f"sell={len(meta.get('grid_sell_rungs') or [])} "
            f"mark={meta.get('grid_mark')!r} band=±{meta.get('grid_band_pct')}%"
        )

        if meta.get("grid_fresh_flat_start"):
            tlog("gridstrat: fresh flat session — symmetric paired ladder reinit at current mark")

        for ln in (meta.get("grid_paired_step_logs") or [])[-8:]:
            tlog(f"gridstrat[{ASSET}]: {ln}")

        # Remnant reconcile → venue book
        before_venue = set(venue_pending)
        rem_logs, venue_pending = _remnant_reconcile_log(
            asset=ASSET,
            meta=meta,
            mark=mark,
            venue_pending=venue_pending,
            drift_cancel=True,
            is_init=(i == 0),
        )
        for ln in rem_logs:
            tlog(ln)

        record_venue_pending_snapshot(state, pending_keys=venue_pending)

        venue_new = venue_pending - before_venue

        state["tick"] = i

        tlog(
            f"venue after reconcile: {len(venue_pending)} limit(s) — "
            + ", ".join(f"{x['side'][0].upper()}@{_fmt(x['price'])}" for x in _keys_to_limits(venue_pending)[:12])
            + (" …" if len(venue_pending) > 12 else "")
        )

        cycle_logs.append(list(term))
        snap = _snapshot_from_state(
            state,
            mark=mark,
            bar=bar,
            venue_pending=venue_pending,
            terminal=[],
            venue_new=venue_new,
        )
        snapshots.append(snap)

        if i == 0 or i % 60 == 0 or filled_keys or venue_new or snap.get("resetThisTick"):
            for ln in term:
                emit(ln)
            emit("")

    return ReplayResult(
        prices=prices,
        times=[b["time"] for b in bars],
        snapshots=snapshots,
        cycle_logs=cycle_logs,
        config={
            "asset": ASSET,
            "bandPct": band,
            "gridNum": cfg.n_grids,
            "investmentUsd": cfg.investment_usd,
            "leverage": cfg.leverage,
            "gridReset": breach_reanchors_on_breach(),
            "perRungUsd": per_usd,
            "priceDecimals": PRICE_DECIMALS,
        },
    )


def main() -> int:
    csv_path = TON_DIR / "BINANCE_TONUSDT_1m.csv"
    listing = REPO / "Varibot" / "strategy_listing_snapshot.json"
    if not listing.is_file():
        listing = TON_DIR / ".listing_stub.json"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text(
            json.dumps({"listings": [{"vari_ticker": ASSET, "mark_price": 1.0}]}),
            encoding="utf-8",
        )

    bars = _load_bars(csv_path)
    result = run_replay(bars, listing_json=str(listing))

    out_json = TON_DIR / "ton_replay_data.json"
    # Surface spacing match/snap controls in payload config for the UI.
    match_ticks = int((os.environ.get("GRID_SPACING_MATCH_TICKS") or "1").strip() or "1")
    snap_ticks = int((os.environ.get("GRID_SPACING_SNAP_TICKS") or "10").strip() or "10")
    payload = {
        "config": {
            **result.config,
            "spacingMatchTicks": match_ticks,
            "spacingSnapTicks": snap_ticks,
        },
        "prices": result.prices,
        "times": result.times,
        "cycleLogs": result.cycle_logs,
        "snapshots": result.snapshots,
    }
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    embed_js = TON_DIR / "ton_replay_embed.js"
    embed_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    embed_js.write_text(
        f"/* Auto-generated by ton_bot_replay.py — do not edit */\n"
        f"window.TON_REPLAY_DATA = {embed_body};\n",
        encoding="utf-8",
    )

    log_path = TON_DIR / "ton_replay_terminal.log"
    with log_path.open("w", encoding="utf-8") as f:
        for i, lines in enumerate(result.cycle_logs):
            f.write(f"--- cycle {i} ---\n")
            for ln in lines:
                f.write(ln + "\n")
            f.write("\n")

    print(f"\nWrote {out_json} ({out_json.stat().st_size // 1024} KB)")
    print(f"Wrote {embed_js} ({embed_js.stat().st_size // 1024} KB)")
    print(f"Wrote {log_path}")
    print(f"Open TON_bot_replay.html (double-click or any static server)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
