#!/usr/bin/env python3
"""Run interval-risk portfolio rebalance (dry-run default; --live sends market orders)."""

from __future__ import annotations

import argparse
import os
import sys

_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)

from portfolio_rebalance import rebalance_portfolio  # noqa: E402
from rebalance_dry_run_report import _enrich_positions, _fetch_mark  # noqa: E402
from variationalbot.config import load_config  # noqa: E402
from variationalbot.domain import parse_portfolio_snapshot  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402


def _resolve_max_slippage() -> float:
    try:
        v = os.getenv("MAX_SLIPPAGE", "").strip()
        if v:
            return float(v)
    except Exception:
        pass
    return 0.002


def main() -> int:
    ap = argparse.ArgumentParser(description="IM-triggered portfolio rebalance.")
    ap.add_argument(
        "--live",
        action="store_true",
        help="Place real market orders (default: dry-run log only).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="With --live: skip confirmation prompt.",
    )
    args = ap.parse_args()

    if args.live and not args.force:
        print("LIVE rebalance: market orders only; pending limits are NOT canceled.", file=sys.stderr)
        confirm = input("Type yes to continue: ").strip().lower()
        if confirm != "yes":
            print("Aborted.", file=sys.stderr)
            return 1

    env_path = os.path.join(_VARIBOT_DIR, ".env")
    cfg = load_config(env_path=env_path)
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    def log(msg: str) -> None:
        print(msg, flush=True)

    raw_pf = ep.get_portfolio(compute_margin=True)
    snap = parse_portfolio_snapshot(raw_pf)
    raw_pos = ep.get_positions()
    lev = int(cfg.max_leverage)

    ran = rebalance_portfolio(
        ep=ep,
        snap=snap,
        positions_raw=raw_pos,
        max_leverage=lev,
        live=bool(args.live),
        dry_run=not bool(args.live),
        log=log,
        max_slippage=_resolve_max_slippage(),
        mark_fetcher=lambda sym: _fetch_mark(ep, sym),
        varibot_dir=_VARIBOT_DIR,
    )
    return 0 if ran else 2


if __name__ == "__main__":
    raise SystemExit(main())
