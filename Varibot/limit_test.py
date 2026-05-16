#!/usr/bin/env python3
"""
Minimal grid limit smoke test: Vari indicative mark for GRID_ASSET, then POST each ladder rung.

- Writes ``Varibot/strategy_listing_snapshot.json`` with one row (same shape as Varibot’s grid feed).
- Sets ``GRIDSTRAT_STATE_PATH`` to a **temporary** file for this process only so this run does not
  overwrite your real ``gridstrat_state.json``.

Usage::

  cd Varibot && python3 limit_test.py           # dry-run (no --live on child)
  cd Varibot && python3 limit_test.py --live
  cd Varibot && python3 limit_test.py --live --timing
  cd Varibot && python3 limit_test.py --live --timing --no-client-rate-limit

Each ladder rung is a **separate** ``multimarketorder.py`` subprocess (cold Python + several HTTP
calls per child), run **one after another** — wall time scales roughly linearly with rung count.
``GRID_ORDER_TYPE=limit`` (defaulted here if unset). Optional ``--multi-script`` (default
``multimarketorder.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from typing import Any, Callable, List, Optional

_VARIBOT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_VARIBOT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _VARIBOT_DIR not in sys.path:
    sys.path.insert(0, _VARIBOT_DIR)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Fetch GRID_ASSET mark and POST grid limit ladder (isolated state).")
    ap.add_argument("--live", action="store_true", help="Pass --live to multimarketorder child.")
    ap.add_argument(
        "--multi-script",
        default="multimarketorder.py",
        help="Script under Varibot/ (default: multimarketorder.py).",
    )
    ap.add_argument(
        "--timing",
        action="store_true",
        help="Log wall seconds for each multimarketorder subprocess (sequential bottleneck).",
    )
    ap.add_argument(
        "--no-client-rate-limit",
        action="store_true",
        help=(
            "Set VARI_RATE_LIMIT_MAX=0 for this process (inherited by child scripts). "
            "Removes local SDK throttling; Omni may return 429 if you burst too hard."
        ),
    )
    return ap.parse_args()


def _install_subprocess_timing(vb: Any) -> None:
    """Wrap varibot._run_script to print per-child duration (each grid rung = one subprocess)."""
    orig: Callable[..., int] = vb._run_script

    def wrapped(script_path: str, *, cwd: str, args: Optional[List[str]] = None, timeout_s: Optional[float] = None) -> int:
        cmd_args = args or []
        hint = ""
        try:
            i = cmd_args.index("--limit-price")
            if i + 1 < len(cmd_args):
                hint = f" --limit-price {cmd_args[i + 1]}"
        except ValueError:
            pass
        try:
            si = cmd_args.index("--side")
            if si + 1 < len(cmd_args):
                hint = f"{cmd_args[si + 1]}{hint}"
        except ValueError:
            pass
        t0 = time.perf_counter()
        print(f"limit_test timing: child start ({hint.strip() or 'multimarket'})", flush=True)
        rc = int(orig(script_path, cwd=cwd, args=cmd_args, timeout_s=timeout_s))
        dt = time.perf_counter() - t0
        print(f"limit_test timing: child end {dt:.2f}s rc={rc} ({hint.strip() or 'multimarket'})", flush=True)
        return rc

    vb._run_script = wrapped  # type: ignore[method-assign]


def main() -> int:
    args = _parse_args()
    os.environ.setdefault("GRID_ORDER_TYPE", "limit")
    if bool(getattr(args, "no_client_rate_limit", False)):
        os.environ["VARI_RATE_LIMIT_MAX"] = "0"
        print("limit_test: VARI_RATE_LIMIT_MAX=0 (no local SDK throttle; children inherit).", flush=True)

    prev_state_path = os.environ.get("GRIDSTRAT_STATE_PATH")
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="limit_test_gridstrat_", suffix=".json")
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        os.environ["GRIDSTRAT_STATE_PATH"] = tmp_path

        import varibot as vb  # import after sys.path

        if bool(getattr(args, "timing", False)):
            _install_subprocess_timing(vb)

        vb.run_auth_or_exit()
        _, ep = vb.build_endpoints()
        grid_asset = (os.environ.get("GRID_ASSET") or "BTC").strip().upper()
        empty_ns = argparse.Namespace(marketstate_json=None)
        listing_json, _ = vb._prepare_varibot_strategy_feed(ep, args=empty_ns, asset_hint=grid_asset)
        with open(listing_json, "r", encoding="utf-8") as f:
            doc = json.load(f)
        listings = doc.get("listings") if isinstance(doc, dict) else None
        row0 = listings[0] if isinstance(listings, list) and listings else {}
        mark = float(row0.get("mark_price") or 0.0)
        print(f"No open positions -> Check {grid_asset} market price... {mark:g}", flush=True)

        from strategy.gridstrat import pick_tickers

        _, _, meta = pick_tickers(listing_json=str(listing_json), marketstate_json=None, top_n=0)
        err = meta.get("error")
        if err:
            print(f"gridstrat error: {err}", flush=True)
            return 1

        lo, hi, gn = meta.get("grid_lower"), meta.get("grid_upper"), meta.get("grid_num")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and gn is not None:
            print(
                f"Entering Grid Limit Orders ({int(gn)}) from {float(lo):g} to {float(hi):g}",
                flush=True,
            )

        ns = argparse.Namespace(multi_script=str(args.multi_script), live=bool(args.live))
        t_batch = time.perf_counter()
        n_lim = vb._execute_grid_market_events(meta, args=ns)
        batch_dt = time.perf_counter() - t_batch
        print(
            f"limit_test: finished ({n_lim} limit child invocations) total_order_phase_s={batch_dt:.2f}",
            flush=True,
        )
        return 0
    finally:
        if prev_state_path is None:
            os.environ.pop("GRIDSTRAT_STATE_PATH", None)
        else:
            os.environ["GRIDSTRAT_STATE_PATH"] = prev_state_path
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
