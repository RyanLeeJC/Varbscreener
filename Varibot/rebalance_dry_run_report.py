#!/usr/bin/env python3
"""Fetch live positions via .env and write rebalance dry-run markdown table."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from portfolio_rebalance import (  # noqa: E402
    LivePosition,
    _assign_sides,
    parse_live_positions_from_raw,
    plan_portfolio_rebalance,
    rebalance_constants,
)
from variationalbot.config import load_config  # noqa: E402
from variationalbot.domain import parse_portfolio_snapshot  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402
from variationalbot.vari.endpoints import Instrument  # noqa: E402


def _fetch_mark(ep: VariEndpoints, sym: str) -> float:
    inst = Instrument(
        instrument_type="perpetual_future",
        underlying=str(sym).strip().upper(),
        funding_interval_s=3600,
        settlement_asset="USDC",
    )
    q = ep.quote_indicative_simple(instrument=inst, qty=1.0)
    if isinstance(q, dict):
        for k in ("mark_price", "index_price", "ask", "bid"):
            if k in q and q[k] is not None:
                mf = float(q[k])
                if mf > 0:
                    return mf
    raise ValueError(f"no mark for {sym}")


def _enrich_positions(ep: VariEndpoints, positions_raw: object) -> List[LivePosition]:
    parsed = parse_live_positions_from_raw(positions_raw)
    by_ticker = {p.ticker: p for p in parsed}
    # Re-scan raw rows for tickers missing mark in payload
    rows: List[dict] = []
    if isinstance(positions_raw, list):
        rows = [p for p in positions_raw if isinstance(p, dict)]
    elif isinstance(positions_raw, dict) and isinstance(positions_raw.get("positions"), list):
        rows = [p for p in positions_raw["positions"] if isinstance(p, dict)]

    from portfolio_rebalance import _position_label, _position_qty_signed  # noqa: E402

    for p in rows:
        sym = _position_label(p)
        if not sym or sym == "UNKNOWN" or sym in by_ticker:
            continue
        q = _position_qty_signed(p)
        if q is None or abs(q) <= 1e-12:
            continue
        try:
            mk = _fetch_mark(ep, sym)
        except Exception:
            continue
        side = "long" if q > 0 else "short"
        by_ticker[sym] = LivePosition(
            ticker=sym, side=side, quantity=abs(float(q)), mark_price=float(mk)
        )

    # Refresh marks for parsed positions when we want venue indicative (optional)
    out: List[LivePosition] = []
    for p in sorted(by_ticker.values(), key=lambda x: x.ticker):
        out.append(p)
    return out


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_side(current: str, assigned: Optional[str]) -> str:
    if assigned is None:
        return current
    if assigned == current:
        return assigned
    return f"{current} → {assigned}"


def main() -> int:
    env_path = os.path.join(_VARIBOT_DIR, ".env")
    cfg = load_config(env_path=env_path)
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    raw_pf = ep.get_portfolio(compute_margin=True)
    snap = parse_portfolio_snapshot(raw_pf)
    raw_pos = ep.get_positions()
    positions = _enrich_positions(ep, raw_pos)

    trig, tgt, rnd, min_usd = rebalance_constants()
    pv = float(snap.portfolio_value_usd or 0)
    mm = float(snap.mm_usage or 0)
    lev = int(cfg.max_leverage)

    forced_preview = mm < trig
    plan_mm = max(mm, trig) if forced_preview else mm

    plan = plan_portfolio_rebalance(
        portfolio_value=pv,
        max_leverage=float(lev),
        current_margin_usage=plan_mm,
        positions=positions,
    )

    out_path = os.path.join(_VARIBOT_DIR, "rebalance_dry_run.md")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: List[str] = [
        "# Interval risk rebalance — dry run",
        "",
        f"Generated: {ts}",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Portfolio value | {_fmt_usd(pv)} |",
        f"| Max leverage | {lev}x |",
        f"| MM usage (live) | {mm * 100:.2f}% |",
        f"| MM trigger | {trig * 100:g}% |",
        f"| IM target (sizing) | {tgt * 100:g}% |",
        f"| Positions (venue) | {len(positions)} |",
    ]

    if plan is None:
        lines.extend(
            [
                "",
                "**Planner:** no rebalance plan (insufficient positions or invalid inputs).",
                "",
            ]
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(out_path)
        return 1

    order_by = {o.ticker: o for o in plan.orders}
    working_set = [p for p in positions if p.ticker in plan.working_tickers]
    side_by = _assign_sides(working_set, plan.n_eff)

    if forced_preview:
        lines.append(f"| Dry-run note | MM below trigger; table uses forced MM ≥ {trig * 100:g}% preview |")
    else:
        lines.append("| Dry-run note | Live MM at or above trigger |")

    lines.extend(
        [
            f"| Target notional / leg | {_fmt_usd(plan.target_notional)} |",
            f"| N_eff (rebalanced) | {plan.n_eff} |",
            f"| Dropped (odd N) | {plan.dropped_ticker or '—'} |",
            f"| Projected order volume | {_fmt_usd(plan.total_volume_usd)} |",
            "",
            "| Ticker | Side | Value Bef | Value Aft |",
            "|--------|------|-----------|-----------|",
        ]
    )

    rows: List[tuple[str, str, float, str]] = []

    if plan.dropped_ticker:
        drop_p = next((p for p in positions if p.ticker == plan.dropped_ticker), None)
        if drop_p:
            rows.append(
                (
                    drop_p.ticker,
                    f"{drop_p.side} (dropped)",
                    drop_p.notional,
                    "—",
                )
            )

    for p in sorted(working_set, key=lambda x: (-x.signed_notional, x.ticker)):
        assigned = side_by[p.ticker]
        bef = p.notional
        aft = plan.target_notional
        side_txt = _fmt_side(p.side, assigned)
        o = order_by.get(p.ticker)
        if o and o.order_notional < min_usd:
            side_txt += " (≈target, no order)"
        rows.append((p.ticker, side_txt, bef, aft))

    for ticker, side, bef, aft in sorted(rows, key=lambda r: r[0]):
        aft_s = _fmt_usd(float(aft)) if isinstance(aft, (int, float)) else str(aft)
        lines.append(f"| {ticker} | {side} | {_fmt_usd(bef)} | {aft_s} |")

    lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
