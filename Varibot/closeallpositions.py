from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from variationalbot.config import load_config
from variationalbot.vari import VariAuth, VariClient, VariEndpoints

# Slippage cap for POST /api/orders/close_all: fraction of notional (0.001 = 0.1%).
DEFAULT_CLOSEALL_SLIPPAGE_PERCENT: float = 0.0005
CLOSEALL_SLIPPAGE_PERCENT_ENV: str = "CLOSEALL_SLIPPAGE_PERCENT"
# After each failed flatten (GET /api/positions still open), add this much to slippage (0.0005 = +0.05%).
SLIPPAGE_RETRY_INCREMENT: float = 0.0003
MAX_CLOSEALL_LIVE_ATTEMPTS: int = 6
# GET /api/positions right after close_all often lags the venue; wait then poll until stable (or best-of-poll).
POST_CLOSE_INITIAL_DELAY_S: float = 2.0
POST_CLOSE_POLL_INTERVAL_S: float = 1.0
POST_CLOSE_POLL_MAX: int = 8

# Exit codes (live): 0 = flat after GET /api/positions, 2 = still open after all attempts, 1 = request/GET error.


def _positions_list(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
        return [p for p in raw["positions"] if isinstance(p, dict)]
    return []


def _position_qty(p: Dict[str, Any]) -> Optional[float]:
    for k in ("qty", "quantity", "position_qty", "net_qty", "net_position", "size", "positionSize"):
        if k not in p:
            continue
        try:
            return float(p[k])
        except (TypeError, ValueError):
            continue
    pi = p.get("position_info")
    if isinstance(pi, dict) and "qty" in pi:
        try:
            return float(pi["qty"])
        except (TypeError, ValueError):
            pass
    return None


def _has_open_positions(positions_raw: Any) -> bool:
    for p in _positions_list(positions_raw):
        q = _position_qty(p)
        if q is not None and abs(float(q)) > 1e-12:
            return True
    return False


def _count_open_positions(positions_raw: Any) -> int:
    n = 0
    for p in _positions_list(positions_raw):
        q = _position_qty(p)
        if q is not None and abs(float(q)) > 1e-12:
            n += 1
    return n


def _get_positions_after_close_all(ep: VariEndpoints) -> Any:
    """
    Fetch positions after close_all. Immediate GET is often stale (still shows all rows open);
    wait POST_CLOSE_INITIAL_DELAY_S, then poll GET up to POST_CLOSE_POLL_MAX times every
    POST_CLOSE_POLL_INTERVAL_S until two consecutive reads agree, or return the snapshot with
    the fewest open rows seen (best-effort against lag).
    """
    time.sleep(POST_CLOSE_INITIAL_DELAY_S)
    prev_n: Optional[int] = None
    best_n = 10**9
    best_raw: Any = None
    last_raw: Any = None
    for _ in range(POST_CLOSE_POLL_MAX):
        last_raw = ep.get_positions()
        n = _count_open_positions(last_raw)
        if n < best_n:
            best_n, best_raw = n, last_raw
        if prev_n is not None and n == prev_n:
            return last_raw
        prev_n = n
        time.sleep(POST_CLOSE_POLL_INTERVAL_S)
    return best_raw if best_raw is not None else last_raw


def _ordinal_word(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Close all open positions (safe-by-default).")
    p.add_argument(
        "--slippage-percent",
        type=float,
        default=None,
        help=(
            f"Slippage as fraction of notional (default: {CLOSEALL_SLIPPAGE_PERCENT_ENV} env or "
            f"{DEFAULT_CLOSEALL_SLIPPAGE_PERCENT} = 0.03%%). Example: 0.001 == 0.1%%."
        ),
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print the full raw JSON request/response (debug mode).",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Actually call the close-all endpoint. Without this flag, script is dry-run only.",
    )
    p.add_argument(
        "--include-positions",
        action="store_true",
        help="Also include current positions snapshot in output (GET /api/positions).",
    )
    return p


def _slippage_percent_str(v: Optional[float]) -> str:
    if v is None:
        v = float(os.environ.get(CLOSEALL_SLIPPAGE_PERCENT_ENV, str(DEFAULT_CLOSEALL_SLIPPAGE_PERCENT)))
    return _slippage_percent_str_from_float(float(v))


def _slippage_percent_str_from_float(v: float) -> str:
    # API payload observed as string, e.g. "0.001"
    return f"{float(v):.10f}".rstrip("0").rstrip(".") or "0"


def _resolve_base_slippage_percent(args_slippage: Optional[float]) -> float:
    if args_slippage is not None:
        return float(args_slippage)
    return float(os.environ.get(CLOSEALL_SLIPPAGE_PERCENT_ENV, str(DEFAULT_CLOSEALL_SLIPPAGE_PERCENT)))


def _order_symbol(order: Dict[str, Any]) -> Optional[str]:
    inst = order.get("instrument")
    if isinstance(inst, dict):
        sym = inst.get("underlying") or inst.get("symbol") or inst.get("asset")
        if sym:
            return str(sym).upper()
    return None


def _aggregated_close_rows(responses: List[Any]) -> List[List[str]]:
    """Merge close_all `orders` across attempts; sum numeric qty per symbol."""
    sums: Dict[str, float] = defaultdict(float)
    non_numeric: Dict[str, str] = {}
    for resp in responses:
        if not isinstance(resp, dict):
            continue
        maybe_orders = resp.get("orders")
        if not isinstance(maybe_orders, list):
            continue
        for o in maybe_orders:
            if not isinstance(o, dict):
                continue
            sym = _order_symbol(o)
            if not sym:
                continue
            qty = o.get("qty")
            try:
                sums[sym] += float(qty)
            except (TypeError, ValueError):
                non_numeric[sym] = "-" if qty is None else str(qty)
    rows: List[List[str]] = []
    for sym in sorted(set(sums.keys()) | set(non_numeric.keys())):
        if sym in sums:
            v = sums[sym]
            if abs(v - round(v)) < 1e-9:
                qty_s = str(int(round(v)))
            else:
                qty_s = f"{v:.10f}".rstrip("0").rstrip(".") or "0"
            rows.append([sym, qty_s])
        else:
            rows.append([sym, non_numeric[sym]])
    return rows


def _print_symbol_qty_table(rows: List[List[str]]) -> None:
    if not rows:
        return
    cols = ["Symbol", "Qty"]
    widths = [len(c) for c in cols]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def line(parts: List[str]) -> str:
        return "  ".join(parts[i].ljust(widths[i]) for i in range(len(parts)))

    print(line(cols))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))


