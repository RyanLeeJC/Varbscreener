#!/usr/bin/env python3
"""
Rebuild Varibot/gridstrat_state.json from live venue marks (fresh paired ladder per ticker).

Use when sim state drifted (e.g. pinned bounds stuck, wrong rung counts) but resting limits
on the exchange are OK. This does NOT cancel or replace venue orders — run reconcile after
the bot loads the new state, or use GRIDSTRAT_RESET=1 on the server for an in-process rebuild.

Examples:
  cd Varibot && python3 rebuild_gridstrat_state.py
  cd Varibot && python3 rebuild_gridstrat_state.py --only TON,ETH
  cd Varibot && python3 rebuild_gridstrat_state.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

_VARIBOT = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_VARIBOT)
for p in (_REPO, _VARIBOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from strategy.gridstrat import (  # noqa: E402
    GridConfig,
    ROOT_STATE_SCHEMA_VERSION,
    _default_state_path,
    breach_reanchors_on_breach,
    grid_trading_ticker_band_pcts,
    resolve_grid_bounds,
)
from strategy.gridstrat_rearm import (  # noqa: E402
    PairedGridNumericConfig,
    derive_sim_ladder_params,
    init_paired_state,
    open_rungs_for_meta,
)
from strategy.gridstrat_state import load_state, save_state  # noqa: E402


def _fresh_asset_state(*, asset: str, mark: float, band_pct: float) -> Dict[str, Any]:
    cfg = GridConfig.from_env(asset)
    err = cfg.validate()
    if err:
        raise RuntimeError(f"{asset}: {err}")
    # Empty state → new pins at current mark, no legacy pinned_lower/upper
    empty: Dict[str, Any] = {}
    eff_lo, eff_hi, _, band_used, pin_updates, pin_delete = resolve_grid_bounds(
        mark=float(mark),
        cfg=cfg,
        state=empty,
        band_pct=float(band_pct),
    )
    for k in pin_delete:
        empty.pop(k, None)
    empty.update(pin_updates)

    anchor = (float(eff_lo) + float(eff_hi)) / 2.0
    pcfg = PairedGridNumericConfig(
        grid_num=int(cfg.n_grids),
        investment_usd=float(cfg.investment_usd),
        leverage=float(cfg.leverage),
        mark=float(mark),
        grid_reset=breach_reanchors_on_breach(),
    )
    params = derive_sim_ladder_params(
        anchor=anchor,
        lower=float(eff_lo),
        upper=float(eff_hi),
        cfg=pcfg,
    )
    params["asset"] = str(asset).strip().upper()
    st = init_paired_state(params=params, tick=0)
    st["last_mark"] = float(mark)
    for pk in ("pinned_lower", "pinned_upper", "pinned_band_pct"):
        if pk in empty:
            st[pk] = empty[pk]
    st["inventory"] = 0.0
    st["inventory_cost"] = 0.0
    st["realized_pnl"] = 0.0
    st["volume_usd"] = 0.0
    st["reset_count"] = 0
    buys, sells = open_rungs_for_meta(st)
    return st, {
        "mark": mark,
        "lower": eff_lo,
        "upper": eff_hi,
        "band_pct": band_used,
        "open_buys": len(buys),
        "open_sells": len(sells),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild gridstrat_state.json from live marks.")
    ap.add_argument(
        "--only",
        help="Comma-separated tickers to rebuild (default: all GRID_TRADING_TICKERS)",
    )
    ap.add_argument(
        "--out",
        default="",
        help="Output path (default: GRIDSTRAT_STATE_PATH or Varibot/gridstrat_state.json)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary only; do not write file",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="Keep other tickers from existing state file; only replace --only tickers",
    )
    args = ap.parse_args()

    os.chdir(_VARIBOT)
    from variationalbot.config import load_config
    from variationalbot.vari import VariAuth, VariClient, VariEndpoints

    import varibot as vb

    load_config(env_path=os.path.join(_VARIBOT, ".env"))
    cfg = load_config(env_path=os.path.join(_VARIBOT, ".env"))
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    tickers = grid_trading_ticker_band_pcts()
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        tickers = {k: v for k, v in tickers.items() if k in want}
        if not tickers:
            print("No matching tickers in GRID_TRADING_TICKERS.", file=sys.stderr)
            return 1

    marks = vb._fetch_grid_marks_for_assets(ep, tickers.keys())
    out_path = args.out.strip() or _default_state_path()

    root: Dict[str, Any] = {"schema_version": ROOT_STATE_SCHEMA_VERSION, "assets": {}}
    if args.merge and os.path.isfile(out_path):
        prev = load_state(out_path)
        if isinstance(prev.get("assets"), dict):
            root["assets"] = dict(prev["assets"])

    print(f"Rebuilding paired state for {len(tickers)} ticker(s) at live marks…")
    for asset, band in sorted(tickers.items()):
        mk = marks.get(asset)
        if mk is None or float(mk) <= 0:
            print(f"  SKIP {asset}: no mark")
            continue
        try:
            st, info = _fresh_asset_state(asset=asset, mark=float(mk), band_pct=float(band))
        except Exception as e:
            print(f"  FAIL {asset}: {e}")
            continue
        root["assets"][asset] = st
        print(
            f"  {asset}: mark={info['mark']:.6g} band=±{info['band_pct']}% "
            f"[{info['lower']:.6g}, {info['upper']:.6g}] "
            f"open buys={info['open_buys']} sells={info['open_sells']}"
        )

    if args.dry_run:
        print(f"\n(dry-run) would write {out_path}")
        return 0

    save_state(out_path, root)
    print(f"\nWrote {os.path.abspath(out_path)}")
    print(
        "Next: restart Varibot or wait one cycle. Consider reconcile posting missing limits.\n"
        "On Render: upload this file or set GRIDSTRAT_RESET=1 for one cycle instead."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
