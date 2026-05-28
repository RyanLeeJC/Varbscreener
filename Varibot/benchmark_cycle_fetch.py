#!/usr/bin/env python3
"""Benchmark grid cycle step 1: venue marks (bulk) + pending limits (bulk GET)."""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Tuple

_VARIBOT = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_VARIBOT)
for p in (_REPO, _VARIBOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from strategy.gridstrat import grid_trading_ticker_band_pcts  # noqa: E402

# Patch HTTP before building endpoints
_api_log: List[Tuple[str, str, float]] = []
_orig_request_json = None


def _install_counter() -> None:
    global _orig_request_json
    from variationalbot.vari.client import VariClient

    _orig_request_json = VariClient.request_json

    def counted(self, method: str, path: str, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        out = _orig_request_json(self, method, path, **kwargs)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        _api_log.append((method.upper(), path.split("?")[0], dt_ms))
        return out

    VariClient.request_json = counted  # type: ignore[method-assign]


def main() -> int:
    _install_counter()
    from variationalbot.config import load_config
    from variationalbot.vari import VariAuth, VariClient, VariEndpoints
    from grid_limits_reconcile import bulk_pending_fetch_enabled

    os.chdir(_VARIBOT)
    load_config(env_path=os.path.join(_VARIBOT, ".env"))

    # Import varibot helpers after env load
    import varibot as vb

    cfg = load_config(env_path=os.path.join(_VARIBOT, ".env"))
    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    assets = list(grid_trading_ticker_band_pcts().keys())
    marks_source = vb._grid_marks_source()
    use_bulk_marks = vb._use_bulk_supported_assets_marks()

    print("=== Grid cycle step 1 benchmark (live) ===")
    print(f"Tickers: {len(assets)} — {', '.join(sorted(assets))}")
    print(f"VARIBOT_MARKS_SOURCE={marks_source!r} bulk_marks={use_bulk_marks}")
    print(f"VARIBOT_PENDING_BULK={os.getenv('VARIBOT_PENDING_BULK', '(default on)')}")
    print(f"VARIBOT_PENDING_BULK_MAX_PAGES={os.getenv('VARIBOT_PENDING_BULK_MAX_PAGES', '(default 6)')}")
    print(f"GRID_ORDERS_PAGE_LIMIT={os.getenv('GRID_ORDERS_PAGE_LIMIT', '(default 50)')}")
    print()

    t_total = time.perf_counter()

    # --- Marks (same as one_cycle: bulk map once, then filter to grid tickers) ---
    _api_log.clear()
    t0 = time.perf_counter()
    bulk_map = None
    if use_bulk_marks:
        bulk_map = vb._fetch_supported_assets_mark_map(ep)
    marks = vb._fetch_grid_marks_for_assets(ep, assets, bulk_map=bulk_map)
    marks_ms = (time.perf_counter() - t0) * 1000.0
    marks_calls = list(_api_log)

    # --- Pending bulk (same as varibot._fetch_cycle_pending_by_asset) ---
    _api_log.clear()
    t0 = time.perf_counter()
    pending_by = vb._fetch_cycle_pending_by_asset(ep, assets=assets)
    pending_ms = (time.perf_counter() - t0) * 1000.0
    pending_calls = list(_api_log)

    total_ms = (time.perf_counter() - t_total) * 1000.0

    n_pending_limits = sum(len(v) for v in (pending_by or {}).values())
    print("--- Marks ---")
    print(f"  Time: {marks_ms:.0f} ms")
    print(f"  API calls: {len(marks_calls)}")
    for method, path, ms in marks_calls:
        print(f"    {method} {path}  ({ms:.0f} ms)")
    print(f"  Grid marks resolved: {len(marks)}/{len(assets)}")
    if marks:
        sample = sorted(marks.items())[:3]
        print(f"  Sample: {sample}")

    print()
    print("--- Pending limits (bulk) ---")
    print(f"  Time: {pending_ms:.0f} ms")
    print(f"  API calls: {len(pending_calls)} (paginated pages)")
    for method, path, ms in pending_calls:
        q = "?" if "?" not in path else ""
        print(f"    {method} {path}{q}  ({ms:.0f} ms)")
    print(f"  Pending limits (grid tickers): {n_pending_limits} across {len(pending_by or {})} tickers")

    print()
    print("--- Step 1 total (marks + pending only) ---")
    print(f"  Wall time: {total_ms:.0f} ms")
    print(f"  API calls: {len(marks_calls) + len(pending_calls)}")
    print()
    print("Note: full one_cycle also calls GET portfolio + GET positions before this (~2 more calls).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