def main() -> int:
    args = build_parser().parse_args()
    cfg = load_config()

    ep = VariEndpoints(
        VariClient(
            base_url=cfg.base_url,
            auth=VariAuth(wallet_address=cfg.wallet_address, vr_token=cfg.vr_token),
        )
    )

    if not args.live:
        slippage_percent = _slippage_percent_str(args.slippage_percent)
        payload: Dict[str, Any] = {"slippage_percent": slippage_percent}

        out: Dict[str, Any] = {
            "ts": time.time(),
            "base_url": cfg.base_url,
            "wallet": cfg.wallet_address,
            "live": False,
            "request": {
                "method": "POST",
                "path": "/api/orders/close_all",
                "json": payload,
            },
        }

        if args.include_positions:
            try:
                positions = ep.get_positions()
            except Exception as e:
                positions = {"error": {"type": type(e).__name__, "message": str(e)}}
            out["positions_before"] = positions

        out["note"] = "Dry-run only. Re-run with --live to close all positions."
        if args.print_json:
            print(json.dumps(out, indent=2, default=str))
        else:
            print(f"Dry-run: close_all with slippage_percent={slippage_percent}")
            if args.include_positions and "positions_before" in out:
                pos = out.get("positions_before") or {}
                # Avoid depending on exact schema; just show a compact list if possible.
                if isinstance(pos, dict) and "positions" in pos and isinstance(pos["positions"], list):
                    items = pos["positions"]
                    # Best-effort: show underlying + quantity if those keys exist.
                    print("Positions before:")
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        sym = item.get("underlying") or item.get("symbol") or item.get("asset")
                        qty = item.get("quantity") or item.get("qty")
                        if sym:
                            print(f"{sym} {qty if qty is not None else ''}".strip())
                elif isinstance(pos, dict):
                    print("Positions before: (see --print-json for raw snapshot)")
                else:
                    print("Positions before: (see --print-json for raw snapshot)")
        return 0

    base = _resolve_base_slippage_percent(args.slippage_percent)
    attempts_out: List[Dict[str, Any]] = []
    close_all_responses: List[Any] = []
    print("Attempting to close all positions...")

    try:
        raw_initial = ep.get_positions()
    except Exception as e:
        print(f"ERROR: GET /api/positions (initial) failed: {e}", file=sys.stderr)
        return 1

    if not _has_open_positions(raw_initial):
        print("No open positions (GET /api/positions).")
        return 0

    n_initial = _count_open_positions(raw_initial)

    for attempt in range(1, MAX_CLOSEALL_LIVE_ATTEMPTS + 1):
        slip_float = base + float(attempt - 1) * SLIPPAGE_RETRY_INCREMENT
        slip_str = _slippage_percent_str_from_float(slip_float)
        if attempt > 1:
            print(
                f"Attempting {_ordinal_word(attempt)} try with higher slippage cap "
                f"{slip_float * 100:.2f}%..."
            )
            time.sleep(1.5)
        try:
            resp = ep.client.request_json(
                "POST", "/api/orders/close_all", json_body={"slippage_percent": slip_str}
            )
        except Exception as e:
            print(f"ERROR: POST /api/orders/close_all failed: {e}", file=sys.stderr)
            return 1

        close_all_responses.append(resp)
        attempts_out.append({"attempt": attempt, "slippage_percent": slip_str, "response": resp})

        try:
            raw_after = _get_positions_after_close_all(ep)
        except Exception as e:
            print(f"ERROR: GET /api/positions after close_all failed: {e}", file=sys.stderr)
            return 1

        n_open = _count_open_positions(raw_after)
        n_closed = max(0, n_initial - n_open)
        print(f"Positions closed: {n_closed}/{n_initial}")

        if not _has_open_positions(raw_after):
            if not args.print_json:
                _print_symbol_qty_table(_aggregated_close_rows(close_all_responses))
            if args.print_json:
                print(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "base_url": cfg.base_url,
                            "wallet": cfg.wallet_address,
                            "live": True,
                            "base_slippage_percent": base,
                            "slippage_retry_increment": SLIPPAGE_RETRY_INCREMENT,
                            "successful_attempt": attempt,
                            "initial_open_positions": n_initial,
                            "attempts": attempts_out,
                        },
                        indent=2,
                        default=str,
                    )
                )
            return 0

        if attempt >= MAX_CLOSEALL_LIVE_ATTEMPTS:
            print(
                f"Still open after {MAX_CLOSEALL_LIVE_ATTEMPTS} attempts "
                f"(last slippage {slip_float * 100:.2f}% notional). Exit 2.",
                file=sys.stderr,
            )
            if args.print_json:
                print(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "base_url": cfg.base_url,
                            "wallet": cfg.wallet_address,
                            "live": True,
                            "base_slippage_percent": base,
                            "slippage_retry_increment": SLIPPAGE_RETRY_INCREMENT,
                            "attempts": attempts_out,
                            "initial_open_positions": n_initial,
                            "error": "positions_still_open_after_max_attempts",
                        },
                        indent=2,
                        default=str,
                    )
                )
            return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

