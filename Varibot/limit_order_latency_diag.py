#!/usr/bin/env python3
"""
Explain limit-order latency: local rate limiter, per-subprocess HTTP fan-out, optional retries.

Run (no secrets required for section 1; section 2 needs .env for a dry multimarket call):

  cd Varibot && python3 limit_order_latency_diag.py
  cd Varibot && python3 limit_order_latency_diag.py --dry-multimarket
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)


def _print_static_diagnosis() -> None:
    mx = os.getenv("VARI_RATE_LIMIT_MAX", "10")
    ws = os.getenv("VARI_RATE_LIMIT_WINDOW_S", "10")
    print("=== Limit order latency — what the code does ===\n")
    print(
        "1) **Client-side rate limiter** (`variationalbot/vari/client.py` VariClient):\n"
        f"   Default: at most {mx!r} HTTP calls per {ws!r}s sliding window (per Python process).\n"
        "   When the window is full, `_wait_for_rate_limit()` sleeps until a slot frees.\n"
        "   Your KeyboardInterrupt stack showed a block there — not Vari 'refusing bursts',\n"
        "   but the SDK deliberately spacing requests to avoid Omni 429s.\n"
        "   Override: VARI_RATE_LIMIT_MAX=0 disables the limiter (you may see 429s from the venue).\n"
    )
    print(
        "2) **One subprocess per rung** (`varibot.run_multimarket_asset_side` → subprocess.run):\n"
        "   Each limit is a fresh `multimarketorder.py` process (imports, load_config, new VariClient).\n"
        "   Wall-clock adds Python startup + several HTTP calls per child.\n"
    )
    print(
        "3) **HTTP calls inside one `multimarketorder.py` limit child** (typical, live, --usd set):\n"
        "   - GET /api/portfolio?compute_margin=true  (IM hard-cap)\n"
        "   - GET /api/positions                      (prev qty)\n"
        "   - POST /api/settlement_pools/set_leverage (unless skipped)\n"
        "   - If sizing from USD: 2× POST /api/quotes/indicative (qty_string_for_usd_at_price)\n"
        "   - If --limit-qty set: 0 indicative calls for sizing\n"
        "   - POST /api/orders/new/limit\n"
        "   Slippage retries add another (2 indicative + 1 limit) per attempt.\n"
        "   With default max=10, a single child can hit the sleeper after several retries.\n"
        "   HTTP 429 handling retries the same request in a loop — each pass still consumes\n"
        "   a rate-limit slot, so bursts of 429s also trigger `_wait_for_rate_limit` sleeps.\n"
    )
    print(
        "4) **`--sleep-between-s`** (multimarketorder, default 1.0):\n"
        "   Only applies between jobs when **multiple** jobs run in one process.\n"
        "   Grid limit path uses one job per subprocess, so this default does **not** add 1s between rungs.\n"
    )


def _dry_multimarket_once() -> int:
    script = os.path.join(_VARIBOT_DIR, "multimarketorder.py")
    cmd = [
        sys.executable,
        "-u",
        script,
        "--usd",
        "10",
        "--assets",
        "BTC",
        "--side",
        "buy",
        "--limit-price",
        "50000",
        "--max-ticker-entries",
        "60",
        "--quiet",
    ]
    t0 = time.perf_counter()
    rc = subprocess.run(cmd, cwd=_VARIBOT_DIR, timeout=120)
    dt = time.perf_counter() - t0
    print(f"\n=== Dry multimarket (1 limit job, no --live) ===\nexit={rc.returncode} elapsed_s={dt:.2f}\n")
    return int(rc.returncode)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-multimarket",
        action="store_true",
        help="Run one cold multimarketorder dry limit (needs VR_TOKEN / VR_WALLET_ADDRESS in env).",
    )
    args = ap.parse_args()

    _print_static_diagnosis()
    if args.dry_multimarket:
        return _dry_multimarket_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
