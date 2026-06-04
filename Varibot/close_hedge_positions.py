#!/usr/bin/env python3
"""Flatten BTC/ETH/SOL book-hedge legs (dry-run default; --live sends reduce-only markets)."""

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

from book_hedge import (  # noqa: E402
    book_hedge_constants,
    compute_hedge_net,
    flatten_hedge_legs,
    hedge_is_active,
    parse_live_positions_from_raw,
)
from rebalance_dry_run_report import _fetch_mark  # noqa: E402
from variationalbot.config import load_config  # noqa: E402
from variationalbot.vari import VariAuth, VariClient, VariEndpoints  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Close BTC/ETH/SOL book-hedge legs to flat (ignores VARIBOT_BOOK_HEDGE_ENABLED)."
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="Place real reduce-only market orders (default: dry-run).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="With --live: skip confirmation prompt.",
    )
    args = ap.parse_args()

    if args.live and not args.force:
        print("LIVE: will flatten BTC/ETH/SOL hedge legs with market orders.", file=sys.stderr)
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

    raw_pos = ep.get_positions()
    _, _, _, _, _, min_usd = book_hedge_constants()
    hedge_net = compute_hedge_net(parse_live_positions_from_raw(raw_pos))
    if not hedge_is_active(hedge_net, min_order_usd=min_usd):
        log(f"flatten_hedge: no material hedge legs (hedge_net=${hedge_net:+.2f})")
        return 0

    ran = flatten_hedge_legs(
        ep=ep,
        positions_raw=raw_pos,
        live=bool(args.live),
        dry_run=not bool(args.live),
        log=log,
        mark_fetcher=lambda sym: _fetch_mark(ep, sym),
    )
    if not args.live:
        return 0
    return 0 if ran else 2


if __name__ == "__main__":
    raise SystemExit(main())
